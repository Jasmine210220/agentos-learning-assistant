import asyncio
import json
import re
import uuid
from typing import Literal
from pydantic import BaseModel, Field
from .store import now
from .models import Evidence, TaskInput
from .llm import DeepSeek, source_context
from .workflow import validate_evidence


def task_answer(result):
    """Show the useful conclusion in chat without buying another summarization call."""
    improvement=result.get('improvement')
    if improvement:
        parts=[improvement.get('summary','')]
        for item in improvement.get('recommendations',[])[:3]:
            paths='、'.join(dict.fromkeys(e['path'] for e in item.get('evidence',[])))
            parts.append(f"**{item['title']}**\n\n{item['reason']}\n\n涉及：{paths}。"+
                         ('可查看差异后应用到副本。' if item['kind']=='simple' else '具体步骤、验收条件和开发工具提示词已放在成果中。'))
        if not improvement.get('ready'): parts.append('建议尚未通过审核，请先查看审核问题。')
        return '\n\n'.join(p for p in parts if p)
    search=result.get('search')
    if search:
        parts=[search.get('summary','')]
        for c in search.get('candidates',[]):
            if not c.get('matched'): continue
            parts.append(f"**[{c['name']}]({c['repository']})** — "+'；'.join(c.get('reasons',[])[:2]))
            if len(parts)>=4: break
        return '\n\n'.join(p for p in parts if p)
    return result.get('analysis',{}).get('summary','') or result.get('bugs',{}).get('summary','')


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=6000)
    attachment_id: str = Field(default='',max_length=32)
    mode: Literal['learn', 'legacy'] = 'learn'
    learner_background: str = Field(default='具备基础编程知识；具体基础未提供', max_length=1000)
    ref: str = Field(default='', max_length=120)


class Reply(BaseModel):
    answer: str = Field(min_length=1, max_length=6000)
    action: Literal['reply', 'analyze', 'search', 'chart', 'debug', 'improve']
    repository: str = ''
    goal: str = ''
    ref: str = ''
    evidence: list[Evidence] = Field(default_factory=list)
    diagram_kind: Literal['flow','architecture','both'] = 'flow'


def explicit_search_request(content):
    """Recognize direct project discovery requests, never quoted/code instructions."""
    request=content.split('```',1)[0].strip()
    if re.search(r'(不要|不用|别|暂不|先不).{0,4}(搜|找|检索)|不需要.{0,4}(搜|找)',request):
        return False
    match=re.match(r'^(?:(?:请|麻烦你?|可以|能不能|能否|你能|你可以)\s*)?(?:(?:帮我|给我|替我)\s*)?(搜索|搜搜|搜|查找|检索|找找|找)\s*(.*)',request,re.I)
    if not match: return False
    verb,subject=match.groups()
    if re.match(r'(功能|按钮|框|接口|结果|索引|算法|引擎|不到|不到的)',subject): return False
    return len(subject.strip())>=3 and any(word in subject.lower() for word in ['项目','仓库','开源','skill','技能','插件','工具','github'])


def explicit_improvement_request(content):
    request=content.split('```',1)[0].strip()
    return bool(re.match(r'^(?:(?:请|帮我|替我|给我|继续|麻烦你?|你可以|需要|希望)\s*)*(修改|改进|优化|重构|对比|借鉴|增加|新增)',request))


class Chats:
    def __init__(self, store, runner, create_task, llm_factory=DeepSeek):
        self.store, self.runner, self.create_task = store, runner, create_task
        self.llm_factory, self.jobs = llm_factory, {}
        store.db.executescript('''
        CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,title TEXT,updated TEXT);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id TEXT,role TEXT,content TEXT,task_id TEXT,status TEXT,evidence TEXT DEFAULT '[]');
        ''')
        if 'usage' not in {r[1] for r in store.db.execute('PRAGMA table_info(messages)')}:
            store.db.execute("ALTER TABLE messages ADD COLUMN usage TEXT NOT NULL DEFAULT '{}'")
        if 'attachment_id' not in {r[1] for r in store.db.execute('PRAGMA table_info(messages)')}:
            store.db.execute("ALTER TABLE messages ADD COLUMN attachment_id TEXT NOT NULL DEFAULT ''")
        if 'learning_request' not in {r[1] for r in store.db.execute('PRAGMA table_info(messages)')}:
            store.db.execute("ALTER TABLE messages ADD COLUMN learning_request TEXT NOT NULL DEFAULT '{}'")
        with store.db:
            store.db.execute("UPDATE messages SET status='interrupted',content=CASE WHEN content='' THEN '回复已中断，请重试。' ELSE content END WHERE status='pending'")
            for task in reversed(store.list()):
                if store.db.execute('SELECT 1 FROM messages WHERE task_id=?', (task['id'],)).fetchone(): continue
                self.create(task['repository'].split('/')[-1] or task['goal'][:36], task['id'])
                self.add(task['id'], 'user', task['repository']+'\n'+task['goal'])
                full = store.get(task['id'])
                self.add(task['id'], 'assistant', task_answer(full['result']) or task['error'] or '任务已保存。', task['id'])

    def create(self, title='新对话', chat_id=None):
        chat_id = chat_id or uuid.uuid4().hex
        with self.store.db: self.store.db.execute('INSERT INTO chats VALUES(?,?,?)', (chat_id,title,now()))
        return self.get(chat_id)

    def list(self):
        return [dict(r) for r in self.store.db.execute('SELECT * FROM chats ORDER BY updated DESC')]

    def get(self, chat_id):
        row = self.store.db.execute('SELECT * FROM chats WHERE id=?', (chat_id,)).fetchone()
        if not row: raise KeyError(chat_id)
        messages = [{**dict(r), 'evidence':json.loads(r['evidence']), 'usage':json.loads(r['usage'])} for r in self.store.db.execute('SELECT * FROM messages WHERE chat_id=? ORDER BY id', (chat_id,))]
        return {**dict(row), 'messages':messages}

    def add(self, chat_id, role, content, task_id=None, status='done'):
        with self.store.db:
            cur = self.store.db.execute('INSERT INTO messages(chat_id,role,content,task_id,status) VALUES(?,?,?,?,?)', (chat_id,role,content,task_id,status))
            self.store.db.execute('UPDATE chats SET updated=? WHERE id=?', (now(),chat_id))
        return cur.lastrowid

    def update(self, message_id, content, status='done', task_id=None, evidence=None):
        with self.store.db:
            self.store.db.execute('UPDATE messages SET content=?,status=?,task_id=COALESCE(?,task_id),evidence=? WHERE id=?', (content,status,task_id,json.dumps(evidence or []),message_id))

    def send(self, chat_id, content, attachment_id='', mode='legacy', learner_background='', ref=''):
        chat = self.get(chat_id)
        if chat_id in self.jobs: raise ValueError('请等待当前回复完成')
        if len(self.jobs) >= 5: raise ValueError('当前任务较多，请稍后再试')
        if not chat['messages']:
            with self.store.db: self.store.db.execute('UPDATE chats SET title=? WHERE id=?', (content[:36],chat_id))
        if attachment_id:
            from .workspaces import get_attachment
            get_attachment(self.store,attachment_id)
        user_id=self.add(chat_id,'user',content)
        if attachment_id:
            with self.store.db: self.store.db.execute('UPDATE messages SET attachment_id=? WHERE id=?',(attachment_id,user_id))
        message_id = self.add(chat_id,'assistant','','', 'pending')
        job = asyncio.create_task(self.respond(chat_id,message_id,mode,learner_background,ref))
        self.jobs[chat_id] = job
        job.add_done_callback(lambda _: self.jobs.pop(chat_id,None))
        return self.get(chat_id)

    async def respond(self, chat_id, message_id, mode='legacy', learner_background='', ref=''):
        if mode == 'learn':
            return await self.respond_learning(chat_id, message_id, learner_background, ref)
        task_id = None
        try:
            async with asyncio.timeout(660):
                messages = self.get(chat_id)['messages']
                previous = next((m['task_id'] for m in reversed(messages) if m['task_id']), None)
                context = {'messages':[{'role':m['role'],'content':m['content']} for m in messages[-13:] if m['content']]}
                attachment_id=next((m['attachment_id'] for m in reversed(messages) if m['role']=='user' and m['attachment_id']), '')
                if attachment_id:
                    from .workspaces import get_attachment
                    context['attachment']=get_attachment(self.store,attachment_id)
                snapshot = None
                if previous:
                    task = self.store.get(previous)
                    context['task'] = {k:task[k] for k in ('repository','goal','status','result')}
                    if task['result'].get('search'):
                        search=task['result']['search']
                        compact={k:search[k] for k in ('conditions','summary','outcome','warnings','rounds') if k in search}
                        compact['candidates']=[{k:c[k] for k in ('repository','description','matched','reasons','unknown','citations') if k in c} for c in search.get('candidates',[])]
                        context['task']['result']={'search':compact,'review':task['result'].get('review')}
                    if task['result'].get('improvement'):
                        improvement=task['result']['improvement']
                        context['task']['result']={**context['task']['result'],'improvement':{'summary':improvement['summary'],'recommendations':[{k:r[k] for k in ('id','title','kind','reason','steps','acceptance')} for r in improvement['recommendations']]}}
                        if not messages[-2].get('attachment_id'):
                            attachment_id=improvement.get('current_attachment_id',improvement['base_attachment_id'])
                            from .workspaces import get_attachment
                            context['attachment']=get_attachment(self.store,attachment_id)
                    path = self.store.workspace(previous)/'snapshot.json'
                    if path.exists():
                        snapshot = json.loads(path.read_text(encoding='utf-8'))
                        context['source'] = source_context(snapshot,task['result'].get('improvement'))
                def record_usage(usage):
                    with self.store.db: self.store.db.execute('UPDATE messages SET usage=? WHERE id=?',(json.dumps(usage),message_id))
                llm = self.llm_factory(record_usage, lambda: None)
                explicit_repository=re.match(r'^\s*(?:请|帮我)?\s*分析(?:一下)?\s*(https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)',messages[-2]['content'])
                if explicit_repository:
                    request=messages[-2]['content']
                    diagram_kind='both' if '架构图' in request and '流程图' in request else 'architecture' if '架构图' in request else 'flow'
                    reply=Reply(answer='正在分析。',action='analyze',repository=explicit_repository.group(1).rstrip('.'),goal=request[:2000],diagram_kind=diagram_kind)
                elif explicit_search_request(messages[-2]['content']):
                    reply=Reply(answer='正在对比项目。' if attachment_id else '正在搜索。',action='improve' if attachment_id else 'search',goal=messages[-2]['content'][:2000])
                elif attachment_id and explicit_improvement_request(messages[-2]['content']):
                    reply=Reply(answer='正在分析修改方案。',action='improve',goal=messages[-2]['content'][:2000])
                else:
                    reply = await llm.ask('对话助手', '你是带真实工具的应用路由器。应用提供 GitHub 搜索、源码读取、绘图与审核；不是只能聊天的裸模型。用户明确要求的只读搜索和分析已获授权，直接选择工具，不询问是否确认搜索，也不要求先提供待搜索仓库的地址。不确定的名称、外观或 logo 是检索线索，先尝试并标明无法验证的条件。只有工具返回失败才报告该次失败，不能因为还没有搜索结果就声称没有搜索能力。用户只讨论功能、明确禁止搜索、或没有任何检索主题时才直接回答或澄清。不要向用户暴露 action/search/analyze 等内部字段。直接回答，不用口号或套话。按最新用户意图选择工具：找项目、找开源方案或skill用search，无需仓库地址；分析已给仓库或本地附件用analyze；定位Bug、分析报错用debug；CSV/JSON/表格数据作图用chart；有本地附件时，找类似项目、对比借鉴、改进或修改代码用improve：会真实搜索参考项目、给本地行号建议、生成可审阅补丁及交给Codex的提示词；不要声称已经应用，实际应用由工作台按钮完成。没有附件的修改请求先请用户添加文件。普通追问用reply并给快照依据。请求流程图用diagram_kind=flow，架构图用architecture，两种都要用both。只有工具实际完成才能说已搜索/已生成。目标goal保留用户条件与要解决的问题。仓库地址只能来自用户或真实搜索结果，不得编造。仅发送仓库链接视为analyze。没有地址和本地附件的分析应询问仓库。数据图缺少数据应询问，不能编造。执行代码必须在文件副本编辑器中由用户点击运行，不能声称已运行。',context,Reply)
                latest=messages[-2]['content']
                request=latest.split('```',1)[0].lower()
                if reply.action=='search' and attachment_id: reply.action='improve'
                if reply.action=='reply' and (attachment_id or '```' in latest) and any(word in request for word in ['排查','查错','debug','定位bug','分析报错']):
                    reply.action='debug';reply.goal=latest[:2000];reply.repository=''
                    reply.answer='正在排查。';reply.evidence=[]
                if reply.action != 'reply':
                    input_text=messages[-2]['content']
                    if reply.action=='chart' and not attachment_id:
                        from .charts import parse_data
                        for message in reversed(messages):
                            if message['role']!='user': continue
                            if message.get('attachment_id'):
                                attachment_id=message['attachment_id'];break
                            try: input_text=parse_data(message['content'])['raw'];break
                            except ValueError: pass
                        else:
                            if previous and task['result'].get('dataset'): input_text=task['result']['dataset']['raw']
                    if reply.action in {'analyze','debug'} and not reply.repository and not attachment_id and previous:
                        attachment_id=task['payload'].get('attachment_id','')
                    kind='local' if reply.action=='analyze' and attachment_id else reply.action
                    source_task_id=''
                    if previous and kind=='analyze' and not attachment_id:
                        candidate=next((c for c in task['result'].get('search',{}).get('candidates',[]) if c['repository'].rstrip('/')==reply.repository.rstrip('/')),None)
                        if candidate:
                            source_task_id=previous
                            reply.ref=candidate.get('commit') or reply.ref
                    task = await self.create_task(TaskInput(repository=reply.repository,goal=reply.goal or messages[-2]['content'][:2000],ref=reply.ref,kind=kind,input_text=input_text,attachment_id=attachment_id,diagram_kind=reply.diagram_kind,source_task_id=source_task_id))
                    task_id = task['id']
                    self.update(message_id, reply.answer, 'pending', task_id)
                    job = self.runner.jobs.get(task_id)
                    if job: await asyncio.shield(job)
                    task = self.store.get(task_id)
                    r=task['result']
                    summary = task_answer(r)
                    if r.get('chart'): summary=f"已生成{r['chart']['title']}。"
                    verdict = {'succeeded':'成果已保存。','needs_attention':'审核未通过，问题与已有成果已保留。','cancelled':'已停止。','interrupted':'已中断。'}.get(task['status'],task['error'] or '任务未完成。')
                    self.update(message_id, (summary+'\n\n' if summary else '')+verdict, task_id=task_id)
                else:
                    if reply.evidence:
                        if not snapshot: raise ValueError('回复引用了未读取的代码，请重试。')
                        if validate_evidence(reply.model_dump(),snapshot): raise ValueError('回复的代码引用未通过校验，请重试。')
                    self.update(message_id,reply.answer,task_id=previous,evidence=[e.model_dump() for e in reply.evidence])
        except asyncio.CancelledError:
            if task_id: self.runner.stop(task_id)
            self.update(message_id,'已停止。','interrupted',task_id)
            raise
        except Exception as exc:
            if task_id: self.runner.stop(task_id)
            message = str(exc) if isinstance(exc,ValueError) else '回复失败，请重试。'
            self.update(message_id,message,'failed',task_id)

    async def respond_learning(self, chat_id, message_id, background, ref):
        task_id = None
        try:
            async with asyncio.timeout(660):
                messages = self.get(chat_id)['messages']
                latest = messages[-2]['content']
                links = re.findall(r'https://github\.com/[^\s<>"）)\]，。；]+', latest)
                previous = next((m['task_id'] for m in reversed(messages) if m['task_id']), None)
                pending = json.loads(messages[-3].get('learning_request', '{}')) if len(messages) >= 3 and messages[-3]['role'] == 'assistant' else {}
                goal = re.sub(r'https://github\.com/[^\s<>"）)\]，。；]+', '', latest).strip(' \t\r\n。，；,.<>[]()（）')
                if links and len(set(links)) == 1 and not goal:
                    pending = {'repository': links[0].rstrip('.'), 'background': background, 'ref': ref}
                    self.update(message_id, '收到项目链接。你想先理解哪一部分？\n\n可以直接回复“理解项目的启动流程”，或描述你关心的一条流程，例如“用户输入后，系统如何处理并返回结果”。不需要先知道文件名。\n\n链接已记住，补充学习目标即可；目前还没有开始读取仓库或调用分析 Agent。')
                    with self.store.db:
                        self.store.db.execute('UPDATE messages SET learning_request=? WHERE id=?', (json.dumps(pending, ensure_ascii=False), message_id))
                    return
                if not links and pending:
                    if goal in {'取消', '不用了', '先不分析', '先不分析了', '停止'}:
                        self.update(message_id, '已取消这次待开始的学习请求。')
                        return
                    if not goal or goal in {'好', '好的', '可以', '开始', '继续', '嗯'}:
                        self.update(message_id, '请补充想理解的流程，例如“理解项目的启动流程”。项目链接已记住，不用重复粘贴。')
                        with self.store.db:
                            self.store.db.execute('UPDATE messages SET learning_request=? WHERE id=?', (json.dumps(pending, ensure_ascii=False), message_id))
                        return
                    links = [pending['repository']]
                    background = background or pending.get('background', '')
                    ref = ref or pending.get('ref', '')
                if links:
                    if len(set(links)) > 1:
                        self.update(message_id, '每次学习一个项目，请只提供一个 GitHub 仓库首页链接。')
                        return
                    task = await self.create_task(TaskInput(kind='learn', repository=links[0].rstrip('.'), goal=goal[:2000],
                        learner_background=background or '具备基础编程知识；具体基础未提供', ref=ref))
                    task_id = task['id']
                    self.update(message_id, '正在读取项目，依次生成解读、学习路线、三个练习和数据传递图（非调用时序）。', 'pending', task_id)
                    job = self.runner.jobs.get(task_id)
                    if job:
                        await asyncio.shield(job)
                    task = self.store.get(task_id)
                    summary = task['result'].get('analysis', {}).get('summary', '')
                    verdict = '学习成果已保存；自动审核未发现剩余问题，不代表人工验收。练习尚未执行，可查看阅读路线、代码依据与数据传递图（非执行时序）。' if task['status'] == 'succeeded' else task.get('error') or '任务未完成，已有成果保留。'
                    self.update(message_id, (summary + '\n\n' if summary else '') + verdict, task_id=task_id)
                elif previous:
                    task = self.store.get(previous)
                    snapshot_path = self.store.workspace(previous) / 'snapshot.json'
                    if not snapshot_path.is_file():
                        self.update(message_id, '上次任务尚无代码快照。请提供仓库链接重新开始。', task_id=previous)
                        return
                    snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
                    class LearningReply(BaseModel):
                        answer: str = Field(min_length=1, max_length=4000)
                        evidence: list[Evidence] = Field(min_length=1)
                    llm = self.llm_factory(lambda usage: None, lambda: None)
                    reply = await llm.ask('学习追问助手',
                        '围绕当前项目回答学习问题，只引用已读快照并提供行号；超出范围明确说明。'
                        '不搜索其他项目、不运行代码、不修改文件，不声称完成这些操作。'
                        '如需重新生成整套学习成果，请提示用户再次附上仓库链接与目标。',
                        {'question': latest, 'background': background, 'analysis': task['result'].get('analysis'),
                         'learning': task['result'].get('learning'), **source_context(snapshot)}, LearningReply)
                    if validate_evidence(reply.model_dump(), snapshot):
                        raise ValueError('追问引用未通过校验，请重试或缩小问题范围')
                    self.update(message_id, reply.answer, task_id=previous, evidence=[e.model_dump() for e in reply.evidence])
                else:
                    self.update(message_id, '请提供一个公开 GitHub 仓库首页链接，并说明想理解的模块或流程。可在输入框上方填写已有基础；本版专注项目学习，不执行搜索、改代码或运行代码。')
        except asyncio.CancelledError:
            if task_id:
                self.runner.stop(task_id)
            self.update(message_id, '已停止。', 'interrupted', task_id)
            raise
        except Exception as exc:
            if task_id:
                self.runner.stop(task_id)
            self.update(message_id, str(exc)[:500] if isinstance(exc, ValueError) else '学习请求失败，请检查链接或网络后重试。', 'failed', task_id)

    def stop(self, chat_id):
        for message in self.get(chat_id)['messages']:
            if message['status']=='pending':
                if message['task_id']: self.runner.stop(message['task_id'])
                self.update(message['id'],'已停止。','interrupted')
        if chat_id in self.jobs: self.jobs[chat_id].cancel()

    async def shutdown(self):
        jobs = list(self.jobs.values())
        for job in jobs: job.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)
