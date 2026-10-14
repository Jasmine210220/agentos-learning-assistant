import TaskProgress from './TaskProgress';
import Artifacts from './Artifacts';
import Editor,{RunHistory} from './Editor';
import Settings from './Settings';
import Markdown from 'react-markdown';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Paperclip, FolderOpen, History, ArrowUp, ChevronRight, Download, FileCode2, FileText, FolderGit2, Loader2, Network, Plus, Settings2, ShieldCheck, Square, X, AlertCircle, PanelRight, MessageSquare } from 'lucide-react';
import type { Connection, Evidence, Event, Task, Claim, Attachment } from './types';
type Message = {id:number;role:string;content:string;task_id?:string;status:string;evidence:Evidence[];attachment_id?:string};
type Chat = {id:string;title:string;updated:string;messages:Message[]};
const terminal = new Set(['succeeded','failed','cancelled','interrupted','needs_attention']);
const statuses:Record<string,string> = {queued:'等待执行',planning:'规划任务',searching:'搜索项目',reading:'读取代码',analyzing:'分析代码',teaching:'制定学习路线',drawing:'生成数据传递图',reviewing:'审核成果',repairing:'修正成果',stopping:'正在停止'};
export default function App() {
 const [connection,setConnection]=useState<Connection>();
 const [health,setHealth]=useState<{configured:boolean;model:string}>();
 const [chats,setChats]=useState<Chat[]>([]);
 const [selected,setSelected]=useState<string>();
 const [chat,setChat]=useState<Chat>();
 const [task,setTask]=useState<Task>();
 const [artifactId,setArtifactId]=useState<string>();
 const [events,setEvents]=useState<Event[]>([]);
 const [draft,setDraft]=useState('');
 const [background,setBackground]=useState('会 Python 基础，不熟悉 FastAPI');
 const [ref,setRef]=useState('');
 const [error,setError]=useState('');
 const [offline,setOffline]=useState(false);
 const [busy,setBusy]=useState(false);
 const [panel,setPanel]=useState(false);
 const [settings,setSettings]=useState(false);
 const [tab,setTab]=useState('report');
 const [svg,setSvg]=useState('');
 const [attachment,setAttachment]=useState<Attachment>();
 const [editor,setEditor]=useState(false);
 const [importChoice,setImportChoice]=useState<File[]>([]);
 const [importSelection,setImportSelection]=useState<Set<string>>(new Set());
 const [runHistory,setRunHistory]=useState(false);
 const fileInput=useRef<HTMLInputElement>(null);
 const folderInput=useRef<HTMLInputElement>(null);
 const [source,setSource]=useState<{path:string;content:string;commit:string;start:number;end:number}>();
 const scroll=useRef<HTMLDivElement>(null);
 const nearBottom=useRef(true);
 const selection=useRef(selected); selection.current=selected;
 const api=useCallback(async(route:string,options:RequestInit={})=>{
  if(!connection) throw new Error('正在连接');
  const r=await fetch(connection.url+route,{...options,headers:{'Content-Type':'application/json',Authorization:`Bearer ${connection.token}`},signal:options.signal||AbortSignal.timeout(10000)});
  if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(typeof d.detail==='string'?d.detail:'请求失败');}return r;
 },[connection]);
 useEffect(()=>{window.agentos?.connection().then(setConnection).catch(e=>setError(String(e)));},[]);
 useEffect(()=>{
  if(!connection)return;let disposed=false;let timer:ReturnType<typeof setTimeout>;
  async function poll(){try{
   const [h,list]=await Promise.all([api('/health').then(r=>r.json()),api('/chats').then(r=>r.json())]);
   if(disposed)return;setHealth(h);setChats(list);setOffline(false);
   if(selected){const c=await (await api(`/chats/${selected}`)).json();if(disposed)return;setChat(c);}
  }catch{if(!disposed)setOffline(true);}if(!disposed)timer=setTimeout(poll,1200);}
  poll();return()=>{disposed=true;clearTimeout(timer);};
 },[api,connection,selected]);
 const latestTask=chat?.messages.filter(m=>m.task_id).at(-1)?.task_id;
 const currentTask=artifactId||latestTask;
 useEffect(()=>{let disposed=false;let timer:ReturnType<typeof setTimeout>;let after=0;
  setTask(undefined);setEvents([]);setSvg('');if(!currentTask)return;
  async function poll(){try{const [t,e]=await Promise.all([api(`/tasks/${currentTask}`).then(r=>r.json()),api(`/tasks/${currentTask}/events?after=${after}`).then(r=>r.json())]);if(disposed)return;setTask(t);if(e.length){after=e.at(-1).id;setEvents(old=>[...old,...e]);}}catch{}if(!disposed)timer=setTimeout(poll,1200);}
  poll();return()=>{disposed=true;clearTimeout(timer);};
 },[api,currentTask]);
 const result=task?.result; const running=!!task&&!terminal.has(task.status);
 const pending=busy||!!chat?.messages.some(m=>m.status==='pending');

 useEffect(()=>{if(nearBottom.current)scroll.current?.scrollTo({top:scroll.current.scrollHeight,behavior:'smooth'});},[chat?.messages.length,chat?.messages.at(-1)?.content,pending]);
 function choose(id?:string){setSelected(id);setChat(undefined);setArtifactId(undefined);setPanel(false);setError('');setDraft('');setAttachment(undefined);setSource(undefined);nearBottom.current=true;}
 async function send(text=draft){if(!text.trim()||pending)return;setBusy(true);setError('');const content=text;try{
  let id=selected;if(!id){const c=await(await api('/chats',{method:'POST'})).json();id=c.id;setSelected(id);selection.current=id;}
  const c=await(await api(`/chats/${id}/messages`,{method:'POST',body:JSON.stringify({content,mode:'learn',learner_background:background,ref})})).json();
  if(selection.current===id){setChat(c);setDraft('');setArtifactId(undefined);setAttachment(undefined);nearBottom.current=true;}
 }catch(e){setError((e as Error).message);}finally{setBusy(false);}}
 async function evidence(e:Evidence, taskId=currentTask){const id=selected;try{const data=await(await api(`/tasks/${taskId}/source?path=${encodeURIComponent(e.path)}`)).json();if(id===selection.current)setSource({...data,start:e.start_line,end:e.end_line});}catch(e){setError((e as Error).message);}}
 async function download(name:string){try{const blob=await(await api(`/tasks/${currentTask}/artifacts/${name}`)).blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){setError((e as Error).message);}}
 const citations=(items:Evidence[])=><div className="citations">{items.map((e,i)=><button key={i} onClick={()=>evidence(e)}><FileCode2 size={12}/>{e.path}:{e.start_line}–{e.end_line}</button>)}</div>;
 const claims=(items:Claim[])=>items.map((c,i)=><article className="claim" key={i}><h3>{c.title}{c.inferred&&<span className="pill">推断</span>}</h3><p>{c.explanation}</p>{citations(c.evidence)}</article>);
 async function importFiles(list:FileList|null){if(!list)return;setBusy(true);try{
  const excluded=/(^|\/)(node_modules|\.git|\.github|\.venv|venv|dist|build|__pycache__)(\/|$)|(^|\/)\.env|secret|credential|private_key|lock/i;
  const supported=/\.(py|ts|tsx|js|jsx|md|toml|json|ya?ml|txt|go|rs|sh|css|scss|html|vue|svelte|csv|tsv)$/i;
  const candidates=Array.from(list).filter(f=>!excluded.test(f.webkitRelativePath||f.name)&&supported.test(f.name)&&f.size<=96000);
  if(!candidates.length)throw new Error('没有可导入的文本文件。');
  setImportChoice(candidates);setImportSelection(new Set());setError('');
 }catch(e){setError((e as Error).message);}finally{setBusy(false);if(fileInput.current)fileInput.current.value='';if(folderInput.current)folderInput.current.value='';}}
 async function confirmImport(){setBusy(true);try{
  const chosen=importChoice.filter(f=>importSelection.has(f.webkitRelativePath||f.name));
  if(!chosen.length||chosen.length>32)throw new Error('请选择 1–32 个文件。');
  if(chosen.reduce((n,f)=>n+f.size,0)>160000)throw new Error('所选文件超过 160 KB，请缩小范围。');
  const files=await Promise.all(chosen.map(async f=>({path:f.webkitRelativePath||f.name,content:new TextDecoder('utf-8',{fatal:true}).decode(await f.arrayBuffer())})));
  setAttachment(await(await api('/attachments',{method:'POST',body:JSON.stringify({files})})).json());setImportChoice([]);setEditor(true);setError('');
 }catch(e){setError((e as Error).message);}finally{setBusy(false);}}
 async function openAttachment(id:string){try{setAttachment(await(await api(`/attachments/${id}`)).json());setEditor(true);}catch(e){setError((e as Error).message);}}
 return <div className="app-shell">
  <aside className="sidebar"><div className="brand">AgentOS <span>学习助手</span></div><button className="new-button" disabled={busy} onClick={()=>choose()}><Plus size={17}/>新对话</button><div className="nav-caption">对话</div><nav className="chat-list">{chats.map(c=><button className={`chat-item ${selected===c.id?'selected':''}`} key={c.id} data-chat-id={c.id} title={c.title+" / "+new Date(c.updated).toLocaleString()} disabled={busy} onClick={()=>choose(c.id)}><MessageSquare size={15}/><span>{c.title}</span></button>)}</nav><button className="settings-button" onClick={()=>setSettings(true)}><Settings2 size={16}/>设置<span className="model">DeepSeek</span></button></aside>
  <main className="main"><header className="topbar"><span>{chat?.title||'新对话'}</span><div>{offline&&<span className="warning">连接中断</span>}{latestTask&&<button className={panel?'selected':''} aria-label="查看成果" onClick={()=>setPanel(!panel)}><PanelRight size={17}/>成果</button>}</div></header>
   {error&&<div className="banner">{error}<button aria-label="关闭错误" onClick={()=>setError('')}><X size={15}/></button></div>}
   <div className="conversation-layout"><section className="conversation">{task?.payload?.kind==='learn'&&<TaskProgress task={task} events={events} offline={offline} onOpen={()=>{setPanel(true);setTab('agents');}}/>}<div className="messages" ref={scroll} onScroll={e=>{const el=e.currentTarget;nearBottom.current=el.scrollHeight-el.scrollTop-el.clientHeight<100;}}><div className="message-column">
    {!chat?.messages.length&&<div className="empty-chat"><MessageSquare size={26}/><strong>从一个项目、一条流程开始</strong><p>粘贴公开 GitHub 仓库链接和学习目标，获取代码解读、阅读路线、三个练习和一张数据传递图。</p><p className="muted">仅静态阅读源码；关键文件未读到时会说明限制。</p></div>}
    {chat?.messages.map((m,index)=><article className={`message ${m.role}`} key={m.id}><div className="message-author">{m.role==='user'?'你':'AgentOS'}</div>{m.content&&<div className="message-text">{m.role==='user'?m.content:<Markdown components={{img:()=>null,a:({children,href})=>href?.startsWith('https://github.com/')?<button className="inline-link" onClick={()=>window.agentos?.openExternal(href)}>{children}</button>:<span>{children}</span>}}>{m.content}</Markdown>}</div>}{m.attachment_id&&<button className="attachment-chip" onClick={()=>openAttachment(m.attachment_id!)}><Paperclip size={13}/>文件副本</button>}{m.status==='pending'&&<div className="progress"><Loader2 className="spin" size={14}/>{m.task_id&&task?.id===m.task_id?statuses[task.status]||'处理中':'正在回复'}</div>}{m.evidence.length>0&&<div className="message-citations">{m.evidence.map((e,i)=><button key={i} onClick={()=>evidence(e,m.task_id)}>{e.path}:{e.start_line}–{e.end_line}</button>)}</div>}{m.task_id&&!chat.messages.slice(0,index).some(p=>p.task_id===m.task_id)&&<button className="artifact-card" onClick={()=>{setArtifactId(m.task_id);setPanel(true);setTab('report');}}><FileText size={18}/><span>查看成果</span><ChevronRight size={16}/></button>}{m.task_id===currentTask&&!chat.messages.slice(0,index).some(p=>p.task_id===m.task_id)&&events.length>0&&<details className="execution"><summary>执行记录</summary>{events.map(e=><div key={e.id}><time>{new Date(e.time).toLocaleTimeString('zh-CN',{hour12:false})}</time><span>{e.summary}</span></div>)}</details>}</article>)}
   </div></div><div className="composer-wrap">{attachment&&<div className="attachment-bar"><button onClick={()=>setEditor(true)}><Paperclip size={14}/>{attachment.files.length} 个文件 · {Math.round(attachment.bytes/1024)} KB</button><button aria-label="移除附件" onClick={()=>setAttachment(undefined)}><X size={14}/></button></div>}<div className="learning-inputs"><label>已有基础<input aria-label="已有基础" maxLength={1000} value={background} onChange={e=>setBackground(e.target.value)}/></label><label>版本（可选）<input aria-label="仓库版本" maxLength={120} placeholder="默认分支 / 提交 SHA" value={ref} onChange={e=>setRef(e.target.value)}/></label></div><form className="composer" onSubmit={e=>{e.preventDefault();send();}}><textarea aria-label="消息" placeholder="粘贴仓库链接开始，也可同时说明学习目标；已有成果可直接追问" value={draft} onChange={e=>setDraft(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();send();}}}/><div className="composer-actions"><div className="input-tools"><span>项目解读 → 学习指导 → 数据传递图</span></div>{pending?<button type="button" disabled={busy} aria-label="停止回复" onClick={()=>api(`/chats/${selected}/stop`,{method:'POST'}).catch(e=>setError(e.message))}><Square size={15}/></button>:<button className="send-button" aria-label="发送" disabled={!draft.trim()||!health?.configured||!connection} type="submit"><ArrowUp size={18}/></button>}</div></form>{health&&!health.configured&&<p className="warning">请先<button type="button" onClick={()=>setSettings(true)}>设置 DeepSeek 密钥</button>，草稿会保留。</p>}</div></section>
   {panel&&<Artifacts currentAttachmentId={attachment?.id} task={task} events={events} api={api} tab={tab} setTab={setTab} onClose={()=>setPanel(false)} onEvidence={e=>evidence(e)} onError={setError} onCopy={a=>{setAttachment(a);setEditor(true);}} onSelect={repo=>send(`分析 ${repo}。保留原需求中的分析、绘图和检查要求：${task?.goal||'分析主要模块与请求流程，生成数据传递图'}`)}/>}</div></main>
 {importChoice.length>0&&<div className="modal-backdrop"><section className="modal"><h2>选择文件</h2><p>仅复制勾选的文件。发送消息时，这些文件内容会交给 DeepSeek。</p><div style={{maxHeight:400,overflow:'auto'}}>{importChoice.map(f=>{const name=f.webkitRelativePath||f.name;return <label key={name} style={{display:'flex',gap:8}}><input type="checkbox" checked={importSelection.has(name)} onChange={e=>setImportSelection(old=>{const next=new Set(old);if(e.target.checked)next.add(name);else next.delete(name);return next;})}/>{name} · {f.size} B</label>;})}</div><div className="settings-actions"><button disabled={busy||!importSelection.size} onClick={confirmImport}>导入所选文件</button><button disabled={busy} onClick={()=>setImportChoice([])}>取消</button></div></section></div>}
 {settings&&<Settings api={api} onClose={()=>setSettings(false)}/>}
 {editor&&attachment&&<Editor key={attachment.id} attachment={attachment} api={api} onChange={setAttachment} onClose={()=>setEditor(false)} onAnalyze={setDraft}/>}
 {runHistory&&<RunHistory api={api} onClose={()=>setRunHistory(false)}/>}
 <input ref={fileInput} type="file" multiple hidden accept=".py,.ts,.tsx,.js,.jsx,.md,.toml,.json,.yaml,.yml,.txt,.go,.rs,.sh,.css,.scss,.html,.vue,.svelte,.csv,.tsv" onChange={e=>importFiles(e.target.files)}/><input ref={folderInput} type="file" multiple hidden {...{webkitdirectory:''}} onChange={e=>importFiles(e.target.files)}/>
    {source && <div className="modal-backdrop" onClick={() => setSource(undefined)}><section className="modal source-modal" onClick={e => e.stopPropagation()}><button className="close" aria-label="关闭代码" onClick={() => setSource(undefined)}><X/></button><h2><FileCode2 size={20}/>{source.path}</h2><p className="muted">本次读取快照 · {source.commit.slice(0,12)} · 引用行 {source.start}–{source.end}</p><div className="code-lines" ref={el => { if (el) el.querySelector('.highlight')?.scrollIntoView({block:'center'}); }}>{source.content.split('\n').map((line,i) => <div key={i} className={i+1 >= source.start && i+1 <= source.end ? 'highlight' : ''}><span>{i+1}</span><code>{line || ' '}</code></div>)}</div></section></div>}
</div>;
}
