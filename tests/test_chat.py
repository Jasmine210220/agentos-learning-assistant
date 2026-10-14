import asyncio
from backend.chat import Chats, Reply, explicit_search_request
from backend.store import Store
from backend.workflow import Runner
from test_backend import FakeLLM, reader, renderer, SNAPSHOT, E


def test_chat_analysis_followup_and_reload(tmp_path):
    store=Store(tmp_path)
    runner=Runner(store,reader,FakeLLM,renderer)
    seen=[]
    class Model:
        def __init__(self,*args): pass
        async def ask(self,role,instruction,data,schema):
            seen.append(data)
            if len(seen)==1: return Reply(answer='请提供仓库链接。',action='reply')
            if len(seen)==2: return Reply(answer='正在分析。',action='analyze',repository=SNAPSHOT['repository'],goal='分析代码')
            return Reply(answer='hello 返回字符串。',action='reply',evidence=[E])
    async def create(data):
        task=store.create(data.model_dump());runner.start(task['id']);return task
    async def run():
        chats=Chats(store,runner,create,Model)
        cid=chats.create()['id']
        for content in ['分析一下入口',SNAPSHOT['repository'],'hello 返回什么？']:
            chats.send(cid,content)
            await chats.jobs[cid]
            await asyncio.sleep(0)
        messages=chats.get(cid)['messages']
        assert len(messages)==6
        assert messages[-1]['evidence']==[E]
        assert messages[-1]['task_id']==messages[3]['task_id']
        assert store.get(messages[3]['task_id'])['status']=='succeeded'
        assert seen[1]['messages'][0]['content']=='分析一下入口'
        assert 'source' in seen[-1]
        restarted=Chats(store,runner,create,Model)
        assert len(restarted.list())==1
        assert restarted.get(cid)['messages']==messages
    asyncio.run(run())


def test_chat_stop_and_invalid_evidence(tmp_path):
    store=Store(tmp_path)
    runner=Runner(store,reader,FakeLLM,renderer)
    class Model:
        def __init__(self,*args): pass
        async def ask(self,*args): return Reply(answer='wrong',action='reply',evidence=[E])
    async def create(data): raise AssertionError('No task expected')
    async def run():
        chats=Chats(store,runner,create,Model)
        cid=chats.create()['id']
        chats.send(cid,'hello');chats.stop(cid)
        await asyncio.gather(chats.jobs[cid],return_exceptions=True)
        await asyncio.sleep(0)
        assert chats.get(cid)['messages'][-1]['status']=='interrupted'
        chats.send(cid,'hello');await chats.jobs[cid]
        assert chats.get(cid)['messages'][-1]['status']=='failed'
    asyncio.run(run())


def test_chat_api_validation_and_history(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app import create_app
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    app=create_app(tmp_path,'chat-test')
    with TestClient(app) as client:
        assert client.get('/chats').status_code==401
        client.headers['Authorization']='Bearer chat-test'
        cid=client.post('/chats').json()['id']
        assert client.get('/chats/'+cid).json()['messages']==[]
        assert client.post('/chats/'+cid+'/messages',json={'content':'hello'}).status_code==409
        assert client.post('/chats/'+cid+'/messages',json={'content':'  '}).status_code==422
        assert client.get('/chats/missing').status_code==404
        assert client.get('/health').json()['active_chats']==0
    with TestClient(create_app(tmp_path,'chat-test')) as client:
        client.headers['Authorization']='Bearer chat-test'
        assert client.get('/chats').json()[0]['id']==cid


def test_explicit_debug_request_uses_reviewed_task(tmp_path):
    store=Store(tmp_path);runner=Runner(store,reader,FakeLLM,renderer)
    class Model:
        def __init__(self,*args): pass
        async def ask(self,*args): return Reply(answer='direct answer',action='reply',evidence=[E])
    async def create(data):
        assert data.kind=='debug' and 'return 1/0' in data.input_text
        task=store.create(data.model_dump());store.update(task['id'],status='succeeded',result={'bugs':{'summary':'静态分析完成'}})
        return store.get(task['id'])
    async def run():
        chats=Chats(store,runner,create,Model);cid=chats.create()['id']
        chats.send(cid,'排查代码错误，不要运行。\n```python\nreturn 1/0\n```')
        await chats.jobs[cid]
        final=chats.get(cid)['messages'][-1]
        assert final['task_id'] and final['status']=='done'
    asyncio.run(run())


def test_explicit_search_intent_boundaries():
    for text in ['帮我搜一下优化代码的项目，似乎是一个马尾辫的logo，具体是做精简ai写的代码的一个skill应该是','帮我找找优化ai写的代码的skill','请搜索 Python RAG 开源项目','找一个代码简化工具']:
        assert explicit_search_request(text),text
    for text in ['不要搜索，先解释这段代码','帮我找代码中的 bug','搜索功能怎么实现？','搜索结果为什么为空？','这句话是“帮我搜索项目”','```python\n# 帮我搜索 GitHub 项目\n```','帮我搜，但先不要搜索','请解释 GitHub 搜索语法','帮我搜索明天的天气']:
        assert not explicit_search_request(text),text


def test_search_executes_without_confirmation_and_preserves_failure(tmp_path):
    store=Store(tmp_path);runner=Runner(store,reader,FakeLLM,renderer);requests=[]
    class Model:
        def __init__(self,*args): pass
        async def ask(self,*args): raise AssertionError('Explicit search must not depend on conversational model permission')
    async def create(data):
        requests.append(data)
        task=store.create(data.model_dump())
        store.update(task['id'],status='failed',error='GitHub 本次请求超时')
        return store.get(task['id'])
    async def run():
        chats=Chats(store,runner,create,Model);cid=chats.create()['id']
        text='帮我搜一下优化代码的项目，似乎是一个马尾辫的logo，具体是做精简ai写的代码的一个skill应该是'
        chats.send(cid,text);await chats.jobs[cid]
        assert len(requests)==1 and requests[0].kind=='search' and requests[0].goal==text
        result=chats.get(cid)['messages'][-1]
        assert result['task_id'] and result['content']=='GitHub 本次请求超时'
        assert '确认' not in result['content']
    asyncio.run(run())
