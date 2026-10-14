import asyncio
from backend.search_tools import excerpts, reference_evidence, SearchTools
from backend.chat import task_answer
from backend.improvement import improve, Improvement, PatchReview
from backend.search_workflow import Assessment, apply_assessment
from test_improvement import Model, ready_task


def test_late_relevant_passage_is_contiguous_and_keeps_original_lines():
    lines=['unrelated introduction '+str(i) for i in range(160)]
    lines[120:124]=['async def search():','    # retry a transient error','    timeout = 30','    return await query(timeout)']
    evidence=excerpts('\n'.join(lines),'client.py',limit=3,goal='检索可靠性和错误处理')
    assert evidence[0]['line']==121
    assert evidence[0]['end_line']==124
    assert evidence[0]['text']=='\n'.join(lines[120:124])


def test_reference_budget_does_not_hide_skill_or_implementation():
    c={'evidence':[{'id':i,'path':'README.md','text':'intro'} for i in range(1,13)]+
       [{'id':13,'path':'SKILL.md','text':'behavior'},{'id':14,'path':'src/client.py','text':'implementation'}]}
    assert [e['id'] for e in reference_evidence(c,4)]==[1,13,14,2]


def test_improvement_excludes_unmatched_and_audits_full_selected_source(tmp_path,monkeypatch):
    store,ident,base=ready_task(tmp_path);result={};seen=[]
    good={'name':'owner/good','repository':'https://github.com/owner/good','verified':True,'matched':True,'confidence':'high','stars':10,
          'evidence':[{'id':1,'path':'README.md','line':1,'text':'real behavior','url':'https://github.com/owner/good'}]}
    bad={**good,'name':'owner/bad','repository':'https://github.com/owner/bad','matched':False,'stars':10000}
    async def discovery(*args,**kwargs):
        result['search']={'collected_repositories':[bad,good],'candidates':[bad,good]}
    async def implementation(self,candidate,goal):
        seen.append(candidate['name']);return candidate
    monkeypatch.setattr(SearchTools,'implementation',implementation)
    class CheckingModel(Model):
        async def ask(self,role,instruction,data,schema):
            if schema in (Improvement,PatchReview):
                assert [r['repository'] for r in data['references']]==[good['repository']]
            if schema is PatchReview:
                assert all(not f['excerpt_only'] for f in data['files'])
                assert data['patched_previews'][0]['checks']
                assert 'get(' in data['patched_previews'][0]['after']['files'][0]['numbered_code']
            return await super().ask(role,instruction,data,schema)
    asyncio.run(improve(store.get(ident),CheckingModel(),lambda *a:None,lambda *a:None,lambda:None,result,lambda:None,store,discovery))
    assert seen==['owner/good']


def test_chat_shows_actual_recommendation_and_unapproved_status():
    text=task_answer({'improvement':{'summary':'发现一个问题','ready':False,'recommendations':[
        {'title':'有限重试','reason':'429 会直接失败','kind':'complex','evidence':[{'path':'client.py'}]}]}})
    assert '429 会直接失败' in text and 'client.py' in text and '尚未通过审核' in text


def test_repository_alias_resolution_never_admits_invented_sources():
    import pytest
    url='https://github.com/Owner/Actual'
    pool={url.lower():{'repository':url,'name':'Owner/Actual','verified':True,'evidence':[{'id':1,'text':'source'}]}}
    a=Assessment(summary='找到',sufficient=True,reason='有依据',next_github_queries=[],next_web_queries=[],matches=[
        {'repository':'owner/actual','matched':True,'confidence':'high','reasons':['source'],'unknown':[],'evidence_ids':[1]}])
    result={};assert apply_assessment(result,pool,a)
    assert result['candidates'][0]['repository']==url
    a.matches[0].repository='https://github.com/Owner/invented'
    with pytest.raises(ValueError): apply_assessment({},pool,a)


def test_truncated_reasoning_review_retries_without_reasoning_and_counts_cost(monkeypatch):
    import json
    import httpx
    from backend.llm import DeepSeek
    from backend import settings
    monkeypatch.setattr(settings,'current',lambda:{'key':'fixture','model':'deepseek-flash'})
    requests=[];original=httpx.AsyncClient
    def handler(request):
        requests.append(json.loads(request.content))
        verdict={'decision':'approve','summary':'通过','blocking_errors':[],'notes':[]}
        return httpx.Response(200,json={'usage':{'prompt_tokens':10,'completion_tokens':20},'choices':[
            {'finish_reason':'length' if len(requests)==1 else 'stop','message':{'content':json.dumps(verdict)}}]})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    async def run():
        model=DeepSeek(lambda _:None,lambda:None)
        verdict=await model.ask('Reviewer Agent','审核',{},PatchReview)
        assert verdict.decision=='approve' and model.usage=={'input':20,'output':40,'calls':2}
    asyncio.run(run())
    assert requests[0]['thinking']['type']=='enabled' and requests[0]['reasoning_effort']=='low'
    assert requests[1]['thinking']['type']=='disabled' and 'reasoning_effort' not in requests[1]


def test_learning_reserves_reasoning_for_facts_and_disputes(monkeypatch):
    import json
    import httpx
    from backend.llm import DeepSeek
    from backend.learning_review import LearningAudit
    from backend import settings
    monkeypatch.setattr(settings, 'current', lambda: {'key': 'fixture', 'model': 'deepseek-flash'})
    requests = []
    original = httpx.AsyncClient
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'usage': {'prompt_tokens': 10, 'completion_tokens': 10},
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{"summary":"checked","findings":[]}'}}]})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    async def run():
        model = DeepSeek(lambda _: None, lambda: None)
        model.learning_mode = True
        for data in ({}, {'fact_check': True}, {'independent_check': True}, {'fact_check': True, 'repair_check': True}):
            await model.ask('Reviewer Agent', 'check', data, LearningAudit)
    asyncio.run(run())
    assert [r['thinking']['type'] for r in requests] == ['disabled', 'disabled', 'enabled', 'disabled']


def test_module_reference_search_reserves_budget_after_strong_evidence(tmp_path,monkeypatch):
    from test_discovery import Model as SearchModel, FakeTools
    from backend.search_workflow import discover
    from backend import settings
    settings.configure(tmp_path);monkeypatch.delenv('TAVILY_API_KEY',raising=False)
    class AlreadyUseful(SearchModel):
        async def ask(self,role,instruction,data,schema):
            if schema is Assessment:
                return Assessment(summary='已有可借鉴模块',sufficient=False,reason='还想找相同前端框架',
                    next_github_queries=['another framework'],next_web_queries=[],matches=[{
                        'repository':'https://github.com/example/noise','matched':True,'confidence':'high',
                        'reasons':['已有实现证据'],'unknown':['前端不同'],'evidence_ids':[1]}])
            return await super().ask(role,instruction,data,schema)
    result={}
    asyncio.run(discover({'goal':'借鉴检索模块','reference_search':True},AlreadyUseful(),lambda *a:None,lambda *a:None,
        lambda:None,result,lambda:None,tools_factory=FakeTools,max_rounds=2,review_result=False))
    assert len(result['search']['rounds'])==1
    assert result['search']['candidates'][0]['citations']


def test_review_can_correct_reason_without_touching_patch_and_is_atomic():
    import pytest
    from backend.improvement import correct_reasons, TextCorrection
    from test_improvement import proposal
    value=Improvement.model_validate(proposal());before=value.model_dump()
    old=value.recommendations[0].reason
    correction=TextCorrection(recommendation_index=0,before=old,after='仅修正与原文不符的理由。')
    with pytest.raises(ValueError):
        correct_reasons(value,[correction,TextCorrection(recommendation_index=99,before='x',after='y')])
    assert value.model_dump()==before
    correct_reasons(value,[correction])
    assert value.recommendations[0].reason=='仅修正与原文不符的理由。'
    assert value.recommendations[0].edits==Improvement.model_validate(before).recommendations[0].edits

def test_learning_extra_metadata_is_removed_without_repeating_source(monkeypatch):
    import json
    import httpx
    from backend.llm import DeepSeek
    from backend.learning_review import LearningAudit
    from backend import settings
    monkeypatch.setattr(settings, 'current', lambda: {'key': 'fixture', 'model': 'deepseek-flash'})
    requests = []; normalized = []
    original = httpx.AsyncClient
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'usage': {'prompt_tokens': 100, 'completion_tokens': 20},
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
                'summary': 'checked', 'findings': [], 'note': 'redundant'})}}]})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    async def run():
        model = DeepSeek(lambda _: None, lambda: None)
        model.learning_mode = True
        model.on_normalize = lambda role, fields: normalized.append(fields)
        result = await model.ask('Reviewer Agent', 'check', {}, LearningAudit)
        assert result.summary == 'checked' and result.findings == []
        assert model.usage == {'input': 100, 'output': 20, 'calls': 1}
    asyncio.run(run())
    assert len(requests) == 1 and normalized == [['note']]
