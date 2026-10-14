const {_electron:electron}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');

(async()=>{
 const root=path.resolve(__dirname,'..');const env={...process.env,AGENTOS_DATA_DIR:path.join(root,'test-output/v03-live'),AGENTOS_PYTHON:path.join(root,'.venv/Scripts/python.exe')};delete env.ELECTRON_RUN_AS_NODE;
 const app=await electron.launch({...(process.env.AGENTOS_TEST_EXE?{executablePath:process.env.AGENTOS_TEST_EXE,args:[]}:{args:[root]}),cwd:root,env,timeout:60000});const proc=app.process();
 try{
  const page=await app.firstWindow({timeout:60000});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.getByRole('textbox',{name:'消息'}).waitFor();
  const api=async(route,method='GET',data)=>page.evaluate(async({route,method,data})=>{const c=await window.agentos.connection();const response=await fetch(c.url+route,{method,headers:{Authorization:'Bearer '+c.token,'Content-Type':'application/json'},...(data?{body:JSON.stringify(data)}:{})});if(!response.ok)throw Error(await response.text());return response.json();},{route,method,data});
  async function message(content){
   await page.getByRole('button',{name:'新对话',exact:true}).click();
   await page.getByRole('textbox',{name:'消息'}).fill(content);
   await page.getByRole('button',{name:'发送',exact:true}).click();
   await page.getByRole('button',{name:'停止回复'}).waitFor();
   await page.getByRole('button',{name:'停止回复'}).waitFor({state:'hidden',timeout:600000});
   const text=await page.locator('.message.assistant').last().innerText();
   assert(!/回复失败|任务未完成|请求失败|模型名称.*无效/.test(text),text);
   await page.locator('.artifact-card').last().click();
   const chats=await api('/chats');const chat=await api('/chats/'+chats[0].id);
   const linked=chat.messages.filter(m=>m.task_id).at(-1);
   const task=await api('/tasks/'+linked.task_id);
   assert.equal(task.status,'succeeded',task.error||JSON.stringify(task.result.review));
   return text;
  }
  if(!process.env.AGENTOS_UI_REPLAY){
   if(!process.env.AGENTOS_UI_DEBUG_ONLY){
   await message('帮我找用 Python 开发的 RAG 开源项目，希望有 Web 界面，适合阅读学习。请搜索真实 GitHub 仓库并给匹配依据。');
   await page.locator('.repo-card').first().waitFor({timeout:20000});
   assert(await page.locator('.repo-card').count()>0);
   await page.screenshot({path:path.join(root,'test-output/screenshots/search-v03.png'),fullPage:true});
   await page.getByRole('button',{name:'协作',exact:true}).click();assert(await page.locator('.agent-node').count()===5);
   await page.screenshot({path:path.join(root,'test-output/screenshots/agents-v03.png'),fullPage:true});
   await message('用下面的数据画柱状图，单位是件，横轴月份，纵轴销量。\n```csv\n月份,销量\n一月,10\n二月,15\n三月,8\n```');
   await page.getByRole('img',{name:'数据图表',exact:true}).waitFor({timeout:20000});
   await page.waitForFunction(()=>{const img=document.querySelector('.diagram-view img');return img&&img.complete&&img.naturalWidth>0;});
   await page.screenshot({path:path.join(root,'test-output/screenshots/chart-v03.png'),fullPage:true});
   await page.getByRole('button',{name:'数据',exact:true}).click();assert.equal(await page.locator('.data-table tbody tr').count(),3);
   }
   await message('排查这段 Python 代码的错误：输入 b=0 时出现 ZeroDivisionError，给出静态依据和修改建议，不要运行。\n```python\ndef divide(a, b):\n    return a / b\n```');
   await page.locator('.bug-item').first().waitFor({timeout:20000});
   await page.screenshot({path:path.join(root,'test-output/screenshots/bugs-v03.png'),fullPage:true});
  }
  await page.getByRole('button',{name:'新对话',exact:true}).click();
  await page.locator('input[type=file]').first().setInputFiles({name:'example.py',mimeType:'text/plain',buffer:Buffer.from('print("sandbox ok")\n')});
  await page.getByRole('textbox',{name:'代码副本'}).waitFor();
  await page.getByRole('textbox',{name:'代码副本'}).fill('print("edited copy")\n');
  await page.getByRole('button',{name:'保存副本',exact:true}).click();
  await page.locator('.diff pre').waitFor();
  assert((await page.locator('.diff pre').innerText()).includes('+print("edited copy")'));
  await page.screenshot({path:path.join(root,'test-output/screenshots/editor-v03.png'),fullPage:true});
  const runtime=await api('/runtime');
  if(runtime.available){
   await page.getByRole('button',{name:'运行',exact:true}).click();
   await page.getByRole('button',{name:'确认运行',exact:true}).click();
   await page.locator('.run-output').getByText('运行完成',{exact:true}).waitFor({timeout:60000});
   assert((await page.locator('.run-output pre').innerText()).includes('edited copy'));
  }else assert(await page.getByRole('button',{name:'运行',exact:true}).isDisabled());
  await page.getByRole('button',{name:'关闭编辑器'}).click();
  await page.reload();await page.locator('.chat-item').first().waitFor();
  assert.deepEqual(errors,[]);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  console.log(JSON.stringify({ok:true,docker:runtime.available,checks:[...(!process.env.AGENTOS_UI_REPLAY?(process.env.AGENTOS_UI_DEBUG_ONLY?['real static bug diagnosis']:['real natural-language search and review','repository cards','agent events','real data chart','data provenance','real static bug diagnosis']):[]),'copy editor','diff','runtime gate','history','no JS errors']}));
 }finally{
  await Promise.race([app.close(),new Promise(r=>setTimeout(r,5000))]);if(proc.exitCode===null)proc.kill();
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
