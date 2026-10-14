"""Real-model review regression on real pinned source, with labelled mutations.

These are reviewer evaluations, not generated user task demonstrations.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from acceptance_metadata import provenance
from backend import settings
from backend.learning_review import audit_learning
from backend.llm import DeepSeek
from backend.learning_exercises import exercise_from_focus


def fixture():
    source = Path(__file__).resolve().parents[1] / 'test-output/public-fixture/snapshot.json'
    if not source.is_file():
        raise SystemExit('Run python scripts/prepare_learning_fixture.py first')
    snapshot = json.loads(source.read_text(encoding='utf-8'))
    def evidence(start, end):
        return dict(path='src/fastapi_cli/discover.py', start_line=start, end_line=end)
    module = dict(title='模块数据', explanation='get_module_data_from_path 返回 ModuleData；sys.path.insert 在调用者 get_import_data 中执行。', evidence=[evidence(45,65),evidence(128,153)], inferred=False)
    flow = dict(title='路径发现', explanation='get_import_data 调用 get_module_data_from_path 获得 ModuleData，然后调用 get_app_name 获得名称字符串，最后返回 ImportData。', evidence=[evidence(128,153)], inferred=False)
    tech = dict(title='Python', explanation='源码用 dataclass 定义 ModuleData。', evidence=[evidence(38,42)], inferred=False)
    exercise = dict(title='查证返回数据', objective='区分直接返回与调用者组装', instructions=['查找这两个函数的 return，记录返回类型与调用位置。'], acceptance=['列出各自的返回语句和引用，说明两者的区别。'], evidence=[evidence(45,65),evidence(128,153)])
    result = dict(diagram_semantics='data_flow', analysis=dict(summary='阅读路径发现中的数据交接。',stack=['Python'],modules=[module],flow=[flow],technologies=[tech],limitations=['仅静态阅读，未执行代码。']),
                  learning=dict(background='Python基础',objective='理解返回类型',flow_indices=[1],reading_order=[dict(title='先看返回数据再看调用',reason='区分数据结构与调用顺序',module_index=1,concepts=['返回值','函数调用'],evidence=module['evidence'])],exercises=[deepcopy(exercise) for _ in range(3)],limitations=['练习未执行。']),
                  diagram=dict(title='get_import_data 中路径有效的成功分支（省略应用名发现细节）',nodes=[dict(id='n1',label='get_module_data_from_path 返回 ModuleData',evidence=[evidence(45,65)]),dict(id='n2',label='get_import_data 组装返回 ImportData',evidence=[evidence(141,153)])],edges=[dict(source='n1',target='n2',label='调用者消费模块数据并继续组装',inferred=False,evidence=[evidence(141,153)])]))
    result['learning']['exercises'] = [exercise_from_focus(dict(symbol=symbol,evidence_ids=[ref]), i,
        [evidence(45,65),evidence(128,153)],snapshot) for i,(symbol,ref) in enumerate([
            ('get_module_data_from_path',1),('get_import_data',2),('get_app_name',2)])]
    return result,snapshot


async def main():
    out = Path(sys.argv[1] if len(sys.argv)>1 else 'test-output/learning-061-review-r1')
    if out.exists():
        raise SystemExit('Use a new output directory')
    out.mkdir(parents=True)
    settings.configure(Path(os.environ['APPDATA']) / 'agentos-lite/data')
    if not settings.current()['key']:
        raise SystemExit('No configured credential; no calls made')
    metadata = provenance()
    baseline,snapshot = fixture()
    cases=[('correct',None),('wrong_return','get_module_data_from_path 返回 ImportData。'),('wrong_owner','get_module_data_from_path 内部执行 sys.path.insert(0, ...) 修改导入路径。'),
           ('condition_correct','未提供 path/app/entrypoint 时，还需检查 config.entrypoint，只有它也为假才调用无参 get_import_data()。'),
           ('condition_missing','未提供 path/app/entrypoint 时，一定调用无参 get_import_data()，不检查配置。')]
    outcomes=[]
    for name,mutation in cases:
        current=deepcopy(baseline)
        expected = name in ('correct', 'condition_correct')
        if mutation:
            current['analysis']['modules'][0]['explanation']=mutation
        if name.startswith('condition_'):
            current['analysis']['modules'][0]['evidence']=[dict(path='src/fastapi_cli/cli.py',start_line=159,end_line=192),dict(path='src/fastapi_cli/config.py',start_line=37,end_line=45)]
        model=DeepSeek(lambda _:None,lambda:None)
        model.learning_mode=model.reserve_request_budget=True
        review=await audit_learning(model,current,snapshot,'理解 _run 的应用发现分支选择，以及 get_import_data 的成功路径中 ModuleData 与 ImportData 的交接。')
        passed=(review['passed'] if expected else not review['passed'] and 'analysis.modules.0' in review['repair_targets'])
        evidence=dict(kind='real_model_reviewer_regression_with_labelled_mutation',case=name,mutation=mutation,target_commit=snapshot['commit'],model=model.config['model'],provenance=metadata,input=current,review=review,usage=model.usage,expected_passed=expected,passed=passed)
        (out/f'{name}.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        outcomes.append(dict(case=name,passed=passed,usage=model.usage))
        print(json.dumps(outcomes[-1],ensure_ascii=False),flush=True)
    (out/'evidence.json').write_text(json.dumps(outcomes,ensure_ascii=False,indent=2),encoding='utf-8')
    if not all(r['passed'] for r in outcomes):
        raise SystemExit(1)


if __name__=='__main__':
    asyncio.run(main())
