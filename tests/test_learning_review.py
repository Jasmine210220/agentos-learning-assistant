import asyncio
import json
from copy import deepcopy
import pytest
from backend.learning_review import audit_learning, LearningAudit, FindingDecisions
from backend.models import LearningAnalysis, Diagram
from test_learning import Model, LEARNING, LEARNING_ANALYSIS, execute
from test_backend import SNAPSHOT, E, DIAGRAM


def result():
    return deepcopy(dict(analysis=LEARNING_ANALYSIS, learning=LEARNING, diagram=DIAGRAM))


FINDING = dict(target='learning.exercises.0', quote='区分已读代码中的定义、传入值和消费位置。', problem='练习需修正',
               evidence=[E], source_quote="return 'hello'")


def test_source_quote_allows_indentation_but_not_rewording():
    from backend.learning_review import source_quote_valid
    assert source_quote_valid(dict(FINDING, source_quote="    return 'hello'  "), SNAPSHOT)
    assert not source_quote_valid(dict(FINDING, source_quote="return 'goodbye'"), SNAPSHOT)


@pytest.mark.parametrize('field,value', [('quote', '原文里没有这个句子'), ('source_quote', 'return False')])
def test_fabricated_review_cannot_reject_or_silently_pass(field, value):
    class Invalid:
        calls = 0
        async def ask(self, role, instruction, data, schema):
            self.calls += 1
            assert schema.__name__ == 'LearningAudit'
            return schema(summary='声称有问题', findings=[dict(FINDING, **{field: value})])
    model = Invalid()
    review = asyncio.run(audit_learning(model, result(), SNAPSHOT, '理解入口'))
    assert model.calls == 2
    assert review['validation_failed'] and not review['passed']
    assert not review['repair_targets']


@pytest.mark.parametrize('verdict,expected', [('uncertain', False), ('confirmed', False)])
def test_criticism_is_adjudicated_with_exact_source(verdict, expected):
    class Judge:
        async def ask(self, role, instruction, data, schema):
            if 'learning.exercises.0' not in data['units']:
                return schema(summary='无问题', findings=[])
            if not data.get('independent_check'):
                return schema(summary='待核对问题', findings=[FINDING])
            assert data['units']['learning.exercises.0'] == LEARNING['exercises'][0]
            assert 'candidates' not in data and 'problem' not in data
            return schema(summary='独立核对', findings=[FINDING] if verdict == 'confirmed' else [])
    review = asyncio.run(audit_learning(Judge(), result(), SNAPSHOT, '理解入口'))
    assert review['passed'] is expected
    assert review['audit']['findings']
    assert review['decisions'][0]['verdict'] == verdict


def test_selective_repair_does_not_regenerate_unaffected_artifacts(tmp_path):
    class Fix(Model):
        reject = True
        async def ask(self, role, instruction, data, schema):
            if schema.__name__ == 'LearningCorrections':
                assert role == 'Learning Agent'
                assert data['analysis'] == LearningAnalysis.model_validate(LEARNING_ANALYSIS).model_dump()
                assert data['output_map'] == {'item0': 'learning.exercises.0'}
                self.reject = False
                value = dict(symbol='hello', evidence_ids=[1])
                return schema(item0=value)
            return await super().ask(role, instruction, data, schema)
    store, task = execute(tmp_path, Fix)
    assert task['status'] == 'succeeded'
    assert task['result']['analysis'] == LearningAnalysis.model_validate(LEARNING_ANALYSIS).model_dump()
    assert task['result']['diagram'] == json.loads((store.workspace(task['id'])/'learning-attempt-1.json').read_text(encoding='utf-8'))['diagram']
    assert task['result']['learning']['exercises'][1:] == LEARNING['exercises'][1:]
    assert task['result']['checks']['round'] == 2
    assert task['result']['review']['outcome'] == 'corrected_and_rechecked'
    calls = [e for e in store.events(task['id']) if e['kind'] == 'model_input']
    assert sum(e['details']['schema'] == 'LearningAnalysis' for e in calls) == 1
    assert sum(e['details']['schema'] == 'Diagram' for e in calls) == 1
    assert (store.workspace(task['id']) / 'learning-attempt-1.json').exists()


def test_invalid_correction_does_not_replace_previous_result(tmp_path):
    class Invalid(Model):
        reject = True
        async def ask(self, role, instruction, data, schema):
            if schema.__name__ == 'LearningAudit':
                return schema(summary='解读需修正', findings=[dict(FINDING, target='analysis.modules.0', quote='函数返回 hello。')] if 'analysis.modules.0' in data['units'] else [])
            if schema.__name__ == 'LearningCorrections':
                return schema(item0=dict(LEARNING_ANALYSIS['modules'][0], evidence=[dict(E, path='unread.py')]))
            return await super().ask(role, instruction, data, schema)
    _, task = execute(tmp_path, Invalid)
    assert task['status'] == 'needs_attention'
    assert task['result']['learning'] == LEARNING
    assert not task['result']['review']['passed']


def test_reserve_stops_before_spending_final_audit_budget(monkeypatch):
    from backend.llm import DeepSeek, ModelBudgetExceeded
    from backend.models import Diagram
    model = DeepSeek(lambda _: None, lambda: None)
    model.config = {'key': 'test-only', 'model': 'test'}
    model.learning_mode = model.reserve_request_budget = True
    model.reserved_tokens = 22000
    model.reserved_calls = 2
    model.usage = dict(input=100000, output=0, calls=5)
    with pytest.raises(ModelBudgetExceeded):
        asyncio.run(model.ask('Chart Agent', 'test', {}, Diagram))
    assert model.usage['calls'] == 5


def test_failed_repair_render_keeps_previous_diagram(tmp_path):
    from test_backend import renderer
    class FixDiagram(Model):
        async def ask(self, role, instruction, data, schema):
            if schema.__name__ == 'LearningAudit':
                return schema(summary='图待修正', findings=[dict(FINDING, target='diagram', quote='返回文本')] if 'diagram' in data['units'] else [])
            if schema.__name__ == 'LearningCorrections':
                diagram = deepcopy(DIAGRAM)
                diagram['nodes'][1]['label'] = '修正后的返回'
                return schema(item0=diagram)
            return await super().ask(role, instruction, data, schema)
    async def render(source, workspace):
        if workspace.name.startswith('render-revision'):
            return 'no file produced'
        return await renderer(source, workspace)
    store, task = execute(tmp_path, FixDiagram, render)
    assert task['status'] == 'failed'
    assert task['result']['diagram'] == json.loads((store.workspace(task['id'])/'learning-attempt-1.json').read_text(encoding='utf-8'))['diagram']
    assert '修正后的返回' not in (store.workspace(task['id']) / 'diagram.mmd').read_text(encoding='utf-8')
    assert not task['result']['review']['passed']


def test_learning_context_keeps_signatures_outside_small_citations():
    from backend.llm import learning_source_context, source_context
    snapshot = deepcopy(SNAPSHOT)
    snapshot['files'][0]['content'] = 'def values(path=None, stream=None):\n' + ''.join(f'    # comment {i}\n' for i in range(20)) + '    return build(path, override=True)\n'
    snapshot['files'][0]['lines'] = 22
    refs = {'evidence': [dict(E, start_line=22, end_line=22)]}
    assert 'def values(' not in source_context(snapshot, refs)['files'][0]['numbered_code']
    assert '1: def values(path=None, stream=None):' in learning_source_context(snapshot, refs)['files'][0]['numbered_code']


def test_execution_notes_distinguish_nested_generator_and_context_manager():
    from backend.llm import learning_source_context
    snapshot = deepcopy(SNAPSHOT)
    snapshot['files'][0]['content'] = 'def outer():\n    def inner():\n        yield 1\n    return inner()\n@contextmanager\ndef opened():\n    yield stream\n'
    notes = learning_source_context(snapshot)['files'][0]['python_deferred_bodies']
    assert {n['name']: n['body_starts_when'] for n in notes} == {'inner': 'iteration', 'opened': 'context_enter'}


def test_exercise_focus_must_exist_in_selected_source():
    from backend.learning_exercises import exercise_from_focus
    with pytest.raises(ValueError, match='不在上游源码'):
        exercise_from_focus(dict(symbol='imaginary_parameter', evidence_ids=[1]), 0, [E], SNAPSHOT)


def test_focus_body_reference_resolves_to_existing_upstream_signature():
    from backend.learning_exercises import exercise_from_focus
    snapshot = deepcopy(SNAPSHOT)
    snapshot['files'][0]['content'] = 'def entry():\n    return 1\n'
    catalog = [dict(E, start_line=2, end_line=2), dict(E, start_line=1, end_line=1)]
    exercise = exercise_from_focus(dict(symbol='entry', evidence_ids=[1]), 0, catalog, snapshot)
    assert exercise['evidence'] == [catalog[1]]


def test_analysis_correction_retains_evidence_consumed_downstream():
    from backend.learning_review import repair_learning
    current = result()
    current['review'] = {'findings': [dict(FINDING, target='analysis.modules.0', quote='函数返回 hello。')]}
    class Narrow:
        async def ask(self, role, instruction, data, schema):
            return schema(item0=dict(LEARNING_ANALYSIS['modules'][0], title='修正标题', evidence=[dict(E, end_line=1)]))
    repaired = asyncio.run(repair_learning(Narrow(),current,SNAPSHOT,'理解入口'))
    assert E in repaired['analysis']['modules'][0]['evidence']
    assert dict(E, end_line=1) in repaired['analysis']['modules'][0]['evidence']
    assert repaired['learning'] == current['learning']


def test_lazy_pipeline_diagram_is_explicit_data_flow_and_rejects_cycles():
    from backend.learning_diagram import diagram_semantics, validate_diagram_semantics
    snapshot = deepcopy(SNAPSHOT)
    snapshot['files'][0]['content'] = 'def values():\n    yield 1\n'
    snapshot['files'][0]['lines'] = 2
    assert diagram_semantics(snapshot, LEARNING_ANALYSIS) == 'data_flow'
    assert diagram_semantics(SNAPSHOT, LEARNING_ANALYSIS) == 'execution_flow'
    graph = Diagram.model_validate(DIAGRAM)
    validate_diagram_semantics(graph, 'data_flow')
    edge = graph.edges[0].model_copy(update={'source': graph.edges[0].target, 'target': graph.edges[0].source})
    graph.edges.append(edge)
    with pytest.raises(ValueError, match='存在环'):
        validate_diagram_semantics(graph, 'data_flow')
    graph = Diagram.model_validate(DIAGRAM)
    graph.nodes.append(graph.nodes[0].model_copy(update={'id': 'n99'}))
    with pytest.raises(ValueError, match='孤立节点'):
        validate_diagram_semantics(graph, 'data_flow')


def test_missing_goal_answer_cannot_succeed(tmp_path):
    class MissingAnswer(Model):
        async def ask(self, role, instruction, data, schema):
            answer = await super().ask(role, instruction, data, schema)
            if schema.__name__ == 'LearningAnalysis':
                answer.goal_answers = []
            return answer
    _, task = execute(tmp_path, MissingAnswer)
    assert task['status'] != 'succeeded'


def test_overview_reuses_cited_answer_instead_of_conflicting_paraphrase(tmp_path):
    class ConflictingSummary(Model):
        async def ask(self, role, instruction, data, schema):
            answer = await super().ask(role, instruction, data, schema)
            if schema.__name__ == 'LearningAnalysis':
                answer.summary = 'This independently generated summary contradicts the answer.'
            return answer
    _, task = execute(tmp_path, ConflictingSummary)
    assert task['status'] == 'succeeded'
    analysis = task['result']['analysis']
    assert analysis['summary'] == analysis['goal_answers'][0]['explanation']


def test_invalid_learning_focus_retries_with_specific_feedback(tmp_path):
    class InvalidOnce(Model):
        attempts = 0
        async def ask(self, role, instruction, data, schema):
            answer = await super().ask(role, instruction, data, schema)
            if schema.__name__ == 'LearningDraft':
                self.attempts += 1
                if self.attempts == 1:
                    invalid = answer.model_dump()
                    invalid['exercises'][0]['focus_id'] = len(data['focus_choices']) + 1
                    return schema.model_validate(invalid)
                else:
                    assert 'focus_id' in data['feedback']
            return answer
    store, task = execute(tmp_path, InvalidOnce)
    assert task['status'] == 'succeeded'
    assert any(e['role'] == 'Learning Agent' and e['kind'] == 'retry' for e in store.events(task['id']))
