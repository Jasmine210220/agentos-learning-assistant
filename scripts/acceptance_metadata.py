"""Public provenance only; no credentials, environment dump, or user paths."""
import hashlib
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
def provenance():
    commit=subprocess.check_output(['git','-c',f'safe.directory={ROOT.as_posix()}','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    dirty=bool(subprocess.check_output(['git','-c',f'safe.directory={ROOT.as_posix()}','status','--porcelain'],cwd=ROOT,text=True).strip())
    hashes={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ('backend','src','electron') for p in (ROOT/folder).rglob('*')
            if p.is_file() and p.suffix in ('.py','.ts','.tsx','.css','.cjs')}
    return dict(source_commit=commit,source_dirty=dirty,source_hashes=hashes)
