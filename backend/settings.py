import base64
import ctypes
import json
import os
from pathlib import Path


_root=None


def configure(root):
    global _root
    _root=Path(root)


def crypt(data,decrypt=False):
    if os.name!='nt': raise ValueError('安全密钥保存仅支持 Windows；其他系统请使用环境变量')
    from ctypes import wintypes
    class Blob(ctypes.Structure): _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_byte))]
    buffer=ctypes.create_string_buffer(data);src=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_byte)));out=Blob()
    lib=ctypes.windll.crypt32
    fn=lib.CryptUnprotectData if decrypt else lib.CryptProtectData
    if not fn(ctypes.byref(src),None,None,None,None,1,ctypes.byref(out)): raise ValueError('Windows 密钥保护失败')
    try: return ctypes.string_at(out.data,out.size)
    finally: ctypes.windll.kernel32.LocalFree(out.data)


def current():
    data={}
    if _root and (_root/'model-settings.json').exists():
        try: data=json.loads((_root/'model-settings.json').read_text(encoding='utf-8'))
        except (OSError,ValueError): pass
    key=os.environ.get('DEEPSEEK_API_KEY','');source='环境变量' if key else '未配置'
    if data.get('protected_key'):
        try: key=crypt(base64.b64decode(data['protected_key']),True).decode();source='Windows 加密保存'
        except (ValueError,OSError): key='';source='密钥无法解密，请重新设置'
    return {'model':data.get('model','deepseek-flash'),'base_url':'https://api.deepseek.com','key':key,'source':source}


def public():
    data=current();return {k:v for k,v in data.items() if k!='key'}|{'configured':bool(data['key'])}


def save(model,key='',clear=False):
    if not model or len(model)>100 or any(c.isspace() for c in model): raise ValueError('模型名称无效')
    if _root is None: raise ValueError('设置目录未初始化')
    path=_root/'model-settings.json';data=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    data['model']=model
    if key: data['protected_key']=base64.b64encode(crypt(key.encode())).decode()
    if clear: data.pop('protected_key',None)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data),encoding='utf-8');temp.replace(path)
    return public()


def search_credentials():
    data={}
    path=_root/'search-settings.json' if _root else None
    if path and path.exists():
        try: data=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError): pass
    result={}
    for name,variables in [('tavily',('TAVILY_API_KEY',)),('github',('GITHUB_TOKEN','GH_TOKEN'))]:
        key=next((os.environ[v] for v in variables if os.environ.get(v)),'')
        source='环境变量' if key else '未配置'
        if data.get(name):
            try: key=crypt(base64.b64decode(data[name]),True).decode();source='Windows 加密保存'
            except (ValueError,OSError): key='';source='密钥无法解密，请重新设置'
        result[name]={'key':key,'source':source}
    return result


def search_public():
    return {name:{'configured':bool(value['key']),'source':value['source']} for name,value in search_credentials().items()}


def save_search(tavily_key='',github_key='',clear_tavily=False,clear_github=False):
    if _root is None: raise ValueError('设置目录未初始化')
    path=_root/'search-settings.json';data=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    for name,key,clear in [('tavily',tavily_key,clear_tavily),('github',github_key,clear_github)]:
        if key: data[name]=base64.b64encode(crypt(key.strip().encode())).decode()
        if clear: data.pop(name,None)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data),encoding='utf-8');temp.replace(path)
    return search_public()
