"""Run installed-app acceptance with an in-memory credential, never a copied file."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import settings


settings.configure(Path(os.environ['APPDATA']) / 'agentos-lite/data')
configuration = settings.current()
if not configuration['key']:
    raise SystemExit('No configured model credential')
environment = dict(os.environ)
environment['DEEPSEEK_API_KEY'] = configuration['key']
environment['LEARNING_LIVE'] = '1'
# Only the key is passed through the existing environment-variable mechanism.
# Its value is not put in command arguments, logs, config files or evidence.
try:
    result = subprocess.run([shutil.which('node') or 'node', 'tests/ui-learning.cjs'],
        env=environment, cwd=Path(__file__).resolve().parents[1])
finally:
    environment.pop('DEEPSEEK_API_KEY', None)
    configuration['key'] = ''
raise SystemExit(result.returncode)
