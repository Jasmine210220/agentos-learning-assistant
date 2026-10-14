"""Bounded, read-only learning workflow; reuses the existing runner and artifacts."""
import asyncio
import json
import re
from copy import deepcopy
from typing import Annotated, Literal
from pydantic import Field, create_model
from .audit import digest, trace_model
from .github import read_repository
from .llm import learning_source_context, ModelBudgetExceeded
from .models import LearningAnalysis, LearningPlan, Diagram, Strict, ScopeAssessment
from .learning_review import audit_learning, repair_learning
from .learning_exercises import ExerciseFocus, exercise_from_focus
from .learning_diagram import validate_diagram_semantics, requests_execution_diagram, DIAGRAM_BOUNDARY, reading_record
from .learning_reading import select_learning_excerpt, include_route_siblings, trim_adjacent_evidence, analysis_citation_contract, resolve_analysis_citations
from .workflow import Stopped, select_repository_files, validate_evidence, diagram_source


def learning_errors(learning, analysis, snapshot):
    errors = validate_evidence(learning, snapshot)
    indices = learning.get('flow_indices', [])
    if not indices or len(indices) != len(set(indices)) or any(type(i) is not int or not 1 <= i <= len(analysis['flow']) for i in indices):
        errors.append('学习路线引用了无效或重复的分析流程编号')
    if any(not 1 <= step['module_index'] <= len(analysis['modules']) for step in learning['reading_order']):
        errors.append('阅读顺序引用了不存在的上游模块')
    upstream = [e for key in ('modules', 'flow', 'technologies', 'goal_answers') for item in analysis.get(key, []) for e in item['evidence']]
    for item in learning['reading_order'] + learning['exercises']:
        for e in item['evidence']:
            if not any(e['path'] == source['path'] and source['start_line'] <= e['start_line'] <= e['end_line'] <= source['end_line'] for source in upstream):
                errors.append('学习引用不属于上游解读的证据范围')
    return errors


async def make_learning(llm, analysis, background, goal, feedback, snapshot):
    catalog = []
    for key in ('modules', 'flow', 'technologies', 'goal_answers'):
        for item in analysis.get(key, []):
            for evidence in item['evidence']:
                if evidence not in catalog:
                    catalog.append(evidence)
    reference = Annotated[int, Field(strict=True, ge=1, le=len(catalog))]
    source_lines = {f['path']: f['content'].splitlines() for f in snapshot['files']}
    focus_catalog = []
    for index, evidence in enumerate(catalog, 1):
        code = '\n'.join(source_lines[evidence['path']][evidence['start_line']-1:evidence['end_line']])
        symbols = list(dict.fromkeys(re.findall(r'\b[A-Za-z_$][A-Za-z0-9_$]*\b', code)))[:48]
        focus_catalog.append(dict(evidence, id=index, available_symbols=symbols))
    focus_choices = []
    seen = set()
    import keyword
    for item in focus_catalog:
        code = '\n'.join(source_lines[item['path']][item['start_line']-1:item['end_line']])
        preferred = re.findall(r'\b(?:def|class)\s+(\w+)', code) + re.findall(r'\b([A-Za-z_]\w*)\s*\(', code)
        names = [s for s in dict.fromkeys(preferred + item['available_symbols']) if not keyword.iskeyword(s) and s not in {'self','str','int','list','dict','None','True','False'}]
        for symbol in names[:8]:
            if (symbol, item['path']) not in seen:
                seen.add((symbol, item['path']))
                focus_choices.append(dict(id=len(focus_choices)+1, symbol=symbol, evidence_id=item['id']))
    if not focus_choices:
        raise ValueError('上游证据中没有可用于练习的代码符号')
    reading = create_model('LearningReading', __base__=Strict,
        title=(str, Field(min_length=1, max_length=100)), reason=(str, Field(min_length=1, max_length=400)),
        module_index=(int, Field(strict=True, ge=1, le=len(analysis['modules']))),
        concepts=(list[str], Field(min_length=1, max_length=4)), evidence_ids=(list[reference], Field(min_length=1, max_length=6)))
    exercise = create_model('LearningExerciseFocus', __base__=Strict,
        focus_id=(int, Field(strict=True, ge=1, le=len(focus_choices))))
    flow_index = Annotated[int, Field(strict=True, ge=1, le=len(analysis['flow']))]
    schema = create_model('LearningDraft', __base__=Strict,
        background=(str, Field(min_length=1, max_length=500)), objective=(str, Field(min_length=1, max_length=400)),
        flow_indices=(list[flow_index], Field(min_length=1, max_length=8)),
        reading_order=(list[reading], Field(min_length=1, max_length=3)),
        exercises=(list[exercise], Field(min_length=3, max_length=3)), limitations=(list[str], ...))
    draft = await llm.ask('Learning Agent',
        '根据上游analysis和学习基础制定3步阅读顺序及恰好三个由浅入深的练习。每步reason不超过80字，concepts仅写2个知识主题，不重复解释源码。'
        'module_index为analysis.modules的一基编号；flow_indices为绘图使用的analysis.flow一基编号，按数据变换路径且不重复。'
        '每步只选最关键的1至2个evidence_ids，最多6个；只能从evidence_catalog选择整数编号，不能自己写文件或行号。concepts写具体知识重点。'
        '三个练习只输出focus_id，从focus_choices选择；每个编号已绑定真实符号和上游来源，不自己组合符号与证据。不输出答案、指令或自查条件。优先选函数或变量名。'
        '三个焦点分别服务于：1追踪参数与数据来源；2核对条件与去向；3区分返回数据与副作用。程序根据这三类任务生成开放式提问。'
        '结合用户基础选择最有学习价值的符号，不选择未读实现中的名字。reading_order只介绍阅读重点，不预测具体运行结果。',
        {'analysis': analysis, 'background': background, 'goal': goal, 'feedback': feedback,
         'evidence_catalog': [dict(e, id=i) for i,e in enumerate(catalog,1)], 'focus_choices': focus_choices,
         **learning_source_context(snapshot, analysis)}, schema)
    value = draft.model_dump()
    for item in value['reading_order']:
        item['evidence'] = [catalog[i-1] for i in dict.fromkeys(item.pop('evidence_ids'))]
    focuses = [focus_choices[f['focus_id']-1] for f in value['exercises']]
    value['exercises'] = [exercise_from_focus(dict(symbol=f['symbol'], evidence_ids=[f['evidence_id']]), i, catalog, snapshot) for i,f in enumerate(focuses)]
    return LearningPlan.model_validate(value)


def report_learning(task, result):
    snapshot, analysis, learning = (result.get(k, {}) for k in ('snapshot', 'analysis', 'learning'))
    lines = ['# 项目学习报告', '', f"仓库：{task['repository']}", f"提交：{snapshot.get('commit', '尚未读取')}",
             f"目标：{task['goal']}", f"任务状态：{task['status']}", '验证边界：静态阅读；未运行仓库代码，练习未执行或评分。', '',
             '## 项目解读', analysis.get('summary', '尚未生成')]
    def evidence(items):
        return [f"- 依据：{e['path']}:{e['start_line']}-{e['end_line']}" for e in items]
    for key, title in [('goal_answers', '针对学习目标'), ('technologies', '技术栈依据'), ('modules', '模块职责'), ('flow', '主要流程')]:
        lines += ['', '## ' + title]
        for item in analysis.get(key, []):
            lines += ['### ' + item['title'] + ('（推断）' if item.get('inferred') else ''), item['explanation'], *evidence(item['evidence'])]
    lines += ['', '## 阅读路线', learning.get('background', ''), learning.get('objective', '')]
    for i, step in enumerate(learning.get('reading_order', []), 1):
        lines += [f"### {i}. {step['title']}", step['reason'], '知识重点：' + '、'.join(step['concepts']), *evidence(step['evidence'])]
    lines += ['', '## 三个练习（未执行）']
    for i, exercise in enumerate(learning.get('exercises', []), 1):
        lines += [f"### {i}. {exercise['title']}", exercise['objective'], *['- ' + s for s in exercise['instructions']],
                  '自查条件：', *['- ' + s for s in exercise['acceptance']], *evidence(exercise['evidence'])]
    lines += ['', '## 范围与限制', reading_record(snapshot), '模型范围判断（待人工核对）：' + analysis.get('scope_reason', ''), *analysis.get('limitations', []), *learning.get('limitations', []),
              ('本次模型按所选代码段阅读，完整文件仅作为可查看的来源快照。已读范围：' + '；'.join(
                  f["path"] + ' ' + '、'.join(f'{a}-{b}' for a,b in f.get('analysis_ranges', []))
                  for f in snapshot.get('files', []) if f.get('analysis_ranges'))) if snapshot.get('excerpted') else '',
              f"读取 {len(snapshot.get('files', []))} 个文件；仅解释已读取范围。", '', '## 审核',
              '自动审核未发现剩余问题（不代表人工验收）' if task['status'] == 'succeeded' else '未完成或未通过', result.get('review', {}).get('summary', ''),
              *result.get('review', {}).get('issues', []), task.get('error') or '', '', '## 数据传递图',
              '箭头表示选定路径中的数据传递，不表示完整调用时序。' if result.get('diagram_semantics') == 'data_flow' else '箭头表示主要调用与控制流程。',
              '```mermaid', result.get('mermaid', ''), '```']
    return '\n'.join(lines)


async def run_learning(runner, task_id):
    store = runner.store
    task = store.get(task_id)
    result = {'schema_version': 1, 'mode': 'learn'}
    previous_result = None
    workspace = store.workspace(task_id)
    def check():
        if task_id in runner.stops:
            raise Stopped()
    def event(role, kind, summary, details=None):
        store.event(task_id, role, kind, summary, details)
    def save():
        store.save_json(task_id, 'result.json', result)
        store.update(task_id, result=result)
    def phase(status, role, summary):
        check()
        store.update(task_id, status=status)
        event(role, 'started', summary)
    def handoff(sender, receiver, artifact):
        event(sender, 'handoff', f'交接给 {receiver}', {'sender': sender, 'receiver': receiver,
              'artifact': artifact, 'artifact_hash': digest(result[artifact]), 'evidence': 'result.json'})
    def finish(status, message=''):
        save()
        final = {**store.get(task_id), 'status': status, 'error': message or None}
        temporary = workspace / 'report.md.tmp'
        temporary.write_text(report_learning(final, result), encoding='utf-8')
        temporary.replace(workspace / 'report.md')
        store.update(task_id, status=status, error=message or None)
        event('System', status, message or '成果已保存，自动审核未发现剩余问题；未人工验收或执行代码')
    llm = runner.llm_factory(lambda usage: store.update(task_id, usage=usage), check)
    llm.reserve_request_budget = True
    llm.learning_mode = True
    if hasattr(llm, 'config'):
        llm.config['model'] = task['payload'].get('config', {}).get('model', llm.config['model'])
    trace_model(llm, event)
    llm.on_retry = lambda role, errors: event(role, 'retry', '调用或输出校验失败，进行有限重试', {'errors': errors})
    llm.on_normalize = lambda role, fields: event(role, 'protocol_normalized', '已本地移除协议外字段，未重新调用模型', {'removed_fields': fields})
    try:
        async with runner.queue:
            check()
            async with asyncio.timeout(runner.timeout_seconds):
                phase('planning', 'Supervisor', '固定学习交付范围')
                result['diagram_semantics'] = 'data_flow'
                result['diagram_boundary'] = DIAGRAM_BOUNDARY
                if requests_execution_diagram(task['goal']):
                    finish('needs_attention', DIAGRAM_BOUNDARY + ' 本次请求未执行；请将目标明确改为理解数据传递后重新提交。')
                    return
                result['plan'] = {'objective': task['goal'], 'background': task['payload'].get('learner_background', ''),
                                  'acceptance': ['有依据的项目解读', '阅读路线与三个练习', '一张已渲染的主数据传递图'],
                                  'execution': '固定串行流程；不搜索项目、不执行代码、不自动修改代码'}
                save()
                event('Supervisor', 'completed', '使用固定流程，不增加自主规划调用', result['plan'])
                handoff('Supervisor', 'Code Agent', 'plan')
                phase('reading', 'Code Agent', '固定仓库版本，围绕一个流程读取源码')
                async def select(files):
                    selection = await select_repository_files(llm, task['goal'], files, compact=True)
                    sizes = {f['path']: f['bytes'] for f in files}
                    paths, used = [], 0
                    candidates = include_route_siblings(selection.paths, files)
                    for path in candidates:
                        if len(paths) < 20 and used + sizes[path] <= 140000:
                            paths.append(path)
                            used += sizes[path]
                    if not paths:
                        raise ValueError('目标文件超出本轮快照读取预算，请选择更小的模块或项目')
                    event('Code Agent', 'scope', '本轮学习读取范围', {'paths': paths, 'bytes': used, 'reason': selection.reason,
                          'route_candidates': [p for p in paths if p not in selection.paths],
                          'omitted': [p for p in candidates if p not in paths]})
                    return paths
                if runner.reader is read_repository:
                    snapshot = await runner.reader(task['repository'], task['payload'].get('ref', ''), event, check, selector=select)
                else:
                    snapshot = await runner.reader(task['repository'], task['payload'].get('ref', ''), event, check)
                snapshot = await select_learning_excerpt(llm, snapshot, task['goal'], event)
                store.save_json(task_id, 'snapshot.json', snapshot)
                result['snapshot'] = {**snapshot, 'files': [{k: v for k, v in f.items() if k != 'content'} for f in snapshot['files']]}
                sources = learning_source_context(snapshot)
                save()
                feedback = []
                for attempt in range(min(1, runner.max_repairs) + 1):
                    if attempt:
                        previous_result = deepcopy(result)
                        store.save_json(task_id, f'learning-attempt-{attempt}.json', result)
                        phase('reviewing', 'Reviewer Agent', '仅修正确认的问题，保留其他成果')
                        # Recheck changed units and dependent artifacts; previously
                        # approved untouched units do not need another full pass.
                        llm.reserved_tokens = 20000
                        llm.reserved_calls = 2
                        try:
                            candidate = await repair_learning(llm, result, snapshot, task['goal'],
                                on_start=lambda role: phase({'Code Agent': 'analyzing', 'Learning Agent': 'teaching', 'Chart Agent': 'drawing'}[role], role, '修正已确认的问题单元'), event=event)
                        finally:
                            llm.reserved_tokens = 0
                            llm.reserved_calls = 0
                        LearningAnalysis.model_validate(candidate['analysis'])
                        LearningPlan.model_validate(candidate['learning'])
                        candidate_diagram = Diagram.model_validate(candidate['diagram'])
                        validate_diagram_semantics(candidate_diagram, candidate.get('diagram_semantics', 'execution_flow'))
                        errors = validate_evidence(candidate, snapshot) + learning_errors(candidate['learning'], candidate['analysis'], snapshot)
                        if errors:
                            finish('needs_attention', '局部修正未通过来源校验，保留上一轮成果：' + '；'.join(errors[:2]))
                            return
                        if candidate['diagram'] != result['diagram']:
                            phase('drawing', 'Chart Agent', '实际渲染修正后的数据传递图')
                            staging = workspace / f'render-revision-{attempt}'
                            staging.mkdir(exist_ok=True)
                            candidate['mermaid'] = diagram_source(candidate_diagram)
                            await runner.renderer(candidate['mermaid'], staging)
                            check()
                            svg = staging / 'diagram.svg'
                            if not svg.is_file() or '<svg' not in svg.read_text(encoding='utf-8'):
                                raise ValueError('修正图未实际渲染，保留上一轮成果')
                            (staging / 'diagram.mmd').write_text(candidate['mermaid'], encoding='utf-8')
                            svg.replace(workspace / 'diagram.svg')
                            (staging / 'diagram.mmd').replace(workspace / 'diagram.mmd')
                        candidate['review'] = {'passed': False, 'summary': '局部修正已完成，等待复核', 'issues': ['尚未完成修正后复核'], 'repair_targets': []}
                        result = candidate
                        save()
                        event('Reviewer Agent', 'repair', '已应用局部修正，其他单元保持不变', {'targets': previous_result['review']['repair_targets']})
                    else:
                        phase('analyzing', 'Code Agent', '解释一个主要流程，核对源码覆盖范围')
                        analysis_schema, citation_catalog = analysis_citation_contract(snapshot)
                        analysis = await llm.ask('Code Agent',
                            '本角色只输出LearningAnalysis字段，不输出reading_order、exercises、diagram；它们由下游角色完成。'
                            '若输入含evidence_catalog，每项只用evidence_ids选择编号，不自己编写行号；编号范围都是实际已读代码，多段依据分别选择编号，不把中间未读段合成一条引用。'
                            '只解释已读源码中的一个主要请求或调用流程。最多3个模块、5个流程步骤、2个核心技术；summary不超过120字，每项explanation不超过120字，每项1至2处关键引用。'
                            'technologies 每项 title 为 stack 中的技术名称且有依赖或代码依据。不要输出Bug，risks必须为空。'
                            'goal_answers必须回答用户目标中明确要求解释的代码问题（1至2项）。第一项必须概括目标的完整起点到终点，作为聊天摘要；不要用用户没问的配置项或参数取代主问题。每项给直接答案和源码依据，不能只说已覆盖或留给练习。阅读路线/练习/图由下游完成。'
                            '每个事实引用精确代码；未读实现不得断言，推断逐条标记。流程区分启动装配与请求执行。'
                            '先核对路由挂载条件及全部已读消息入口；用户未指定演示接口时，不得用受限的演示路由替代真实业务入口。最终回复要区分模型结构化分析、确定性文本组合与HTTP响应。'
                            '描述函数时区分它直接返回的数据、调用者随后组装的数据及副作用发生位置；只把return、赋值和状态修改归给实际包含该语句的函数。'
                            'python_returned_value_flows给出返回对象创建、字段回填和return的实际行号：如果对象提前创建后仅回填字段，不能声称创建函数在最后再次调用。'
                            '条件分支保留所有前置条件及配置优先级；未给定的配置不得默认为空。'
                            '遵守源码附带的python_deferred_bodies语法事实：区分创建迭代器、消费者迭代和函数体执行。'
                            '如果缺少用户指定流程的关键实现，scope_complete=false，scope_reason说明缺少哪些文件或步骤；'
                            '本任务是主链学习导航：入参、关键调用、回复生成与最终响应赋值有依据即可闭合；未展开的辅助策略分支、依赖装配和供应商SDK内部只记limitations，不要求逐个展开。正常返回的关键调用缺失才判范围不足。'
                            '覆盖范围以用户指定起终点为准，第三方库内部实现、安装入口若不在该流程内，不作为缺失条件。'
                            '不能凭常识补齐。范围足够时true。明确未执行代码。feedback只用于修正错误，不扩大范围。',
                            {'goal': task['goal'], 'background': result['plan']['background'], 'feedback': feedback, **sources,
                             **({'evidence_catalog': [dict(e, id=i) for i,e in enumerate(citation_catalog,1)]} if citation_catalog else {})}, analysis_schema)
                        analysis = resolve_analysis_citations(analysis.model_dump(), citation_catalog)
                        result['analysis'] = analysis.model_dump()
                        narrowed = trim_adjacent_evidence(result['analysis'], snapshot)
                        if narrowed:
                            event('Code Agent', 'citation_normalized', '已将少量相邻行越界引用收窄至实际已读范围，仍待语义审核', {'changes': narrowed})
                        # Technology claims are the canonical, cited list. Avoid two
                        # independently generated names disagreeing in presentation.
                        result['analysis']['stack'] = [c.title for c in analysis.technologies]
                        if analysis.goal_answers:
                            # One canonical answer instead of an independently
                            # paraphrased overview contradicting the cited body.
                            result['analysis']['summary'] = analysis.goal_answers[0].explanation
                        save()
                        errors = validate_evidence(result['analysis'], snapshot)
                        if analysis.risks:
                            errors.append('学习流程不得附加未经要求的 Bug 判断')
                        if not analysis.goal_answers:
                            errors.append('未回答用户明确提出的代码学习问题')
                        if errors:
                            raise ValueError('解读成果校验未通过：' + '；'.join(errors[:3]))
                        event('Code Agent', 'completed', '项目解读已保存，尚待最终审核', {'artifact': 'analysis'})
                        if not analysis.scope_complete:
                            event('Reviewer Agent', 'scope', '复核读取范围是否满足用户指定起终点')
                            scope = await llm.ask('Reviewer Agent',
                                '只复核源码范围，不扩写目标。先精确读goal中的起点、终点和排除项。'
                                '分析声称缺少的内容若已被用户排除（例如安装入口、第三方内部），不能据此判定不足。'
                                '如果已读代码能解释用户指定起终点之间的主流程，scope_complete=true，并引用依据；'
                                '确实缺少该范围内关键步骤则false。不能依赖常识填补未读实现。',
                                {'goal': task['goal'], 'analysis': result['analysis'], **learning_source_context(snapshot, result['analysis'])}, ScopeAssessment)
                            result['scope_review'] = scope.model_dump()
                            if validate_evidence(result['scope_review'], snapshot):
                                raise ValueError('范围复核引用未通过校验')
                            result['analysis']['scope_complete'] = scope.scope_complete
                            result['analysis']['scope_reason'] = scope.reason
                            save()
                            if not scope.scope_complete:
                                finish('needs_attention', '已读范围不足以解释指定流程：' + scope.reason)
                                return
                        event('Code Agent', 'artifact', '解读成果已完成来源及范围规范化', {'artifact': 'analysis', 'output_hash': digest(result['analysis'])})
                        handoff('Code Agent', 'Learning Agent', 'analysis')
                        phase('teaching', 'Learning Agent', '依据项目解读制定阅读路线和三个练习')
                        learning_feedback = feedback
                        for learning_attempt in range(2):
                            try:
                                learning = await make_learning(llm, result['analysis'], result['plan']['background'], task['goal'], learning_feedback, snapshot)
                                break
                            except ValueError as exc:
                                if learning_attempt:
                                    raise
                                learning_feedback = str(exc)
                                event('Learning Agent', 'retry', '学习成果引用校验失败，按具体错误重试一次', {'error': learning_feedback})
                        result['learning'] = learning.model_dump()
                        errors = learning_errors(result['learning'], result['analysis'], snapshot)
                        if errors:
                            raise ValueError('学习成果校验未通过：' + '；'.join(errors[:3]))
                        save()
                        event('Learning Agent', 'artifact', '来源编号已映射到上游代码依据', {'artifact': 'learning', 'output_hash': digest(result['learning'])})
                        event('Learning Agent', 'completed', '阅读顺序与三个练习已保存（未执行）', {'artifact': 'learning'})
                        handoff('Learning Agent', 'Chart Agent', 'learning')
                        phase('drawing', 'Chart Agent', '绘制学习路线选定的主要流程并实际渲染')
                        result['rendered'] = False
                        result['diagram_semantics'] = 'data_flow'
                        result.pop('review', None)
                        save()
                        data_instruction = (
                            '生成一张简洁的数据变换图，不是函数调用图。只画4至6个节点，每个节点表示一种具体数据形态（例如文本、记录、键值对、映射），不要用函数名作为节点。'
                            '每条边用产生目标数据的实际函数/操作作标签，附上该转换的源码证据。数据从生产者流向消费者；不得画函数调用/返回、迭代控制或模块依赖。'
                            '只选一个有意义的完整数据路径，标题明确路径的前置条件，例如某配置为真。不要把互斥分支串成链。'
                            '按转换顺序编号n1,n2等，所有节点组成一条有向无环路径，不能有孤立节点。子函数的中间计算可合并为一个转换，不展开往返调用。'
                            '节点和边label最多40字符。必须有逐项源码证据，未验证推断标记inferred。根据analysis与learning选定的范围生成，不增加未读处理。'
                            'previous_diagram和render_feedback是上次真实校验结果，若提供则只修正这些问题。')
                        for render_attempt in range(2):
                            diagram = await llm.ask('Chart Agent',
                                data_instruction,
                                {'analysis': result['analysis'], 'learning': result['learning'], 'diagram_semantics': result['diagram_semantics'], 'render_feedback': result.get('render_error'), 'previous_diagram': result.get('diagram') if render_attempt else None,
                                 **learning_source_context(snapshot, result['analysis'])}, Diagram)
                            result['diagram'] = diagram.model_dump()
                            if result['diagram_semantics'] == 'data_flow':
                                diagram.title = '数据传递图（非执行时序）：' + diagram.title.removeprefix('数据传递图（非执行时序）：')
                                result['diagram'] = diagram.model_dump()
                            errors = validate_evidence(result['diagram'], snapshot)
                            if errors:
                                raise ValueError('数据传递图证据未通过：' + '；'.join(errors[:3]))
                            result['mermaid'] = diagram_source(diagram)
                            (workspace / 'diagram.mmd').write_text(result['mermaid'], encoding='utf-8')
                            save()
                            try:
                                validate_diagram_semantics(diagram, result['diagram_semantics'])
                                await runner.renderer(result['mermaid'], workspace)
                                check()
                                if not (workspace / 'diagram.svg').is_file() or '<svg' not in (workspace / 'diagram.svg').read_text(encoding='utf-8'):
                                    raise ValueError('实际 SVG 文件缺失或无效')
                                result['rendered'] = True
                                result.pop('render_error', None)
                                break
                            except ValueError as exc:
                                result['render_error'] = str(exc)[:300]
                                if render_attempt:
                                    raise
                                event('Chart Agent', 'repair', '实际渲染失败，进行一次修正', {'error': result['render_error']})
                        save()
                        event('Chart Agent', 'completed', '数据传递图实际渲染完成', {'artifact': 'diagram.svg'})
                        handoff('Chart Agent', 'Reviewer Agent', 'diagram')
                    phase('reviewing', 'Reviewer Agent', '核对结论、阅读路线、练习与图的一致性')
                    try:
                        result['review'] = await audit_learning(llm, result, snapshot, task['goal'],
                            changed_targets=previous_result['review']['repair_targets'] if attempt else None)
                    except ModelBudgetExceeded:
                        raise
                    except ValueError:
                        result['review'] = {'passed': False, 'outcome': 'invalid_audit', 'summary': '审核输出无效，未得到可用结论',
                                            'issues': ['审核协议或引用校验失败，已有成果保留'], 'repair_targets': [], 'validation_failed': True}

                    review = result['review']
                    if attempt and review['passed']:
                        review['outcome'] = 'corrected_and_rechecked'
                        review['summary'] = '已确认错误已局部修正并经自动复核；不代表人工验收或内容绝对正确。' + review['summary']
                        review['prior_review'] = previous_result['review']
                    result['checks'] = {'evidence_valid': True, 'diagram_rendered': result['rendered'], 'errors': [], 'round': attempt + 1,
                                        'review_scope': 'changed_units_and_dependents' if attempt else 'all_units'}
                    save()
                    event('Reviewer Agent', 'completed' if review['passed'] else 'rejected', review['summary'], review)
                    if review['passed']:
                        if not (workspace / 'diagram.svg').is_file() or (workspace / 'diagram.mmd').read_text(encoding='utf-8') != result['mermaid']:
                            raise ValueError('交付前图表文件校验失败，不能标记成功')
                        finish('succeeded')
                        return
                    if review.get('unresolved_findings'):
                        finish('needs_attention', '原疑点未重现但证据不足以关闭，争议保留；不自动通过。')
                        return
                    if review.get('validation_failed'):
                        finish('needs_attention', '审核依据未确认，已有成果保留，不自动修正或通过')
                        return
                    feedback = review['issues'] or [review['summary']]
                    if attempt < min(1, runner.max_repairs):
                        event('Reviewer Agent', 'handoff', '交接给存在已确认问题的角色', {'sender': 'Reviewer Agent', 'receiver': 'Code / Learning / Chart Agent', 'artifact': 'review', 'targets': review['repair_targets'], 'artifact_hash': digest(review)})
                finish('needs_attention', '有限修正后仍未通过审核，已有成果保留；请缩小目标或重新发起。')
    except (asyncio.CancelledError, Stopped):
        finish('cancelled' if task_id in runner.stops else 'interrupted', '学习任务已停止，已有成果保留')
    except TimeoutError:
        finish('failed', '达到学习任务时限，已有成果保留')
    except ModelBudgetExceeded:
        prior = result.get('review')
        result['review'] = {'passed': False, 'outcome': 'budget_exhausted', 'summary': '预算不足，审核未完成', 'issues': ['不能将未完成复核的成果当作通过'], 'repair_targets': [], 'prior_review': prior}
        if not result.get('analysis') and previous_result:
            result = previous_result
        finish('needs_attention' if result.get('analysis') else 'failed', '调用预算不足，已有成果未通过完整验收；请缩小学习范围')
    except Exception as exc:
        message = str(exc)[:500] if isinstance(exc, ValueError) else f'学习任务失败（{type(exc).__name__}），请检查网络或运行环境'
        key = getattr(llm, 'config', {}).get('key', '')
        if key:
            message = message.replace(key, '[已隐藏]')
        finish('failed', message)
