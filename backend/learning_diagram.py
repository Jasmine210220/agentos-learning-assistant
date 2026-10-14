"""Explicit diagram semantics for lazy pipelines; no execution of source code."""
from .llm import learning_source_context
import re

DIAGRAM_BOUNDARY = '当前只生成数据传递图，不提供调用顺序图或执行时序图；箭头表示有代码依据的数据来源与去向，渲染成功不代表语义正确。'


def requests_execution_diagram(goal):
    return bool(re.search(r'时序|调用顺序|执行顺序|sequence\s*diagram|call\s*order|execution\s*order', goal, re.I))


def reading_record(snapshot):
    files = []
    for f in snapshot.get('files', []):
        ranges = f.get('analysis_ranges', []) if snapshot.get('excerpted') else [[1, f.get('lines') or len(f.get('content', '').splitlines())]]
        if ranges:
            files.append(f['path'] + '：' + '、'.join(f'{a}-{b}' for a,b in ranges))
    return '程序记录的实际已读范围（不等于目标已完整覆盖）：' + '；'.join(files)


def diagram_semantics(snapshot, analysis):
    references = [e for key in ('flow', 'goal_answers') for claim in analysis.get(key, []) for e in claim['evidence']]
    context = learning_source_context(snapshot, references)
    for file in context['files']:
        for function in file.get('python_deferred_bodies', []):
            if any(e['path'] == file['path'] and e['start_line']-4 <= function['yield_line'] <= e['end_line']+4 for e in references):
                return 'data_flow'
    return 'execution_flow'


def validate_diagram_semantics(diagram, semantics):
    if semantics != 'data_flow':
        return
    adjacency = {n.id: [] for n in diagram.nodes}
    for edge in diagram.edges:
        if edge.source not in adjacency or edge.target not in adjacency:
            raise ValueError('数据图引用了不存在的节点')
        adjacency[edge.source].append(edge.target)
    touched = {key for key, targets in adjacency.items() if targets} | {target for targets in adjacency.values() for target in targets}
    if touched != set(adjacency):
        raise ValueError('数据图存在孤立节点：' + '、'.join(sorted(set(adjacency) - touched)) + '；补齐有来源的数据转换或移除无关节点')
    visiting, done = set(), set()
    def visit(node):
        if node in visiting:
            raise ValueError('数据传递图存在环：' + '、'.join(sorted(visiting)) + ' → ' + node + '。合并相互调用的处理节点，或拆为不同的数据处理阶段；不得画函数调用后再返回原节点。')
        if node in done:
            return
        visiting.add(node)
        for target in adjacency[node]:
            visit(target)
        visiting.remove(node)
        done.add(node)
    for node in adjacency:
        visit(node)
