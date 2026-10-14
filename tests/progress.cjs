// Pure event-state regression: no model, no pre-recorded successful UI task.
const assert=require('node:assert/strict'),fs=require('node:fs'),ts=require('typescript');
const mod={exports:{}};
new Function('exports',ts.transpileModule(fs.readFileSync('src/progress.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText)(mod.exports);
const {taskProgress}=mod.exports;
const events=[{id:1,role:'Code Agent',kind:'started',summary:'读取源码'},{id:2,role:'Code Agent',kind:'completed',summary:'解读保存'},{id:3,role:'Reviewer Agent',kind:'started',summary:'核对成果'}];
let p=taskProgress({status:'reviewing'},events);
assert(p.active);assert.equal(p.role,'自动审核');assert.equal(p.action,'核对成果');
assert.equal(p.stages.find(s=>s.id==='Code Agent').state,'完成');
assert.equal(p.stages.find(s=>s.id==='Learning Agent').state,'等待');
for(const [status,expected] of [['needs_attention','未通过'],['failed','未通过'],['interrupted','中断'],['cancelled','中断']]){
 p=taskProgress({status},events);assert(!p.active);assert.equal(p.stages.at(-1).state,expected);
}
p=taskProgress({status:'succeeded'},[...events,{id:4,role:'Reviewer Agent',kind:'completed'}]);
assert(!p.active);assert(p.ended);assert.equal(p.stages.at(-1).state,'完成');
assert(!taskProgress({status:'queued'},[]).active);
assert(!taskProgress({status:'reviewing'},events,true).active);
assert.match(taskProgress({status:'reviewing'},events,true).action,/无法确认/);
console.log('Progress state regression passed: running, waiting, complete, rejected, interrupted, history, offline');
