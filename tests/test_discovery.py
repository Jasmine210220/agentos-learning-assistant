import asyncio
import json
import os
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from backend import settings
from backend.app import create_app
from backend.models import Review
from backend.search_tools import SearchTools, repository_links, public_url
from backend.search_workflow import discover, DiscoveryPlan, Selection, Assessment, apply_assessment, github_query, broaden_queries, candidate_rank, merge_candidates


def test_web_contract_credentials_caps_and_links():
    seen=[]
    def respond(request):
        seen.append(request)
        assert request.url.host=='api.tavily.com'
        assert request.headers['authorization']=='Bearer tavily-test'
        assert 'github-test' not in str(request.headers)
        body=json.loads(request.content)
        assert body['include_images'] is False and body['include_answer'] is False and body['auto_parameters'] is False
        return httpx.Response(200,json={'results':[{'url':'https://example.org/project','title':'Project','content':'snippet','raw_content':'https://github.com/example/target/tree/main\nFull text'},{'url':'http://127.0.0.1/secret','content':'ignore'}],'usage':{'credits':1}})
    async def run():
        tool=SearchTools({'tavily':{'key':'tavily-test'},'github':{'key':'github-test'}},transport=httpx.MockTransport(respond))
        try:
            for _ in range(6):
                result=await tool.web_search('code cleanup')
                assert len(result)==1 and result[0]['content_kind']=='网页正文'
            with pytest.raises(ValueError,match='上限'): await tool.web_search('another')
            assert tool.credits==6 and len(seen)==6
        finally: await tool.close()
    asyncio.run(run())
    assert repository_links('https://github.com/a/b/tree/main https://github.com/a/b https://github.com/topics/rag')==['https://github.com/a/b']
    for url in ['https://user:pass@example.org/a','file:///a','https://127.0.0.1','https://10.0.0.1/a','https://x.local/a','javascript:alert(1)']:
        assert not public_url(url)


def test_github_verification_reads_skill_and_pins_sha():
    sha='a'*40;paths=[]
    def respond(request):
        paths.append(str(request.url))
        if request.url.host=='raw.githubusercontent.com':
            assert 'authorization' not in request.headers and sha in request.url.path
            return httpx.Response(200,text='# Skill\nSimplifies generated code while preserving behavior.\nMore details here.')
        assert request.headers['authorization']=='Bearer gh-test'
        if '/commits/' in request.url.path: return httpx.Response(200,json={'sha':sha})
        return httpx.Response(200,json={'tree':[{'path':'skills/cleanup/SKILL.md','type':'blob','mode':'100644','size':100},{'path':'.env','type':'blob','mode':'100644','size':10},{'path':'linked/SKILL.md','type':'blob','mode':'120000','size':10}]})
    async def run():
        tool=SearchTools({'tavily':{'key':''},'github':{'key':'gh-test'}},transport=httpx.MockTransport(respond))
        try:
            c=tool.candidate({'html_url':'https://github.com/example/skill','default_branch':'main'})
            r=await tool.verify(c,'精简代码 skill')
            assert r['verified'] and r['commit']==sha
            assert [d['path'] for d in r['documents']]==['README.md','skills/cleanup/SKILL.md']
            assert all(e['text'] in next(d['content'] for d in r['documents'] if d['path']==e['path']) for e in r['evidence'])
            assert not any('.env' in p or '/linked/' in p for p in paths)
        finally: await tool.close()
    asyncio.run(run())


def test_verification_uses_bounded_archive_when_api_limited(monkeypatch):
    async def archive(client,owner,repo,ref,event,check,selector):
        assert 'authorization' not in client.headers
        assert set(await selector([{'path':'SKILL.md'},{'path':'README.md'},{'path':'main.py'}]))=={'SKILL.md','README.md'}
        return {'commit':'a'*40,'files':[{'path':'SKILL.md','content':'Simplify code while preserving behavior.'}]}
    monkeypatch.setattr('backend.search_tools.read_archive',archive)
    def respond(request):
        return httpx.Response(403) if request.url.host=='api.github.com' else httpx.Response(404)
    async def run():
        tool=SearchTools({'tavily':{'key':''},'github':{'key':''}},transport=httpx.MockTransport(respond))
        try:
            result=await tool.verify(tool.candidate({'html_url':'https://github.com/example/skill'}),'simplify')
            assert result['verified'] and result['transport']=='archive'
            assert result['evidence'][0]['path']=='SKILL.md' and 'a'*40 in result['evidence'][0]['url']
        finally: await tool.close()
    asyncio.run(run())


class FakeTools:
    def __init__(self,credentials,check): self.check=check;self.web_calls=0;self.github_queries=0;self.credits=0;self.closed=False
    async def close(self): self.closed=True
    async def github_search(self,q,sort='best_match'):
        self.check();self.github_queries+=1
        return [self.candidate('target' if q=='code simplifier skill' else 'noise')]
    def candidate(self,name): return {'repository':'https://github.com/example/'+name,'name':name,'description':name,'verified':False,'evidence':[]}
    async def web_search(self,q):
        self.web_calls+=1;self.credits+=1
        return [{'url':'https://example.org/guide','title':'A cleanup skill','snippet':'Try a code simplifier skill','content':'https://github.com/example/target','content_kind':'网页正文'}]
    async def metadata(self,url): return self.candidate('target')
    async def verify(self,candidate,goal):
        return {**candidate,'verified':True,'verification_attempted':True,'documents':[{'path':'SKILL.md','content':'Code simplifier'}],
                'evidence':[{'id':1,'path':'SKILL.md','line':1,'text':'Code simplifier','url':candidate['repository']+'/blob/main/SKILL.md'}]}


class Model:
    def __init__(self): self.round=0
    async def ask(self,role,prompt,data,schema):
        if schema is DiscoveryPlan: return DiscoveryPlan(name_clue_query='',github_queries=['code cleanup'],web_queries=['代码精简 skill'],conditions=['精简代码'])
        if schema is Selection:
            self.round+=1
            return Selection(repositories=['https://github.com/example/'+('noise' if self.round==1 else 'target')])
        if schema is Assessment:
            found=self.round>=2
            return Assessment(summary='找到匹配' if found else '继续核实',sufficient=found,reason='发现新术语',next_github_queries=[] if found else ['code simplifier skill'],next_web_queries=[],matches=[{'repository':'https://github.com/example/'+('target' if found else 'noise'),'matched':found,'confidence':'high' if found else 'low','reasons':['用途有原文依据'] if found else [],'unknown':[],'evidence_ids':[1] if found else []}])
        return Review(passed=True,summary='证据一致',issues=[])


def run_discovery(tmp_path,monkeypatch,model=None,factory=FakeTools,web=True):
    settings.configure(tmp_path)
    monkeypatch.setenv('TAVILY_API_KEY','test') if web else monkeypatch.delenv('TAVILY_API_KEY',raising=False)
    result={};events=[];saves=[]
    asyncio.run(discover({'goal':'查找测试用途的 skill'},model or Model(),lambda *e:events.append(e),lambda *e:None,
                         lambda:saves.append(json.loads(json.dumps(result))),result,lambda:None,tools_factory=factory))
    return result,events,saves


def test_two_round_discovery_improves_known_fixture(tmp_path,monkeypatch):
    result,events,saves=run_discovery(tmp_path,monkeypatch)
    s=result['search']
    assert len(s['rounds'])==2 and s['candidates'][0]['repository'].endswith('/target')
    assert s['candidates'][0]['citations'][0]['path']=='SKILL.md'
    assert s['mode']=='web_github' and s['usage']['web_requests']==1
    assert result['review']['passed'] and s['outcome']=='found'
    assert any(e[1]=='handoff' for e in events) and saves[-1]['search']['collected_repositories']


def test_missing_web_key_honest_fallback(tmp_path,monkeypatch):
    result,_,_=run_discovery(tmp_path,monkeypatch,web=False)
    s=result['search'];assert s['mode']=='github_only' and s['usage']['web_requests']==0
    assert any('GitHub' in w for w in s['warnings']) and s['candidates'][0]['matched']


def test_name_clue_is_searched_separately_from_function(tmp_path,monkeypatch):
    queries=[]
    class ClueModel(Model):
        async def ask(self,role,prompt,data,schema):
            if schema is DiscoveryPlan:
                return DiscoveryPlan(name_clue_query='distinctive-name',github_queries=['code cleanup','simplify skill'],web_queries=[],conditions=[])
            return await super().ask(role,prompt,data,schema)
    class ClueTools(FakeTools):
        async def github_search(self,q,sort='best_match'):
            queries.append((q,sort))
            return await super().github_search(q,sort)
    run_discovery(tmp_path,monkeypatch,model=ClueModel(),factory=ClueTools,web=False)
    assert queries[:2]==[('distinctive-name','best_match'),('code cleanup','stars')]


def test_queries_are_short_and_empty_round_broadens():
    assert github_query('code simplification skill AI generated code cleanup')=='code simplification skill AI generated code cleanup'
    assert github_query('code-simplifier skill ponytail logo')=='code-simplifier skill ponytail logo'
    assert broaden_queries(['code simplification','refactor reduce'],{'code simplification','refactor reduce'})==['simplification','reduce']
    assert github_query('ponytail in:name,description,readme')=='ponytail in:name,description,readme'
    assert DiscoveryPlan(name_clue_query='',github_queries=['workflow automation self-hosted node code'],web_queries=[],conditions=[])
    with pytest.raises(ValueError):
        DiscoveryPlan(name_clue_query='',github_queries=['x'*221],web_queries=[],conditions=[])
    assert DiscoveryPlan(name_clue_query='',github_queries=['workflow automation in:readme'],web_queries=[],conditions=[])


def test_empty_results_trigger_broader_retry(tmp_path,monkeypatch):
    queries=[]
    class EmptyFirst(FakeTools):
        async def github_search(self,q,sort='best_match'):
            queries.append(q);self.github_queries+=1
            return [] if ' ' in q else [self.candidate('noise')]
    class EmptyAssessment(Model):
        async def ask(self,role,prompt,data,schema):
            if schema is Assessment:
                return Assessment(summary='没有结果',matches=[],sufficient=False,reason='继续找',next_github_queries=['more restrictive terms'],next_web_queries=[])
            return await super().ask(role,prompt,data,schema)
    r,_,_=run_discovery(tmp_path,monkeypatch,model=EmptyAssessment(),factory=EmptyFirst,web=False)
    assert queries[:2]==['code cleanup','cleanup']
    assert r['search']['collected_repositories']


def test_round_cap_and_duplicate_query_stop(tmp_path,monkeypatch):
    class Continue(Model):
        async def ask(self,role,prompt,data,schema):
            if schema is Assessment:
                return Assessment(summary='未找到',matches=[],sufficient=False,reason='尝试替代词',next_github_queries=['alternate '+str(len(data['rounds']))],next_web_queries=[])
            return await super().ask(role,prompt,data,schema)
    r,_,_=run_discovery(tmp_path,monkeypatch,Continue());s=r['search']
    assert len(s['rounds'])==3 and s['outcome']=='not_found' and '三轮' in s['stop_reason']
    class Repeated(Continue):
        async def ask(self,role,prompt,data,schema):
            value=await super().ask(role,prompt,data,schema)
            if schema is Assessment: value.next_github_queries=[' CODE   CLEANUP ']
            return value
    r,_,_=run_discovery(tmp_path,monkeypatch,Repeated())
    assert r['search']['usage']['github_queries']==1 and '重复' in r['search']['stop_reason']


def test_invalid_evidence_not_accepted():
    a=Assessment(summary='x',sufficient=True,reason='x',next_github_queries=[],next_web_queries=[],matches=[{'repository':'https://github.com/a/b','matched':True,'confidence':'high','reasons':['x'],'unknown':[],'evidence_ids':[3]}])
    with pytest.raises(ValueError): apply_assessment({}, {'https://github.com/a/b':{'verified':True,'evidence':[]}},a)


def test_popular_channel_and_relevance_channel_keep_request_budget():
    requests=[]
    def respond(request):
        requests.append(request)
        return httpx.Response(200,json={'items':[{'html_url':f'https://github.com/example/r{i}','stargazers_count':1000-i} for i in range(30)]})
    async def run():
        tool=SearchTools({'tavily':{'key':''},'github':{'key':''}},transport=httpx.MockTransport(respond))
        try:
            for i in range(6):
                rows=await tool.github_search('workflow automation',sort='stars' if i%2 else 'best_match')
                assert len(rows)==30 and rows[-1]['discovery'][0]['rank']==30
            with pytest.raises(ValueError,match='上限'): await tool.github_search('extra')
        finally: await tool.close()
    asyncio.run(run())
    assert all(r.url.params['per_page']=='30' for r in requests)
    assert 'sort' not in requests[0].url.params
    assert requests[1].url.params['q']=='workflow automation in:name,description,readme'
    assert requests[1].url.params['sort']=='stars' and requests[1].url.params['order']=='desc'


def test_relevance_precedes_popularity_and_niche_candidates_survive():
    rows=[{'name':'popular-unrelated','matched':False,'confidence':'high','stars':500000},
          {'name':'partial','matched':True,'confidence':'low','stars':200000},
          {'name':'niche','matched':True,'confidence':'high','stars':2},
          {'name':'mature','matched':True,'confidence':'high','stars':12000},
          {'name':'archived','matched':True,'confidence':'high','stars':100000,'archived':True}]
    assert [c['name'] for c in sorted(rows,key=candidate_rank)]==['mature','niche','archived','partial','popular-unrelated']


def test_late_popular_candidate_is_not_dropped_and_evidence_is_preserved():
    pool={f'https://github.com/a/r{i}':{'repository':f'https://github.com/a/r{i}'} for i in range(70)}
    target={'repository':'https://github.com/a/target','verified':True,'evidence':[{'id':1}]}
    merge_candidates(pool,[target])
    merge_candidates(pool,[{'repository':target['repository'],'verified':False,'discovery':[{'sort':'stars','rank':1}]}])
    assert pool[target['repository']]['verified'] and pool[target['repository']]['evidence']==[{'id':1}]
    assert pool[target['repository']]['discovery']==[{'sort':'stars','rank':1}]


def test_obscure_match_continues_if_new_search_direction_exists():
    repo='https://github.com/a/niche';pool={repo:{'verified':True,'stars':3,'evidence':[{'id':1,'text':'actual feature'}]}}
    a=Assessment(summary='候选',sufficient=True,reason='继续比较',next_github_queries=['related tool'],next_web_queries=[],matches=[{
        'repository':repo,'matched':True,'confidence':'high','reasons':['功能匹配'],'unknown':[],'evidence_ids':[1]}])
    search={}
    assert not apply_assessment(search,pool,a)
    assert search['candidates'][0]['matched']
    a.next_github_queries=[]
    assert apply_assessment(search,pool,a)


def test_review_requests_one_evidence_based_repair(tmp_path,monkeypatch):
    class Repair(Model):
        def __init__(self): super().__init__();self.reviews=0
        async def ask(self,role,prompt,data,schema):
            if schema is Review:
                self.reviews+=1
                return Review(passed=self.reviews>1,summary='已核对',issues=['删除过度推断'] if self.reviews==1 else [])
            return await super().ask(role,prompt,data,schema)
    model=Repair();result,events,_=run_discovery(tmp_path,monkeypatch,model=model)
    assert model.reviews==2 and result['review']['passed']
    assert any(e[2]=='修正缺少依据的表述' for e in events)


def test_web_failure_keeps_github_results(tmp_path,monkeypatch):
    class BrokenWeb(FakeTools):
        async def web_search(self,q):
            self.web_calls+=1
            raise ValueError('Tavily 搜索额度不足')
    result,_,_=run_discovery(tmp_path,monkeypatch,factory=BrokenWeb)
    assert result['search']['outcome']=='found'
    assert result['search']['rounds'][0]['errors']==['Tavily 搜索额度不足']


def test_all_sources_fail_and_clients_close(tmp_path,monkeypatch):
    instances=[]
    class Broken(FakeTools):
        def __init__(self,*args):
            super().__init__(*args);instances.append(self)
        async def github_search(self,q,sort='best_match'): raise ValueError('GitHub 请求失败')
        async def web_search(self,q): raise ValueError('Tavily 请求失败')
    class Empty(Model):
        async def ask(self,role,prompt,data,schema):
            if schema is Assessment:
                return Assessment(summary='没有结果',matches=[],sufficient=False,reason='来源失败',next_github_queries=[],next_web_queries=[])
            return await super().ask(role,prompt,data,schema)
    with pytest.raises(ValueError,match='均未成功'):
        run_discovery(tmp_path,monkeypatch,model=Empty(),factory=Broken)
    assert instances[0].closed


def test_search_settings_api_does_not_expose_keys(tmp_path,monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY','tavily-secret');monkeypatch.setenv('GITHUB_TOKEN','github-secret')
    with TestClient(create_app(tmp_path,'token')) as client:
        assert client.get('/settings/search').status_code==401
        client.headers['Authorization']='Bearer token'
        data=client.get('/settings/search');assert data.json()['tavily']['configured']
        assert 'tavily-secret' not in data.text and 'github-secret' not in data.text
        if os.name=='nt':
            saved=client.post('/settings/search',json={'tavily_key':'saved-placeholder'})
            assert saved.status_code==200 and 'saved-placeholder' not in saved.text
            assert 'saved-placeholder' not in (tmp_path/'search-settings.json').read_text()
            assert settings.search_credentials()['tavily']['key']=='saved-placeholder'
            client.post('/settings/search',json={'clear_tavily':True})
            assert settings.search_credentials()['tavily']['key']=='tavily-secret'
