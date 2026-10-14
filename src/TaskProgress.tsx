import {ChevronRight, Loader2} from 'lucide-react';
import type {Task, Event} from './types';
import {taskProgress} from './progress';

export default function TaskProgress({task,events,offline,onOpen}:{task:Task;events:Event[];offline:boolean;onOpen:()=>void}){
 const p=taskProgress(task,events,offline);
 return <section className="task-progress" aria-label="任务协作进度">
  <button className="progress-heading" onClick={onOpen} aria-label="展开协作详情">
   {p.active&&<Loader2 className="spin" size={15}/>}<strong>{p.ended?'历史任务':p.role}</strong><span>{p.status}</span><ChevronRight size={15}/>
  </button>
  <p role="status">{p.action}</p>
  <ol>{p.stages.map(s=><li key={s.id} className={s.active?'active':''}><span>{s.name}</span><small>{s.state}</small></li>)}</ol>
 </section>;
}
