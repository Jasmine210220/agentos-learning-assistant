import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from .store import Store
from .models import TaskInput
from .github import parse_repository
from .workflow import Runner
from .llm import MODEL, MAX_CALLS, MAX_TOTAL_TOKENS
from .chat import Chats, MessageInput
from .workspaces import save_attachment, get_attachment, edit_copy
from pydantic import BaseModel, Field
from . import settings
from .runtime import Runtime


class ImportFile(BaseModel):
    path: str = Field(max_length=240)
    content: str = Field(max_length=96_000)


class ImportInput(BaseModel):
    files: list[ImportFile] = Field(min_length=1,max_length=32)


class EditInput(BaseModel):
    path: str = Field(max_length=240)
    content: str = Field(max_length=96_000)


class ApplyInput(BaseModel):
    recommendation_id: str = Field(min_length=1,max_length=8)
    base_attachment_id: str = Field(min_length=32,max_length=32)


class RestoreInput(BaseModel):
    base_attachment_id: str = Field(min_length=32,max_length=32)


class SettingsInput(BaseModel):
    model: str = Field(min_length=1,max_length=100)
    key: str = Field(default='',max_length=1000)
    clear: bool = False


class SearchSettingsInput(BaseModel):
    tavily_key: str = Field(default='',max_length=1000)
    github_key: str = Field(default='',max_length=1000)
    clear_tavily: bool = False
    clear_github: bool = False


class RunInput(BaseModel):
    attachment_id: str = Field(min_length=32,max_length=32)
    path: str = Field(max_length=240)
    confirmed: bool = False


def create_app(root=None, token=None, runner_factory=Runner):
    root = Path(root or os.environ.get("AGENTOS_DATA_DIR", str(Path.home() / ".agentos-lite")))
    store = Store(root)
    settings.configure(root)
    token = token or os.environ.get("AGENTOS_TOKEN") or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        app.state.runner = runner_factory(store)
        app.state.chats = Chats(store, app.state.runner, create_task)
        app.state.runtime=Runtime(store)
        yield
        await app.state.runtime.shutdown()
        await app.state.chats.shutdown()
        await app.state.runner.shutdown()
        store.db.close()

    app = FastAPI(title="AgentOS Lite", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.add_middleware(CORSMiddleware, allow_origins=["null", "http://127.0.0.1:5173", "http://localhost:5173"], allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])

    def authorize(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization, f"Bearer {token}"): raise HTTPException(401, "本地访问凭据无效")

    def task_or_404(task_id):
        try: return store.get(task_id)
        except KeyError: raise HTTPException(404, "任务不存在") from None

    @app.get("/health", dependencies=[Depends(authorize)])
    def health():
        return {"ok": True, **settings.public(), "version": "0.6.8", "active_chats": len(app.state.chats.jobs)+len(app.state.runtime.jobs), "limits": {"calls": MAX_CALLS, "tokens": MAX_TOTAL_TOKENS, "seconds": 600}}

    @app.get("/tasks", dependencies=[Depends(authorize)])
    def tasks(): return store.list()

    def chat_or_404(chat_id):
        try: return app.state.chats.get(chat_id)
        except KeyError: raise HTTPException(404, '对话不存在') from None

    @app.get('/chats', dependencies=[Depends(authorize)])
    async def chats(): return app.state.chats.list()

    @app.post('/chats', dependencies=[Depends(authorize)])
    async def new_chat(): return app.state.chats.create()

    @app.get('/chats/{chat_id}', dependencies=[Depends(authorize)])
    async def get_chat(chat_id: str): return chat_or_404(chat_id)

    @app.post('/chats/{chat_id}/messages', dependencies=[Depends(authorize)])
    async def send_message(chat_id: str, data: MessageInput):
        chat_or_404(chat_id)
        if not data.content.strip(): raise HTTPException(422, '消息不能为空')
        if not settings.current()['key']: raise HTTPException(409, '请先在设置中配置 DeepSeek 密钥')
        try: return app.state.chats.send(chat_id, data.content.strip(),data.attachment_id,data.mode,data.learner_background,data.ref)
        except ValueError as exc: raise HTTPException(409,str(exc)) from None

    @app.post('/chats/{chat_id}/stop', dependencies=[Depends(authorize)])
    async def stop_chat(chat_id: str):
        chat_or_404(chat_id)
        app.state.chats.stop(chat_id)
        return {'accepted':True}

    @app.post("/tasks", dependencies=[Depends(authorize)], status_code=201)
    async def create_task(data: TaskInput):
        try:
            if data.repository: parse_repository(data.repository)
            if data.kind=='analyze' and not data.repository and not data.attachment_id: raise ValueError('请提供仓库或代码文件')
            if data.kind=='learn' and (not data.repository or data.attachment_id): raise ValueError('学习模式需要一个公开 GitHub 仓库链接，不使用本地附件')
            if data.kind=='improve' and not data.attachment_id: raise ValueError('请先添加项目文件或目录')
            if data.attachment_id: get_attachment(store,data.attachment_id)
            if data.source_task_id:
                parent=task_or_404(data.source_task_id)
                candidate=next((c for c in parent['result'].get('search',{}).get('candidates',[]) if c['repository'].rstrip('/')==data.repository.rstrip('/')),None)
                if not candidate: raise ValueError('所选仓库不在该次搜索结果中')
                data.ref=candidate.get('commit') or data.ref
        except ValueError as exc: raise HTTPException(422, str(exc)) from None
        if not settings.current()['key']: raise HTTPException(409, '请先在设置中配置 DeepSeek 密钥')
        if len(app.state.runner.jobs) >= 5: raise HTTPException(429, "最多保留 5 个排队或运行任务，请等待或停止现有任务")
        payload = data.model_dump()
        payload["config"] = {"provider": "deepseek", "model": settings.current()['model'], "max_calls": MAX_CALLS, "max_tokens": MAX_TOTAL_TOKENS, "timeout_seconds": 600}
        task = store.create(payload)
        store.event(task["id"], "System", "queued", "任务已保存，等待执行")
        if data.source_task_id:
            store.event(task['id'],'Supervisor','handoff','接收所选搜索成果',{'sender':'Research Agent','receiver':'Code Agent','source_task_id':data.source_task_id,'artifact':'search.candidates','repository':data.repository,'commit':data.ref,'goal':parent['goal']})
        app.state.runner.start(task["id"])
        return task

    @app.post('/attachments',dependencies=[Depends(authorize)])
    async def upload(data:ImportInput):
        try: return save_attachment(store,[f.model_dump() for f in data.files])
        except ValueError as exc: raise HTTPException(422,str(exc)) from None

    @app.get('/attachments/{ident}',dependencies=[Depends(authorize)])
    async def attachment(ident:str):
        try: return get_attachment(store,ident)
        except ValueError as exc: raise HTTPException(404,str(exc)) from None

    @app.post('/attachments/{ident}/edit',dependencies=[Depends(authorize)])
    async def edit(ident:str,data:EditInput):
        try: return edit_copy(store,ident,data.path,data.content)
        except ValueError as exc: raise HTTPException(422,str(exc)) from None

    @app.get('/attachments/{ident}/download',dependencies=[Depends(authorize)])
    async def download_attachment(ident:str):
        import io,zipfile
        try: data=get_attachment(store,ident)
        except ValueError as exc: raise HTTPException(404,str(exc)) from None
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            for file in data['files']: archive.writestr(file['path'],file['content'].encode('utf-8'))
        return Response(output.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="project-copy.zip"'})

    @app.post('/tasks/{task_id}/apply',dependencies=[Depends(authorize)])
    async def apply_change(task_id:str,data:ApplyInput):
        task_or_404(task_id)
        from .improvement import apply_recommendation
        try: return apply_recommendation(store,task_id,data.recommendation_id,data.base_attachment_id)
        except ValueError as exc: raise HTTPException(409,str(exc)) from None

    @app.post('/tasks/{task_id}/restore',dependencies=[Depends(authorize)])
    async def restore_copy(task_id:str,data:RestoreInput):
        task_or_404(task_id)
        from .improvement import restore_original
        try: return restore_original(store,task_id,data.base_attachment_id)
        except ValueError as exc: raise HTTPException(409,str(exc)) from None

    @app.get('/settings',dependencies=[Depends(authorize)])
    async def get_settings(): return settings.public()

    @app.get('/settings/search',dependencies=[Depends(authorize)])
    async def get_search_settings(): return settings.search_public()

    @app.post('/settings/search',dependencies=[Depends(authorize)])
    async def save_search_settings(data:SearchSettingsInput):
        try: return settings.save_search(**data.model_dump())
        except ValueError as exc: raise HTTPException(422,str(exc)) from None

    @app.post('/settings/search/test',dependencies=[Depends(authorize)])
    async def test_search_settings():
        from .search_tools import SearchTools
        import httpx
        tool=SearchTools(settings.search_credentials())
        try:
            if settings.search_credentials()['tavily']['key']:
                await tool.web_search('GitHub open source')
                return {'ok':True,'message':'全网搜索连接正常','requests':tool.web_calls}
            await tool.github_search('code-simplifier')
            return {'ok':True,'message':'GitHub 项目搜索连接正常，可直接使用','requests':tool.github_queries}
        except ValueError as exc: raise HTTPException(422,str(exc)) from None
        except httpx.HTTPError: raise HTTPException(422,'搜索连接失败，请检查网络或稍后重试') from None
        finally: await tool.close()

    @app.post('/settings',dependencies=[Depends(authorize)])
    async def update_settings(data:SettingsInput):
        try: return settings.save(data.model,data.key,data.clear)
        except ValueError as exc: raise HTTPException(422,str(exc)) from None

    @app.post('/settings/test',dependencies=[Depends(authorize)])
    async def test_settings():
        from .llm import DeepSeek
        from .models import Review
        try:
            model=DeepSeek(lambda usage:None,lambda:None)
            await model.ask('连接测试','这是连接测试，只返回 passed=true, summary=连接正常, issues=[]。',{},Review)
            return {'ok':True,'message':'连接正常'}
        except ValueError as exc: raise HTTPException(422,str(exc)) from None

    @app.get('/runtime',dependencies=[Depends(authorize)])
    async def runtime_health(): return await app.state.runtime.health()

    @app.get('/runs',dependencies=[Depends(authorize)])
    async def runs(): return app.state.runtime.list()

    @app.get('/runs/{ident}',dependencies=[Depends(authorize)])
    async def run_detail(ident:str):
        try: return app.state.runtime.get(ident)
        except ValueError as exc: raise HTTPException(404,str(exc)) from None

    @app.post('/runs',dependencies=[Depends(authorize)])
    async def run_code(data:RunInput):
        try: return await app.state.runtime.start(data.attachment_id,data.path,data.confirmed)
        except ValueError as exc: raise HTTPException(409,str(exc)) from None

    @app.post('/runs/{ident}/stop',dependencies=[Depends(authorize)])
    async def stop_run(ident:str):
        try: app.state.runtime.stop(ident);return {'accepted':True}
        except ValueError as exc: raise HTTPException(404,str(exc)) from None

    @app.get("/tasks/{task_id}", dependencies=[Depends(authorize)])
    def get_task(task_id: str): return task_or_404(task_id)

    @app.get("/tasks/{task_id}/events", dependencies=[Depends(authorize)])
    def events(task_id: str, after: int = Query(default=0, ge=0)):
        task_or_404(task_id)
        return store.events(task_id, after)

    @app.post("/tasks/{task_id}/stop", dependencies=[Depends(authorize)])
    async def stop(task_id: str):
        task_or_404(task_id)
        app.state.runner.stop(task_id)
        return {"accepted": True}

    @app.get("/tasks/{task_id}/source", dependencies=[Depends(authorize)])
    def source(task_id: str, path: str):
        task_or_404(task_id)
        snapshot_path = store.workspace(task_id) / "snapshot.json"
        if not snapshot_path.exists(): raise HTTPException(404, "尚未保存代码快照")
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        for f in snapshot["files"]:
            if f["path"] == path: return {**f, "commit": snapshot["commit"]}
        raise HTTPException(404, "文件不在本次读取范围内")

    @app.post('/tasks/{task_id}/copy',dependencies=[Depends(authorize)])
    async def copy_snapshot(task_id:str):
        task_or_404(task_id)
        path=store.workspace(task_id)/'snapshot.json'
        if not path.is_file(): raise HTTPException(404,'没有可编辑的代码快照')
        try: return save_attachment(store,json.loads(path.read_text(encoding='utf-8'))['files'])
        except ValueError as exc: raise HTTPException(422,str(exc)) from None

    @app.get("/tasks/{task_id}/artifacts/{name}", dependencies=[Depends(authorize)])
    def artifact(task_id: str, name: str):
        task = task_or_404(task_id)
        if task['payload'].get('kind') == 'learn' and name in {'diagram.svg', 'diagram.mmd'} and not task['result'].get('rendered'):
            raise HTTPException(404, '本轮图表尚未通过渲染验证')
        allowed = {"report.md": "text/markdown", "diagram.svg": "image/svg+xml", "diagram.mmd": "text/plain", "result.json": "application/json"}
        allowed.update({'architecture.svg':'image/svg+xml','architecture.mmd':'text/plain','chart.svg':'image/svg+xml','data.json':'application/json'})
        allowed.update({'handoff.md':'text/markdown','changes.patch':'text/plain'})
        if name not in allowed: raise HTTPException(404, "成果不存在")
        path = store.workspace(task_id) / name
        if not path.is_file(): raise HTTPException(404, "成果尚未生成")
        return FileResponse(path, media_type=allowed[name], headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    return app
