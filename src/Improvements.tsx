import {useState} from 'react';
import type {Api,Attachment,Evidence,Task} from './types';

export default function Improvements({task,api,onCopy,onEvidence,onError,currentAttachmentId}:{task:Task;api:Api;onCopy:(a:Attachment)=>void;onEvidence:(e:Evidence)=>void;onError:(s:string)=>void;currentAttachmentId?:string}){
 const data=task.result?.improvement;const [busy,setBusy]=useState('');const [copied,setCopied]=useState('');
 const [currentId,setCurrentId]=useState('');
 if(!data)return <p className="muted">正在整理修改建议。</p>;
 const base=currentId||data.current_attachment_id||data.base_attachment_id;
 const stale=!!currentAttachmentId&&currentAttachmentId!==base;
 async function apply(id:string){setBusy(id);try{
  const attachment=await(await api(`/tasks/${task.id}/apply`,{method:'POST',body:JSON.stringify({recommendation_id:id,base_attachment_id:base})})).json();
  setCurrentId(attachment.id);onCopy(attachment);
 }catch(e){onError((e as Error).message);}finally{setBusy('');}}
 async function open(id:string){try{onCopy(await(await api(`/attachments/${id}`)).json());}catch(e){onError((e as Error).message);}}
 async function restore(){setBusy('restore');try{const a=await(await api(`/tasks/${task.id}/restore`,{method:'POST',body:JSON.stringify({base_attachment_id:base})})).json();setCurrentId(a.id);onCopy(a);}catch(e){onError((e as Error).message);}finally{setBusy('');}}
 async function copy(id:string,text:string){try{await window.agentos!.copyText(text);setCopied(id);}catch{onError('复制失败，请展开提示词后手动复制。');}}
 return <div className="improvements"><p>{data.summary}</p>
 <div className="settings-actions"><button onClick={()=>open(base)}>打开当前副本</button><button onClick={()=>open(data.base_attachment_id)}>打开原副本</button>{data.recommendations.some(r=>r.applied_attachment_id)&&<button disabled={!!busy} onClick={restore}>还原全部修改</button>}</div>
 <p className="muted">补丁只应用到副本，可在编辑器中导出。原项目文件不变。</p>
 {stale&&<p className="warning">当前工作台选中了其他版本，请重新发送这份副本获取修改建议。</p>}
 {!data.ready&&<p className="warning">建议尚未通过审核，暂不能应用。</p>}
 {task.result?.search_error&&<p className="muted">参考项目搜索未完成：{task.result.search_error}</p>}
 {data.recommendations.map(item=><article className="improvement-card" key={item.id}>
  <h3>{item.title}</h3><p className="muted">{item.kind==='simple'?'局部修改':'交给开发工具实施'}</p><p>{item.reason}</p>
  <div className="citations">{item.evidence.map((e,i)=><button key={i} onClick={()=>onEvidence(e)}>{e.path}:{e.start_line}–{e.end_line}</button>)}</div>
  <ol>{item.steps.map((step,i)=><li key={i}>{step}</li>)}</ol>
  <details><summary>验收要求</summary><ul>{item.acceptance.map((s,i)=><li key={i}>{s}</li>)}</ul></details>
  {!!item.citations.length&&<details><summary>借鉴依据</summary>{item.citations.map((c,i)=><blockquote key={i}>{c.text}<br/><button onClick={()=>window.agentos?.openExternal(c.url).catch(e=>onError(e.message))}>{c.path}:{c.line}</button></blockquote>)}</details>}
  {item.kind==='simple'&&<><details className="diff"><summary>修改差异</summary><pre>{item.diff||'暂无可应用补丁'}</pre></details>{item.checks.map((s,i)=><p className="muted" key={i}>{s}</p>)}
   {item.applied_attachment_id?<button onClick={()=>open(base)}>已应用 · 打开副本</button>:<button className="secondary" disabled={!data.ready||task.status!=='succeeded'||!!busy||stale} onClick={()=>apply(item.id)}>{busy===item.id?'应用中…':'应用到副本'}</button>}
  </>}
  <details className="source-details"><summary>给 Codex 的提示词</summary><textarea readOnly aria-label={`实施提示词 ${item.id}`} value={item.prompt}/></details>
  <button onClick={()=>copy(item.id,item.prompt)}>{copied===item.id?'已复制':'复制实施提示词'}</button>
 </article>)}
 {!data.recommendations.length&&<p>本次没有足够依据提出具体修改。</p>}
 {data.limitations.map((s,i)=><p className="muted" key={i}>{s}</p>)}
 </div>;
}
