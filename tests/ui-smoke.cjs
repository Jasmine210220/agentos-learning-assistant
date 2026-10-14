const { _electron: electron } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const root = path.resolve(__dirname, '..');
  fs.mkdirSync(path.join(root, 'test-output/screenshots'), { recursive: true });
  const env = { ...process.env, AGENTOS_DATA_DIR: process.env.AGENTOS_UI_HOME_ONLY ? path.join(root, 'test-output/ui-empty') : path.join(root, 'test-output/live'), AGENTOS_PYTHON: process.env.AGENTOS_PYTHON || 'python' };
  delete env.ELECTRON_RUN_AS_NODE;
  const executablePath = process.env.AGENTOS_TEST_EXE;
  const app = await electron.launch({ ...(executablePath ? {executablePath, args: []} : {args: [root]}), cwd: root, env, timeout: 60000 });
  const appProcess = app.process();
  let exitCode = 1;
  try {
    const page = await app.firstWindow({ timeout: 60000 });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.getByRole('textbox', {name:'消息'}).waitFor({timeout:20000});
    await page.locator('.empty-chat').waitFor();
    await page.screenshot({ path: path.join(root, 'test-output/screenshots/home.png'), fullPage: true });
    await page.getByRole('button', { name: '设置' }).click();
    await page.getByText('DeepSeek 官方直连', { exact: true }).waitFor();
    await page.getByRole('button', { name: '关闭设置', exact: true }).click();
    if (process.env.AGENTOS_UI_HOME_ONLY) { console.log('Home and settings passed'); exitCode = 0; return; }
    await page.waitForFunction(async()=>{const c=await window.agentos.connection();return (await(await fetch(c.url+'/chats',{headers:{Authorization:'Bearer '+c.token}})).json()).length>0;});
    const chatId = await page.evaluate(async()=>{const c=await window.agentos.connection();const h={Authorization:'Bearer '+c.token};const tasks=await(await fetch(c.url+'/tasks',{headers:h})).json();return tasks.find(t=>t.status==='succeeded'&&t.repository.includes('fastapi-cli')).id;});
    const chatTitle=await page.evaluate(async(id)=>{const c=await window.agentos.connection();return (await(await fetch(c.url+'/chats/'+id,{headers:{Authorization:'Bearer '+c.token}})).json()).title;},chatId);
    await page.locator(`[data-chat-id="${chatId}"]`).click();
    await page.locator('.artifact-card').first().waitFor();
    await page.screenshot({path:path.join(root,'test-output/screenshots/chat.png'),fullPage:true});
    if(process.env.AGENTOS_LIVE_CHAT){
      await page.getByRole('textbox',{name:'消息'}).fill('入口是如何找到要运行的 FastAPI 应用的？请简要说明，并给出代码依据。');
      await page.getByRole('button',{name:'发送',exact:true}).click();
      await page.getByRole('button',{name:'停止回复'}).waitFor();
      await page.getByRole('button',{name:'停止回复'}).waitFor({state:'hidden',timeout:180000});
      const answer=await page.locator('.message.assistant').last().innerText();
      assert(!answer.includes('回复失败')&&!answer.includes('未通过校验'),answer);
      assert(await page.locator('.message-citations button').count()>0,answer);
      await page.screenshot({path:path.join(root,'test-output/screenshots/chat-followup.png'),fullPage:true});
    }
    await page.locator('.artifact-card').first().click();
    await page.getByRole('heading', { name: '项目概览', exact: true }).waitFor({ timeout: 20000 });
    await page.screenshot({ path: path.join(root, 'test-output/screenshots/report.png'), fullPage: true });
    await page.locator('.citations button').first().click();
    await page.locator('.code-lines .highlight').first().waitFor();
    assert(await page.locator('.code-lines code').count() > 0);
    await page.screenshot({ path: path.join(root, 'test-output/screenshots/evidence.png'), fullPage: true });
    await page.getByRole('button', { name: '关闭代码', exact: true }).click();
    await page.getByRole('button', { name: '图表', exact: true }).click();
    const image = page.getByRole('img', { name: '根据已读取代码生成的流程图' });
    await image.waitFor();
    await page.waitForFunction(() => { const img = document.querySelector('.diagram-view img'); return img && img.complete && img.naturalWidth > 0; });
    await page.screenshot({ path: path.join(root, 'test-output/screenshots/diagram.png'), fullPage: true });
    await page.getByRole('button', { name: '审核', exact: true }).click();
    await page.getByRole('heading', { name: '审核通过', exact: true }).waitFor();
    await page.getByRole('button', { name: '文件', exact: true }).click();
    assert(await page.locator('.file-row').count() > 0);
    if(process.env.AGENTOS_LIVE_NEW_CHAT){
      await page.getByRole('button',{name:'新对话',exact:true}).click();
      await page.getByRole('textbox',{name:'消息'}).fill('请帮我分析项目入口和核心调用关系。');
      await page.getByRole('button',{name:'发送',exact:true}).click();
      await page.getByRole('button',{name:'停止回复'}).waitFor();
      await page.getByRole('button',{name:'停止回复'}).waitFor({state:'hidden',timeout:180000});
      const answer=await page.locator('.message.assistant').last().innerText();
      assert(/仓库|链接|GitHub/i.test(answer),answer);
      assert(!answer.includes('回复失败'),answer);
      assert.equal(await page.locator('.message.user').count(),1);
      await page.reload();
      await page.locator('.chat-item').filter({hasText:'请帮我分析项目入口和核心调用关系。'}).first().click();
      await page.locator('.message.assistant').waitFor();
      assert.equal(await page.locator('.message.assistant').innerText(),answer);
    }
    await page.reload();
    await page.locator('.chat-item').first().waitFor();
    assert.deepEqual(errors, []);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    assert.equal(overflow, false);
    console.log(JSON.stringify({ok:true, checks:['desktop startup','backend readiness','settings','persisted report','evidence source','rendered SVG','review','scope','reload history','no JS errors','no horizontal overflow']}));
    exitCode = 0;
  } finally {
    const close = app.close();
    await Promise.race([close, new Promise(resolve => setTimeout(resolve, 5000))]);
    if (appProcess.exitCode === null) appProcess.kill();
    process.exitCode = exitCode;
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
