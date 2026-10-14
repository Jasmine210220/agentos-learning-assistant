import asyncio
import json
import os
import re
import shutil
import uuid
from .store import now
from .workspaces import get_attachment


IMAGE='python:3.13-slim'
TIMEOUT=15
OUTPUT_LIMIT=64_000


class Runtime:
    def __init__(self,store):
        self.store=store;self.jobs={};self.containers={}
        store.db.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,attachment_id TEXT,path TEXT,created TEXT,status TEXT,result TEXT)')
        self.orphans=[r[0] for r in store.db.execute("SELECT id FROM runs WHERE status IN ('running','interrupted')") if re.fullmatch('[a-f0-9]{32}',r[0])]
        with store.db: store.db.execute("UPDATE runs SET status='interrupted' WHERE status='running'")

    async def command(self,*args,timeout=8):
        executable=shutil.which('docker')
        if not executable: raise ValueError('未安装 Docker')
        # The CLI connects only to the local engine, never a user-configured remote context.
        host='npipe:////./pipe/docker_engine' if os.name=='nt' else 'unix:///var/run/docker.sock'
        env={k:v for k,v in os.environ.items() if k in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','HOME'}}
        process=await asyncio.create_subprocess_exec(executable,'--host',host,*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env,creationflags=0x08000000 if os.name=='nt' else 0)
        try:
            out,err=await asyncio.wait_for(process.communicate(),timeout)
            if process.returncode: raise ValueError('Docker 服务不可用或操作失败')
            return out.decode('utf-8',errors='replace')
        finally:
            if process.returncode is None: process.kill();await process.wait()

    async def health(self):
        try:
            info=json.loads(await self.command('info','--format','{{json .}}'))
            for ident in list(self.orphans):
                # Only remove containers belonging to persisted runs from this application.
                name='agentos-'+ident
                existing=await self.command('ps','-a','--filter','name=^/'+name+'$','--format','{{.Names}}')
                if name in existing.splitlines(): await self.command('rm','-f',name)
                self.orphans.remove(ident)
            if info.get('OSType')!='linux': raise ValueError('请切换到 Docker Linux 容器')
            if not info.get('MemoryLimit') or not info.get('PidsLimit') or not info.get('CpuCfsQuota'):
                raise ValueError('当前 Docker 环境不支持所需资源限制')
            image=json.loads(await self.command('image','inspect',IMAGE))
            return {'available':True,'image':IMAGE,'image_id':image[0]['Id'],'timeout':TIMEOUT,'memory':'128 MB','network':False}
        except (ValueError,TimeoutError,OSError,KeyError,IndexError):
            return {'available':False,'image':IMAGE,'reason':'需要运行中的 Docker Linux 引擎和本地 python:3.13-slim 镜像。'}

    def get(self,ident):
        row=self.store.db.execute('SELECT * FROM runs WHERE id=?',(ident,)).fetchone()
        if row is None: raise ValueError('运行记录不存在')
        return {**dict(row),'result':json.loads(row['result'])}

    def list(self): return [self.get(r[0]) for r in self.store.db.execute('SELECT id FROM runs ORDER BY created DESC LIMIT 100')]

    async def start(self,attachment_id,path,confirmed):
        if not confirmed: raise ValueError('请确认执行的文件和隔离环境')
        if self.jobs: raise ValueError('已有代码在运行，请等待或停止')
        data=get_attachment(self.store,attachment_id)
        file=next((f for f in data['files'] if f['path']==path),None)
        if not file or not path.lower().endswith('.py'): raise ValueError('请选择副本中的 Python 文件')
        environment=await self.health()
        if not environment['available']: raise ValueError(environment['reason'])
        ident=uuid.uuid4().hex
        with self.store.db: self.store.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',(ident,attachment_id,path,now(),'running',json.dumps({'environment':environment})))
        self.jobs[ident]=asyncio.create_task(self.run(ident,file['content'],environment))
        self.jobs[ident].add_done_callback(lambda _:self.jobs.pop(ident,None))
        return self.get(ident)

    async def run(self,ident,code,environment):
        name='agentos-'+ident;self.containers[ident]=name
        result={'environment':environment,'stdout':'','stderr':'','exit_code':None};status='failed';process=None;readers=[]
        try:
            docker=shutil.which('docker');host='npipe:////./pipe/docker_engine' if os.name=='nt' else 'unix:///var/run/docker.sock'
            args=[docker,'--host',host,'run','--rm','--pull','never','--name',name,'-i','--network','none','--read-only','--memory','128m','--memory-swap','128m','--cpus','1','--pids-limit','32','--cap-drop','ALL','--security-opt','no-new-privileges','--user','65534:65534','--tmpfs','/tmp:rw,noexec,nosuid,size=16m','--workdir','/tmp',environment['image_id'],'python','-I','-u','-c',"import sys; code=sys.stdin.read(); exec(compile(code, 'main.py', 'exec'), {'__name__':'__main__','__file__':'main.py'})"]
            env={k:v for k,v in os.environ.items() if k in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','HOME'}}
            process=await asyncio.create_subprocess_exec(*args,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env,creationflags=0x08000000 if os.name=='nt' else 0)
            used=0
            async def consume(stream,key):
                nonlocal used
                while chunk:=await stream.read(4096):
                    remaining=max(0,OUTPUT_LIMIT-used);result[key]+=chunk[:remaining].decode('utf-8',errors='replace');used+=len(chunk)
                    if used>OUTPUT_LIMIT: raise ValueError('输出超过 64 KB，已停止')
            async with asyncio.timeout(TIMEOUT):
                process.stdin.write(code.encode());await process.stdin.drain();process.stdin.close()
                readers=[asyncio.create_task(consume(process.stdout,'stdout')),asyncio.create_task(consume(process.stderr,'stderr'))]
                await asyncio.gather(*readers)
                result['exit_code']=await process.wait();status='succeeded' if result['exit_code']==0 else 'failed'
        except TimeoutError: status='timed_out';result['error']='达到 15 秒运行时限'
        except asyncio.CancelledError: status='cancelled';result['error']='已停止运行'
        except Exception as exc: result['error']=str(exc) if isinstance(exc,ValueError) else '隔离运行失败'
        finally:
            try: await self.command('rm','-f',name)
            except (ValueError,TimeoutError,OSError): pass
            for reader in readers:
                if not reader.done(): reader.cancel()
            await asyncio.gather(*readers,return_exceptions=True)
            if process:
                if process.returncode is None: process.kill()
                # Drain the pipes after cancelling bounded readers. Waiting with a full
                # pipe can otherwise deadlock even after the child has exited.
                try: await asyncio.wait_for(process.communicate(),5)
                except TimeoutError: result['error']=result.get('error','运行进程输出清理超时')
            with self.store.db: self.store.db.execute('UPDATE runs SET status=?,result=? WHERE id=?',(status,json.dumps(result,ensure_ascii=False),ident))
            self.jobs.pop(ident,None);self.containers.pop(ident,None)

    def stop(self,ident):
        self.get(ident)
        if job:=self.jobs.get(ident):
            job.cancel()
            with self.store.db: self.store.db.execute("UPDATE runs SET status='cancelled' WHERE id=?",(ident,))

    async def shutdown(self):
        jobs=list(self.jobs.values())
        for job in jobs: job.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)
