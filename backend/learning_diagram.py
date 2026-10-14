"""Explicit diagram semantics for lazy pipelines; no execution of source code."""
from .llm import learning_source_context


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
