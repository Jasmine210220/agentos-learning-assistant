import type {Event, Task} from './types';

export const learningRoles = [['Supervisor','任务范围'],['Code Agent','项目解读'],['Learning Agent','学习指导'],['Chart Agent','数据传递图'],['Reviewer Agent','自动审核']];
export const terminalStates = new Set(['succeeded','failed','cancelled','interrupted','needs_attention']);
export function taskProgress(task: Task, events: Event[], offline = false) {
 const ended = terminalStates.has(task.status);
 const started = events.filter(e=>e.kind==='started').at(-1);
 const current = started?.role;
 const stages = learningRoles.map(([id,name])=>{
  const entries=events.filter(e=>e.role===id);
  const last=entries.filter(e=>['started','completed','rejected','failed'].includes(e.kind)).at(-1);
  let state='等待';
  if(last?.kind==='completed')state='完成';
  if(last?.kind==='rejected'||last?.kind==='failed')state='未通过';
  if(id===current){
   if(task.status==='needs_attention'||task.status==='failed')state='未通过';
   else if(task.status==='cancelled'||task.status==='interrupted')state='中断';
   else if(!ended&&task.status!=='queued')state=offline?'连接中断 · 状态待同步':task.status==='stopping'?'正在停止':'运行';
  }
  // A started event is not proof of current execution once the task is terminal.
  if(ended&&last?.kind==='started'&&state==='等待')state='中断';
  return {id,name,state,active:state==='运行'};
 });
 const role=learningRoles.find(([id])=>id===current)?.[1]||'任务';
 const status=({succeeded:'任务完成 · 自动审核未发现剩余问题',needs_attention:'需关注 · 未通过',failed:'任务失败',cancelled:'已取消',interrupted:'已中断',queued:'等待执行',stopping:'正在停止'} as Record<string,string>)[task.status]||'执行中';
 const action=ended?task.error||status:offline?'无法确认当前动作，等待重新连接':events.filter(e=>e.role===current&&!['model_input','model_output','handoff'].includes(e.kind)).at(-1)?.summary||status;
 return {stages,role,action,status,ended,active:!ended&&!offline&&task.status!=='queued'&&task.status!=='stopping'&&!!current};
}
