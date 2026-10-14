import base64
import json
import os
from urllib.parse import urlencode, quote
import httpx
from pydantic import Field
from .models import Strict
from .github import bounded_get, parse_repository


class SearchPlan(Strict):
    query: str = Field(min_length=1,max_length=220)
    conditions: list[str] = Field(max_length=8)


class Match(Strict):
    repository: str
    reasons: list[str] = Field(max_length=5)
    unknown: list[str] = Field(max_length=5)
    readme_quotes: list[str] = Field(max_length=3)
    readme_evidence_ids: list[int] = Field(default_factory=list,max_length=3)


class Matches(Strict):
    summary: str
    matches: list[Match] = Field(max_length=6)


async def search_repositories(query, event, check, client=None):
    owned = client is None
    headers={'User-Agent':'AgentOS-Lite','Accept':'application/vnd.github+json'}
    if token := os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN'): headers['Authorization']='Bearer '+token
    client=client or httpx.AsyncClient(timeout=20,follow_redirects=False,headers=headers)
    public_client=httpx.AsyncClient(timeout=15,follow_redirects=False) if owned else client
    try:
        check()
        payload=json.loads(await bounded_get(client,'https://api.github.com/search/repositories?'+urlencode({'q':query,'per_page':6}),limit=1_000_000))
        candidates=[]
        for item in payload.get('items',[])[:6]:
            check()
            url=item['html_url'];owner,repo=parse_repository(url)
            readme='';readme_error=''
            try:
                data=json.loads(await bounded_get(client,f'https://api.github.com/repos/{owner}/{repo}/readme',limit=300_000))
                readme=base64.b64decode(data.get('content',''),validate=False).decode('utf-8')[:14_000]
            except (ValueError,UnicodeDecodeError,httpx.HTTPError):
                branch=quote(item.get('default_branch','HEAD'),safe='')
                for filename in ['README.md','readme.md','README.rst']:
                    check()
                    try:
                        readme=(await bounded_get(public_client,f'https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{filename}',limit=160_000)).decode('utf-8')[:14_000]
                        break
                    except (ValueError,UnicodeDecodeError,httpx.HTTPError): pass
                if not readme: readme_error='README 暂不可读取，相关条件待验证'
            excerpts=[{'id':i+1,'text':line.strip()[:240]} for i,line in enumerate(readme.splitlines()) if line.strip()][:100]
            candidates.append({'repository':url,'name':item['full_name'],'description':item.get('description') or '',
                'language':item.get('language'),'stars':item.get('stargazers_count',0),'updated':item.get('pushed_at'),
                'topics':item.get('topics',[]),'archived':bool(item.get('archived')),'readme':readme,'readme_excerpts':excerpts,'readme_error':readme_error})
            event('Research Agent','tool','已读取 '+item['full_name'],{'repository':url,'readme_read':bool(readme)})
        return {'query':query,'total':payload.get('total_count',0),'incomplete':bool(payload.get('incomplete_results')),'candidates':candidates}
    finally:
        if owned: await client.aclose();await public_client.aclose()


def verify_matches(result, matches):
    known={c['repository']:c for c in result['candidates']}
    seen=set()
    for match in matches.matches:
        if match.repository not in known or match.repository in seen: raise ValueError('搜索解释引用了不存在或重复的候选仓库')
        seen.add(match.repository)
        source=known[match.repository]['readme']
        excerpts={e['id']:e['text'] for e in known[match.repository].get('readme_excerpts',[])}
        if match.readme_evidence_ids:
            if any(i not in excerpts for i in match.readme_evidence_ids): raise ValueError('README 证据编号不存在')
            match.readme_quotes=[excerpts[i] for i in dict.fromkeys(match.readme_evidence_ids)]
        if any(not q.strip() or q not in source for q in match.readme_quotes): raise ValueError('搜索解释中的 README 引文校验失败')
    for c in result['candidates']:
        match=next((m for m in matches.matches if m.repository==c['repository']),None)
        c.update(reasons=match.reasons if match else [],unknown=match.unknown if match else ['尚未完成匹配评估'],readme_quotes=match.readme_quotes if match else [])
        if c['readme_error']: c['unknown'].append(c['readme_error'])
    order={m.repository:i for i,m in enumerate(matches.matches)}
    result['candidates'].sort(key=lambda c:(not bool(c['reasons']),order.get(c['repository'],len(order))))
    return result
