import {ChevronRight,ExternalLink} from 'lucide-react';
import type {Result} from './types';

export default function SearchResults({search,onSelect,onError}:{search:NonNullable<Result['search']>;onSelect:(url:string)=>void;onError:(message:string)=>void}){
 const open=(url:string)=>window.agentos?.openExternal(url).catch(e=>onError(e.message));
 return <><p className="muted">{search.mode==='web_github'?'全网 + GitHub':search.mode==='github_only'?'仅 GitHub':''}{search.rounds?` · ${search.rounds.length} 轮`:''}</p>
 {search.warnings?.map(w=><p className="warning" key={w}>{w}</p>)}
 {search.outcome==='not_found'&&<p>尚未找到符合要求的项目，以下为相关候选。</p>}
 <div className="stack">{search.conditions.map(c=><span key={c}>{c}</span>)}</div>
 {search.rounds?<details className="source-details"><summary>检索记录</summary>{search.rounds.map(r=><section key={r.number}><h3>第 {r.number} 轮</h3>{r.queries.map((q,i)=><p key={i}>{q.source==='web'?'全网':q.sort==='stars'?'GitHub · 热度':'GitHub · 相关性'}：{q.query} · {q.status==='ok'?`${q.count} 个结果`:'失败'}</p>)}<p className="muted">{r.reason}</p></section>)}<p>{search.stop_reason}</p><p className="muted">全网请求 {search.usage?.web_requests||0} 次 · GitHub 查询 {search.usage?.github_queries||0} 次</p></details>:<div className="search-query"><code>{search.query}</code></div>}
 {search.candidates.map((c,i)=><article className="repo-card" key={c.repository}><div className="repo-title"><h3>{i+1}. {c.name}</h3><button aria-label={`打开 ${c.name}`} onClick={()=>open(c.repository)}><ExternalLink size={14}/></button></div><p>{c.description}</p><div className="repo-meta">{c.language||'语言未知'} · ★ {c.stars.toLocaleString()} · {c.updated?new Date(c.updated).toLocaleDateString():'更新日期未知'}</div>
 {c.verified!==undefined&&<p className="muted">{c.verified?'已读取仓库正文':'正文未核实'} · {c.matched?'相关项目':'待核对候选'}</p>}
 {c.reasons?.map((r,j)=><p key={j}>{r}</p>)}{c.unknown?.map((r,j)=><p className="muted" key={j}>待确认：{r}</p>)}{c.verification_warnings?.map(w=><p className="muted" key={w}>{w}</p>)}
 {!!c.citations?.length?<details><summary>核实依据</summary>{c.citations.map((e,j)=><blockquote key={j}>{e.text}<br/><button onClick={()=>open(e.url)}>{e.path}:{e.line}</button></blockquote>)}</details>:!!c.readme_quotes?.length&&<details><summary>README 依据</summary>{c.readme_quotes.map((q,j)=><blockquote key={j}>{q}</blockquote>)}</details>}
 <button className="secondary" onClick={()=>onSelect(c.repository)}>分析此仓库<ChevronRight size={14}/></button></article>)}
 {!search.candidates.length&&<p>{search.summary||'正在搜索…'}</p>}
 {!!search.sources?.length&&<details className="source-details"><summary>网页来源（{search.sources.length}）</summary>{search.sources.map(s=><article className="claim" key={s.url}><button onClick={()=>open(s.url)}>{s.title||s.url}<ExternalLink size={13}/></button><p className="muted">{s.content_kind} · {new URL(s.url).hostname}</p><p>{s.snippet}</p></article>)}</details>}</>;
}
