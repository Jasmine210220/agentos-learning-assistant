import asyncio
import json
import os
from pathlib import Path
from typing import TypedDict, Annotated
from pydantic import Field, create_model
from langgraph.graph import StateGraph, START, END
from .github import read_repository
from .llm import DeepSeek, MODEL, MAX_CALLS, MAX_TOTAL_TOKENS, ModelBudgetExceeded, source_context
from .models import Plan, Analysis, Diagram, Review, TERMINAL, FileSelection, Strict
from .audit import digest, trace_model


PROJECT = Path(__file__).resolve().parent.parent


class Stopped(Exception):
    pass


async def select_repository_files(llm, goal, files, compact=False):
    """Resolve validated catalog IDs locally; the model never supplies paths."""
    if compact:
        from pathlib import PurePosixPath
        source_files = [f for f in files if not any(part.lower() in
                        {'tests', 'test', 'docs', 'docs_src', 'examples', 'benchmarks', 'tools'}
                        for part in PurePosixPath(f['path']).parts[:-1])
                        and PurePosixPath(f['path']).suffix.lower() not in {'.md', '.txt', '.css', '.scss', '.html'}]
        if source_files:
            files = source_files
    if not files:
        raise ValueError('GitHub 仓库没有符合读取限制的源码文件，无法进行代码分析')
    file_id = Annotated[int, Field(strict=True, ge=1, le=len(files))]
    schema = create_model('RepositoryFileSelection', __base__=Strict,
        file_ids=(list[file_id], Field(min_length=1, max_length=24)),
        reason=(str, ...))
    choice = await llm.ask('Code Agent',
        ('根据学习目标选择直接参与主流程的源码，最多12个文件，总量不超过140000字节；后续会在固定预算内选择关键代码段，不必因一个核心文件较大而遗漏它。覆盖真实对外路由、服务、调度、模型调用及最终回复/返回。入口可能分散在不同路由文件，不要只凭文件名选演示入口。README、依赖清单、测试和日志美化不影响主链时不要选。不要追求全仓覆盖。' if compact else '根据目标围绕一条主要请求选择最关键的8–12个文件，最多16个且总字节不超过70000。') +
        '只返回给定可读目录的 file_ids 整数编号，顺序决定优先级，不能编造路径或编号。'
        '文件名不能证明业务职责：不要把企业/租户等路由文件臆断为管理旁路。对于消息请求目标，优先覆盖同级可能的API入口，再选分析器、响应组合器和核心运行时；知识检索等辅助内部实现可暂不展开。'
        '目录可能因文件大小或安全限制而不包含某些文件，不得凭记忆补全；在 reason 说明影响。'
        '优先实际入口、路由、服务及所需类型；README和依赖清单各1个即可。'
        '不要让测试、工具、自动生成代码或无关示例占用名额。',
        {'goal': goal, 'source': 'GitHub remote repository',
         'files': [dict(f, id=i) for i, f in enumerate(files, 1)]}, schema)
    return FileSelection(paths=[files[i-1]['path'] for i in dict.fromkeys(choice.file_ids)], reason=choice.reason)


def bounded_analysis_files(paths, files):
    """Reserve room for full-source review and repairs within the task budget."""
    sizes = {f['path']: max(0, f.get('bytes', 0)) for f in files}
    chosen, omitted, total = [], [], 0
    for path in dict.fromkeys(paths):
        size = sizes.get(path, 0)
        if len(chosen) >= 16 or total + size > 70000:
            omitted.append(path)
        else:
            chosen.append(path)
            total += size
    if not chosen:
        raise ValueError('选定文件超出本轮分析读取预算，请指定更小的模块')
    return chosen, omitted


def validate_evidence(value, snapshot):
    files = {f["path"]: f for f in snapshot["files"]}
    errors = []
    def walk(item):
        if isinstance(item, dict):
            if {"path", "start_line", "end_line"} <= item.keys():
                f = files.get(item["path"])
                if not f or not (1 <= item["start_line"] <= item["end_line"] <= f["lines"]):
                    errors.append(f"无效证据：{item['path']}:{item['start_line']}-{item['end_line']}")
                elif 'analysis_ranges' in f and not any(start <= item['start_line'] <= item['end_line'] <= end for start, end in f['analysis_ranges']):
                    errors.append(f"证据超出模型已读代码段：{item['path']}:{item['start_line']}-{item['end_line']}")
            for v in item.values(): walk(v)
        elif isinstance(item, list):
            for v in item: walk(v)
    walk(value)
    return errors


def diagram_source(diagram):
    ids = [n.id for n in diagram.nodes]
    if len(ids) != len(set(ids)): raise ValueError("图节点 ID 重复")
    # Quoted plain-text labels: replace syntax delimiters without introducing
    # HTML entities (Mermaid strict mode would visibly double-escape them).
    def label(text):
        return text.translate(str.maketrans({'"':'＂','[':'［',']':'］','|':'｜','<':'＜','>':'＞','&':'＆','#':'＃','`':'｀','\\':'＼','\n':' ','\r':' '}))
    lines = ["flowchart TD"]
    for node in diagram.nodes:
        lines.append(f'    {node.id}["{label(node.label)}"]')
    for edge in diagram.edges:
        if edge.source not in ids or edge.target not in ids: raise ValueError("图连线引用了不存在的节点")
        if edge.inferred:
            lines.append(f'    {edge.source} -. "{label("推断：" + edge.label)}" .-> {edge.target}')
        else:
            lines.append(f'    {edge.source} -->|"{label(edge.label)}"| {edge.target}')
    return "\n".join(lines)


async def render_mermaid(source, workspace):
    runtime = os.environ.get("AGENTOS_ELECTRON") or str(PROJECT / "node_modules/electron/dist/electron.exe")
    if not Path(runtime).exists(): raise ValueError("缺少流程图渲染运行环境，请安装项目依赖")
    request = workspace / "render-input.json"
    output = workspace / "render-output.json"
    request.write_text(json.dumps({"source": source}), encoding="utf-8")
    if output.exists(): output.unlink()
    if os.environ.get("AGENTOS_PACKAGED") == "1":
        command = [runtime, "--render-mermaid", str(request), str(output)]
    else:
        command = [runtime, str(PROJECT / "electron/render.cjs"), str(request), str(output)]
    env = dict(os.environ)
    # The renderer does not need platform or model credentials.
    for key in list(env):
        if any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")) or key == "ELECTRON_RUN_AS_NODE":
            env.pop(key, None)
    process = await asyncio.create_subprocess_exec(*command, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout=35)
        except TimeoutError:
            raise ValueError("流程图渲染超时（35 秒），已有源数据保留") from None
        if not output.exists(): raise ValueError("流程图渲染失败，渲染进程没有返回结果")
        result = json.loads(output.read_text(encoding="utf-8"))
        if not result.get("ok"): raise ValueError("Mermaid 渲染失败：" + str(result.get("error", "未知错误"))[:300])
        svg = result["svg"]
        if "<svg" not in svg or len(svg) < 100: raise ValueError("图表渲染返回了无效 SVG")
        (workspace / "diagram.svg").write_text(svg, encoding="utf-8")
        return svg
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def report_markdown(task, result):
    snapshot = result.get("snapshot", {})
    analysis = result.get("analysis", {})
    review = result.get("review", {})
    lines = ["# 仓库分析报告", "", f"仓库：{task['repository']}", f"提交：{snapshot.get('commit', '尚未读取')}",
             f"目标：{task['goal']}", f"任务状态：{task['status']}", "", "## 项目概览", analysis.get("summary", "尚未生成"), "",
             "技术栈：" + "、".join(analysis.get("stack", [])), ""]
    for key, title in [("modules", "模块职责"), ("flow", "关键流程")]:
        lines += [f"## {title}", ""]
        for item in analysis.get(key, []):
            lines += [f"### {item['title']}" + ("（推断）" if item.get("inferred") else ""), item["explanation"]]
            for e in item["evidence"]:
                lines.append(f"- 证据：{e['path']}:{e['start_line']}-{e['end_line']}")
            lines.append("")
    lines += ["## 风险与修改建议", ""]
    for item in analysis.get("risks", []):
        lines += [f"### {item['title']}", f"验证状态：{item.get('verification', '待验证风险')}（未执行代码）"]
        for key, label in [("severity", "级别"), ("trigger", "触发条件"), ("cause", "原因"), ("impact", "影响"), ("suggestion", "修改建议")]:
            lines.append(f"- {label}：{item.get(key, '')}")
        for e in item.get("evidence", []):
            lines.append(f"- 证据：{e['path']}:{e['start_line']}-{e['end_line']}")
        if item.get("patch"):
            lines += ["", "建议补丁（未应用）：", "```diff", item["patch"], "```"]
        lines.append("")
    if not analysis.get("risks"):
        lines += ["本次已读范围内未提出有充分依据的问题；不代表全仓无 Bug。", ""]
    lines += ["## 审核", review.get("summary", "尚未完成审核"), "审核状态：" + ("通过" if task["status"] == "succeeded" else "未通过或尚未完成")]
    lines += [f"- {s}" for s in review.get("issues", [])]
    lines += ["", "## 分析范围与限制", f"已读取 {len(snapshot.get('files', []))} 个文件；未执行仓库代码。"]
    lines += [f"- {s}" for s in analysis.get("limitations", [])]
    lines += [f"- {f['path']}" for f in snapshot.get("files", [])]
    lines += ["", "## 流程图", "```mermaid", result.get("mermaid", ""), "```"]
    return "\n".join(lines)


class State(TypedDict):
    task_id: str


class Runner:
    def __init__(self, store, reader=read_repository, llm_factory=DeepSeek, renderer=render_mermaid, *, timeout_seconds=600, max_repairs=2):
        self.store, self.reader, self.llm_factory, self.renderer = store, reader, llm_factory, renderer
        self.timeout_seconds = min(600, max(.01, timeout_seconds))
        self.max_repairs = min(2, max(0, max_repairs))
        self.jobs = {}
        self.stops = set()
        self.queue = asyncio.Lock()

    def start(self, task_id):
        job = asyncio.create_task(self.run(task_id))
        self.jobs[task_id] = job
        def finished(_):
            self.jobs.pop(task_id, None)
            self.stops.discard(task_id)
        job.add_done_callback(finished)

    def stop(self, task_id):
        previous = self.store.get(task_id)["status"]
        if previous in TERMINAL: return
        self.stops.add(task_id)
        self.store.update(task_id, status="stopping")
        self.store.event(task_id, "System", "stopping", "停止请求已接收，不再发起新步骤")
        if job := self.jobs.get(task_id): job.cancel()
        if previous == "queued":
            self.store.update(task_id, status="cancelled", error="用户已停止排队任务")
            self.store.event(task_id, "System", "cancelled", "排队任务已停止，未新增执行步骤")

    async def shutdown(self):
        tasks = list(self.jobs.values())
        for job in tasks: job.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, task_id):
        if self.store.get(task_id)['payload'].get('kind') == 'learn':
            from .learning import run_learning
            return await run_learning(self, task_id)
        if self.store.get(task_id)['payload'].get('kind') in {'search','chart','debug','improve'}:
            from .extended import run_extended
            return await run_extended(self,task_id)
        result = {}
        task = self.store.get(task_id)
        def check():
            if task_id in self.stops: raise Stopped()
        def event(role, kind, summary, details=None):
            self.store.event(task_id, role, kind, summary, details)
        def save():
            self.store.update(task_id, result=result)
            self.store.save_json(task_id, "result.json", result)
        def phase(status, role, summary):
            check()
            self.store.update(task_id, status=status)
            event(role, "started", summary)
        def handoff(sender, receiver, artifact):
            event(sender, "handoff", f"交接给 {receiver}", {"sender": sender, "receiver": receiver, "artifact": artifact, "artifact_hash": digest(result.get(artifact, {})), "evidence": "result.json", "task_id": task_id})
        llm = self.llm_factory(lambda usage: self.store.update(task_id, usage=usage), check)
        if hasattr(llm, 'config') and task['payload'].get('config', {}).get('model'):
            llm.config['model'] = task['payload']['config']['model']
        trace_model(llm, event)
        event("System", "limits", "本次执行限额", {"timeout_seconds": self.timeout_seconds, "max_repairs": self.max_repairs, "max_calls": MAX_CALLS, "max_tokens": MAX_TOTAL_TOKENS})
        llm.on_retry = lambda role, errors: event(role, 'retry', '模型调用暂时失败，正在进行受限重试' if errors[0]['field'] == 'transport' else '模型输出格式校验失败，正在进行受限修正', {'errors':errors})
        context = {}
        try:
            async with self.queue:
                check()
                async with asyncio.timeout(self.timeout_seconds):
                    async def plan_node(state):
                        phase("planning", "Supervisor", "制定分析计划与验收条件")
                        plan = await llm.ask("Supervisor", "本轮只做静态仓库分析、流程图、审核。围绕一条主要请求制定紧凑计划。默认只交付一张流程图，仅当 diagram_kind 明确为 architecture 或 both 时才要求架构图。验收不得擅自添加额外图、至少N条Bug或额外章节；只报告有证据的问题，没有明显Bug也可通过。所有角色按用户明确目标验收，不以自设数量扩写。", {"goal": task["goal"], "repository": task["repository"], "diagram_kind":task["payload"].get("diagram_kind","flow")}, Plan)
                        result["plan"] = plan.model_dump(); save()
                        event("Supervisor", "completed", "分析计划已生成", result["plan"])
                        handoff("Supervisor", "Code Agent", "plan")
                        return state

                    async def read_node(state):
                        phase("reading", "Code Agent", "固定提交版本并读取代码快照")
                        if task['payload'].get('attachment_id'):
                            from .workspaces import get_attachment, snapshot_from_files
                            snapshot=snapshot_from_files(get_attachment(self.store,task['payload']['attachment_id'])['files'])
                        else:
                            async def select_files(files):
                                selected=await select_repository_files(llm,task['goal'],files)
                                # Python package initializers can contain startup bindings.
                                # Keep nonempty ancestors instead of assuming they are boilerplate.
                                initializers=[]
                                for candidate in files:
                                    path=candidate['path']
                                    if path.endswith('/__init__.py') and 0<candidate.get('bytes',0)<=4000 and any(p.startswith(path.removesuffix('__init__.py')) for p in selected.paths):
                                        initializers.append(path)
                                selected.paths, omitted=bounded_analysis_files(list(dict.fromkeys(initializers+selected.paths)), files)
                                if omitted: event("Code Agent","scope","按本轮读取预算缩小范围",{"omitted":omitted,"byte_budget":70000,"file_budget":16})
                                event('Code Agent','tool','已按任务目标选择源文件',selected.model_dump())
                                return selected.paths
                            if self.reader is read_repository:
                                snapshot=await self.reader(task['repository'],task['payload'].get('ref',''),event,check,selector=select_files)
                            else: snapshot=await self.reader(task['repository'],task['payload'].get('ref',''),event,check)
                        self.store.save_json(task_id, "snapshot.json", snapshot)
                        result["snapshot"] = {k: v for k, v in snapshot.items() if k != "files"}
                        result["snapshot"]["files"] = [{k: v for k, v in f.items() if k != "content"} for f in snapshot["files"]]
                        context.update(snapshot=snapshot, sources=source_context(snapshot)); save()
                        return state

                    async def code_node(state):
                        phase("analyzing", "Code Agent", "分析模块职责与关键调用，建立代码证据")
                        analysis = await llm.ask("Code Agent", "只分析已提供的文件。聚焦一条主请求，最多4个主要模块、5个流程步骤，每条 explanation 80至200字，summary 150字内；不要罗列每个类或文件。模块和流程都必须给出精确行号的证据。不能推断未读文件的实现。推断必须标记 inferred=true。不能用 tools/scripts 测试脚本代替业务源码解释业务调用。用户要求检查Bug时最多填写2个有直接触发证据的 risks，每项有触发条件、原因、影响、建议及依据，只能是静态推测或待验证风险。业务未要求唯一性、可配置性等条件时不能自设要求当作Bug；未读启动文件时不能断言依赖注入未初始化。summary 介绍项目用途，不写修复过程。描述实际读取限制，不能把引用摘录或函数末尾当作文件被截断。每条风险的 trigger 必须是能进入 cause 所指分支的具体输入，逐句追踪条件判断和上游校验；不能把相反分支的输入当触发条件。没有证据不能断言异常向客户端泄漏堆栈。启动文件已提供时不能模板化声称启动文件缺失。", {"goal": task["goal"], "plan": result["plan"], **context["sources"]}, Analysis)
                        result["analysis"] = analysis.model_dump(); save()
                        event("Code Agent", "completed", "代码分析已生成，等待审核", {"artifact": "analysis"})
                        handoff("Code Agent", "Chart Agent", "analysis")
                        return state

                    async def chart_node(state):
                        phase("drawing", "Chart Agent", "生成有证据的流程图并实际渲染")
                        await make_chart()
                        event("Chart Agent", "completed", "流程图已完成渲染验证", {"artifact": "diagram.svg"})
                        handoff("Chart Agent", "Reviewer Agent", "diagram")
                        return state

                    async def make_chart(feedback=None,targets=None):
                        update_flow=targets is None or 'flow' in targets
                        update_arch=targets is None or 'architecture' in targets
                        if update_flow: result['rendered'] = False
                        if result.get('checks') and update_flow: result['checks']['diagram_rendered'] = False
                        save()
                        for render_attempt in range(2):
                            try:
                                diagram_kind=task['payload'].get('diagram_kind','flow')
                                style='模块架构图，节点为模块或外部系统，连线为已证实的依赖' if diagram_kind=='architecture' else '核心请求或调用流程图'
                                result['diagram_kind']='architecture' if diagram_kind=='architecture' else 'flow'
                                workspace = self.store.workspace(task_id)
                                if update_flow:
                                    diagram = await llm.ask("Chart Agent", "依据分析生成"+style+"，只选一个明确请求（例如创建资源），画4至6个按时间顺序执行的动作节点，从请求入口到响应结束。节点写动作如“创建领域实体”“保存到数据库”“组装响应”，不要用类、接口或模块作节点。边表示实际执行顺序，不画调用树或继承映射。同一函数先save再to_snapshot，顺序不得反转或跳过。没有条件分支时用一条线性流程，不混入启动装配。节点 ID 使用 n1、n2 等。节点与连线都有证据，每条引用尽量限于关键的5至15行。无法直接确认的关系必须 inferred=true；不得用脚本调用猜测未读业务模块。宁可少节点也不要用大面积推断补全流程。", {"analysis": result["analysis"], "feedback": feedback, **source_context(context['snapshot'])}, Diagram)
                                    result["diagram"] = diagram.model_dump()
                                    result["rendered"] = False; save()
                                    result["mermaid"] = diagram_source(diagram)
                                    (workspace / "diagram.mmd").write_text(result["mermaid"], encoding="utf-8")
                                    save()
                                    await self.renderer(result["mermaid"], workspace)
                                    check()
                                    result["rendered"] = True; save()
                                if diagram_kind=='both' and update_arch:
                                    arch=await llm.ask('Chart Agent','生成或修正模块架构图。最多8个节点、12条连线，只选关键实际模块或外部系统，连线是有证据的依赖，不能画成请求步骤。不支持的关系标记推断，不猜测未读代码。每条引用尽量限于关键5至15行。根据 feedback 修订。',{'analysis':result['analysis'],'feedback':feedback,'previous':result.get('architecture'),**source_context(context['snapshot'])},Diagram)
                                    arch_source=diagram_source(arch);arch_dir=workspace/'architecture';arch_dir.mkdir(exist_ok=True)
                                    await self.renderer(arch_source,arch_dir)
                                    (workspace/'architecture.svg').write_bytes((arch_dir/'diagram.svg').read_bytes())
                                    (workspace/'architecture.mmd').write_text(arch_source,encoding='utf-8')
                                    result['architecture']={'diagram':arch.model_dump(),'mermaid':arch_source,'rendered':True};save()
                                return
                            except ValueError as exc:
                                if render_attempt == 1 or "渲染" not in str(exc) and "图" not in str(exc): raise
                                feedback = [str(exc)]
                                event("Chart Agent", "repair", "图表校验失败，进行一次受限修复", {"issues": feedback})

                    async def review_node(state):
                        for attempt in range(self.max_repairs + 1):
                            phase("reviewing", "Reviewer Agent", f"校验证据与成果，审核第 {attempt + 1} 轮")
                            errors = validate_evidence(result["analysis"], context["snapshot"]) + validate_evidence(result["diagram"], context["snapshot"])
                            if result.get('architecture'): errors += validate_evidence(result['architecture'],context['snapshot'])
                            if sum(e['inferred'] for e in result['diagram']['edges']) > len(result['diagram']['edges'])/2:
                                errors.append('图中超过半数关系为推断；请缩小到已读取代码能支持的流程，不能将未读业务流程当作完成成果。')
                            if not result.get("rendered"): errors.append("流程图未通过实际渲染")
                            workspace=self.store.workspace(task_id)
                            for svg_name, source_name, source in [('diagram.svg','diagram.mmd',result.get('mermaid',''))]+([('architecture.svg','architecture.mmd',result['architecture']['mermaid'])] if result.get('architecture') else []):
                                svg_path,source_path=workspace/svg_name,workspace/source_name
                                if not svg_path.is_file() or '<svg' not in svg_path.read_text(encoding='utf-8'):
                                    errors.append(f'图表文件缺失或无效：{svg_name}')
                                if not source_path.is_file() or source_path.read_text(encoding='utf-8')!=source:
                                    errors.append(f'图表源码缺失或不一致：{source_name}')
                            result["checks"] = {"evidence_valid": not errors, "diagram_rendered": result.get("rendered", False), "errors": errors, "round": attempt + 1}
                            if errors:
                                review = Review(passed=False, summary="程序校验未通过", issues=errors)
                            else:
                                review = await llm.ask("Reviewer Agent", "独立审查分析和图表是否满足目标，核对代码内容是否支持结论。用户 goal 和请求中的 diagram_kind 是范围依据，plan 只是建议；不能以 plan 自设的额外图、至少N条风险或额外章节要求退回。flow 只需一条有证据的主请求流程，无需覆盖所有请求；风险数量没有下限，不得为凑数编造。不能因写了读取限制就批准缺失核心业务源码的完整业务分析；脚本或测试不能证明未读业务模块的内部调用。逐条检查图连线的起点与终点是否对应代码中真实的被调用对象，标记推断也不能接受明显连错模块。区分启动依赖装配和请求执行顺序。当前提供的文件是完整已读快照，不得因某个引用范围结束就说后文被截断。检查方向、推断标记、架构图和风险证据。对每条风险先代入 trigger 中的具体输入，按 if/else、早返回和上游校验逐句追踪，确认确实到达 cause 指定语句；例如空字符串为假值，不能触发 if value 的真分支问题。触发条件与代码路径矛盾是必须退回的实质错误，不是措辞问题。核对影响是否依赖未提供的配置，不得断言默认 HTTP 错误向客户端泄漏堆栈。检查 limitations 是否与实际已提供的入口文件相矛盾。不得声称代码已运行。重要问题 passed=false 并给具体修复项；repair_targets 只填实际需要修正的成果：code=分析报告，flow=主图，architecture=附加架构图。通过时 issues 和 repair_targets 都为空。summary 不超过100字，只给结论，不复述所有正确内容；issues 最多3个实质性问题，每个只写具体错误与修复要求。不因措辞或额外的安全审计要求退回成果。", {"goal": task["goal"], "plan": result["plan"], "analysis": result["analysis"], "diagram": result["diagram"], 'architecture':result.get('architecture'), 'diagram_kind':task['payload'].get('diagram_kind','flow'), **source_context(context['snapshot'])}, Review)
                            if review.issues: review.passed = False
                            result["review"] = review.model_dump(); save()
                            event("Reviewer Agent", "completed" if review.passed else "rejected", review.summary, result["review"])
                            if review.passed:
                                self.store.update(task_id, status="succeeded")
                                event("System", "succeeded", "分析、流程图与审核均已完成，成果已保存")
                                return state
                            if attempt == self.max_repairs:
                                self.store.update(task_id, status="needs_attention", error=f"已达到 {self.max_repairs} 轮修复上限，成果保留但审核未通过")
                                event("System", "needs_attention", "修复次数已用完，保留未通过的成果")
                                return state
                            targets=review.repair_targets or ['code','flow','architecture']
                            if 'code' in targets:
                                phase("repairing", "Code Agent", f"根据审核反馈修复，第 {attempt + 1} 轮")
                                handoff("Reviewer Agent", "Code Agent", "review")
                                analysis = await llm.ask("Code Agent", "只修正审核指出的问题，保留已有正确内容，不扩大范围。summary 必须是项目用途概述，不叙述审核和修复过程；不保留已被否定的结论作为正文。最多4个模块、5个流程步骤，每条解释80至200字，引用关键5至15行。", {"analysis": result["analysis"], "issues": review.issues, **context['sources']}, Analysis)
                                result["analysis"] = analysis.model_dump(); save()
                                event("Code Agent", "completed", "分析修订已保存")
                            if 'flow' in targets or ('architecture' in targets and task['payload'].get('diagram_kind')=='both'):
                                handoff("Reviewer Agent", "Chart Agent", "review")
                                phase("drawing", "Chart Agent", "修正受影响的图表并验证渲染")
                                await make_chart(review.issues,targets)
                                event("Chart Agent", "completed", "修订图表已渲染")
                                handoff("Chart Agent", "Reviewer Agent", "diagram")
                        return state

                    graph = StateGraph(State)
                    for name, fn in [("plan", plan_node), ("read", read_node), ("code", code_node), ("chart", chart_node), ("review", review_node)]: graph.add_node(name, fn)
                    for a, b in [(START, "plan"), ("plan", "read"), ("read", "code"), ("code", "chart"), ("chart", "review"), ("review", END)]: graph.add_edge(a, b)
                    await graph.compile().ainvoke({"task_id": task_id})
        except (asyncio.CancelledError, Stopped):
            status = "cancelled" if task_id in self.stops else "interrupted"
            self.store.update(task_id, status=status, error="用户已停止任务" if status == "cancelled" else "应用退出，任务已中断")
            event("System", status, "执行已停止，已生成成果保留")
        except TimeoutError:
            self.store.update(task_id, status="failed", error=f"达到任务 {self.timeout_seconds:g} 秒时限，已有成果保留")
            event("System", "failed", "任务达到执行时限")
        except ModelBudgetExceeded as exc:
            partial = bool(result.get('analysis') and result.get('diagram'))
            status = 'needs_attention' if partial else 'failed'
            message = '部分完成：已生成报告和图表，但预算不足以完成修复与复审；成果未通过审核。' if partial else str(exc)
            self.store.update(task_id, status=status, error=message)
            event('System', status, message)
        except Exception as exc:
            # Never echo arbitrary HTTP response bodies or credentials into persisted logs.
            message = str(exc)[:500] if isinstance(exc, ValueError) else f"执行失败（{type(exc).__name__}），请检查网络或运行环境"
            for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
                if key := os.environ.get(name): message = message.replace(key, "[已隐藏]")
            self.store.update(task_id, status="failed", error=message)
            event("System", "failed", message)
        finally:
            final = self.store.get(task_id)
            (self.store.workspace(task_id) / "report.md").write_text(report_markdown(final, result), encoding="utf-8")
            self.jobs.pop(task_id, None)
            self.stops.discard(task_id)
