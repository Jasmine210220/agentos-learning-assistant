const {_electron:electron}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
 const root=path.resolve(__dirname,'..');
 const env={...process.env,AGENTOS_DATA_DIR:path.join(root,'test-output/quality-ui')};delete env.ELECTRON_RUN_AS_NODE;
 const app=await electron.launch({executablePath:process.env.AGENTOS_TEST_EXE,args:[],cwd:root,env,timeout:60000});
 const proc=app.process();
 try{
  const page=await app.firstWindow();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.locator('.chat-item').first().click();
  assert((await page.locator('.message.assistant .message-text').innerText()).length>200);
  await page.locator('.artifact-card').first().click();
  const simple=page.locator('.improvement-card').filter({has:page.getByRole('button',{name:'应用到副本',exact:true})}).first();
  await simple.getByText('修改差异',{exact:true}).click();
  assert((await simple.locator('.diff pre').innerText()).includes('+++'));
  await page.screenshot({path:path.join(root,'test-output/screenshots/quality-052.png'),fullPage:true});
  let applied=0;
  while(await page.getByRole('button',{name:'应用到副本',exact:true}).count()){
   await page.getByRole('button',{name:'应用到副本',exact:true}).first().click();
   await page.getByRole('textbox',{name:'代码副本'}).waitFor();
   await page.getByRole('button',{name:'关闭编辑器'}).click();
   await page.getByRole('button',{name:'已应用 · 打开副本',exact:true}).nth(applied).waitFor();
   applied++;assert(applied<=3);
  }
  assert.equal(applied,2);
  await page.getByRole('button',{name:'还原全部修改',exact:true}).click();
  await page.getByRole('textbox',{name:'代码副本'}).waitFor();
  await page.getByRole('button',{name:'关闭编辑器'}).click();
  await page.locator('.chat-item').nth(1).click();
  await page.locator('.artifact-card').first().click();
  const complex=page.locator('.improvement-card').filter({hasText:'交给开发工具实施'}).first();
  await complex.getByText('给 Codex 的提示词',{exact:true}).click();
  assert((await complex.locator('textarea').inputValue()).includes('pubmed.py'));
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({ok:true,checks:['real project answer','reviewed diff','apply both edits to copy','restore copy','complex handoff prompt','no renderer errors']}));
 }finally{await Promise.race([app.close(),new Promise(r=>setTimeout(r,5000))]);if(proc.exitCode===null)proc.kill();}
})().catch(e=>{console.error(e);process.exitCode=1;});
