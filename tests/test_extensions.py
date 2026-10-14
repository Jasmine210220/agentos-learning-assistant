import asyncio
import base64
import json
import pytest
import httpx
from backend.store import Store
from backend.github import priority,rank_candidates
from backend.charts import parse_data,render_chart,ChartSpec
from backend.research import search_repositories,verify_matches,Matches
from backend.workspaces import save_attachment,get_attachment,edit_copy
from backend.models import BugReport,Review,TaskInput
from backend.workflow import Runner
from backend.runtime import Runtime
from backend.app import create_app
from fastapi.testclient import TestClient


def test_reader_business_priority():
    assert priority('backend/app/api/chat.py')<priority('backend/tools/run_real_sample.py')
    rank=rank_candidates(['app/api/a.py','app/api/b.py','app/services/a.py','app/services/b.py'])
    assert rank['app/services/a.py']<rank['app/api/b.py']


def test_charts_preserve_missing_negative_and_source():
    data=parse_data('月份,收入,支出\n一月,10,-3\n二月,,2\n三月,15,0')
    for kind in ['line','bar']:
        svg,series=render_chart(data,ChartSpec(title='<测试>',kind=kind,x='月份',y=['收入','支出'],unit='元'))
        assert '&lt;测试&gt;' in svg and '<测试>' not in svg
        assert series[0]['values']==[10,None,15]
        assert series[1]['values']==[-3,2,0]
    with pytest.raises(ValueError): render_chart(data,ChartSpec(title='x',kind='bar',x='月份',y=['不存在'],unit=''))
    with pytest.raises(ValueError): parse_data('a,b\n'+''.join('x,1\n' for _ in range(201)))
    with pytest.raises(ValueError): parse_data('[{"a":1,"b":2},{"a":3}]')
    markdown=parse_data('| 月份 | 数量 |\n|---|---|\n| 一月 | 1 |')
    assert markdown['rows']==[{'月份':'一月','数量':'1'}]


def test_attachment_paths_and_copy_edit(tmp_path):
    store=Store(tmp_path)
    for path in ['../main.py','/tmp/a.py','C:/main.py','.env','secrets.json','../data.csv','secrets.csv','.git/data.csv']:
        with pytest.raises(ValueError): save_attachment(store,[{'path':path,'content':'x'}])
    a=save_attachment(store,[{'path':'main.py','content':'print(1)\n'}])
    b=edit_copy(store,a['id'],'main.py','print(2)\n')
    assert b['id']!=a['id'] and '-print(1)' in b['diff'] and '+print(2)' in b['diff']
    assert get_attachment(store,a['id'])['files'][0]['content']=='print(1)\n'
    with pytest.raises(ValueError): get_attachment(store,'../oops')


def test_github_search_and_quotes():
    def respond(request):
        if request.url.path=='/search/repositories':
            assert request.url.params['q']=='rag language:Python'
            return httpx.Response(200,json={'total_count':1,'items':[{'html_url':'https://github.com/a/b','full_name':'a/b','description':'RAG example','language':'Python','pushed_at':'2026-10-01'}]})
        return httpx.Response(200,json={'content':base64.b64encode(b'Has a web UI.').decode()})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result=await search_repositories('rag language:Python',lambda *a:None,lambda:None,client)
            matches=Matches(summary='one',matches=[{'repository':'https://github.com/a/b','reasons':['有界面'],'unknown':['学习难度未知'],'readme_quotes':['Has a web UI.']}])
            verify_matches(result,matches)
            assert result['candidates'][0]['reasons']==['有界面']
            matches.matches[0].readme_quotes=['invented']
            with pytest.raises(ValueError): verify_matches(result,matches)
            result['candidates'][0]['readme_excerpts']=[{'id':7,'text':'Has a web UI.'}]
            matches.matches[0].readme_evidence_ids=[7]
            verify_matches(result,matches)
            assert result['candidates'][0]['readme_quotes']==['Has a web UI.']
            matches.matches[0].readme_evidence_ids=[999]
            with pytest.raises(ValueError): verify_matches(result,matches)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(429))) as client:
            with pytest.raises(ValueError,match='限流'): await search_repositories('rag',lambda *a:None,lambda:None,client)
    asyncio.run(run())


def test_chart_workflow_and_bad_data(tmp_path):
    class Model:
        def __init__(self,*a): pass
        async def ask(self,role,instruction,data,schema):
            if schema is Review: return Review(passed=True,summary='字段与目标一致',issues=[])
            return ChartSpec(title='销量',kind='bar',x='月',y=['销量'],unit='件')
    async def run():
        store=Store(tmp_path);runner=Runner(store,llm_factory=Model)
        task=store.create(TaskInput(kind='chart',goal='画销量',input_text='月,销量\n一月,10\n二月,20').model_dump())
        await runner.run(task['id'])
        assert store.get(task['id'])['status']=='succeeded'
        assert (store.workspace(task['id'])/'chart.svg').exists()
        bad=store.create(TaskInput(kind='chart',goal='画销量',input_text='no data').model_dump())
        await runner.run(bad['id'])
        assert store.get(bad['id'])['status']=='failed'
        assert not (store.workspace(bad['id'])/'chart.svg').exists()
    asyncio.run(run())


def test_static_bug_review_and_no_execution(tmp_path):
    class Model:
        def __init__(self,*a): pass
        async def ask(self,role,instruction,data,schema):
            if schema is BugReport: return BugReport(summary='存在除零风险',findings=[{'title':'除零','severity':'medium','trigger':'b=0','cause':'除数未检查','impact':'抛异常','suggestion':'检查 b','evidence':[{'path':'snippet.py','start_line':2,'end_line':2}]}],limitations=['未执行'])
            return Review(passed=True,summary='证据支持静态结论',issues=[])
    async def run():
        store=Store(tmp_path);runner=Runner(store,llm_factory=Model)
        task=store.create(TaskInput(kind='debug',goal='检查除零',input_text='```python\ndef divide(a,b):\n    return a/b\n```').model_dump())
        await runner.run(task['id'])
        data=store.get(task['id']);assert data['status']=='succeeded'
        assert data['result']['bugs']['findings'][0]['verification']=='静态推测'
    asyncio.run(run())


def test_runtime_requires_confirmation_and_available_engine(tmp_path,monkeypatch):
    store=Store(tmp_path);runtime=Runtime(store)
    attachment=save_attachment(store,[{'path':'main.py','content':'print(1)'}])
    async def unavailable(): return {'available':False,'reason':'不可用'}
    monkeypatch.setattr(runtime,'health',unavailable)
    async def run():
        with pytest.raises(ValueError,match='确认'): await runtime.start(attachment['id'],'main.py',False)
        with pytest.raises(ValueError,match='不可用'): await runtime.start(attachment['id'],'main.py',True)
        assert not runtime.jobs
    asyncio.run(run())


def test_attachment_and_settings_api(tmp_path,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-secret')
    with TestClient(create_app(tmp_path,'token')) as client:
        assert client.post('/attachments',json={'files':[{'path':'a.py','content':'x'}]}).status_code==401
        client.headers['Authorization']='Bearer token'
        a=client.post('/attachments',json={'files':[{'path':'a.py','content':'print(1)'}]}).json()
        assert client.get('/attachments/'+a['id']).json()['files'][0]['path']=='a.py'
        assert client.get('/settings').json()['configured']
        assert 'test-secret' not in client.get('/settings').text
        assert client.post('/runs',json={'attachment_id':a['id'],'path':'a.py','confirmed':False}).status_code==409


def test_runtime_capability_field_and_recovery(tmp_path,monkeypatch):
    store=Store(tmp_path);runtime=Runtime(store)
    ident='a'*32
    store.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',(ident,'b'*32,'x.py','2026-10-13','running','{}'));store.db.commit()
    runtime=Runtime(store);calls=[]
    async def command(*args,**kwargs):
        calls.append(args)
        if args[0]=='info': return json.dumps({'OSType':'linux','MemoryLimit':True,'PidsLimit':True,'CpuCfsQuota':True})
        if args[0]=='image': return '[{"Id":"sha256:local"}]'
        if args[0]=='ps': return 'agentos-'+ident
        return ''
    monkeypatch.setattr(runtime,'command',command)
    assert asyncio.run(runtime.health())['available']
    assert ('rm','-f','agentos-'+ident) in calls
    assert runtime.get(ident)['status']=='interrupted'


def test_windows_key_protection_roundtrip():
    import os
    from backend.settings import crypt
    if os.name!='nt': pytest.skip('Windows DPAPI')
    value=b'unit-test-placeholder-not-a-key'
    protected=crypt(value)
    assert protected!=value and crypt(protected,True)==value
