"""Bounded external search and GitHub verification. Credentials never enter model context."""
import base64
import ipaddress
import json
import re
from urllib.parse import urlparse, quote, urlencode
import httpx
from .github import bounded_get, parse_repository, safe_source, GitHubLimited, read_archive
from .store import now


def public_url(value):
    try:
        u=urlparse(value)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.port not in (None,443): return ''
        if '.' not in u.hostname or u.hostname.endswith(('.local','.internal','.localhost')): return ''
        try:
            if not ipaddress.ip_address(u.hostname).is_global: return ''
        except ValueError: pass
        return u._replace(fragment='').geturl()
    except (ValueError,TypeError): return ''


def repository_links(text):
    result=[]
    for value in re.findall(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',text):
        value=value.rstrip('.')
        try:
            owner,repo=parse_repository(value)
            if owner.lower() in {'topics','collections','features','settings','marketplace','orgs','users','search'}: continue
            value=f'https://github.com/{owner}/{repo}'
            if value.lower() not in {x.lower() for x in result}: result.append(value)
        except ValueError: pass
    return result


def search_terms(goal):
    words=set(re.findall(r'[a-z][a-z0-9_]{2,}',goal.lower()))
    for clue,terms in [('检索','search query retrieve'),('错误','error exception retry timeout'),
                       ('可靠','retry timeout rate limit'),('精简','simplify refactor cleanup'),
                       ('代码','code'),('文献','pubmed literature paper'),('工作流','workflow automation'),
                       ('知识库','rag retrieval knowledge'),('结果','result response')]:
        if clue in goal: words.update(terms.split())
    return words-{'github','https','project','repository','readme','com','the','and','for','with'}


def excerpts(content,path,limit=8,goal=''):
    """Keep contiguous, verbatim passages; rank evidence by the actual question."""
    lines=content.splitlines();words=search_terms(goal);blocks=[]
    for start in range(0,len(lines),4):
        text='\n'.join(lines[start:start+4])
        if not text.strip() or text.count('img.shields.io')>1: continue
        score=sum(min(text.lower().count(w),2) for w in words)
        if start<12: score+=1
        blocks.append((score,start,text))
    chosen=sorted(blocks,key=lambda x:(-x[0],x[1]))[:limit]
    return [{'path':path,'line':start+1,'end_line':min(start+4,len(lines)),
             'text':text[:900]} for _,start,text in chosen]


def reference_evidence(candidate,limit=16):
    # Round-robin preserves source/skill passages instead of consuming the budget
    # with a README alone. IDs always refer to the original verified document.
    groups={}
    for e in candidate.get('evidence',[]): groups.setdefault(e['path'],[]).append(e)
    result=[]
    for i in range(max((len(v) for v in groups.values()),default=0)):
        for values in groups.values():
            if i<len(values): result.append(values[i])
            if len(result)>=limit: return result
    return result


class SearchTools:
    def __init__(self,credentials,check=lambda:None,transport=None):
        self.check=check;self.credentials=credentials
        headers={'User-Agent':'AgentOS-Lite','Accept':'application/vnd.github+json'}
        if credentials['github']['key']: headers['Authorization']='Bearer '+credentials['github']['key']
        self.github=httpx.AsyncClient(timeout=15,follow_redirects=False,headers=headers,transport=transport)
        self.public=httpx.AsyncClient(timeout=20,follow_redirects=False,transport=transport)
        self.web_calls=0;self.github_queries=0;self.credits=0

    async def close(self):
        await self.github.aclose();await self.public.aclose()

    async def web_search(self,query):
        self.check()
        key=self.credentials['tavily']['key']
        if not key: raise ValueError('未配置 Tavily，当前仅搜索 GitHub')
        if self.web_calls>=6: raise ValueError('已达到全网搜索请求上限')
        self.web_calls+=1
        body={'query':query[:400],'search_depth':'basic','max_results':5,'topic':'general','include_answer':False,'include_raw_content':'markdown','include_images':False,'auto_parameters':False,'include_usage':True}
        async with self.public.stream('POST','https://api.tavily.com/search',headers={'Authorization':'Bearer '+key},json=body) as response:
            if response.status_code in (401,403): raise ValueError('Tavily 密钥无效或无访问权限，请检查搜索设置')
            if response.status_code in (429,432,433): raise ValueError('Tavily 请求限流或额度不足')
            if response.status_code!=200: raise ValueError(f'Tavily 请求失败（HTTP {response.status_code}）')
            content=bytearray()
            async for chunk in response.aiter_bytes():
                self.check();content.extend(chunk)
                if len(content)>2_000_000: raise ValueError('搜索响应超过读取上限')
        data=json.loads(content);self.credits+=max(0,float(data.get('usage',{}).get('credits',0)))
        results=[]
        for item in data.get('results',[])[:5]:
            url=public_url(item.get('url',''))
            if not url: continue
            raw=item.get('raw_content') or '';snippet=item.get('content') or ''
            results.append({'url':url,'title':str(item.get('title',''))[:250],'content':str(raw or snippet)[:8000],
                            'snippet':str(snippet)[:1800],'content_kind':'网页正文' if raw else '搜索摘要','query':query,'retrieved_at':now()})
        return results

    async def github_search(self,query,sort='best_match'):
        self.check()
        if sort not in {'best_match','stars'}: raise ValueError('无效的搜索排序')
        if self.github_queries>=6: raise ValueError('已达到 GitHub 检索上限')
        self.github_queries+=1
        # Popularity retrieval needs README vocabulary: many established projects
        # do not put every feature in their short description or repository name.
        effective=query
        if sort=='stars' and not re.search(r'\bin:',query): effective+=' in:name,description,readme'
        params={'q':effective,'per_page':30}
        if sort=='stars': params.update(sort='stars',order='desc')
        data=json.loads(await bounded_get(self.github,'https://api.github.com/search/repositories?'+urlencode(params),limit=1_500_000))
        return [{**self.candidate(i),'discovery':[{'query':effective,'sort':sort,'rank':rank+1}]}
                for rank,i in enumerate(data.get('items',[])[:30]) if not i.get('private')]

    def candidate(self,item):
        owner,repo=parse_repository(item['html_url'])
        return {'repository':f'https://github.com/{owner}/{repo}','name':f'{owner}/{repo}','description':str(item.get('description') or '')[:1000],
                'language':item.get('language'),'stars':item.get('stargazers_count',0),'updated':item.get('pushed_at'),
                'archived':bool(item.get('archived')),'fork':bool(item.get('fork')),'default_branch':item.get('default_branch','HEAD'),'topics':item.get('topics',[]),
                'verified':False,'reasons':[],'unknown':['尚未核实'],'readme_quotes':[],'evidence':[]}

    async def metadata(self,url):
        self.check();owner,repo=parse_repository(url)
        item=json.loads(await bounded_get(self.github,f'https://api.github.com/repos/{owner}/{repo}',limit=150_000))
        if item.get('private'): raise ValueError('本次搜索仅支持公开仓库')
        return self.candidate(item)

    async def verify(self,candidate,goal):
        self.check();owner,repo=parse_repository(candidate['repository']);api=f'https://api.github.com/repos/{owner}/{repo}'
        result={**candidate,'evidence':[],'documents':[],'verification_warnings':[],'verified':False}
        branch=quote(candidate.get('default_branch','HEAD'),safe='');ref=branch
        words=search_terms(goal)
        def rank(path):
            name=path.lower();score=sum(3 for w in words if w in name)
            if name.endswith('skill.md'): score+=10 if 'skill' in goal.lower() else -5
            elif name.endswith('readme.md'): score+=2
            if any(s in name for s in ('/test','test_','release','examples/')): score-=3
            return (-score,len(path))
        try:
            commit=json.loads(await bounded_get(self.github,f'{api}/commits/{branch}',limit=150_000)).get('sha','')
            if re.fullmatch('[a-f0-9]{40}',commit): ref=commit;result['commit']=commit
        except (ValueError,httpx.HTTPError): result['verification_warnings'].append('未能固定提交，按分支读取')
        async def read(path):
            self.check()
            value=(await bounded_get(self.public,f'https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{quote(path,safe="/")}',limit=120_000)).decode('utf-8')[:24000]
            result['documents'].append({'path':path,'content':value,'url':f'https://github.com/{owner}/{repo}/blob/{ref}/{quote(path,safe="/")}'})
            return value
        for filename in ['README.md','readme.md','README.rst']:
            try: await read(filename);break
            except (ValueError,UnicodeError,httpx.HTTPError): pass
        try:
            tree=json.loads(await bounded_get(self.github,f'{api}/git/trees/{ref}?recursive=1',limit=1_500_000))
            if tree.get('truncated'): result['verification_warnings'].append('目录响应不完整')
            paths=[f['path'] for f in tree.get('tree',[]) if f.get('type')=='blob' and f.get('mode')!='120000' and 0<f.get('size',0)<=120000 and safe_source(f['path'])][:2000]
            ordered=sorted(paths,key=rank)
            sources=[p for p in ordered if p.lower().endswith(('.py','.ts','.tsx','.js','.jsx','.go','.rs'))]
            result['file_candidates']=list(dict.fromkeys(ordered[:10]+sources[:12]))
            chosen=[p for p in sorted(paths,key=rank) if p not in {d['path'] for d in result['documents']} and (p.lower().endswith(('skill.md','readme.md')) or any(w in p.lower() for w in words))][:2]
            for path in chosen:
                try: await read(path)
                except (ValueError,UnicodeError,httpx.HTTPError): result['verification_warnings'].append('文件无法读取：'+path)
        except GitHubLimited:
            # Public Git archives do not require the REST API's anonymous quota.
            # Reuse the bounded, in-memory reader; never extract or execute files.
            try:
                async def select_docs(entries):
                    paths=[e['path'] for e in entries if safe_source(e['path'])]
                    result['file_candidates']=sorted(paths,key=rank)[:20]
                    root=next((p for p in paths if p.lower()=='readme.md'),None)
                    chosen=[p for p in sorted(paths,key=rank) if p!=root and
                            (p.lower().endswith(('readme.md','skill.md')) or any(w in p.lower() for w in words))][:2]
                    return ([root] if root else [])+chosen
                snapshot=await read_archive(self.public,owner,repo,candidate.get('default_branch',''),lambda *args:None,self.check,select_docs)
                result['commit']=snapshot['commit'];result['transport']='archive'
                result['documents']=[{'path':d['path'],'content':d['content'][:24000],'url':f'https://github.com/{owner}/{repo}/blob/{snapshot["commit"]}/{quote(d["path"],safe="/")}'} for d in snapshot['files']]
                result['verification_warnings']=['已通过公开源码归档核实']
            except (ValueError,httpx.HTTPError): result['verification_warnings'].append('目录暂不可读取，已保留可读取正文')
        except (ValueError,httpx.HTTPError): result['verification_warnings'].append('目录暂不可读取')
        for doc in result['documents']:
            for entry in excerpts(doc['content'],doc['path'],goal=goal):
                result['evidence'].append({**entry,'id':len(result['evidence'])+1,'url':doc['url']})
        result['verified']=bool(result['documents']);result['retrieved_at']=now()
        if not result['verified']: result['verification_warnings'].append('未取得仓库正文，不能核实功能')
        return result

    async def implementation(self,candidate,goal):
        """Read at most two pinned source files; never execute external code."""
        ref=candidate.get('commit','')
        if not re.fullmatch('[a-f0-9]{40}',ref): return candidate
        owner,repo=parse_repository(candidate['repository']);words=search_terms(goal)
        read={d['path'] for d in candidate.get('documents',[])}
        paths=[p for p in candidate.get('file_candidates',[]) if safe_source(p)
               and p.lower().endswith(('.py','.ts','.tsx','.js','.jsx','.go','.rs','skill.md')) and p not in read]
        paths.sort(key=lambda p:(-sum(w in p.lower() for w in words),len(p)))
        for path in paths[:2]:
            try:
                value=(await bounded_get(self.public,f'https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{quote(path,safe="/")}',limit=120_000)).decode('utf-8')
                url=f'https://github.com/{owner}/{repo}/blob/{ref}/{quote(path,safe="/")}'
                candidate.setdefault('documents',[]).append({'path':path,'content':value,'url':url})
                for e in excerpts(value,path,limit=6,goal=goal):
                    candidate['evidence'].append({**e,'id':len(candidate['evidence'])+1,'url':url})
            except (ValueError,UnicodeError,httpx.HTTPError):
                candidate.setdefault('verification_warnings',[]).append('实现文件无法读取：'+path)
        return candidate
