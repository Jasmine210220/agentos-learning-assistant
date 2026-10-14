// Installed-app integration acceptance; real model and GitHub when LEARNING_LIVE=1.
const {_electron: electron} = require('playwright');
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const version = require('../package.json').version;
const out = path.join(root, process.env.LEARNING_UI_OUTPUT || 'test-output/learning-desktop-061');
const data = path.join(out, 'data');
if(fs.existsSync(path.join(out,'evidence.json'))) throw new Error('Use a fresh evidence output directory');
fs.mkdirSync(data, {recursive:true});
const live = process.env.LEARNING_LIVE === '1';
const repository = process.env.LEARNING_REPOSITORY || 'https://github.com/fastapi/fastapi-cli';
const ref = process.env.LEARNING_REF || '093be9ca19f2ef44ba523aae0a98d6c9ea2e0888';
const fixture = require('../tests/fixtures/fastapi-cli.json');
const goal = process.env.LEARNING_GOAL || fixture.goal;
// Live credentials must arrive in memory from the launcher. Never copy settings
// files into evidence or project directories.
const exe = process.env.AGENTOS_TEST_EXE || path.join(root, 'test-output/验收 学习061/AgentOS Lite/AgentOS Lite.exe');
const env = {...process.env, AGENTOS_DATA_DIR:data};
delete env.ELECTRON_RUN_AS_NODE;
delete env.AGENTOS_DEV_URL;
delete env.AGENTOS_PYTHON;
for (const key of Object.keys(env)) if (key.toLowerCase() === 'path') delete env[key];
env.PATH = process.env.SystemRoot + '\\System32;' + process.env.SystemRoot;
const evidence = {kind:'installed_or_packaged_ui_integration_not_content_acceptance',content_acceptance:'pending', version, deployment:process.env.LEARNING_DEPLOYMENT || 'installed', real_model:live, executable:exe, python_node_path_removed:true, checks:[]};
const errors = [];
let app, connection;
async function launch() {
  app = await electron.launch({executablePath:exe, env, timeout:60000});
  const page = await app.firstWindow();
  page.on('pageerror', e => errors.push(e.message));
  await page.getByRole('textbox', {name:'消息', exact:true}).waitFor();
  connection = await page.evaluate(() => window.agentos.connection());
  return page;
}
async function api(route) {
  const response = await fetch(connection.url + route, {headers:{Authorization:'Bearer '+connection.token}, signal:AbortSignal.timeout(10000)});
  assert(response.ok, route);
  return response.json();
}
(async()=>{
  try {
    let page = await launch();
    assert.equal((await api('/health')).version, version);
    await page.screenshot({path:path.join(out,'startup.png')});
    evidence.checks.push('Application executable and bundled backend started without Python/Node PATH');
    assert.equal(await page.getByRole('button',{name:'添加目录',exact:true}).count(),0);
    if (live) {
      assert((await api('/health')).configured, 'Model credential unavailable');
      await page.getByRole('button',{name:'设置',exact:false}).click();
      await page.getByRole('textbox',{name:'模型',exact:true}).fill(process.env.LEARNING_MODEL || 'deepseek-flash');
      await page.getByRole('textbox',{name:'DeepSeek 密钥',exact:true}).fill(process.env.DEEPSEEK_API_KEY);
      await page.getByRole('button',{name:'保存',exact:true}).click();
      await page.getByText('已保存',{exact:true}).waitFor();
      assert.equal(await page.getByRole('textbox',{name:'DeepSeek 密钥',exact:true}).inputValue(),'');
      await page.screenshot({path:path.join(out,'settings-saved.png')});
      await page.getByRole('button',{name:'关闭设置',exact:true}).click();
      evidence.checks.push('Model configured through UI; key cleared from input and stored by Windows DPAPI');
      delete env.DEEPSEEK_API_KEY;

      await page.getByRole('textbox',{name:'已有基础',exact:true}).fill(process.env.LEARNING_BACKGROUND || fixture.background);
      await page.getByRole('textbox',{name:'仓库版本',exact:true}).fill(ref);
      await page.getByRole('textbox',{name:'消息',exact:true}).fill(repository + ' ' + goal);
      await page.getByRole('button',{name:'发送',exact:true}).click();
      let task, lastStatus, progressCaptured=false;
      const started = Date.now();
      while (Date.now()-started < 660000) {
        const tasks = await api('/tasks');
        if (tasks.length) {
          task = await api('/tasks/'+tasks[0].id);
          if(task.status!==lastStatus) { console.log(JSON.stringify({task:task.id,status:task.status,usage:task.usage})); lastStatus=task.status; }
          if (!progressCaptured && ['analyzing','teaching','drawing','reviewing'].includes(task.status)) {
            await page.getByRole('region',{name:'任务协作进度'}).waitFor();
            await page.screenshot({path:path.join(out,'running-progress.png')});
            await page.getByRole('button',{name:'展开协作详情',exact:true}).click();
            await page.getByRole('button',{name:'协作',exact:true}).waitFor();
            progressCaptured=true;
          }
          if (['succeeded','failed','needs_attention','cancelled','interrupted'].includes(task.status)) break;
        }
        await new Promise(r=>setTimeout(r,1500));
      }
      assert(task, 'No task created');
      const events = await api('/tasks/'+task.id+'/events');
      fs.writeFileSync(path.join(out,'task.json'),JSON.stringify({task,events},null,2));
      await page.screenshot({path:path.join(out,'task-finished.png')});
      evidence.task_status=task.status; evidence.model=process.env.LEARNING_MODEL||'deepseek-flash';
      evidence.input={repository,ref,goal,background:process.env.LEARNING_BACKGROUND||fixture.background};
      assert.equal(task.payload.kind,'learn');
      assert.equal(task.result.learning.exercises.length,3);
      assert.equal(task.result.snapshot.commit,ref);
      assert.equal(task.repository,repository);
      // The top-bar button toggles an already open panel; the message card opens it.
      await page.locator('.artifact-card').first().click();
      for (const tab of ['解读','学习','数据传递图','来源','审核','协作']) {
        await page.getByRole('button',{name:tab,exact:true}).click();
        if (tab==='数据传递图') await page.getByRole('img',{name:'根据已读取代码生成的数据传递图'}).waitFor();
        if (tab==='学习') assert.equal(await page.locator('.result-body .claim').count(),task.result.learning.reading_order.length+3);
        await page.screenshot({path:path.join(out,tab+'.png')});
      }
      await page.getByRole('button',{name:'学习',exact:true}).click();
      await page.locator('.result-body .citations button').first().click();
      await page.locator('.source-modal .highlight').first().waitFor();
      await page.screenshot({path:path.join(out,'source.png')});
      await page.getByRole('button',{name:'关闭代码',exact:true}).click();
      const exported = path.join(out,'report.md');
      await app.evaluate(({dialog}, filePath) => {dialog.showSaveDialog=async()=>({canceled:false,filePath});}, exported);
      await page.getByRole('button',{name:'导出报告',exact:true}).click();
      for(let i=0;i<30&&!fs.existsSync(exported);i++) await new Promise(r=>setTimeout(r,100));
      assert(fs.readFileSync(exported,'utf8').includes('三个练习（未执行）'));
      evidence.checks.push('Real chat → GitHub → independent learning roles → SVG → source citations → exported report');
      evidence.task_id = task.id;
      const previousUrl=connection.url;
      await app.close(); app=null;
      let closed=false;
      for(let i=0;i<30;i++) {try {await fetch(previousUrl+'/health',{signal:AbortSignal.timeout(500)});} catch {closed=true;break;} await new Promise(r=>setTimeout(r,200));}
      assert(closed,'Backend port not released');
      page=await launch();
      const restored=await api('/tasks/'+task.id);
      assert.deepEqual(restored.result,task.result);
      assert.equal(restored.status,task.status);
      assert((await api('/health')).configured,'DPAPI model setting not restored');
      const chats=await api('/chats');
      await page.locator(`[data-chat-id="${chats[0].id}"]`).click();
      await page.locator('.artifact-card').first().click();
      await page.getByRole('button',{name:'学习',exact:true}).click();
      await page.getByText('历史任务',{exact:true}).waitFor();
      assert.equal(await page.locator('.task-progress .spin').count(),0);
      await page.screenshot({path:path.join(out,'restart.png')});
      evidence.checks.push('Backend exited; complete learning artifacts survived application restart without regeneration');
    }
    assert.deepEqual(errors,[]);
    evidence.integration_passed=true;
    evidence.passed=!live||evidence.task_status==='succeeded';
    if(!evidence.passed)process.exitCode=1;
  } catch(e) {evidence.passed=false;evidence.error=e.message;process.exitCode=1;}
  finally {
    if(app) await app.close();
    const credentialFile=path.join(data,'model-settings.json');
    if(fs.existsSync(credentialFile))fs.unlinkSync(credentialFile); // Only this isolated test's credential copy.
    fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify(evidence,null,2));
    console.log(JSON.stringify(evidence));
  }
})();
