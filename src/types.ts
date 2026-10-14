export type Evidence = { path: string; start_line: number; end_line: number };
export type Claim = { title: string; explanation: string; evidence: Evidence[]; inferred: boolean };
export type Task = { id: string; created: string; updated: string; repository: string; goal: string; status: string; error?: string; payload?: {ref: string; kind?:string;attachment_id?:string}; usage?: {input: number; output: number; calls: number}; result?: Result };
export type Bug = {title:string;severity:string;trigger:string;cause:string;impact:string;suggestion:string;evidence:Evidence[];verification:string;patch:string};
export type Diagram = { title: string; nodes: {id: string; label: string; evidence: Evidence[]}[]; edges: {source: string; target: string; label: string; inferred: boolean; evidence: Evidence[]}[] };
export type Attachment = {id:string;files:{path:string;content:string;lines:number;bytes:number}[];bytes:number;diff?:string};
export type Api = (route:string,options?:RequestInit)=>Promise<Response>;
export type Result = {
  mode?: string;
  learning?: {background:string;objective:string;flow_indices:number[];reading_order:{title:string;reason:string;module_index:number;concepts:string[];evidence:Evidence[]}[];exercises:{title:string;objective:string;instructions:string[];acceptance:string[];evidence:Evidence[]}[];limitations:string[]};
  improvement?:{summary:string;base_attachment_id:string;current_attachment_id?:string;ready:boolean;limitations:string[];recommendations:{id:string;title:string;kind:'simple'|'complex';reason:string;steps:string[];acceptance:string[];evidence:Evidence[];citations:{url:string;text:string;path:string;line:number}[];diff:string;checks:string[];prompt:string;applied_attachment_id?:string}[]};
  search_error?:string;
  plan?: { objective: string; focus: string[]; acceptance: string[] };
  snapshot?: { excerpted?: boolean; analysis_bytes?: number; commit: string; ref: string; files: {path: string; lines: number; analysis_ranges?: number[][]}[]; skipped: {path: string; reason: string}[]; total_tree_files: number; tree_truncated: boolean; read_bytes: number };
  analysis?: { summary: string; stack: string[]; modules: Claim[]; flow: Claim[]; limitations: string[];risks?:Bug[];technologies?:Claim[];goal_answers?:Claim[];scope_complete?:boolean;scope_reason?:string };
  diagram?: Diagram;
  diagram_kind?:string;diagram_semantics?:string;
  architecture?:{diagram:Diagram;mermaid:string;rendered:boolean};
  bugs?:{summary:string;findings:Bug[];limitations:string[]};
  search?:{query:string;conditions:string[];summary?:string;total?:number;incomplete?:boolean;mode?:string;outcome?:string;stop_reason?:string;warnings?:string[];usage?:{web_requests:number;github_queries:number};rounds?:{number:number;reason:string;queries:{source:string;query:string;sort?:string;count:number;status:string}[]}[];sources?:{url:string;title:string;snippet:string;content_kind:string}[];candidates:{repository:string;name:string;description:string;language:string;stars:number;updated:string;verified?:boolean;matched?:boolean;confidence?:string;verification_warnings?:string[];citations?:{path:string;line:number;text:string;url:string}[];reasons?:string[];unknown?:string[];readme_quotes?:string[]}[]};
  dataset?:{columns:string[];rows:Record<string,string|number|null>[];source:string;raw:string};
  chart?:{title:string;kind:string;x:string;y:string[];unit:string;source:string;rendered:boolean};
  mermaid?: string; rendered?: boolean;
  review?: {passed: boolean; summary: string; issues: string[];findings?:{target:string;quote:string;problem:string;evidence:Evidence[]}[];decisions?:{target:string;quote:string;verdict:string}[];independent_audit?:{summary:string}};
  checks?: {evidence_valid: boolean; diagram_rendered: boolean; errors: string[]; round: number};
};
export type Event = { id: number; time: string; role: string; kind: string; summary: string; details: Record<string, unknown> };
export type Connection = {url: string; token: string};
declare global { interface Window { agentos?: {connection: () => Promise<Connection>;openExternal:(url:string)=>Promise<void>;saveFile:(name:string,bytes:Uint8Array)=>Promise<boolean>;copyText:(text:string)=>Promise<void>} } }
