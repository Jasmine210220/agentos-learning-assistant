import asyncio
import json
from copy import deepcopy
import pytest
from backend.learning_review import audit_learning, units
from backend.learning_diagram import reading_record, requests_execution_diagram
from backend.llm import ModelBudgetExceeded
from backend.models import TaskInput
from backend.store import Store
from backend.workflow import Runner
from test_learning import Model, execute
from test_learning_review import result, FINDING
from test_backend import SNAPSHOT, reader, renderer


def test_review_includes_user_facing_scope_objective_and_limits():
    data = result()
    catalog = units(data)
    assert {'analysis.scope_reason', 'analysis.limitations.0', 'learning.objective',
            'learning.background', 'learning.limitations.0'} <= catalog.keys()
    assert 'main.py：1-4' in reading_record(SNAPSHOT)
    snapshot = deepcopy(SNAPSHOT)
    snapshot['excerpted'] = True
    snapshot['files'][0]['analysis_ranges'] = [[2,2]]
    assert 'main.py：2-2' in reading_record(snapshot)
    assert '1-4' not in reading_record(snapshot)


def test_disagreement_preserves_first_evidence_without_extra_calls(tmp_path):
    class Dispute(Model):
        audits = 0
        async def ask(self, role, instruction, data, schema):
            if schema.__name__ == 'LearningAudit':
                self.audits += 1
                assert self.audits <= 2
                return schema(summary='未重现' if data.get('independent_check') else '疑点',
                              findings=[] if data.get('independent_check') else [FINDING])
            assert schema.__name__ != 'LearningCorrections'
            return await super().ask(role,instruction,data,schema)
    store, task = execute(tmp_path, Dispute)
    assert task['status'] == 'needs_attention'
    review=task['result']['review']
    assert review['outcome']=='disputed' and not review['passed']
    assert review['unresolved_findings']==[FINDING]
    assert review['decisions'][0]['verdict']=='uncertain'
    assert task['result']['rendered'] and (store.workspace(task['id'])/'report.md').exists()


@pytest.mark.parametrize('failure,outcome',[('invalid','invalid_audit'),('budget','budget_exhausted')])
def test_incomplete_audit_never_leaves_success(tmp_path,failure,outcome):
    class Broken(Model):
        async def ask(self,role,instruction,data,schema):
            if schema.__name__=='LearningAudit':
                if failure=='budget': raise ModelBudgetExceeded('test budget')
                return schema(summary='invalid',findings=[{'target':'not-a-unit'}])
            return await super().ask(role,instruction,data,schema)
    _,task=execute(tmp_path,Broken)
    assert task['status']=='needs_attention'
    assert task['result']['review']['outcome']==outcome
    assert task['result']['review']['passed'] is False
    assert len(task['result']['learning']['exercises'])==3


def test_first_clean_audit_is_only_no_issues_found(tmp_path):
    _,task=execute(tmp_path)
    assert task['result']['review']['outcome']=='no_issues_found'
    assert '不是人工验收' in task['result']['review']['summary']


@pytest.mark.parametrize('goal',['画执行时序图','解释调用顺序','draw a sequence diagram'])
def test_unsupported_sequence_stops_before_model_call(tmp_path,goal):
    assert requests_execution_diagram(goal)
    class NoCalls(Model):
        async def ask(self,*args): raise AssertionError('unsupported request must not call model')
    store=Store(tmp_path)
    task=store.create(TaskInput(kind='learn',repository=SNAPSHOT['repository'],goal=goal).model_dump())
    asyncio.run(Runner(store,reader,NoCalls,renderer).run(task['id']))
    final=store.get(task['id'])
    assert final['status']=='needs_attention'
    assert '不提供调用顺序图或执行时序图' in final['error']
    assert 'snapshot' not in final['result']


@pytest.mark.parametrize('valid',[True,False])
def test_dispute_closure_requires_bound_source_and_reason(valid):
    class Closure:
        calls=0
        async def ask(self,role,instruction,data,schema):
            self.calls+=1
            if not data.get('independent_check'):
                return schema(summary='初审疑点',findings=[FINDING])
            assert data['original_findings'][0]['finding_id']==1
            return schema(summary='原疑点未重现',findings=[],resolutions=[dict(finding_id=1,verdict='dismissed',
                reason='引用范围内的 return 支持原陈述，初审混淆了练习提问与事实断言。',
                evidence=FINDING['evidence'],source_quote="return 'hello'" if valid else 'return imaginary')])
    model=Closure()
    review=asyncio.run(audit_learning(model,result(),SNAPSHOT,'理解入口'))
    assert model.calls==2
    assert review['passed'] is valid
    assert review['outcome']==('not_reproduced_with_evidence' if valid else 'invalid_audit')
    if valid:
        assert review['decisions'][0]['reason'] and review['decisions'][0]['evidence']


def test_report_preserves_data_path_title_and_program_reading_record():
    from backend.learning import report_learning
    value=result()
    value['snapshot']=SNAPSHOT
    value['diagram']['title']='数据传递图（仅条件 A 成立）'
    value['diagram_semantics']='data_flow'
    report=report_learning(dict(repository=SNAPSHOT['repository'],goal='read',status='succeeded'),value)
    assert '数据传递图（仅条件 A 成立）' in report
    assert 'main.py：1-4' in report
    assert '不代表人工验收' in report
