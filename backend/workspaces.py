import difflib
import hashlib
import json
import re
import uuid
from pathlib import PurePosixPath
from .github import safe_source, MAX_FILE_BYTES, MAX_TOTAL_BYTES


def attachment_path(store, ident):
    if not re.fullmatch('[a-f0-9]{32}',ident): raise ValueError('无效附件编号')
    return store.root/'attachments'/f'{ident}.json'


def save_attachment(store, files):
    if not 1<=len(files)<=32: raise ValueError('每次可导入 1–32 个文件')
    total=0;seen=set();clean=[]
    for f in files:
        path=f['path'];content=f['content'];name=PurePosixPath(path)
        allowed = safe_source(path) or (name.suffix.lower() in {'.csv','.tsv'} and safe_source(str(name.with_suffix('.txt'))))
        if not path or ':' in path or not allowed or name.as_posix()!=path:
            raise ValueError('文件路径或格式不支持：'+path[:100])
        if path.casefold() in seen: raise ValueError('文件路径重复')
        seen.add(path.casefold());size=len(content.encode('utf-8'));total+=size
        if '\x00' in content or size>MAX_FILE_BYTES or total>MAX_TOTAL_BYTES: raise ValueError('文件超过文本读取限制')
        clean.append({'path':path,'content':content,'lines':len(content.splitlines()),'bytes':size})
    ident=uuid.uuid4().hex
    data={'id':ident,'files':clean,'bytes':total}
    path=attachment_path(store,ident);path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    return data


def get_attachment(store,ident):
    path=attachment_path(store,ident)
    if not path.is_file(): raise ValueError('附件不存在')
    return json.loads(path.read_text(encoding='utf-8'))


def snapshot_from_files(files,label='本地副本'):
    return {'repository':label,'commit':hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest(),
        'ref':'copy','description':'用户选择的文件副本','files':files,'skipped':[],'tree_truncated':False,
        'total_tree_files':len(files),'candidate_files':len(files),'read_bytes':sum(f['bytes'] for f in files),'directory':[f['path'] for f in files]}


def edit_copy(store,ident,path,content):
    data=get_attachment(store,ident)
    original=next((f for f in data['files'] if f['path']==path),None)
    if original is None: raise ValueError('文件不在所选副本中')
    files=[{'path':f['path'],'content':content if f['path']==path else f['content']} for f in data['files']]
    updated=save_attachment(store,files)
    updated['diff']=''.join(difflib.unified_diff(original['content'].splitlines(True),content.splitlines(True),fromfile='before/'+path,tofile='after/'+path))
    return updated
