// Installed-app lifecycle of a saved REAL failed task; never a successful run.
const {_electron: electron}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const source=path.join(root,'test-output/learning-desktop-061');
const out=path.join(root,'test-output/learning-desktop-recovery-061');
const saved=JSON.parse(fs.readFileSync(path.join(source,'task.json'),'utf8')).task;
assert.equal(saved.status,'needs_attention');
if(fs.existsSync(path.join(out,'evidence.json')))throw Error('Preserve earlier evidence');
fs.mkdirSync(out,{recursive:true});
const exe=path.join(root,'test-output/验收 学习061/AgentOS Lite/AgentOS Lite.exe');
const env={...process.env,AGENTOS_DATA_DIR:path.join(source,'data')};
for(const key of Object.keys(env))if(key.toLowerCase()==='path'||['ELECTRON_RUN_AS_NODE','AGENTOS_DEV_URL','AGENTOS_PYTHON','DEEPSEEK_API_KEY'].includes(key))delete env[key];
env.PATH=process.env.SystemRoot+'\\System32;'+process.env.SystemRoot;
const evidence={kind:'installed_app_saved_failed_task_lifecycle',task_id:saved.id,task_status:saved.status,real_model_rerun:false,executable:exe,checks:[]};
let app,connection;
async function api(route){const r=await fetch(connection.url+route,{headers:{Authorization:'Bearer '+connection.token}});assert(r.ok);return r.json();}
async function launch(){app=await electron.launch({executablePath:exe,env,timeout:60000});const page=await app.firstWindow();await page.getByRole('textbox',{name:'消息',exact:true}).waitFor();connection=await page.evaluate(()=>window.agentos.connection());assert.equal((await api('/health')).version,require('../package.json').version);return page;}
(async()=>{try{
 let page=await launch();
 const task=await api('/tasks/'+saved.id);assert.equal(task.status,'needs_attention');assert.deepEqual(task.result,saved.result);
 const chats=await api('/chats');await page.locator(`[data-chat-id="${chats[0].id}"]`).click();
 await page.getByRole('button',{name:'查看成果',exact:true}).first().click();
 await page.getByText('任务状态：需关注 · 未通过',{exact:false}).waitFor();
 for(const tab of ['解读','学习','流程图','来源','审核','协作']){
  await page.getByRole('button',{name:tab,exact:true}).click();
  if(tab==='流程图')await page.getByRole('img',{name:'根据已读取代码生成的流程图'}).waitFor();
  await page.screenshot({path:path.join(out,tab+'.png')});
 }
 await page.getByRole('button',{name:'学习',exact:true}).click();await page.locator('.result-body .citations button').first().click();await page.locator('.source-modal .highlight').first().waitFor();await page.screenshot({path:path.join(out,'source.png')});await page.getByRole('button',{name:'关闭代码',exact:true}).click();
 const exported=path.join(out,'failed-report.md');await app.evaluate(({dialog},filePath)=>{dialog.showSaveDialog=async()=>({canceled:false,filePath});},exported);await page.getByRole('button',{name:'导出报告',exact:true}).click();
 for(let i=0;i<30&&!fs.existsSync(exported);i++)await new Promise(r=>setTimeout(r,100));
 assert(fs.readFileSync(exported,'utf8').includes('needs_attention'));
 evidence.checks.push('Saved real failure remains needs_attention; report, learning, SVG, citations and native export available');
 const url=connection.url;await app.close();app=null;
 let closed=false;for(let i=0;i<30;i++){try{await fetch(url+'/health',{signal:AbortSignal.timeout(500)});}catch{closed=true;break;}await new Promise(r=>setTimeout(r,200));}assert(closed);
 page=await launch();const restored=await api('/tasks/'+saved.id);assert.equal(restored.status,'needs_attention');assert.deepEqual(restored.result,saved.result);assert.deepEqual(restored.usage,saved.usage);
 evidence.checks.push('Backend port released; restart preserved failed state, artifacts and usage without model calls');
 evidence.passed=true;
}catch(e){evidence.passed=false;evidence.error=e.message;process.exitCode=1;}finally{if(app)await app.close();fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify(evidence,null,2));console.log(JSON.stringify(evidence));}})();
