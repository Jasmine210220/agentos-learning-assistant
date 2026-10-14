import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.workflow import render_mermaid

async def main():
    directory = Path('test-output/render-smoke').resolve()
    directory.mkdir(parents=True, exist_ok=True)
    svg = await render_mermaid('flowchart TD\n n1["请求入口"] --> n2["处理结果"]', directory)
    print(json.dumps({'ok':'<svg' in svg,'bytes':len(svg)}))

if __name__ == '__main__': asyncio.run(main())
