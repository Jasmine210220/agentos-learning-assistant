"""Explicit real model/GitHub acceptance. Never stores credentials in evidence."""
import argparse
import asyncio
import json
import hashlib
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import settings
from backend.models import TaskInput
from backend.store import Store
from backend.workflow import Runner


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='test-output/learning-live-060')
    parser.add_argument('--repository', default='https://github.com/fastapi/fastapi-cli')
    parser.add_argument('--ref', default='093be9ca19f2ef44ba523aae0a98d6c9ea2e0888')
    parser.add_argument('--goal', default='理解 src/fastapi_cli/cli.py 中 dev 函数经 _run、get_import_data 发现给定 Python 文件内的应用，到调用 uvicorn.run 为止的流程。不解释命令安装入口或 uvicorn 内部。给阅读顺序、三个练习和一张流程图。')
    parser.add_argument('--background', default='会 Python 基础，不熟悉 FastAPI 和命令行应用发现机制')
    args = parser.parse_args()
    source_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('backend').glob('*.py')}
    if (Path(args.output) / 'evidence.json').exists():
        raise SystemExit('Use a new output directory; prior acceptance evidence must be preserved')
    settings.configure(Path(os.environ['APPDATA']) / 'agentos-lite/data')
    if not settings.current()['key']:
        raise SystemExit('No configured model credential; live acceptance blocked')
    store = Store(Path(args.output))
    task = store.create(TaskInput(kind='learn', repository=args.repository, ref=args.ref,
        goal=args.goal, learner_background=args.background).model_dump())
    runner = Runner(store)
    job = asyncio.create_task(runner.run(task['id']))
    last = 0
    while not job.done():
        for e in store.events(task['id'], last):
            last = e['id']
            if e['kind'] in {'started', 'completed', 'retry', 'rejected', 'failed', 'needs_attention'}:
                print(e['role'] + ': ' + e['summary'], flush=True)
        await asyncio.sleep(2)
    await job
    final = store.get(task['id'])
    (store.root / 'evidence.json').write_text(json.dumps({'real_model': True, 'real_github': True, 'source_hashes': source_hashes, 'task': final,
        'events': store.events(task['id'])}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'id': task['id'], 'status': final['status'], 'error': final['error'], 'usage': final['usage']}, ensure_ascii=False))
    store.db.close()
    if final['status'] != 'succeeded':
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
