"""Explicit real model/GitHub acceptance. Never stores credentials in evidence."""
import argparse
import asyncio
import json
import hashlib
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from acceptance_metadata import provenance
from check_learning_acceptance import structural_checks
from backend import settings
from backend.models import TaskInput
from backend.store import Store
from backend.workflow import Runner


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='test-output/learning-live-060')
    parser.add_argument('--repository', default='https://github.com/fastapi/fastapi-cli')
    parser.add_argument('--ref', default='093be9ca19f2ef44ba523aae0a98d6c9ea2e0888')
    spec = json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/fastapi-cli.json').read_text(encoding='utf-8'))
    parser.add_argument('--goal', default=spec['goal'])
    parser.add_argument('--background', default=spec['background'])
    parser.add_argument('--structural-only', action='store_true', help='Exit 0 means structural checks only; content acceptance remains pending')
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
    snapshot_path = store.workspace(task['id'])/'snapshot.json'
    snapshot = json.loads(snapshot_path.read_text(encoding='utf-8')) if snapshot_path.exists() else {}
    checks = structural_checks(final, store.events(task['id']), snapshot, store.workspace(task['id']))
    (store.root / 'evidence.json').write_text(json.dumps({'kind': 'real_complete_learning_task', 'provenance': provenance(), 'model':settings.current()['model'], 'input':vars(args), 'expected_facts':spec['expected_facts'], 'structural_checks':checks, 'content_acceptance':'pending_explicit_source_review', 'real_model': True, 'real_github': True, 'source_hashes': source_hashes, 'task': final,
        'events': store.events(task['id'])}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'id': task['id'], 'status': final['status'], 'error': final['error'], 'usage': final['usage']}, ensure_ascii=False))
    store.db.close()
    if not all(checks.values()):
        raise SystemExit(1)
    if not args.structural_only:
        print('Structural checks passed; content review required. Run check_learning_acceptance.py with a hash-bound manual record.')
        raise SystemExit(2)


if __name__ == '__main__':
    asyncio.run(main())
