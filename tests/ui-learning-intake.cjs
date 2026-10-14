// Packaged UI intake test. No repository/model task is started or simulated.
const {_electron:electron}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),version=require('../package.json').version;
const out=path.join(root,'test-output/learning-intake-062');
if(fs.existsSync(path.join(out,'evidence.json')))throw Error('Preserve previous evidence');
fs.mkdirSync(out,{recursive:true});
const env={...process.env,AGENTOS_DATA_DIR:path.join(out,'data'),DEEPSEEK_API_KEY:'intake-test-unused-no-model-calls'};
for(const k of Object.keys(env))if(k.toLowerCase()==='path'||['ELECTRON_RUN_AS_NODE','AGENTOS_DEV_URL','AGENTOS_PYTHON'].includes(k))delete env[k];
env.PATH=process.env.SystemRoot+'\\System32;'+process.env.SystemRoot;
let app,connection;
const evidence={version,kind:'packaged_link_only_intake_no_model_calls',repository:'https://github.com/Jasmine210220/zhiying-ai-customer-service',checks:[]};
async function api(route){const r=await fetch(connection.url+route,{headers:{Authorization:'Bearer '+connection.token}});assert(r.ok);return r.json();}
async function launch(){app=await electron.launch({executablePath:path.join(root,`release-v${version}/win-unpacked/AgentOS Lite.exe`),env,timeout:60000});const page=await app.firstWindow();await page.getByRole('textbox',{name:'消息',exact:true}).waitFor();connection=await page.evaluate(()=>window.agentos.connection());assert.equal((await api('/health')).version,version);return page;}
(async()=>{try{
 let page=await launch();await page.getByRole('textbox',{name:'消息',exact:true}).fill(evidence.repository);await page.getByRole('button',{name:'发送',exact:true}).click();await page.getByText('收到项目链接。你想先理解哪一部分？',{exact:false}).waitFor();assert.deepEqual(await api('/tasks'),[]);await page.screenshot({path:path.join(out,'link-only.png')});evidence.checks.push('User repository URL receives goal guidance; no task created');
 const chats=await api('/chats');await app.close();app=null;page=await launch();await page.locator(`[data-chat-id="${chats[0].id}"]`).click();await page.getByRole('textbox',{name:'消息',exact:true}).fill('好的');await page.getByRole('button',{name:'发送',exact:true}).click();await page.getByText('请补充想理解的流程',{exact:false}).waitFor();assert.deepEqual(await api('/tasks'),[]);await page.screenshot({path:path.join(out,'restart.png')});evidence.checks.push('Pending link survives restart; acknowledgement does not trigger analysis');evidence.passed=true;
}catch(e){evidence.passed=false;evidence.error=e.message;process.exitCode=1;}finally{if(app)await app.close();fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify(evidence,null,2));console.log(JSON.stringify(evidence));}})();
