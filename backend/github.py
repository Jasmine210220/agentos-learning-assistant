import re
import asyncio
import io
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlparse, quote
import httpx


MAX_FILES = 24
MAX_FILE_BYTES = 96_000
MAX_TOTAL_BYTES = 160_000
MAX_RESPONSE_BYTES = 6_000_000
# A documentation-heavy repository archive can be large even when the selected
# source is small. Download remains bounded; extraction/model budgets stay separate.
MAX_ARCHIVE_BYTES = 32_000_000
ALLOWED = {".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".toml", ".json", ".yaml", ".yml", ".txt", ".go", ".rs", ".sh", ".css", ".scss", ".html", ".vue", ".svelte"}
EXCLUDE = {"node_modules", ".git", ".github", "vendor", "dist", "build", ".venv", "venv", "__pycache__", "fixtures", "migrations", "alembic"}


class GitHubLimited(ValueError):
    pass


def parse_repository(value):
    value = value.strip().rstrip("/")
    if not value.startswith("https://"):
        raise ValueError("请输入 https://github.com/所有者/仓库 格式的公开仓库地址")
    url = urlparse(value)
    if url.hostname != "github.com" or url.port or url.username or url.password or url.query or url.fragment:
        raise ValueError("仅支持 GitHub 官方公开仓库地址")
    parts = url.path.strip("/").split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) and p not in {".", ".."} for p in parts):
        raise ValueError("请提供仓库首页地址，分支或提交在版本字段填写")
    owner, repo = parts
    repo = repo.removesuffix(".git")
    if not repo:
        raise ValueError("仓库名不能为空")
    return owner, repo


def safe_source(path):
    p = PurePosixPath(path)
    name = p.name.lower()
    return not p.is_absolute() and ".." not in p.parts and "\\" not in path and not any(x.lower() in EXCLUDE for x in p.parts) and (p.suffix.lower() in ALLOWED or name == 'dockerfile') and not name.startswith(".env") and not any(x in name for x in ["lock", "secret", "credential", "private_key"]) and name not in {"license.txt"}


def priority(path):
    name = PurePosixPath(path).name.lower()
    parts = [part.lower() for part in PurePosixPath(path).parts]
    if any(part in {'test','tests','testing','testdata'} for part in parts): return 8
    if name=='__init__.py': return 9
    if any(part in {'scripts','tools','docs','docs_src','examples','benchmarks'} for part in parts): return 7
    if name.startswith("readme"): return 0
    if name in {"pyproject.toml", "requirements.txt", "package.json"}: return 1
    if name in {"main.py", "app.py", "server.py", "main.ts", "main.tsx", "index.ts", "app.ts", "app.tsx"}: return 2
    if name.startswith('test_'): return 8
    if PurePosixPath(path).suffix in {'.py',".ts", '.tsx',".js",'.jsx', ".go", ".rs"}: return 3
    if PurePosixPath(path).suffix == '.sh' or name == 'dockerfile': return 5
    return 6


def rank_candidates(paths):
    """Interleave modules at a given priority so one large directory cannot consume the snapshot."""
    groups = {}
    for path in paths:
        parts = PurePosixPath(path).parts
        group = '/'.join(parts[:-1])
        groups.setdefault((priority(path), group), []).append(path)
    ranked = []
    for level in sorted({k[0] for k in groups}):
        buckets = [sorted(v, key=lambda p:(p.count('/'),p)) for k,v in sorted(groups.items()) if k[0]==level]
        while any(buckets):
            for bucket in buckets:
                if bucket: ranked.append(bucket.pop(0))
    return {path:i for i,path in enumerate(ranked)}


async def bounded_get(client, url, *, limit=MAX_RESPONSE_BYTES):
    for attempt in range(3):
        try:
            async with client.stream("GET", url) as response:
                if response.status_code == 404: raise ValueError("仓库、版本或文件不存在，或不是公开资源")
                if response.status_code in {403, 429}: raise GitHubLimited("GitHub 拒绝访问或请求限流，请稍后再试")
                if response.status_code>=500 and attempt<2:
                    await asyncio.sleep(2**attempt)
                    continue
                if response.status_code != 200: raise ValueError(f"GitHub 请求失败（HTTP {response.status_code}）")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > limit: raise ValueError("响应超过读取上限，已停止下载")
                return bytes(content)
        except httpx.TransportError:
            if attempt==2:raise ValueError('GitHub 网络超时或连接失败，已达到 3 次尝试上限') from None
            await asyncio.sleep(2**attempt)


async def read_repository(repository, ref, event, check, client=None, selector=None):
    import json
    owner, repo = parse_repository(repository)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=20, follow_redirects=False, headers={"User-Agent": "AgentOS-Lite/0.1", "Accept": "application/vnd.github+json"})
    api = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        check()
        metadata = json.loads(await bounded_get(client, api))
        resolved_ref = ref or metadata["default_branch"]
        commit = json.loads(await bounded_get(client, f"{api}/commits/{quote(resolved_ref, safe='')}"))
        sha = commit["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", sha): raise ValueError("GitHub 返回了无效提交版本")
        tree = json.loads(await bounded_get(client, f"{api}/git/trees/{sha}?recursive=1"))
        entries = tree.get("tree", [])
        candidates = sorted([x for x in entries if x.get("type") == "blob" and x.get("mode") != "120000" and safe_source(x["path"])], key=lambda x: (priority(x["path"]), x["path"].count("/"), x["path"]))
        rank = rank_candidates([x['path'] for x in candidates])
        candidates.sort(key=lambda x:rank[x['path']])
        if selector:
            eligible=[x for x in candidates if 0<x.get('size',1)<=MAX_FILE_BYTES]
            selected=await selector([{'path':x['path'],'bytes':x.get('size',0)} for x in eligible[:1000]])
            order={p:i for i,p in enumerate(selected)}
            candidates.sort(key=lambda x:(0,order[x['path']]) if x['path'] in order else (1,rank[x['path']]))
        files, skipped, used = [], [], 0
        for item in candidates:
            check()
            path = item["path"]
            if item.get("size", 0) > MAX_FILE_BYTES:
                skipped.append({"path": path, "reason": "单文件过大"}); continue
            if selector and path not in order:
                skipped.append({'path':path,'reason':'不在本次目标文件范围'});continue
            if item.get('size')==0:
                skipped.append({'path':path,'reason':'空文件'});continue
            if len(files) >= MAX_FILES or used >= MAX_TOTAL_BYTES:
                skipped.append({"path": path, "reason": "读取数量或总量上限"}); continue
            if item.get("size", 0) > MAX_FILE_BYTES:
                skipped.append({"path": path, "reason": "单文件过大"}); continue
            if used + item.get("size", 0) > MAX_TOTAL_BYTES:
                skipped.append({"path": path, "reason": "总量上限"}); continue
            try:
                raw = await bounded_get(client, f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}/{quote(path, safe='/')}", limit=min(MAX_FILE_BYTES, MAX_TOTAL_BYTES-used))
                content = raw.decode("utf-8")
                if "\x00" in content: raise ValueError("非文本文件")
                files.append({"path": path, "content": content, "lines": len(content.splitlines()), "bytes": len(raw)})
                used += len(raw)
                event("Code Agent", "tool", f"已读取 {path}", {"path": path, "bytes": len(raw), "commit": sha})
            except (ValueError, UnicodeDecodeError, httpx.HTTPError) as exc:
                skipped.append({"path": path, "reason": str(exc)[:160]})
        if not files: raise ValueError("仓库中没有可读取的支持格式文本文件")
        return {"repository": f"https://github.com/{owner}/{repo}", "commit": sha, "ref": resolved_ref,
                "description": metadata.get("description") or "", "files": files, "skipped": skipped,
                "tree_truncated": bool(tree.get("truncated")), "total_tree_files": sum(x.get("type") == "blob" for x in entries),
                "candidate_files": len(candidates), "read_bytes": used, "directory": [x["path"] for x in candidates[:300]],
                "limits": {"max_files": MAX_FILES, "max_file_bytes": MAX_FILE_BYTES, "max_total_bytes": MAX_TOTAL_BYTES}}
    except GitHubLimited:
        event("Code Agent", "tool", "GitHub API 限流，切换到公开 Git 引用与受限源码归档")
        return await read_archive(client, owner, repo, ref, event, check,selector)
    finally:
        if own_client: await client.aclose()


async def read_archive(client, owner, repo, ref, event, check,selector=None):
    check()
    raw_refs = await bounded_get(client, f"https://github.com/{owner}/{repo}.git/info/refs?service=git-upload-pack", limit=2_000_000)
    pairs = re.findall(rb"([0-9a-f]{40}) (HEAD|refs/[^\x00\n ]+)", raw_refs)
    refs = {name.decode(): sha.decode() for sha, name in pairs}
    if not ref:
        sha = refs.get("HEAD")
    elif re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        sha = ref.lower()
    else:
        sha = refs.get(f"refs/heads/{ref}") or refs.get(f"refs/tags/{ref}^{{}}") or refs.get(f"refs/tags/{ref}")
    if not sha: raise ValueError("无法解析指定版本，请填写存在的分支、标签或完整 40 位提交 SHA")
    check()
    archive = await bounded_get(client, f"https://codeload.github.com/{owner}/{repo}/zip/{sha}", limit=MAX_ARCHIVE_BYTES)
    event('Code Agent','tool','已下载 GitHub 源码归档',{'bytes':len(archive),'commit':sha,'limit':MAX_ARCHIVE_BYTES})
    files, skipped, used = [], [], 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            entries = []
            total = 0
            for entry in z.infolist():
                if entry.is_dir(): continue
                total += 1
                path = entry.filename.partition('/')[2]
                if safe_source(path) and (entry.external_attr >> 16) & 0o170000 != 0o120000:
                    entries.append((path, entry))
            entries.sort(key=lambda x: (priority(x[0]), x[0].count('/'), x[0]))
            rank = rank_candidates([p for p,_ in entries])
            entries.sort(key=lambda x:rank[x[0]])
            if selector:
                selected=await selector([{'path':p,'bytes':e.file_size} for p,e in entries if 0<e.file_size<=MAX_FILE_BYTES][:1000])
                order={p:i for i,p in enumerate(selected)}
                entries.sort(key=lambda x:(0,order[x[0]]) if x[0] in order else (1,rank[x[0]]))
            for path, entry in entries:
                check()
                if entry.file_size > MAX_FILE_BYTES:
                    skipped.append({"path":path,"reason":"单文件过大"}); continue
                if selector and path not in order:
                    skipped.append({'path':path,'reason':'不在本次目标文件范围'});continue
                if entry.file_size==0:
                    skipped.append({'path':path,'reason':'空文件'});continue
                if len(files) >= MAX_FILES or used + entry.file_size > MAX_TOTAL_BYTES:
                    skipped.append({"path":path,"reason":"读取数量或总量上限"}); continue
                if entry.file_size > MAX_FILE_BYTES:
                    skipped.append({"path":path,"reason":"单文件过大"}); continue
                try:
                    with z.open(entry) as stream: raw = stream.read(MAX_FILE_BYTES + 1)
                    if len(raw) > MAX_FILE_BYTES: raise ValueError("单文件过大")
                    content = raw.decode('utf-8')
                    if '\x00' in content: raise ValueError("非文本文件")
                    files.append({"path":path,"content":content,"lines":len(content.splitlines()),"bytes":len(raw)})
                    used += len(raw)
                    event("Code Agent","tool",f"已读取 {path}",{"path":path,"bytes":len(raw),"commit":sha,"transport":"archive"})
                except (UnicodeDecodeError, ValueError):
                    skipped.append({"path":path,"reason":"不支持的文本格式"})
    except zipfile.BadZipFile:
        raise ValueError("GitHub 返回了无效的源码归档") from None
    if not files: raise ValueError("仓库归档没有可读取的支持格式文本文件")
    return {"repository":f"https://github.com/{owner}/{repo}","commit":sha,"ref":ref or "HEAD","description":"",
            "files":files,"skipped":skipped,"tree_truncated":False,"total_tree_files":total,"candidate_files":len(entries),
            "read_bytes":used,"directory":[p for p,_ in entries[:300]],"transport":"archive",
            "limits":{"max_files":MAX_FILES,"max_file_bytes":MAX_FILE_BYTES,"max_total_bytes":MAX_TOTAL_BYTES,"max_archive_bytes":MAX_ARCHIVE_BYTES}}
