import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.store import Store
from backend.github import parse_repository, safe_source, read_repository, priority
from backend.models import Plan, Analysis, Diagram, Review
from backend.workflow import Runner, diagram_source, validate_evidence


SNAPSHOT = {"repository":"https://github.com/example/demo", "commit":"a"*40, "ref":"main", "files":[{"path":"main.py","content":"def hello():\n    return 'hello'\n\nhello()\n","lines":4,"bytes":45}], "skipped":[], "total_tree_files":1, "candidate_files":1, "read_bytes":45, "tree_truncated":False, "directory":["main.py"]}
E = {"path":"main.py","start_line":1,"end_line":2}
CLAIM = {"title":"入口","explanation":"函数返回 hello。","evidence":[E],"inferred":False}
ANALYSIS = {"summary":"演示项目","stack":["Python"],"modules":[CLAIM],"flow":[CLAIM],"limitations":["未执行代码"]}
DIAGRAM = {"title":"调用流程","nodes":[{"id":"n1","label":"入口","evidence":[E]},{"id":"n2","label":"返回文本","evidence":[E]}],"edges":[{"source":"n1","target":"n2","label":"调用","inferred":False,"evidence":[E]}]}


class FakeLLM:
    reject = False
    def __init__(self, usage, check): self.check = check
    async def ask(self, role, instruction, data, schema):
        self.check()
        if schema is Plan: return Plan(objective="test",focus=["代码"],acceptance=["证据有效"])
        if schema is Analysis: return Analysis.model_validate(ANALYSIS)
        if schema is Diagram: return Diagram.model_validate(DIAGRAM)
        return Review(passed=not self.reject, summary="审核结论", issues=["需修订"] if self.reject else [])


async def reader(*args): return SNAPSHOT
async def renderer(source, workspace):
    (workspace / "diagram.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>test</text></svg>', encoding="utf-8")
    return "svg"


def test_repository_boundaries():
    assert parse_repository("https://github.com/example/demo.git") == ("example","demo")
    for bad in ["http://github.com/a/b","https://github.com.evil.com/a/b","https://user:pw@github.com/a/b","https://github.com/a/b/tree/main","https://github.com/a/..","https://localhost/a/b"]:
        with pytest.raises(ValueError): parse_repository(bad)
    for bad in ["../main.py", "node_modules/a.js", ".env.txt", "secrets.json", "a\\b.py"]: assert not safe_source(bad)
    assert safe_source("app/main.py")
    assert priority('src/fastapi_cli/cli.py') < priority('tests/assets/main.py')


def test_evidence_and_diagram_boundaries():
    assert not validate_evidence(ANALYSIS,SNAPSHOT)
    assert validate_evidence({"evidence":[{**E,"end_line":999}]},SNAPSHOT)
    assert validate_evidence({"evidence":[{**E,"path":"unread.py"}]},SNAPSHOT)
    d = Diagram.model_validate(DIAGRAM)
    d.nodes[0].label = '"] --> evil["'
    assert '＂］' in diagram_source(d)
    assert '"] --> evil["' not in diagram_source(d)
    d.edges[0].target = "missing"
    with pytest.raises(ValueError): diagram_source(d)


@pytest.mark.parametrize('reject,expected',[ (False,'succeeded'),(True,'needs_attention') ])
def test_complete_workflow_persists_and_caps_repairs(tmp_path,reject,expected):
    class Model(FakeLLM): pass
    Model.reject = reject
    store = Store(tmp_path)
    task = store.create({"repository":SNAPSHOT['repository'],"goal":"分析代码","ref":""})
    async def run():
        runner = Runner(store,reader,Model,renderer)
        await runner.run(task['id'])
    asyncio.run(run())
    task = store.get(task['id'])
    assert task['status'] == expected
    assert task['result']['checks']['round'] == (3 if reject else 1)
    assert (store.workspace(task['id'])/'report.md').exists()
    events = store.events(task['id'])
    assert len({e['role'] for e in events if e['kind']=='started'}) == 4
    assert all('receiver' in e['details'] for e in events if e['kind']=='handoff')
    last = events[2]['id']
    assert all(e['id'] > last for e in store.events(task['id'],last))
    store.db.close()
    reopened = Store(tmp_path)
    reopened.recover()
    assert reopened.get(task['id'])['status'] == expected
    reopened.db.close()


def test_recovery_marks_abandoned_task(tmp_path):
    store = Store(tmp_path)
    task = store.create({'repository':SNAPSHOT['repository'],'goal':'test'})
    store.update(task['id'],status='analyzing')
    store.recover()
    assert store.get(task['id'])['status']=='interrupted'


def test_cancel_inflight_stops_next_role(tmp_path):
    entered = asyncio.Event()
    class Slow(FakeLLM):
        async def ask(self,*args):
            entered.set()
            await asyncio.sleep(10)
            return await super().ask(*args)
    async def run():
        store = Store(tmp_path)
        runner = Runner(store,reader,Slow,renderer)
        task = store.create({'repository':SNAPSHOT['repository'],'goal':'test'})
        runner.start(task['id'])
        job = runner.jobs[task['id']]
        await asyncio.wait_for(entered.wait(), 2)
        runner.stop(task['id'])
        await asyncio.wait_for(job, 2)
        assert store.get(task['id'])['status']=='cancelled'
        assert not any(e['role']=='Code Agent' for e in store.events(task['id']))
    asyncio.run(run())


def test_api_auth_paths_and_source_snapshot(tmp_path,monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-key-not-real')
    def factory(store): return Runner(store,reader,FakeLLM,renderer)
    app = create_app(tmp_path,'test-token',factory)
    with TestClient(app) as client:
        assert client.get('/tasks').status_code == 401
        client.headers['Authorization']='Bearer test-token'
        assert client.get('/health').json()['configured']
        assert client.post('/tasks',json={'repository':'https://evil.example/a/b','goal':'test'}).status_code == 422
        response=client.post('/tasks',json={'repository':SNAPSHOT['repository'],'goal':'test'})
        assert response.status_code==201
        task_id=response.json()['id']
        import time
        for _ in range(100):
            data=client.get('/tasks/'+task_id).json()
            if data['status']=='succeeded':break
            time.sleep(.01)
        assert data['status']=='succeeded'
        assert client.get(f'/tasks/{task_id}/source',params={'path':'main.py'}).json()['content']==SNAPSHOT['files'][0]['content']
        assert client.get(f'/tasks/{task_id}/source',params={'path':'../../secret'}).status_code==404
        assert client.get(f'/tasks/{task_id}/artifacts/history.sqlite3').status_code==404
        assert client.get(f'/tasks/{task_id}/artifacts/report.md').status_code==200
        assert 'test-key-not-real' not in json.dumps(client.get(f'/tasks/{task_id}/events').json())


def test_api_limit_archive_fallback_and_fixed_sha():
    import httpx, io, zipfile
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as z:
        z.writestr('demo-sha/main.py',"def hello():\n    return 'hello'\n")
        z.writestr('demo-sha/../../outside.py','bad')
        z.writestr('demo-sha/.env.txt','secret')
        z.writestr('demo-sha/node_modules/evil.js','bad')
    sha='b'*40
    seen=[]
    def handler(request):
        seen.append(str(request.url))
        if request.url.host=='api.github.com': return httpx.Response(403,json={'message':'limited'})
        if request.url.host=='github.com': return httpx.Response(200,content=f'0050{sha} HEAD\x00symref=HEAD:refs/heads/main\n'.encode())
        assert str(request.url).endswith('/'+sha)
        return httpx.Response(200,content=data.getvalue())
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await read_repository('https://github.com/example/demo','',lambda *a:None,lambda:None,client)
    result=asyncio.run(run())
    assert result['commit']==sha
    assert [f['path'] for f in result['files']]==['main.py']
    assert result['transport']=='archive'


def test_cancel_before_runner_starts(tmp_path):
    async def run():
        store=Store(tmp_path)
        runner=Runner(store,reader,FakeLLM,renderer)
        task=store.create({'repository':SNAPSHOT['repository'],'goal':'test'})
        runner.start(task['id'])
        runner.stop(task['id'])
        await asyncio.sleep(.01)
        assert store.get(task['id'])['status']=='cancelled'
        assert task['id'] not in runner.jobs
    asyncio.run(run())


def test_render_failure_not_success_and_one_retry(tmp_path):
    calls=[]
    async def broken(source,workspace):
        calls.append(source)
        raise ValueError('Mermaid 渲染失败：fixture')
    async def run():
        store=Store(tmp_path)
        task=store.create({'repository':SNAPSHOT['repository'],'goal':'test'})
        await Runner(store,reader,FakeLLM,broken).run(task['id'])
        assert store.get(task['id'])['status']=='failed'
        assert not store.get(task['id'])['result']['rendered']
        assert len(calls)==2
    asyncio.run(run())


def test_structured_output_repair_is_bounded_and_accounted(monkeypatch):
    import httpx
    from backend.llm import DeepSeek
    monkeypatch.setenv('DEEPSEEK_API_KEY','fixture-key')
    count=[]
    usage=[]
    original=httpx.AsyncClient
    def handler(request):
        count.append(json.loads(request.content))
        content={'summary':'invalid'} if len(count)==1 else ANALYSIS
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(content)},'finish_reason':'stop'}],'usage':{'prompt_tokens':20,'completion_tokens':10}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    async def run():
        result=await DeepSeek(usage.append,lambda:None).ask('Code Agent','test',{},Analysis)
        assert result.summary==ANALYSIS['summary']
    asyncio.run(run())
    assert len(count)==2
    assert usage[-1]=={'input':40,'output':20,'calls':2}
    assert len(count[1]['messages'])==4
    assert 'Field required' in count[1]['messages'][-1]['content']
    assert 'fixture-key' not in count[1]['messages'][-1]['content']


def test_excerpt_context_preserves_original_line_numbers():
    from backend.llm import source_context
    snapshot={'repository':'repo','commit':'a'*40,'files':[{'path':'a.py','content':'\n'.join(str(n) for n in range(1,101))}]}
    context=source_context(snapshot,{'path':'a.py','start_line':50,'end_line':52})
    code=context['files'][0]['numbered_code']
    assert '50: 50' in code and '46: 46' in code
    assert '1: 1\n' not in code
    assert context['files'][0]['excerpt_only']


def test_transient_protocol_disconnect_retries(monkeypatch):
    import httpx
    from backend.llm import DeepSeek
    monkeypatch.setenv('DEEPSEEK_API_KEY','fixture-key')
    original=httpx.AsyncClient
    requests=[]
    def handler(request):
        requests.append(request)
        if len(requests)==1: raise httpx.RemoteProtocolError('peer disconnected',request=request)
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(ANALYSIS)},'finish_reason':'stop'}],'usage':{}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    async def run():
        result=await DeepSeek(lambda _:None,lambda:None).ask('Code Agent','test',{},Analysis)
        assert result.summary==ANALYSIS['summary']
    asyncio.run(run())
    assert len(requests)==2
