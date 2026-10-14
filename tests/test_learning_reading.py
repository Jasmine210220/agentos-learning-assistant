import asyncio
from copy import deepcopy
import pytest
from backend.learning_reading import source_blocks, select_learning_excerpt, include_route_siblings, trim_adjacent_evidence, analysis_citation_contract, resolve_analysis_citations
from backend.llm import learning_source_context
from backend.workflow import validate_evidence
from test_backend import SNAPSHOT, E


def test_excerpt_preserves_line_numbers_and_rejects_unread_evidence():
    snapshot = deepcopy(SNAPSHOT)
    file = snapshot['files'][0]
    file['content'] = 'def hidden():\n    return 1\ndef visible():\n    return 2\n'
    file['lines'] = 4
    file['analysis_ranges'] = [[3, 4]]
    context = learning_source_context(snapshot)
    code = context['files'][0]['numbered_code']
    assert '3: def visible():' in code and 'hidden' not in code
    assert not validate_evidence(dict(E, start_line=3, end_line=4), snapshot)
    assert validate_evidence(dict(E, start_line=1, end_line=4), snapshot)


def test_returned_object_creation_is_distinct_from_later_field_updates():
    snapshot = deepcopy(SNAPSHOT)
    file = snapshot['files'][0]
    file['content'] = 'def handler():\n    response = build(None)\n    outcome = run()\n    response.message = outcome.message\n    return response\n'
    file['analysis_ranges'] = [[1, 5]]
    flow = learning_source_context(snapshot)['files'][0]['python_returned_value_flows'][0]
    assert flow['function'] == 'handler'
    assert flow['assignments_in_source_order'] == [
        {'line': 2, 'code': 'response = build(None)'}, {'line': 4, 'code': 'response.message = outcome.message'}]
    assert flow['returns'] == [{'line': 5, 'code': 'return response'}]


def test_direct_side_effect_owner_is_not_the_callee():
    snapshot = deepcopy(SNAPSHOT)
    snapshot['files'][0]['content'] = 'def callee():\n    return 42\ndef caller():\n    value = callee()\n    sys.path.insert(0, "example")\n    return value\n'
    owners = learning_source_context(snapshot)['files'][0]['python_direct_call_owners']
    assert owners == [{'function': 'caller', 'line': 5, 'call': 'sys.path.insert'}]


def test_blocks_cover_full_file_without_executing_and_retain_decorators():
    snapshot = deepcopy(SNAPSHOT)
    file = snapshot['files'][0]
    file['content'] = 'raise RuntimeError("never execute")\n@router.post("/messages")\ndef entry():\n    return 1\n'
    blocks = source_blocks(snapshot)
    assert blocks[0]['start_line'] == 1
    assert blocks[1]['start_line'] == 2 and blocks[1]['symbol'] == 'entry'
    assert blocks[-1]['end_line'] == 4


def test_route_catalog_cannot_hide_business_entry_behind_chat_filename():
    files = [{'path': p, 'bytes': size} for p, size in [
        ('app/api/chat.py', 2000), ('app/api/enterprise.py', 17000),
        ('app/api/huge.py', 60000), ('app/services/other.py', 1000)]]
    selected = include_route_siblings(['app/api/chat.py'], files)
    assert selected == ['app/api/enterprise.py', 'app/api/chat.py']


def test_citation_overhang_only_narrows_and_never_admits_unread_evidence():
    snapshot = deepcopy(SNAPSHOT)
    snapshot['files'][0]['analysis_ranges'] = [[50, 60]]
    value = {'evidence': [dict(E, start_line=44, end_line=56), dict(E, start_line=1, end_line=60), dict(E, start_line=42, end_line=49)]}
    changes = trim_adjacent_evidence(value, snapshot)
    assert len(changes) == 1
    assert changes[0]['original']['start_line'] == 44
    assert value['evidence'][0] == dict(E, start_line=50, end_line=56)
    assert value['evidence'][1]['start_line'] == 1
    assert value['evidence'][2]['end_line'] == 49


def test_analysis_source_ids_cannot_bridge_unread_gap():
    from test_learning import LEARNING_ANALYSIS
    snapshot = deepcopy(SNAPSHOT)
    snapshot['excerpted'] = True
    snapshot['files'][0]['analysis_ranges'] = [[1, 2], [10, 12]]
    schema, catalog = analysis_citation_contract(snapshot)
    value = deepcopy(LEARNING_ANALYSIS)
    for section in ('modules', 'flow', 'technologies', 'goal_answers'):
        for claim in value[section]:
            claim.pop('evidence', None)
            claim['evidence_ids'] = [1, 2]
    draft = schema.model_validate(value)
    resolved = resolve_analysis_citations(draft.model_dump(), catalog)
    assert [e.model_dump() for e in resolved.flow[0].evidence] == [dict(E,start_line=1,end_line=2),dict(E,start_line=10,end_line=12)]
    value['flow'][0]['evidence_ids'] = [3]
    with pytest.raises(ValueError):
        schema.model_validate(value)


def test_large_source_selects_bounded_ranges_not_truncated_file():
    snapshot = deepcopy(SNAPSHOT)
    file = snapshot['files'][0]
    file['content'] = ''.join(f'def f{i}():\n    return "' + 'x'*1000 + '"\n' for i in range(60))
    file['lines'] = 120
    class Model:
        async def ask(self, role, instruction, data, schema):
            assert data['blocks'][-1][3] == 120
            return schema(block_ids=[1, 60], reason='入口及返回段')
    selected = asyncio.run(select_learning_excerpt(Model(), snapshot, '理解流程', lambda *a: None))
    assert selected['files'][0]['content'] == file['content']
    assert selected['files'][0]['analysis_ranges'] == [[1, 2], [119, 120]]
    assert 'analysis_ranges' not in file
    assert selected['analysis_bytes'] <= 45000


def test_selected_entry_reads_same_file_return_bridge_without_execution():
    snapshot = deepcopy(SNAPSHOT)
    file = snapshot['files'][0]
    file['content'] = ('def entry():\n    return helper()\ndef helper():\n    return 42\n' +
        ''.join(f'def unused{i}():\n    return "' + 'x'*1000 + '"\n' for i in range(50)))
    class Model:
        async def ask(self, role, instruction, data, schema):
            return schema(block_ids=[1], reason='entry')
    selected = asyncio.run(select_learning_excerpt(Model(), snapshot, 'return flow', lambda *a: None))
    assert selected['files'][0]['analysis_ranges'] == [[1, 4]]
    assert not validate_evidence(dict(E, start_line=3, end_line=4), selected)
    assert validate_evidence(dict(E, start_line=5, end_line=6), selected)


def test_long_exception_tail_does_not_hide_success_return():
    snapshot = deepcopy(SNAPSHOT)
    file = snapshot['files'][0]
    file['content'] = ('def entry():\n    try:\n' +
        ('        value = "' + 'x'*3000 + '"\n')*16 +
        '        return helper()\n    except Exception:\n' +
        ('        value = "' + 'y'*3000 + '"\n')*16 +
        '        raise\ndef helper():\n    return 42\n')
    return_line = file['content'].splitlines().index('        return helper()') + 1
    class Model:
        async def ask(self, role, instruction, data, schema):
            return schema(block_ids=[1], reason='entry')
    selected = asyncio.run(select_learning_excerpt(Model(), snapshot, 'success path', lambda *a: None))
    assert any(start <= return_line <= end for start,end in selected['files'][0]['analysis_ranges'])
    assert selected['analysis_bytes'] <= 45000


def test_late_success_call_and_two_handoffs_are_not_lost_from_compact_catalog():
    snapshot = deepcopy(SNAPSHOT)
    file = snapshot['files'][0]
    file['content'] = ('def entry():\n' + ''.join(f'    auxiliary{i}()\n' for i in range(16)) +
        '    return bridge()\ndef bridge():\n    return final_reply()\ndef final_reply():\n    return 42\n' +
        ''.join(f'def unused{i}():\n    return "' + 'x'*1000 + '"\n' for i in range(50)))
    class Model:
        async def ask(self, role, instruction, data, schema):
            return schema(block_ids=[1], reason='entry')
    selected = asyncio.run(select_learning_excerpt(Model(), snapshot, 'reply flow', lambda *a: None))
    code = learning_source_context(selected)['files'][0]['numbered_code']
    assert 'def bridge()' in code and 'def final_reply()' in code
