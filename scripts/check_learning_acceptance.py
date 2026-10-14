"""Structural checks are separate from a hash-bound, explicit content review."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.workflow import validate_evidence
from backend.learning import learning_errors
from backend.audit import digest
from backend.learning_facts import validate_return_fields
from backend.models import Diagram

def structural_checks(task,events,snapshot,workspace):
    result=task.get('result',{})
    checks={'task_succeeded':task['status']=='succeeded',
            'automated_review_completed':bool(result.get('review',{}).get('passed')),
            'data_flow':result.get('diagram_semantics')=='data_flow',
            'target_commit_fixed':snapshot.get('commit')==task['payload'].get('ref'),
            'three_exercises':len(result.get('learning',{}).get('exercises',[]))==3,
            'citations_valid':not validate_evidence(result,snapshot),
            'svg_rendered':(workspace/'diagram.svg').is_file() and '<svg' in (workspace/'diagram.svg').read_text(encoding='utf-8'),
            'within_budget':task['usage']['calls']<=12 and task['usage']['input']+task['usage']['output']<=120000}
    checks['learning_uses_analysis']=bool(result.get('learning') and not learning_errors(result['learning'],result['analysis'],snapshot))
    for role,artifact in [('Learning Agent','analysis'),('Chart Agent','learning')]:
        checks[role+'_handoff']=any(e['role']==role and e['kind']=='model_input' and
            e['details'].get('input_hashes',{}).get(artifact)==digest(result.get(artifact)) for e in events)
    report=workspace/'report.md'
    checks['static_boundary']=report.is_file() and '未运行仓库代码，练习未执行或评分' in report.read_text(encoding='utf-8')
    if 'source_facts' in result:
        try:
            validate_return_fields(Diagram.model_validate(result.get('diagram')), result['source_facts'])
            checks['return_field_spelling_coverage_only'] = True
        except (ValueError, TypeError):
            checks['return_field_spelling_coverage_only'] = False
    return checks

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('evidence',type=Path)
    parser.add_argument('--manual',type=Path,required=True)
    args=parser.parse_args()
    evidence=json.loads(args.evidence.read_text(encoding='utf-8'))
    manual=json.loads(args.manual.read_text(encoding='utf-8'))
    spec=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/fastapi-cli.json').read_text(encoding='utf-8'))
    expected={f['id'] for f in spec['expected_facts']}
    checks=manual.get('checks',{})
    passed=(manual.get('evidence_sha256')==hashlib.sha256(args.evidence.read_bytes()).hexdigest()
            and evidence.get('structural_checks') and all(evidence['structural_checks'].values())
            and expected<=checks.keys() and all(checks[k].get('passed') is True and checks[k].get('evidence') for k in expected)
            and all(manual.get(k) is True for k in ('analysis_checked','learning_checked','diagram_checked','exercises_suitable','passed')))
    print(json.dumps(dict(kind='explicit_content_review_gate',passed=bool(passed),automated_task_status_alone_is_insufficient=True)))
    raise SystemExit(0 if passed else 1)

if __name__=='__main__': main()
