import asyncio
from copy import deepcopy
import pytest
from backend.audit import digest
from backend.chat import Chats
from backend.learning import learning_errors
from backend.learning_exercises import exercise_from_focus
from backend.learning_review import LearningAudit, FindingDecisions
from backend.models import LearningAnalysis, LearningPlan, Diagram, Review, TaskInput, ScopeAssessment
from backend.store import Store
from backend.workflow import Runner
from test_backend import SNAPSHOT, ANALYSIS, DIAGRAM, E, CLAIM, reader, renderer


LEARNING = dict(background='会Python基础', objective='理解hello调用', flow_indices=[1],
    reading_order=[dict(title='入口', reason='先看函数再看调用', module_index=1, concepts=['返回值'], evidence=[E])],
    exercises=[exercise_from_focus({'symbol':'hello','evidence_ids':[1]}, i, [E], SNAPSHOT) for i in range(3)],
    limitations=['练习未执行'])
LEARNING_ANALYSIS = dict(ANALYSIS, goal_answers=[CLAIM], scope_complete=True, scope_reason='入口和函数均已读取', technologies=[dict(CLAIM, title='Python')])
LEARNING_ANALYSIS['summary'] = CLAIM['explanation']


class Model:
    reject = False
    incomplete = False
    def __init__(self, usage, check):
        self.check = check
    async def ask(self, role, instruction, data, schema):
        self.check()
        if schema is LearningAnalysis:
            return schema.model_validate(dict(LEARNING_ANALYSIS, scope_complete=not self.incomplete))
        if schema is ScopeAssessment:
            return schema(scope_complete=not self.incomplete, reason='关键步骤源码不足', evidence=[E])
        if schema.__name__ == 'LearningDraft':
            assert data['analysis']['scope_complete']
            value = deepcopy(LEARNING)
            for item in value['reading_order']:
                item.pop('evidence')
                item['evidence_ids'] = [1]
            focus = next(f['id'] for f in data['focus_choices'] if f['symbol']=='hello')
            value['exercises'] = [dict(focus_id=focus) for _ in range(3)]
            return schema.model_validate(value)
        if schema is Diagram:
            assert data['learning']['flow_indices'] == [1]
            return schema.model_validate(DIAGRAM)
        if schema.__name__ == 'LearningAudit':
            return schema(summary='核对结果', findings=[dict(target='learning.exercises.0', quote='区分已读代码中的定义、传入值和消费位置。',
                problem='练习需修正', evidence=[E], source_quote="return 'hello'")] if self.reject and 'learning.exercises.0' in data['units'] else [])
        if schema is FindingDecisions:
            return schema(decisions=[dict(finding_id=1, verdict='confirmed', reason='核对依据', evidence=[E], source_quote="return 'hello'")])
        if schema.__name__ == 'LearningCorrections':
            value = dict(symbol='hello', evidence_ids=[1])
            return schema.model_validate({'item0': value})
        raise AssertionError(schema)


def execute(tmp_path, model=Model, render=renderer):
    store = Store(tmp_path)
    task = store.create(TaskInput(kind='learn', repository=SNAPSHOT['repository'], learner_background='会Python').model_dump())
    asyncio.run(Runner(store, reader, model, render).run(task['id']))
    return store, store.get(task['id'])


def test_learning_handoffs_exports_and_reload(tmp_path):
    store, task = execute(tmp_path)
    assert task['status'] == 'succeeded'
    events = store.events(task['id'])
    calls = [e for e in events if e['kind'] == 'model_input']
    assert {e['role'] for e in calls} == {'Code Agent', 'Learning Agent', 'Chart Agent', 'Reviewer Agent'}
    teaching = next(e for e in calls if e['role'] == 'Learning Agent')
    drawing = next(e for e in calls if e['role'] == 'Chart Agent')
    assert teaching['details']['input_hashes']['analysis'] == digest(task['result']['analysis'])
    assert drawing['details']['input_hashes']['learning'] == digest(task['result']['learning'])
    report = (store.workspace(task['id']) / 'report.md').read_text(encoding='utf-8')
    assert '任务状态：succeeded' in report and '三个练习（未执行）' in report and 'main.py:1-2' in report
    store.db.close()
    reopened = Store(tmp_path)
    reopened.recover()
    assert reopened.get(task['id']) == task


def test_scope_gap_stops_before_learning(tmp_path):
    class Incomplete(Model): incomplete = True
    store, task = execute(tmp_path, Incomplete)
    assert task['status'] == 'needs_attention'
    assert 'learning' not in task['result']
    assert not any(e['role'] == 'Learning Agent' for e in store.events(task['id']))


def test_learning_rejects_unread_or_unconsumed_evidence():
    value = deepcopy(LEARNING)
    value['flow_indices'] = [99]
    value['reading_order'][0]['module_index'] = 99
    value['exercises'][0]['evidence'] = [dict(E, start_line=4, end_line=4)]
    assert len(learning_errors(value, LEARNING_ANALYSIS, SNAPSHOT)) == 3


def test_review_repair_is_bounded_and_preserves_unapproved_report(tmp_path):
    class Reject(Model): reject = True
    store, task = execute(tmp_path, Reject)
    assert task['status'] == 'needs_attention'
    assert sum(e['kind'] == 'model_input' and e['details']['schema'] == 'LearningAudit' for e in store.events(task['id'])) == 4
    assert task['result']['checks']['round'] == 2
    assert '未完成或未通过' in (store.workspace(task['id']) / 'report.md').read_text(encoding='utf-8')


def test_missing_svg_never_succeeds(tmp_path):
    async def broken(*args): return '<svg/>'
    store, task = execute(tmp_path, render=broken)
    assert task['status'] == 'failed'
    assert not task['result']['rendered']


def test_repair_budget_preserves_last_reviewed_artifacts(tmp_path):
    from backend.llm import ModelBudgetExceeded
    class Budget(Model):
        reject = True
        async def ask(self, role, instruction, data, schema):
            if schema.__name__ == 'LearningCorrections':
                raise ModelBudgetExceeded('budget')
            return await super().ask(role, instruction, data, schema)
    store, task = execute(tmp_path, Budget)
    assert task['status'] == 'needs_attention'
    assert len(task['result']['learning']['exercises']) == 3
    assert task['result']['rendered']
    assert not task['result']['review']['passed']
    assert task['result']['review']['issues']
    report = (store.workspace(task['id']) / 'report.md').read_text(encoding='utf-8')
    assert '未完成或未通过' in report and '追踪参数与数据来源' in report


def test_learning_cancel_during_teaching_and_recovery(tmp_path):
    async def run():
        entered = asyncio.Event()
        class Slow(Model):
            async def ask(self, role, instruction, data, schema):
                if schema.__name__ == 'LearningDraft':
                    entered.set()
                    await asyncio.sleep(10)
                return await super().ask(role, instruction, data, schema)
        store = Store(tmp_path)
        task = store.create(TaskInput(kind='learn', repository=SNAPSHOT['repository']).model_dump())
        runner = Runner(store, reader, Slow, renderer)
        runner.start(task['id'])
        job = runner.jobs[task['id']]
        await entered.wait()
        runner.stop(task['id'])
        await job
        assert store.get(task['id'])['status'] == 'cancelled'
        assert not any(e['role'] == 'Chart Agent' for e in store.events(task['id']))
        store.update(task['id'], status='teaching')
        store.recover()
        assert store.get(task['id'])['status'] == 'interrupted'
    asyncio.run(run())


def test_learning_chat_does_not_route_to_search_or_use_old_attachment(tmp_path):
    async def run():
        store = Store(tmp_path)
        runner = Runner(store, reader, Model, renderer)
        inputs = []
        async def create(data):
            inputs.append(data)
            task = store.create(data.model_dump())
            runner.start(task['id'])
            return task
        chats = Chats(store, runner, create)
        cid = chats.create()['id']
        chats.send(cid, '帮我搜索学习项目', mode='learn')
        await chats.jobs[cid]
        await asyncio.sleep(0)
        assert not inputs
        assert '链接' in chats.get(cid)['messages'][-1]['content']
        chats.send(cid, SNAPSHOT['repository'] + ' 理解入口', mode='learn', learner_background='初学Python', ref='abc')
        await chats.jobs[cid]
        assert inputs[0].kind == 'learn' and inputs[0].learner_background == '初学Python'
        assert inputs[0].ref == 'abc' and not inputs[0].attachment_id
        assert '三个练习' in chats.get(cid)['messages'][-1]['content'] or '学习成果已保存' in chats.get(cid)['messages'][-1]['content']
    asyncio.run(run())


def test_exercises_are_exactly_three():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        LearningPlan.model_validate(dict(LEARNING, exercises=LEARNING['exercises'][:2]))


def test_link_only_waits_for_goal_and_survives_restart(tmp_path):
    async def run():
        store = Store(tmp_path)
        runner = Runner(store, reader, Model, renderer)
        inputs = []
        async def create(data):
            inputs.append(data)
            task = store.create(data.model_dump())
            runner.start(task['id'])
            return task
        chats = Chats(store, runner, create)
        cid = chats.create()['id']
        chats.send(cid, SNAPSHOT['repository'], mode='learn', learner_background='初学Python', ref='abc')
        await chats.jobs[cid]
        await asyncio.sleep(0)
        assert not inputs and not store.list()
        assert '链接已记住' in chats.get(cid)['messages'][-1]['content']
        chats = Chats(store, runner, create)
        chats.send(cid, '好的', mode='learn')
        await chats.jobs[cid]
        await asyncio.sleep(0)
        assert not inputs
        chats.send(cid, '理解入口', mode='learn')
        await chats.jobs[cid]
        assert len(inputs) == 1
        assert inputs[0].repository == SNAPSHOT['repository']
        assert inputs[0].goal == '理解入口'
        assert inputs[0].ref == 'abc'
        assert inputs[0].learner_background == '初学Python'
    asyncio.run(run())
