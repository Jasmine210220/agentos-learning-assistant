const {_electron:electron}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');

(async()=>{
 const root=path.resolve(__dirname,'..');
 const env={...process.env,AGENTOS_DATA_DIR:path.join(root,'test-output/search-route-ui'),AGENTOS_PYTHON:path.join(root,'.venv/Scripts/python.exe')};delete env.ELECTRON_RUN_AS_NODE;
 const app=await electron.launch({...(process.env.AGENTOS_TEST_EXE?{executablePath:process.env.AGENTOS_TEST_EXE,args:[]}:{args:[root]}),cwd:root,env,timeout:60000});
 const appProcess=app.process();
 try{
  const page=await app.firstWindow();
  await page.getByRole('textbox',{name:'消息'}).fill('帮我搜一下优化代码的项目，似乎是一个马尾辫的logo，具体是做精简ai写的代码的一个skill应该是');
  await page.getByRole('button',{name:'发送',exact:true}).click();
  const linked=await page.waitForFunction(async()=>{
   const c=await window.agentos.connection();const get=async p=>(await fetch(c.url+p,{headers:{Authorization:'Bearer '+c.token}})).json();
   const chats=await get('/chats');if(!chats.length)return false;
   const chat=await get('/chats/'+chats[0].id);const m=chat.messages.at(-1);
   if(!m?.task_id)return false;
   const task=await get('/tasks/'+m.task_id);return {kind:task.payload.kind,content:m.content};
  },null,{timeout:15000});
  const result=await linked.jsonValue();assert.equal(result.kind,'search');assert(!/确认|无法.*搜索/.test(result.content));
  await page.getByRole('button',{name:'停止回复'}).click();
  await page.getByRole('button',{name:'停止回复'}).waitFor({state:'hidden'});
  console.log(JSON.stringify({ok:true,checks:['packaged explicit search starts without confirmation','cancellation']}));
 }finally{await Promise.race([app.close(),new Promise(r=>setTimeout(r,5000))]);if(appProcess.exitCode===null)appProcess.kill();}
})().catch(e=>{console.error(e);process.exitCode=1;});
