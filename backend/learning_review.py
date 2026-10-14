"""Evidence-bound review and bounded, atomic corrections for learning artifacts."""
from copy import deepcopy
from typing import Literal
from pydantic import Field, create_model
from .models import Strict, Evidence, Claim, Exercise, ReadingStep, Diagram
from .llm import learning_source_context
from .workflow import validate_evidence
from .learning_exercises import ExerciseFocus, exercise_from_focus


class Finding(Strict):
    target: str
    quote: str = Field(min_length=2, max_length=300)
    problem: str = Field(min_length=1, max_length=400)
    evidence: list[Evidence] = Field(min_length=1, max_length=3)
    source_quote: str = Field(min_length=3, max_length=180)


class LearningAudit(Strict):
    summary: str = Field(min_length=1, max_length=600)
    findings: list[Finding] = Field(max_length=4)


class Decision(Strict):
    finding_id: int = Field(ge=1)
    verdict: Literal['confirmed', 'dismissed', 'uncertain']
    reason: str = Field(min_length=1, max_length=400)
    evidence: list[Evidence] = Field(min_length=1, max_length=3)
    source_quote: str = Field(min_length=3, max_length=180)


class FindingDecisions(Strict):
    decisions: list[Decision] = Field(min_length=1, max_length=4)


def units(result):
    output = {'analysis.summary': result['analysis']['summary']}
    for section in ('modules', 'flow', 'technologies', 'goal_answers'):
        output.update({f'analysis.{section}.{i}': item for i, item in enumerate(result['analysis'].get(section, []))})
    for section in ('reading_order', 'exercises'):
        output.update({f'learning.{section}.{i}': item for i, item in enumerate(result['learning'][section])})
    output['diagram'] = result['diagram']
    return output


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


def source_quote_valid(item, snapshot):
    if validate_evidence(item, snapshot):
        return False
    files = {f['path']: f['content'].splitlines() for f in snapshot['files']}
    # Indentation is presentation, not source semantics. Keep an exact single
    # source line match while accepting omitted or added surrounding whitespace.
    return any(item['source_quote'].strip() in [line.strip() for line in files[e['path']][e['start_line']-1:e['end_line']]]
               for e in item['evidence'])


def finding_errors(findings, catalog, snapshot):
    errors = []
    for i, f in enumerate(findings, 1):
        if f['target'] not in catalog or not any(f['quote'] in s for s in strings(catalog.get(f['target']))):
            errors.append(f'问题{i}的target/quote不是成果中的原文，不能据此退回')
        if not source_quote_valid(f, snapshot):
            errors.append(f'问题{i}的源码原文不在所引用的快照行内')
    return errors


async def audit_learning(llm, result, snapshot, goal, changed_targets=None):
    catalog = units(result)
    instruction = (
        '核对学习成果的实质代码事实。units的键是精确target。只有实际存在的错误才输出findings；无错误返回空数组。'
        '每个问题必须逐字摘录一个目标中的quote，并给出反驳它的源码evidence及逐字source_quote（不带行号前缀）。'
        'source_quote只摘一行关键源码（去掉行首缩进），不能拼接、省略或改写。quote只摘一段原有文字或图的一个标签，不要摘JSON语法。summary不超过80字。'
        '重点检查直接返回类型/副作用所属函数、条件优先级、练习前提与自查一致性、图中调用顺序和边条件。'
        'python_returned_value_flows是静态语法事实：逐项核对创建返回对象的调用发生在前面还是最后，回填已有对象不等于最后再次调用创建函数。'
        'python_direct_call_owners标明语句直接属于哪个函数，不能把调用者的状态修改归给被调用者（也不能反过来）。'
        '逐单元核对，不要让形式问题遮蔽事实错误。特别检查“函数签名中有某参数”“一定/只/恒”等断言和生成器调用与实际迭代的区别。'
        '禁止替成果补前提后判正确：逐字保留原文限定条件。未传入参数不等于对应配置为空；若原文说一定走某分支或不检查配置，必须检查所有更早的if/elif及配置赋值，寻找反例，不能把原文改成带额外条件的正确句子。'
        '引用包含相邻签名、空行或少量上下文不是错误；只要范围内确有支持结论的语句，不得因起止行不够紧凑退回。'
        'python_deferred_bodies是静态语法事实。图若把生成器函数体执行放在消费者开始迭代之前，是实质时序错误；核对消费for语句与yield所在函数。'
        '当diagram_semantics为data_flow，图明确不是执行时序，箭头仅表示数据的生产与消费，不得按调用先后批评；仍核对数据来源与去向。goal_answers必须实际回答用户点名的问题，不能只声称覆盖。'
        '练习可以要求读者自己查出答案，不需要预先提供答案；不要把阅读问题当事实断言。'
        '只画选定分支是允许的，若图标题或边条件已经限定范围，不要求扩展全部分支。'
        '摘要可以省略细节，不能把未提及内容当作错误；不能因没写某个其他副作用而指称已有归属错误。'
        '不得扩大goal范围，不要求未读取第三方实现或额外练习。不要输出泛泛建议，不要凭记忆改写原文。'
        '所有相互冲突的主张要核对源码，inferred不是错误事实的免责条款。最多4个确凿实质问题，简洁中文。')
    audits = []
    # Exercises now use source-bound focus choices and open-ended templates.
    # Jointly check the compact artifacts once, retaining blind confirmation of
    # disputed claims instead of paying twice for overlapping source context.
    groups = [catalog]
    if changed_targets is not None:
        analysis_changed = any(t.startswith('analysis.') for t in changed_targets)
        groups = [{k: v for k, v in catalog.items() if k in changed_targets or k == 'diagram'
                   or analysis_changed and k.startswith('learning.')}]
    for group in groups:
        bound_finding = create_model('LearningFinding', __base__=Finding,
                                     target=(Literal[tuple(group)], ...))
        audit_schema = create_model('LearningAudit', __base__=LearningAudit,
                                    findings=(list[bound_finding], Field(max_length=4)))
        data = {'goal': goal, 'units': group, 'fact_check': any(k.startswith('analysis.') for k in group), 'diagram_semantics': result.get('diagram_semantics', 'execution_flow'), 'repair_check': changed_targets is not None,
                **learning_source_context(snapshot, None if snapshot.get('excerpted') and any(k.startswith('analysis.') for k in group) else group)}
        for attempt in range(2):
            part = (await llm.ask('Reviewer Agent', instruction, data, audit_schema)).model_dump()
            errors = finding_errors(part['findings'], group, snapshot)
            if not errors:
                break
            if attempt:
                return {'passed': False, 'summary': '审核意见无法绑定原文或代码，停止交付而非自动通过',
                        'issues': errors, 'repair_targets': [], 'findings': [], 'audit': part,
                        'validation_failed': True, 'decisions': []}
            data['invalid_audit'] = part
            data['validation_errors'] = errors
            data['repair_check'] = True
        audits.append(part)
    findings = [f for part in audits for f in part['findings']]
    audit = {'summary': '；'.join(part['summary'] for part in audits), 'findings': findings, 'groups': audits}
    if not findings:
        return {'passed': True, 'summary': audit['summary'], 'issues': [], 'repair_targets': [],
                'findings': [], 'audit': audit, 'decisions': []}
    # A fresh reading sees only original units and source, never the proposed
    # criticisms, so it cannot simply echo an earlier reviewer's explanation.
    suspected = {f['target']: catalog[f['target']] for f in findings}
    independent_finding = create_model('LearningFinding', __base__=Finding,
                                      target=(Literal[tuple(suspected)], ...))
    independent_schema = create_model('LearningAudit', __base__=LearningAudit,
                                      findings=(list[independent_finding], Field(max_length=8)))
    independent = (await llm.ask('Reviewer Agent',
        '独立核对下面完整的成果单元，不参考其他审核者的结论。只有原文明示的肯定陈述与源码矛盾才退回。'
        '先从完整句子确定主语：不要把相邻函数的返回或yield归到另一个函数。'
        '核对python_returned_value_flows：对象提前创建、随后字段回填、最后return这三步不能合并为末尾调用创建函数。'
        '核对python_direct_call_owners给出的直接归属；不得把调用者语句归给被调用函数。'
        '不能替原文补前提；未传参数不等于配置为空。对于一定/只/不检查等断言，逐字核对限定条件与全部前置if/elif，源码存在另一分支即为反例。'
        '必须核对python_deferred_bodies：创建迭代器不执行函数体；图中读取/解析应由消费者迭代驱动。不能将这种时序错误当作简化图而通过。'
        '但data_flow图已明确不表示执行时序，只核对数据传递关系；不得把数据流箭头当控制调用先后批评。'
        '开放式练习问题要求读者查证某函数产出什么，不等于断言它产出特定类型；不需要附答案，不要求范围外细节。'
        '对真实错误返回精确target、逐字quote、简洁problem、反驳该断言的evidence与单行source_quote。'
        '没有实质矛盾则findings为空。图的标题或条件边明确限定主分支时，不要求扩展到所有分支。'
        'source_quote只摘一行源码，不带缩进或行号；summary不超过80字。',
        {'goal': goal, 'units': suspected, 'diagram_semantics': result.get('diagram_semantics', 'execution_flow'), 'independent_check': True, 'repair_check': changed_targets is not None,
         **learning_source_context(snapshot, {'units': suspected, 'references': [f['evidence'] for f in findings]})},
        independent_schema)).model_dump()
    errors = finding_errors(independent['findings'], suspected, snapshot)
    if errors:
        return {'passed': False, 'summary': '独立复核无法绑定原文或源码', 'issues': errors,
                'repair_targets': [], 'findings': [], 'audit': audit, 'independent_audit': independent,
                'validation_failed': True, 'decisions': []}
    retained = independent['findings']
    decisions = [dict(target=f['target'], quote=f['quote'], verdict=(
        'confirmed' if any(g['target'] == f['target'] and (g['quote'] in f['quote'] or f['quote'] in g['quote']) for g in retained)
        else 'replaced' if any(g['target'] == f['target'] for g in retained) else 'dismissed')) for f in findings]
    return {'passed': not retained, 'summary': '已独立复核原文及源码；' + ('仍有问题需修正' if retained else '未重现需退回的实质问题'),
            'issues': [f"{f['target']}：{f['problem']}" for f in retained],
            'repair_targets': list(dict.fromkeys(f['target'] for f in retained)), 'findings': retained,
            'audit': audit, 'independent_audit': independent, 'decisions': decisions}


def target_schema(target):
    if target == 'diagram':
        return Diagram
    if target == 'analysis.summary':
        return str
    if target.startswith('analysis.'):
        return Claim
    if target.startswith('learning.exercises.'):
        return Exercise
    if target.startswith('learning.reading_order.'):
        return ReadingStep
    raise ValueError('不支持的成果修正位置')


def set_unit(result, target, value):
    parts = target.split('.')
    parent = result
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
    parent[key] = value


async def repair_learning(llm, result, snapshot, goal, on_start=None, event=None):
    """Patch only confirmed units; caller validates and atomically commits them."""
    candidate = deepcopy(result)
    evidence_catalog = []
    for section in ('modules', 'flow', 'technologies', 'goal_answers'):
        for claim in result['analysis'].get(section, []):
            for evidence in claim['evidence']:
                if evidence not in evidence_catalog:
                    evidence_catalog.append(evidence)
    findings = result['review']['findings']
    # Preserve ownership: only each originating role edits its own artifacts.
    for prefix, role in [('analysis.', 'Code Agent'), ('learning.', 'Learning Agent'), ('diagram', 'Chart Agent')]:
        selected = [f for f in findings if f['target'].startswith(prefix)]
        if not selected:
            continue
        if on_start:
            on_start(role)
        targets = list(dict.fromkeys(f['target'] for f in selected))
        catalog = units(candidate)
        def correction_schema(target):
            original = ExerciseFocus if target.startswith('learning.exercises.') else target_schema(target)
            if not target.startswith('learning.'):
                return original
            fields = {name: (field.annotation, deepcopy(field)) for name, field in original.model_fields.items() if name not in ('evidence', 'evidence_ids')}
            from typing import Annotated
            reference = Annotated[int, Field(strict=True, ge=1, le=len(evidence_catalog))]
            fields['evidence_ids'] = (list[reference], Field(min_length=1, max_length=6))
            return create_model('LearningUnitCorrection', __base__=Strict, **fields)
        schema = create_model('LearningCorrections', __base__=Strict,
                              **{f'item{i}': (correction_schema(t), ...) for i, t in enumerate(targets)})
        data = {'goal': goal, 'diagram_semantics': result.get('diagram_semantics', 'execution_flow'), 'corrections': [dict(f, original=catalog[f['target']]) for f in selected],
                'output_map': {f'item{i}': t for i, t in enumerate(targets)},
                **learning_source_context(snapshot, {'findings': selected, 'originals': [catalog[t] for t in targets]})}
        # Keep actual upstream consumption visible to the existing call audit.
        if role == 'Learning Agent':
            data['evidence_catalog'] = [dict(e, id=i) for i, e in enumerate(evidence_catalog, 1)]
        if role in ('Learning Agent', 'Chart Agent'):
            data['analysis'] = candidate['analysis']
        if role == 'Chart Agent':
            data['learning'] = candidate['learning']
        output = (await llm.ask(role,
            '只修复已确认的问题单元，不重写整个报告。按output_map返回完整替换单元。'
            '保留仍有效的引用，可增加已读证据，不要移除下游仍需使用的分析引用。学习单元仅输出evidence_ids，必须从evidence_catalog选择编号，禁止重新编写文件或行号。'
            '练习只输出symbol与evidence_ids，symbol必须逐字出现在所选源码中；程序会生成固定类型的开放式查证问题，不要输出instructions或acceptance。'
            '图允许用标题/边标签清楚限定一个分支，或用条件边表达分流与汇合；不要串联互斥分支。'
            'data_flow图用4至6个数据形态节点（不是函数名）表示一条数据变换路径，边写实际转换操作，必须无环且无孤立节点；不画控制调用/返回，标题保留非执行时序及分支条件。'
            '不增加功能、不扩展目标、不声称运行过。', data, schema)).model_dump()
        for i, target in enumerate(targets):
            value = output[f'item{i}']
            if target == 'diagram' and candidate.get('diagram_semantics') == 'data_flow':
                value['title'] = '数据传递图（非执行时序）：' + value['title'].removeprefix('数据传递图（非执行时序）：')
            if target.startswith('learning.exercises.'):
                value = exercise_from_focus(value, int(target.split('.')[-1]), evidence_catalog, snapshot)
            elif target.startswith('learning.'):
                value['evidence'] = [evidence_catalog[j-1] for j in dict.fromkeys(value.pop('evidence_ids'))]
            elif target.startswith('analysis.') and target != 'analysis.summary':
                # These citations also form the downstream evidence catalog.
                # Correct text without invalidating already-consumed source IDs.
                value['evidence'] = deepcopy(catalog[target]['evidence']) + [
                    e for e in value['evidence'] if e not in catalog[target]['evidence']]
            set_unit(candidate, target, value)
        if event:
            event(role, 'completed', '局部修正输出已生成，等待校验及复核', {'targets': targets})
    candidate['analysis']['stack'] = [c['title'] for c in candidate['analysis']['technologies']]
    if any(f['target'] == 'analysis.goal_answers.0' for f in findings):
        candidate['analysis']['summary'] = candidate['analysis']['goal_answers'][0]['explanation']
    return candidate
