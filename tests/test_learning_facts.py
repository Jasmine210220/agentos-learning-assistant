import pytest
from backend.learning_facts import source_facts, validate_return_fields, scope_notes
from backend.models import Diagram
from backend.learning import report_learning


def snapshot(code, ranges=None):
    file = dict(path='demo.py', content=code, lines=len(code.splitlines()))
    if ranges is not None:
        file['analysis_ranges'] = ranges
    return dict(files=[file], excerpted=ranges is not None, commit='fixed', repository='public')


def test_named_return_fields_include_all_inputs_and_preserve_nested_ownership():
    code = 'def build():\n    def nested():\n        return Other(hidden=1)\n    return Result(text=name, path=parent.resolve(), paths=items)\n'
    facts = source_facts(snapshot(code), '理解 build 如何返回结果')
    wanted = [f for f in facts if f.get('requested')]
    assert len(wanted) == 1
    assert wanted[0]['fields'] == [dict(name='text', expression='name'), dict(name='path', expression='parent.resolve()'), dict(name='paths', expression='items')]
    assert wanted[0]['evidence']['start_line'] == 4
    assert next(f for f in facts if f['name'] == 'nested')['requested'] is False


def test_unread_return_and_dynamic_keywords_do_not_produce_false_contracts():
    code = 'def build():\n    return Result(text=name, path=parent)\n'
    assert not source_facts(snapshot(code, [[1, 1]]), 'build')
    assert not source_facts(snapshot('def build():\n    return Result(**data)\n'), 'build')
    assert not source_facts(snapshot('def build():\n    return Result(data)\n'), 'build')


def test_dataclass_fact_reports_literal_declaration_not_immutability():
    facts = source_facts(snapshot('@dataclass\nclass Plain:\n    value: str\n@dataclass(frozen=True)\nclass Frozen:\n    value: str\n'))
    assert [(f['name'], f['frozen']) for f in facts] == [('Plain', '未声明'), ('Frozen', 'True')]
    assert source_facts(snapshot('@dataclass\nclass Plain:\n    value: str\n', [[2, 3]])) == []


def diagram(label):
    e = dict(path='demo.py', start_line=2, end_line=2)
    return Diagram(title='数据传递', nodes=[dict(id='n1', label=label, evidence=[e]), dict(id='n2', label='返回对象', evidence=[e])], edges=[dict(source='n1', target='n2', label='构造结果', inferred=False, evidence=[e])])


def test_field_coverage_rejects_string_only_return_and_checks_identifier_boundaries():
    facts = source_facts(snapshot('def build():\n    return Result(text=name, path=parent, paths=items)\n'), 'build')
    with pytest.raises(ValueError, match='path'):
        validate_return_fields(diagram('text、paths'), facts)
    validate_return_fields(diagram('text、path、paths'), facts)
    # An unnamed target must not force every return in a whole repository.
    validate_return_fields(diagram('text'), source_facts(snapshot('def other():\n    return Result(extra=value)\n'), '理解入口'))


def test_report_scope_uses_program_record_not_contradictory_model_text():
    snap = snapshot('def build():\n    return Result(text=name, path=parent)\n')
    result = dict(snapshot=snap, analysis=dict(scope_reason='未读消费者', limitations=['完全没有读取demo.py']), learning=dict(limitations=['从未读取代码']), source_facts=source_facts(snap, 'build'))
    report = report_learning(dict(repository='public', goal='build', status='needs_attention'), result)
    assert 'demo.py：1-2' in report and 'path ← `parent`' in report
    assert '未读消费者' not in report and '完全没有读取demo.py' not in report and '从未读取代码' not in report
    assert '其他文件' in ' '.join(scope_notes(snap))


def test_model_context_syntax_facts_cannot_leak_outside_visible_lines():
    from backend.llm import learning_source_context
    code = '@dataclass\nclass Item:\n    value: str\n\n' + '\n'.join('# filler' for _ in range(15)) + '\ndef build():\n    return Result(text=name)\n'
    data = learning_source_context(snapshot(code), {'evidence': [dict(path='demo.py', start_line=20, end_line=21)]})
    assert not any(f['kind'] == 'dataclass_declaration' for f in data['files'][0]['python_syntax_facts'])


@pytest.mark.parametrize('repair', [True, False])
def test_workflow_retries_missing_fields_once_before_render_and_never_silently_passes(tmp_path, repair):
    import asyncio
    from copy import deepcopy
    from backend.models import TaskInput
    from backend.store import Store
    from backend.workflow import Runner
    from test_learning import Model
    from test_backend import SNAPSHOT, DIAGRAM
    snap = deepcopy(SNAPSHOT)
    snap['files'][0]['content'] = 'def hello():\n    return Result(text=name, path=parent)\n'
    snap['files'][0]['lines'] = 2
    rendered = []
    async def reader(*args): return snap
    async def renderer(source, workspace):
        rendered.append(source)
        (workspace / 'diagram.svg').write_text('<svg/>')
    class Fields(Model):
        drawings = 0
        async def ask(self, role, instruction, data, schema):
            if schema is Diagram:
                self.drawings += 1
                assert data['requested_return_fields'][0]['name'] == 'hello'
                if self.drawings == 2:
                    assert 'path' in data['render_feedback']
                value = deepcopy(DIAGRAM)
                value['nodes'][0]['label'] = 'text、path' if repair and self.drawings == 2 else 'text'
                return schema.model_validate(value)
            return await super().ask(role, instruction, data, schema)
    store = Store(tmp_path)
    task = store.create(TaskInput(kind='learn', repository=snap['repository'], goal='理解 hello').model_dump())
    asyncio.run(Runner(store, reader, Fields, renderer).run(task['id']))
    final = store.get(task['id'])
    assert final['status'] == ('succeeded' if repair else 'failed')
    assert len(rendered) == (1 if repair else 0)
    assert sum(e['role'] == 'Chart Agent' and e['kind'] == 'model_input' for e in store.events(task['id'])) == 2


def test_decorator_only_and_return_only_do_not_leak_hidden_names():
    assert source_facts(snapshot('@dataclass\nclass SecretName:\n    value: str\n', [[1,1]])) == []
    assert source_facts(snapshot('def secret_function():\n    return Result(text=name)\n', [[2,2]]), 'secret_function') == []


@pytest.mark.parametrize('code', [
    'def build(flag):\n    if flag:\n        return A(text=name)\n    return B(path=parent)\n',
    'def build(flag):\n    if flag:\n        return A(text=name)\n    return other\n',
    'def build(flag):\n    if flag:\n        return A(text=name)\n    return B(**data)\n',
    'def build(flag):\n    if flag:\n        return A(text=name)\n    return B(data)\n',
])
def test_multiple_or_partial_returns_are_records_not_merged_contracts(code):
    facts=source_facts(snapshot(code), 'build')
    assert facts and not any(f.get('coverage_required') for f in facts)
    validate_return_fields(diagram('仅所选分支的数据'), facts)


def test_partial_body_cannot_claim_complete_contract():
    code='def build():\n    hidden = secret()\n    return Result(text=name)\n'
    facts=source_facts(snapshot(code, [[1,1],[3,3]]),'build')
    assert facts[0]['fields']==[dict(name='text',expression='name')]
    assert not facts[0]['coverage_required']
    assert 'secret' not in str(facts)


def test_duplicate_function_name_does_not_merge_unrelated_fields():
    snap=snapshot('def build():\n    return Result(text=name)\n')
    snap['files'].append(dict(path='other.py',content='def build():\n    return Other(path=parent)\n'))
    facts=source_facts(snap,'build')
    assert len(facts)==2 and not any(f['coverage_required'] for f in facts)
    validate_return_fields(diagram('text'),facts)


def test_very_long_identifier_does_not_make_diagram_impossible():
    field='long_'+'x'*101
    facts=source_facts(snapshot(f'def build():\n    return Result({field}=value)\n'),'build')
    assert facts[0]['fields'][0]['name']==field
    assert not facts[0]['coverage_required']
    validate_return_fields(diagram('返回数据'),facts)


def test_unicode_fields_and_coverage_do_not_certify_relationships():
    facts=source_facts(snapshot('def build():\n    return Result(内容=name, 路径=parent)\n'),'build')
    with pytest.raises(ValueError,match='路径'):
        validate_return_fields(diagram('内容'),facts)
    # Both names occur, but this graph says nothing about their actual sources.
    # This intentionally passes spelling coverage only, never semantic review.
    validate_return_fields(diagram('内容、路径'),facts)


def test_scope_preserves_fetch_failures_excerpt_and_target_gap():
    snap=snapshot('def build():\n    return Result(text=name)\n', [[1,1]])
    snap.update(tree_truncated=True,skipped=[dict(path='missing.py',reason='读取失败'),dict(path='big.py',reason='单文件过大')])
    result=dict(snapshot=snap,analysis=dict(scope_complete=False,scope_reason='目标调用的实现尚缺'),learning={})
    report=report_learning(dict(repository='public',goal='build',status='needs_attention'),result)
    for text in ['missing.py','读取失败','big.py','单文件过大','文件树被截断','代码段选择受读取预算限制','目标调用的实现尚缺','未运行仓库代码']:
        assert text in report


def test_all_model_static_records_are_limited_to_sent_code():
    from backend.llm import learning_source_context
    import json
    code='def visible():\n    return 1\n'+('\n'*20)+'@contextmanager\ndef hidden():\n    obj = Secret()\n    obj.update(secret)\n    yield obj\n    return obj\n'
    context=learning_source_context(snapshot(code),{'evidence':[dict(path='demo.py',start_line=1,end_line=2)]})
    encoded=json.dumps(context)
    for word in ('hidden','Secret','update','contextmanager','yield'):
        assert word not in encoded


def test_non_python_and_invalid_source_do_not_invent_contracts():
    assert source_facts(snapshot('this is not valid Python !'),'build')==[]
    snap=snapshot('def build():\n    return Result(text=name)\n');snap['files'][0]['path']='demo.txt'
    assert source_facts(snap,'build')==[]
