const {_electron:electron}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
(async()=>{
 const root=path.resolve(__dirname,'..');
 const env={...process.env,AGENTOS_DATA_DIR:path.join(root,'test-output/search-route-live'),AGENTOS_PYTHON:path.join(root,'.venv/Scripts/python.exe')};delete env.ELECTRON_RUN_AS_NODE;
 const app=await electron.launch({...(process.env.AGENTOS_TEST_EXE?{executablePath:process.env.AGENTOS_TEST_EXE,args:[]}:{args:[root]}),cwd:root,env,timeout:60000});const proc=app.process();
 try{
  const page=await app.firstWindow();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.getByRole('textbox',{name:'消息'}).waitFor();
  const chats=await page.evaluate(async()=>{const c=await window.agentos.connection();return (await fetch(c.url+'/chats',{headers:{Authorization:'Bearer '+c.token}})).json();});
  await page.locator(`.chat-item[data-chat-id="${chats[0].id}"]`).click();
  await page.locator('.artifact-card').last().click();
  await page.locator('.repo-card').first().waitFor();
  assert((await page.locator('.repo-card').first().innerText()).includes('DietrichGebert/ponytail'));
  await page.getByText('检索记录',{exact:true}).click();
  assert(await page.getByText(/GitHub · 热度/).count()>0);
  await page.getByText('核实依据',{exact:true}).first().click();
  assert(await page.locator('.repo-card blockquote').count()>0);
  fs.mkdirSync(path.join(root,'test-output/screenshots'),{recursive:true});
  await page.screenshot({path:path.join(root,'test-output/screenshots/discovery-v051.png'),fullPage:true});
  await page.getByRole('button',{name:'设置 DeepSeek',exact:true}).click();
  assert.equal(await page.getByRole('textbox',{name:'Tavily API Key'}).getAttribute('type'),'password');
  await page.getByRole('button',{name:'保存搜索设置',exact:true}).click();
  await page.getByRole('status').filter({hasText:'搜索设置已保存'}).waitFor();
  if(process.env.AGENTOS_TEST_NO_KEY_SEARCH){
   await page.getByRole('button',{name:'保存并测试搜索',exact:true}).click();
   await page.getByRole('status').filter({hasText:'GitHub 项目搜索连接正常'}).waitFor({timeout:30000});
  }
  await page.getByRole('textbox',{name:'Tavily API Key'}).scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(root,'test-output/screenshots/search-settings-v04.png'),fullPage:true});
  assert.deepEqual(errors,[]);console.log(JSON.stringify({ok:true,checks:['real search history','query log','repository citations','search settings save','no renderer errors']}));
 }finally{await Promise.race([app.close(),new Promise(r=>setTimeout(r,5000))]);if(proc.exitCode===null)proc.kill();}
})().catch(e=>{console.error(e);process.exitCode=1;});
