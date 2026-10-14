"""Syntax records from visible source only; never import or execute target code."""
import ast
import re


def source_facts(snapshot, goal=''):
    facts, requested_functions = [], {}
    for file in snapshot.get('files', []):
        if not file['path'].endswith('.py') or 'content' not in file:
            continue
        try:
            tree = ast.parse(file['content'])
        except (SyntaxError, ValueError, RecursionError):
            continue
        def visible(start, end):
            return 'analysis_ranges' not in file or all(any(a <= n <= b for a,b in file['analysis_ranges']) for n in range(start,end+1))
        def evidence(start, end):
            return dict(path=file['path'], start_line=start, end_line=end)
        def direct(statement):
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                return
            yield statement
            for child in ast.iter_child_nodes(statement):
                yield from direct(child)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for decorator in node.decorator_list:
                    target = decorator.func if isinstance(decorator, ast.Call) else decorator
                    # Record spelling, not imported identity or runtime behavior.
                    if ast.unparse(target).split('.')[-1] != 'dataclass' or not visible(decorator.lineno, node.lineno):
                        continue
                    frozen = next((ast.unparse(k.value) for k in decorator.keywords if k.arg == 'frozen'), '未声明') if isinstance(decorator, ast.Call) else '未声明'
                    facts.append(dict(kind='dataclass_declaration', name=node.name, frozen=frozen,
                                      declaration=ast.unparse(decorator), evidence=evidence(decorator.lineno,node.lineno)))
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            header_end = max(node.lineno, node.body[0].lineno-1)
            if not visible(node.lineno, header_end):
                continue
            requested = bool(re.search(r'(?<!\w)' + re.escape(node.name) + r'(?!\w|\.py\b)', goal))
            identity = (file['path'], node.lineno)
            if requested:
                requested_functions.setdefault(node.name, []).append(identity)
            returns = [r for stmt in node.body for r in direct(stmt) if isinstance(r, ast.Return)]
            full = visible(node.lineno, node.end_lineno)
            for returned in returns:
                if not isinstance(returned.value, ast.Call) or not visible(returned.lineno,returned.end_lineno):
                    continue
                call = returned.value
                # Positional, **kwargs, indirect and zero-keyword returns are not
                # complete named-field contracts; never infer missing fields.
                if call.args or not call.keywords or any(k.arg is None for k in call.keywords):
                    continue
                fields = [dict(name=k.arg, expression=ast.unparse(k.value)) for k in call.keywords]
                manageable = len(fields)<=12 and sum(len(f['name']) for f in fields)<=600 and all(len(f['name'])<=100 for f in fields)
                reason = ('函数有未读区间，不能建立完整字段覆盖检查' if not full else
                          '多个返回位置，仅展示当前返回记录，不要求合并互斥分支' if len(returns)!=1 else
                          '字段数量或名称长度超出简图容量，仅展示记录' if not manageable else '')
                facts.append(dict(kind='direct_return_call', name=node.name, callee=ast.unparse(call.func),
                    fields=fields, requested=requested, coverage_required=requested and not reason,
                    coverage_note=reason or '仅检查显式字段覆盖，不验证数据关系或运行类型',
                    identity_evidence=evidence(node.lineno,header_end), evidence=evidence(returned.lineno,returned.end_lineno)))
    for fact in facts:
        if fact.get('requested') and len(requested_functions.get(fact['name'], []))>1:
            fact['coverage_required']=False
            fact['coverage_note']='目标函数名匹配多个定义，仅展示分别记录，不强制合并字段'
    return facts


def validate_return_fields(diagram, facts):
    """Field spelling coverage only. This never certifies edge semantics."""
    text = '\n'.join([n.label for n in diagram.nodes] + [e.label for e in diagram.edges])
    missing = sorted({field['name'] for fact in facts if fact.get('coverage_required')
                      for field in fact['fields'] if not re.search(r'(?<!\w)' + re.escape(field['name']) + r'(?!\w)', text)})
    if missing:
        raise ValueError('数据图未明确覆盖目标函数返回调用的字段：' + '、'.join(missing) + '；在字段集合节点或边中注明每个字段，不把单个字段作为整个对象的唯一来源')


def scope_notes(snapshot):
    if not snapshot.get('files'):
        return ['尚未取得代码快照。']
    notes = ['仅静态阅读，未运行仓库代码；练习未执行或评分。',
             '完整文件快照可供查看；模型输入仅包含已读区间，每次调用还可能按引用选取子集。' if snapshot.get('excerpted') else '本轮已读取选定文件；每次模型输入按引用选取源码子集，其他文件不属于本轮已读范围。',
             '已读取文件不等于所有函数均已展开解释，自动审核不代表人工验收。']
    if snapshot.get('tree_truncated'):
        notes.append('仓库文件树被截断，不能视为完整仓库范围。')
    skipped = [s for s in snapshot.get('skipped',[]) if s.get('reason')!='不在本次目标文件范围']
    for entry in skipped[:30]:
        notes.append('未取得文件：' + entry.get('path','') + '（' + entry.get('reason','原因未记录') + '）')
    if len(skipped)>30:
        notes.append(f'另有 {len(skipped)-30} 个未取得文件，详见来源快照 skipped 记录。')
    if snapshot.get('excerpted'):
        notes.append('代码段选择受读取预算限制，区间外代码不属于模型已读范围，不能据此宣称目标完整覆盖。')
    return notes
