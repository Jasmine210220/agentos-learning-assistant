"""Fetch only pinned public source, hash-check it; never copy personal task data."""
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'tests/fixtures/fastapi-cli.json'
OUTPUT = ROOT / 'test-output/public-fixture/snapshot.json'

def prepare():
    spec = json.loads(MANIFEST.read_text(encoding='utf-8'))
    files = []
    for path, expected in spec['files'].items():
        url = f"https://raw.githubusercontent.com/fastapi/fastapi-cli/{spec['commit']}/{path}"
        with urlopen(url, timeout=30) as response:
            data = response.read(200001)
        if len(data)>200000 or hashlib.sha256(data).hexdigest()!=expected:
            raise ValueError('Public source hash mismatch: '+path)
        text = data.decode('utf-8')
        files.append(dict(path=path,content=text,lines=len(text.splitlines()),bytes=len(data)))
    snapshot = dict(repository=spec['repository'],commit=spec['commit'],ref=spec['commit'],files=files,
                    read_bytes=sum(f['bytes'] for f in files),skipped=[],tree_truncated=False,
                    total_tree_files=len(files),candidate_files=len(files),directory=list(spec['files']))
    OUTPUT.parent.mkdir(parents=True,exist_ok=True)
    if OUTPUT.exists() and json.loads(OUTPUT.read_text(encoding='utf-8')) != snapshot:
        raise ValueError('Existing fixture differs; preserve it and choose a clean checkout')
    OUTPUT.write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Prepared pinned public fixture; all three SHA256 checks passed.')

if __name__=='__main__': prepare()
