import asyncio
import json
import re
from .models import BugReport, Review, FileSelection
from .charts import ChartSpec, parse_data, render_chart
from .github import read_repository
from .llm import source_context
from .workspaces import get_attachment, snapshot_from_files


async def run_extended(runner,task_id):
    from .workflow import validate_evidence
    store=runner.store;task=store.get(task_id);payload=task['payload'];result={}
    def event(role,kind,summary,details=None): store.event(task_id,role,kind,summary,details)
    def check():
        if task_id in runner.stops: raise asyncio.CancelledError()
    def phase(status,role,summary):
        check();store.update(task_id,status=status);event(role,'started',summary)
    def save(): store.update(task_id,result=result);store.save_json(task_id,'result.json',result)
    def handoff(sender,receiver,artifact): event(sender,'handoff',f'交接给 {receiver}',{'sender':sender,'receiver':receiver,'artifact':artifact,'task_id':task_id})
    llm=runner.llm_factory(lambda usage:store.update(task_id,usage=usage),check)
    if hasattr(llm,'config') and payload.get('config',{}).get('model'):
        llm.config['model']=payload['config']['model']
    from .audit import trace_model
    trace_model(llm,event)
    llm.on_retry=lambda role,errors:event(role,"retry","模型调用进行受限重试",{"errors":errors})
    event("System","limits","本次执行限额",{"timeout_seconds":runner.timeout_seconds,"max_repairs":runner.max_repairs})
    workspace=store.workspace(task_id)
    try:
        async with runner.queue:
            check()
            async with asyncio.timeout(runner.timeout_seconds):
                kind=payload['kind']
                if kind=='search':
                    from .search_workflow import discover
                    await discover(task,llm,event,phase,save,result,check)
                elif kind=='improve':
                    from .improvement import improve
                    await improve(task,llm,event,phase,save,result,check,store)
                elif kind=='chart':
                    phase('reading','Chart Agent','读取表格数据')
                    source=payload.get('input_text','')
                    source_name='用户消息'
                    if payload.get('attachment_id'):
                        files=get_attachment(store,payload['attachment_id'])['files']
                        candidates=[f for f in files if f['path'].lower().endswith(('.csv','.tsv','.json'))]
                        if len(candidates)!=1: raise ValueError('请选择一个 CSV、TSV 或 JSON 数据文件')
                        source=candidates[0]['content'];source_name=candidates[0]['path']
                    data=parse_data(source)
                    result['dataset']={**data,'source':source_name};save()
                    phase('drawing','Chart Agent','生成数据图表')
                    spec=await llm.ask('Chart Agent','选择用户提供的表格中的真实字段生成折线图或柱状图。不要生成数据。单位只使用用户说明或列名中明确给出的单位；未给出时 unit 为空字符串。按用户要求选择 kind、横轴 x、纵轴 y。',{'goal':task['goal'],'columns':data['columns'],'sample':data['rows'][:8]},ChartSpec)
                    if spec.unit and spec.unit not in task['goal']+' '.join(data['columns']): spec.unit=''
                    svg,series=render_chart(data,spec)
                    (workspace/'chart.svg').write_text(svg,encoding='utf-8')
                    (workspace/'data.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
                    result['chart']={**spec.model_dump(),'series':series,'source':source_name,'rendered':True};save()
                    event('Chart Agent','completed','图表已生成，数据未补造',{'artifact':'chart.svg'});handoff('Chart Agent','Reviewer Agent','chart.svg')
                    phase('reviewing','Reviewer Agent','检查字段、数值和来源')
                    review=await llm.ask('Reviewer Agent','程序已验证字段存在、数值直接来自数据且缺失值未补造。独立检查图类型、横纵轴和单位是否满足用户目标；不添加额外需求。',{'goal':task['goal'],'chart':spec.model_dump(),'source':source_name,'columns':data['columns'],'rows':data['rows'][:12]},Review)
                    if not review.passed or review.issues:
                        event('Reviewer Agent','rejected',review.summary,review.model_dump());handoff('Reviewer Agent','Chart Agent','review')
                        phase('repairing','Chart Agent','根据审核反馈调整图表')
                        spec=await llm.ask('Chart Agent','根据反馈调整图表字段和类型，仅可选择实际数据列，不补造单位。',{'goal':task['goal'],'previous':spec.model_dump(),'issues':review.issues,'columns':data['columns'],'rows':data['rows'][:8]},ChartSpec)
                        if spec.unit and spec.unit not in task['goal']+' '.join(data['columns']): spec.unit=''
                        svg,series=render_chart(data,spec);(workspace/'chart.svg').write_text(svg,encoding='utf-8')
                        result['chart']={**spec.model_dump(),'series':series,'source':source_name,'rendered':True};save();event('Chart Agent','completed','图表已修正');handoff('Chart Agent','Reviewer Agent','chart.svg')
                        phase('reviewing','Reviewer Agent','复核图表字段与目标')
                        review=await llm.ask('Reviewer Agent','核对所选图类型、字段及单位满足目标，不新增需求。',{'goal':task['goal'],'chart':spec.model_dump(),'columns':data['columns'],'rows':data['rows'][:8]},Review)
                    result['review']=review.model_dump()
                else:
                    phase('reading','Code Agent','读取代码与错误信息')
                    if payload.get('attachment_id'): snapshot=snapshot_from_files(get_attachment(store,payload['attachment_id'])['files'])
                    elif task['repository']:
                        async def select(files):
                            selection=await llm.ask('Code Agent','选择与报错和排查目标有关的核心源码，优先8–12个，总大小优先不超过100000字节。只选给定真实路径，不用测试脚本代替实现。',{'goal':task['goal'],'logs':payload.get('input_text',''),'files':files},FileSelection)
                            if not set(selection.paths)<={f['path'] for f in files}: raise ValueError('文件选择引用了未知路径')
                            event('Code Agent','tool','已选择待排查文件',selection.model_dump())
                            return selection.paths
                        if runner.reader is read_repository: snapshot=await runner.reader(task['repository'],payload.get('ref',''),event,check,selector=select)
                        else: snapshot=await runner.reader(task['repository'],payload.get('ref',''),event,check)
                    else:
                        text=payload.get('input_text','');blocks=re.findall(r'```(?:python|py|javascript|js|typescript|ts)?\s*\n([\s\S]*?)```',text,re.I)
                        if not blocks: raise ValueError('请附上代码文件，或使用代码块粘贴代码及报错')
                        content=blocks[0];snapshot=snapshot_from_files([{'path':'snippet.py','content':content,'lines':len(content.splitlines()),'bytes':len(content.encode())}],'消息中的代码')
                    store.save_json(task_id,'snapshot.json',snapshot)
                    result['snapshot']={**snapshot,'files':[{k:v for k,v in f.items() if k!='content'} for f in snapshot['files']]};save()
                    for attempt in range(runner.max_repairs+1):
                        phase('analyzing','Code Agent','定位问题并整理修改建议')
                        bugs=await llm.ask('Code Agent','结合用户报错与实际代码进行静态 Bug 排查。每个问题给出触发条件、原因、影响、建议及行号依据。不要把风格偏好当 Bug，不得声称已复现、已修复或测试通过。没有足够证据时 findings 为空并说明缺失信息。patch 可给建议补丁文本但不能自动应用。根据 feedback 修订。',{'goal':task['goal'],'logs':payload.get('input_text',''),'feedback':result.get('review'),**source_context(snapshot)},BugReport)
                        result['bugs']=bugs.model_dump();save();event('Code Agent','completed','静态排查结果已保存');handoff('Code Agent','Reviewer Agent','bugs')
                        phase('reviewing','Reviewer Agent','审核问题证据与验证状态')
                        errors=validate_evidence(result['bugs'],snapshot)
                        if errors: review=Review(passed=False,summary='引用校验未通过',issues=errors)
                        else: review=await llm.ask('Reviewer Agent','核对问题触发条件、原因与代码依据是否一致。只接受静态推测或待验证风险，不得声称实际执行。按文件语言的标准语义审查，不混用JavaScript、NumPy与Python标量语义。只有改变触发条件、原因或修改有效性的实质错误才拒绝；不影响结论的措辞、任务外的额外建议不能进入issues。结论正确则passed=true且issues=[]，无需制造修复项。',{'goal':task['goal'],'bugs':result['bugs'],**source_context(snapshot)},Review)
                        result['review']=review.model_dump();save()
                        if review.passed and not review.issues: break
                        event('Reviewer Agent','rejected',review.summary,review.model_dump())
                review=result['review']
                review['passed']=bool(review['passed'] and not review['issues'])
                event('Reviewer Agent','completed' if review['passed'] else 'rejected',review['summary'],review)
                save();store.update(task_id,status='succeeded' if review['passed'] else 'needs_attention')
    except asyncio.CancelledError:
        status='cancelled' if task_id in runner.stops else 'interrupted'
        store.update(task_id,status=status,error='执行已停止，已有成果保留')
        event('System',status,'执行已停止，已有成果保留')
    except TimeoutError:
        message=f'达到任务 {runner.timeout_seconds:g} 秒时限，已有成果保留'
        store.update(task_id,status='failed',error=message);event('System','failed',message)
    except Exception as exc:
        message=str(exc)[:500] if isinstance(exc,ValueError) else '执行失败，请检查网络或输入数据'
        store.update(task_id,status='failed',error=message);event('System','failed',message)
    finally:
        if result.get('improvement'):
            from .improvement import report_text
            report=report_text(task,result)
        else: report='# '+task['goal']+'\n\n'+json.dumps(result,ensure_ascii=False,indent=2)
        (workspace/'report.md').write_text(report,encoding='utf-8')
        runner.jobs.pop(task_id,None);runner.stops.discard(task_id)
