import asyncio
import io
import json
import zipfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.store import Store
from backend.workspaces import save_attachment,get_attachment,snapshot_from_files
from backend.improvement import (prepare_edits,validate_proposal,apply_recommendation,improve,
                                 ProjectProfile,Improvement,PatchReview,restore_original,normalize_proposal)
from backend.models import Review


CONTENT='def total(items):\n    return sum(item["price"] for item in items)\n\ndef label():\n    return "old"\n'


def proposal():
    return {'summary':'增加缺省值并规划接口','limitations':['只读取上传文件，尚未运行测试'], 'recommendations':[
        {'title':'处理缺少价格的条目','kind':'simple','reason':'直接索引可能引发 KeyError',
         'steps':['缺少 price 时按 0 计算'],'acceptance':['空列表返回 0','缺少价格按 0 计算'],
         'evidence':[{'path':'app.py','start_line':1,'end_line':2}],'references':[],
         'edits':[{'path':'app.py','before':'item["price"]','after':'item.get("price", 0)'}]},
        {'title':'实现持久化订单接口','kind':'complex','reason':'当前仅有本地计算函数',
         'steps':['明确订单模型','实现存储与接口'],'acceptance':['接口及存储有集成测试'],
         'evidence':[{'path':'app.py','start_line':1,'end_line':2}],'references':[],'edits':[]}]}


def ready_task(tmp_path):
    store=Store(tmp_path);attachment=save_attachment(store,[{'path':'app.py','content':CONTENT}])
    task=store.create({'kind':'improve','repository':'','goal':'改进这个项目','attachment_id':attachment['id']})
    p=proposal();p.update({'ready':True,'base_attachment_id':attachment['id'],'base_hash':snapshot_from_files(attachment['files'])['commit']})
    for i,item in enumerate(p['recommendations']): item['id']=str(i+1)
    store.update(task['id'],status='succeeded',result={'improvement':p,'review':{'passed':True,'issues':[]}})
    return store,task['id'],attachment


def test_exact_edits_syntax_and_size_limits():
    files=[{'path':'app.py','content':CONTENT}]
    updated,diff,checks=prepare_edits(files,proposal()['recommendations'][0]['edits'])
    assert 'item.get' in updated[0]['content'] and '-    return sum' in diff and '未运行' in checks[0]
    for edits in [[{'path':'missing.py','before':'x','after':'y'}],
                  [{'path':'app.py','before':'return','after':'yield'}],
                  [{'path':'app.py','before':CONTENT,'after':'def broken('}],
                  [{'path':'app.py','before':CONTENT,'after':'x=1\n'*121}]]:
        with pytest.raises(ValueError): prepare_edits(files,edits)


def test_apply_is_immutable_idempotent_and_conflict_safe(tmp_path):
    store,ident,base=ready_task(tmp_path)
    updated=apply_recommendation(store,ident,'1',base['id'])
    assert updated['id']!=base['id'] and get_attachment(store,base['id'])['files'][0]['content']==CONTENT
    assert apply_recommendation(store,ident,'1',updated['id'])['id']==updated['id']
    assert apply_recommendation(store,ident,'1',base['id'])['id']==updated['id']
    with pytest.raises(ValueError,match='开发工具'): apply_recommendation(store,ident,'2',updated['id'])
    store.update(ident,status='needs_attention')
    with pytest.raises(ValueError,match='审核'): apply_recommendation(store,ident,'1',updated['id'])


def test_multiple_edits_preserve_previous_copy_changes(tmp_path):
    store,ident,base=ready_task(tmp_path);task=store.get(ident)
    second={**task['result']['improvement']['recommendations'][0],'id':'3','edits':[{'path':'app.py','before':'"old"','after':'"new"'}]}
    task['result']['improvement']['recommendations'].append(second);store.update(ident,result=task['result'])
    first=apply_recommendation(store,ident,'1',base['id']);second=apply_recommendation(store,ident,'3',first['id'])
    assert 'item.get' in second['files'][0]['content'] and '"new"' in second['files'][0]['content']
    assert apply_recommendation(store,ident,'1',base['id'])['id']==second['id']
    with pytest.raises(ValueError,match='版本'): restore_original(store,ident,base['id'])
    restored=restore_original(store,ident,second['id'])
    assert restored['files'][0]['content']==CONTENT
    assert not store.get(ident)['result']['improvement']['recommendations'][0].get('applied_attachment_id')
    assert 'item.get' in get_attachment(store,second['id'])['files'][0]['content']


def test_review_notes_and_plan_without_patch_are_not_applyable():
    from pydantic import ValidationError
    with pytest.raises(ValidationError): PatchReview(decision='reject',summary='实际没有错误',blocking_errors=[],notes=['未运行'])
    p=proposal();p['recommendations'][0]['edits']=[]
    assert normalize_proposal(Improvement.model_validate(p)).recommendations[0].kind=='complex'


def test_unknown_reference_and_local_evidence_rejected(tmp_path):
    store,_,base=ready_task(tmp_path);snap=snapshot_from_files(base['files']);p=proposal()
    p['recommendations'][0]['references']=[{'repository':'https://github.com/fake/repo','evidence_id':1}]
    p['recommendations'][0]['evidence'][0]['end_line']=100
    assert len(validate_proposal(p,snap,{}))>=2


def test_frontend_files_and_export_paths(tmp_path):
    store=Store(tmp_path)
    data=save_attachment(store,[{'path':'src/App.vue','content':'<template><div>Hello</div></template>'},{'path':'src/style.css','content':'body { color: black; }'}])
    assert len(data['files'])==2
    for files in [[{'path':'../evil.py','content':'x=1'}],[{'path':'src//app.py','content':'x=1'}],
                  [{'path':'app.py','content':'x=1'},{'path':'App.py','content':'x=2'}]]:
        with pytest.raises(ValueError): save_attachment(store,files)


class Model:
    async def ask(self,role,instruction,data,schema):
        if schema is ProjectProfile: return ProjectProfile(summary='订单价格计算函数',search_goal='寻找订单计算与订单 API 公开项目',stack=['Python'])
        if schema is Improvement: return Improvement.model_validate(proposal())
        if schema is PatchReview: return PatchReview(decision='approve',summary='代码依据一致，未运行',blocking_errors=[],notes=['尚未运行'])
        return Review(passed=True,summary='代码依据一致，未运行',issues=[])


def test_improvement_flow_and_handoff_persist(tmp_path):
    store,ident,base=ready_task(tmp_path);task=store.get(ident);result={}
    async def search(task,llm,event,phase,save,result,check,**kw):
        assert kw['max_rounds']==2
        result['search']={'collected_repositories':[],'candidates':[]}
        result['review']={'passed':True,'summary':'未找到参考项目','issues':[]}
    asyncio.run(improve(task,Model(),lambda *a:None,lambda *a:None,lambda:None,result,lambda:None,store,discovery=search))
    assert result['improvement']['ready'] and result['search_review']['passed']
    complex_item=result['improvement']['recommendations'][1]
    assert 'app.py:1-2' in complex_item['prompt'] and '集成测试' in complex_item['prompt']
    assert (store.workspace(ident)/'handoff.md').exists() and (store.workspace(ident)/'snapshot.json').exists()


def test_rejected_edit_is_repaired_then_reviewed_again(tmp_path):
    store,ident,base=ready_task(tmp_path);result={}
    class RepairModel(Model):
        def __init__(self):self.reviews=0
        async def ask(self,role,instruction,data,schema):
            if schema is PatchReview:
                self.reviews+=1
                if self.reviews==1:return PatchReview(decision='reject',summary='行为被扩大',blocking_errors=[{'problem':'不能额外处理 None','required_change':'仅处理字段缺失'}],notes=[])
            return await super().ask(role,instruction,data,schema)
    async def search(*args,**kwargs):pass
    model=RepairModel()
    asyncio.run(improve(store.get(ident),model,lambda *a:None,lambda *a:None,lambda:None,result,lambda:None,store,discovery=search))
    assert model.reviews==2 and result['improvement']['ready']


def test_api_apply_and_export_require_auth(tmp_path,monkeypatch):
    store,ident,base=ready_task(tmp_path);store.db.close()
    with TestClient(create_app(tmp_path,'token')) as client:
        assert client.post('/tasks/'+ident+'/apply',json={'recommendation_id':'1','base_attachment_id':base['id']}).status_code==401
        assert client.get('/attachments/'+base['id']+'/download').status_code==401
        client.headers['Authorization']='Bearer token'
        response=client.post('/tasks/'+ident+'/apply',json={'recommendation_id':'1','base_attachment_id':base['id']});assert response.status_code==200
        updated=response.json();download=client.get('/attachments/'+updated['id']+'/download');assert download.status_code==200
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert archive.namelist()==['app.py'] and 'item.get' in archive.read('app.py').decode()
        assert client.get('/attachments/'+base['id']).json()['files'][0]['content']==CONTENT


def test_attachment_search_routes_to_improvement_and_keeps_context(tmp_path):
    from backend.chat import Chats
    from backend.workflow import Runner
    store=Store(tmp_path);runner=Runner(store);a=save_attachment(store,[{'path':'app.py','content':CONTENT}]);created=[]
    async def create(data):
        created.append(data);task=store.create(data.model_dump())
        store.update(task['id'],status='succeeded',result={'improvement':{'summary':'已提出建议','base_attachment_id':a['id'],'recommendations':[]}})
        return store.get(task['id'])
    async def run():
        chats=Chats(store,runner,create);cid=chats.create()['id']
        chats.send(cid,'帮我找类似项目并提出修改建议',a['id']);await chats.jobs[cid];await asyncio.sleep(0)
        chats.send(cid,'继续修改这个项目');await chats.jobs[cid]
        assert all(d.kind=='improve' and d.attachment_id==a['id'] for d in created) and len(created)==2
    asyncio.run(run())


def test_discussion_does_not_force_another_edit_task():
    from backend.chat import explicit_improvement_request
    assert explicit_improvement_request('继续修改这个项目')
    assert explicit_improvement_request('请帮我优化这段代码')
    for text in ['这个修改方案为什么这样设计？','请解释修改的理由','不要修改，只解释代码','你觉得优化方案合理吗？','```python\n# 修改函数\n```']:
        assert not explicit_improvement_request(text)
