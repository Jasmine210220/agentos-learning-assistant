"""One bounded source-excerpt selection, using static syntax only."""
import ast
from copy import deepcopy
from typing import Annotated
from pydantic import Field, create_model
from .models import Strict, Claim, LearningAnalysis

CONTEXT_BYTES = 45000


def analysis_citation_contract(snapshot):
    """Constrain excerpted analysis to source IDs, as learning already does."""
    if not snapshot.get('excerpted'):
        return LearningAnalysis, []
    catalog = []
    for file in snapshot['files']:
        for start, end in file.get('analysis_ranges', []):
            for left in range(start, end + 1, 20):
                catalog.append(dict(path=file['path'], start_line=left, end_line=min(end, left+19)))
    if not catalog:
        raise ValueError('没有可引用的已读源码')
    reference = Annotated[int, Field(strict=True, ge=1, le=len(catalog))]
    fields = {name: (field.annotation, deepcopy(field)) for name, field in Claim.model_fields.items() if name != 'evidence'}
    fields['evidence_ids'] = (list[reference], Field(min_length=1, max_length=3))
    fields['explanation'] = (str, Field(max_length=600))
    fields['title'] = (str, Field(max_length=60))
    claim = create_model('SourceBoundClaim', __base__=Strict, **fields)
    fields = {name: (field.annotation, deepcopy(field)) for name, field in LearningAnalysis.model_fields.items()}
    for name, maximum in [('modules',3), ('flow',5), ('technologies',2), ('goal_answers',2)]:
        fields[name] = (list[claim], Field(min_length=1, max_length=maximum))
    return create_model('LearningAnalysis', __base__=Strict, **fields), catalog


def resolve_analysis_citations(value, catalog):
    if not catalog:
        return LearningAnalysis.model_validate(value)
    value = deepcopy(value)
    for section in ('modules', 'flow', 'technologies', 'goal_answers'):
        for claim in value[section]:
            claim['evidence'] = [catalog[i-1] for i in dict.fromkeys(claim.pop('evidence_ids'))]
    return LearningAnalysis.model_validate(value)


def include_route_siblings(paths, files):
    """Bounded sibling route coverage; never recursively traverse dependencies."""
    from pathlib import PurePosixPath
    parents = {str(PurePosixPath(p).parent) for p in paths
               if any(part.lower() in {'api', 'routes', 'routers', 'controllers'}
                      for part in PurePosixPath(p).parts[:-1])}
    siblings, used = [], 0
    for f in files:
        p = PurePosixPath(f['path'])
        entry = p.name in {'main.py', 'app.py', 'server.py', 'main.ts', 'app.ts', 'server.ts'} and f['bytes'] <= 6000
        if (entry or str(p.parent) in parents) and f['path'] not in paths and p.suffix in {'.py', '.ts', '.js'} and p.name != '__init__.py':
            if len(siblings) < 8 and used + f['bytes'] <= 40000:
                siblings.append(f['path'])
                used += f['bytes']
    return list(dict.fromkeys(siblings + paths))


def merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def trim_adjacent_evidence(value, snapshot):
    """Narrow small citation overhangs to visible lines, never expand evidence.

    Semantic review still sees only the narrowed source. The caller records
    original citations so normalization cannot erase a model's bad reference.
    """
    files = {f['path']: f for f in snapshot['files']}
    changes = []
    def visit(item):
        if isinstance(item, dict):
            if {'path', 'start_line', 'end_line'} <= item.keys():
                file = files.get(item['path'], {})
                for start, end in file.get('analysis_ranges', []):
                    left, right = max(start, item['start_line']), min(end, item['end_line'])
                    if left <= right and 0 < left-item['start_line'] + item['end_line']-right <= 8:
                        original = dict(item)
                        item.update(start_line=left, end_line=right)
                        changes.append({'original': original, 'narrowed': dict(item)})
                        break
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    visit(value)
    return changes


def source_blocks(snapshot):
    blocks = []
    for file in snapshot['files']:
        lines = file['content'].splitlines()
        labels = {}
        return_lines = set()
        direct_returns = set()
        if file['path'].endswith('.py'):
            try:
                tree = ast.parse(file['content'])
                return_lines = {node.lineno for node in ast.walk(tree) if isinstance(node, ast.Return)}
                def direct(node, conditional=False):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        return
                    if isinstance(node, ast.Return) and not conditional:
                        direct_returns.add(node.lineno)
                    nested = conditional or isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Match, ast.ExceptHandler))
                    for child in ast.iter_child_nodes(node):
                        direct(child, nested)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        labels[min([node.lineno] + [d.lineno for d in node.decorator_list])] = node.name
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for statement in node.body:
                            direct(statement)
            except (SyntaxError, ValueError, RecursionError):
                pass
        start, size, name = 1, 0, 'module'
        def add(end):
            code = '\n'.join(lines[start-1:end])
            import re
            calls = list(dict.fromkeys(re.findall(r'\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(', code)))
            blocks.append(dict(id=len(blocks)+1, path=file['path'], start_line=start, end_line=end,
                               symbol=name, bytes=len(code.encode('utf-8')), calls=calls,
                               returns=any(start <= line <= end for line in return_lines),
                               direct_return=any(start <= line <= end for line in direct_returns),
                               preview='\n'.join(lines[start-1:min(end,start+2)])[:120]))
        for number, line in enumerate(lines, 1):
            if number > start and (number in labels or size + len(line.encode('utf-8')) + 1 > 3500):
                add(number-1)
                start, size = number, 0
            if number in labels:
                name = labels[number]
            size += len(line.encode('utf-8')) + 1
        if lines:
            add(len(lines))
    return blocks


async def select_learning_excerpt(llm, snapshot, goal, event):
    if sum(len(f['content'].encode('utf-8')) for f in snapshot['files']) <= CONTEXT_BYTES:
        return snapshot
    blocks = source_blocks(snapshot)
    # Show actual route declarations and application mounting before asking the
    # model to choose. File names alone repeatedly misidentified demo endpoints.
    entries = []
    for file in snapshot['files']:
        if not file['path'].endswith('.py'):
            continue
        try:
            tree = ast.parse(file['content'])
        except (SyntaxError, ValueError):
            continue
        lines = file['content'].splitlines()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                route = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in {'post', 'get', 'put', 'delete', 'patch'} for d in node.decorator_list)
                mount = any(isinstance(n, ast.Attribute) and n.attr == 'include_router' for n in ast.walk(node))
                if route or mount:
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    calls = [ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)]
                    entries.append(dict(path=file['path'], start_line=start,
                        code='\n'.join(lines[start-1:min(node.end_lineno,start+(26 if mount else 5))]),
                        calls=list(dict.fromkeys(calls))[:12]))
    block_id = Annotated[int, Field(strict=True, ge=1, le=len(blocks))]
    schema = create_model('LearningSourceSelection', __base__=Strict,
                          block_ids=(list[block_id], Field(min_length=1, max_length=30)), reason=(str, ...))
    file_names = list(dict.fromkeys(b['path'] for b in blocks))
    catalog = [[b['id'], file_names.index(b['path']) + 1, b['start_line'], b['end_line'],
                b['symbol'], b['bytes'], ','.join(b['calls'][:8]), b['returns']] for b in blocks]
    selected = await llm.ask('Code Agent',
        '先读entrypoints中的真实源码，辨认业务入口和挂载条件，再为用户指定的一条端到端流程选择关键源码段，按主链优先排序，总量不超过35000字节，单个文件不超过18000字节。长函数优先首尾，不必展开辅助策略/检索内部；必须留空间读分析器和回复组合函数。'
        '只从目录选择block_ids；同一长函数分为多个连续段，注意起点、中段和最终返回/提交不可缺失。'
        '优先真实对外路由、服务入口、核心调度、模型调用和最终回复组合/输出；演示路由不能代替真实入口。'
        '选择路由挂载/依赖装配以确认入口是否启用。辅助知识检索、策略细节可保留调用边界，不必展开所有分支。'
        '同目录路由都属于候选；选择实际业务消息处理函数及其router前缀定义，不要只选名为chat的文件。'
        '不选择无关管理接口、测试或大段配置说明。reason不超过120字，不重复逐段清单；预算不足说明缺失，不假称已读。',
        {'goal': goal, 'entrypoints': entries, 'files': dict(enumerate(file_names, 1)),
         'block_columns': ['id','file_id','start_line','end_line','symbol','bytes','calls','contains_return'], 'blocks': catalog}, schema)
    chosen, used, per_file = [], 0, {}
    candidates = [blocks[i-1] for i in dict.fromkeys(selected.block_ids)]
    # A selected long function needs its actual exit/callee boundary as well as
    # its beginning. Include both before spending space on intermediate details.
    selected_symbols = {(b['path'], b['symbol']) for b in candidates if b['symbol'] != 'module'}
    for key in sorted(selected_symbols):
        complete = [b for b in blocks if (b['path'], b['symbol']) == key]
        for boundary in [complete[0], *[b for b in complete if b['returns']], complete[-1]]:
            if boundary not in candidates:
                candidates.append(boundary)
    # Two bounded static call-bridge passes, including same-file service helpers.
    # Only declarations already downloaded are eligible; no target execution.
    existing = {(b['path'], b['symbol']) for b in candidates}
    bridges, bridge_bytes = [], 0
    for _ in range(2):
        ordered = sorted(candidates + bridges, key=lambda b: not b['direct_return'])
        called = list(dict.fromkeys(call.split('.')[-1] for b in ordered for call in b['calls']))
        for name in called:
            if name[:1].isupper() or name in {'get','add','update','model_dump','model_validate','model_copy','dict','list','str','int'}:
                continue
            keys = list(dict.fromkeys((b['path'], b['symbol']) for b in blocks if b['symbol'] == name))
            for key in keys:
                if key in existing:
                    continue
                members = [b for b in blocks if (b['path'], b['symbol']) == key]
                boundary = members if sum(b['bytes'] for b in members) <= 6000 else list({b['id']: b for b in [members[0], *[b for b in members if b['returns']], members[-1]]}.values())
                size = sum(b['bytes'] for b in boundary)
                if bridge_bytes + size <= 12000:
                    bridges.extend(boundary)
                    bridge_bytes += size
                    existing.add(key)
    candidates = bridges + candidates
    groups = {}
    for b in candidates:
        groups.setdefault((b['path'], b['symbol']), []).append(b)
    endpoints = {b['id'] for group in groups.values() for b in
                 (min(group, key=lambda b: b['start_line']), max(group, key=lambda b: b['start_line']))}
    endpoints.update(b['id'] for b in candidates if b['returns'])
    starts = {min(group, key=lambda b: b['start_line'])['id'] for group in groups.values()}
    # Normal completion must win over early guards/exception tails when the
    # context is full; otherwise a valid workflow appears to have no reply.
    candidates.sort(key=lambda b: 0 if b['direct_return'] else 1 if b['id'] in starts else 2 if b['id'] in endpoints else 3)
    for block in candidates:
        if used + block['bytes'] <= CONTEXT_BYTES - 1000 and per_file.get(block['path'], 0) + block['bytes'] <= 24000:
            chosen.append(block)
            used += block['bytes']
            per_file[block['path']] = per_file.get(block['path'], 0) + block['bytes']
    if not chosen:
        raise ValueError('目标代码段超出学习读取预算，未开始生成结论')
    snapshot = deepcopy(snapshot)
    for file in snapshot['files']:
        file['analysis_ranges'] = merge_ranges([[b['start_line'], b['end_line']] for b in chosen if b['path'] == file['path']])
        # Prefix declarations are part of a selected HTTP entry's identity.
        if file['analysis_ranges'] and file['path'].endswith('.py'):
            try:
                for node in ast.walk(ast.parse(file['content'])):
                    if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call) and getattr(node.value.func, 'id', '') == 'APIRouter':
                        new_lines = [line for i, line in enumerate(file['content'].splitlines(), 1)
                                     if node.lineno <= i <= node.end_lineno and not any(a <= i <= b for a,b in file['analysis_ranges'])]
                        extra = sum(len(line.encode('utf-8')) + 1 for line in new_lines)
                        if used + extra <= CONTEXT_BYTES:
                            file['analysis_ranges'] = merge_ranges(file['analysis_ranges'] + [[node.lineno, node.end_lineno]])
                            used += extra
            except (SyntaxError, ValueError):
                pass
    snapshot['excerpted'] = True
    snapshot['analysis_bytes'] = used
    snapshot['analysis_selection'] = selected.reason
    event('Code Agent', 'scope', '已固定模型阅读代码段；完整快照保留供来源查看',
          {'ranges': [{k:b[k] for k in ('path','start_line','end_line')} for b in chosen], 'bytes': used,
           'reason': selected.reason, 'omitted_ids': [i for i in selected.block_ids if blocks[i-1] not in chosen]})
    return snapshot
