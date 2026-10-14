"""Persist actual role calls and content-addressed handoffs, without credentials."""
import hashlib
import json
import uuid


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def trace_model(llm, event):
    original = llm.ask

    async def ask(role, instruction, data, schema):
        call_id = uuid.uuid4().hex
        # These hashes identify the exact upstream objects consumed by this call.
        inputs = {key: digest(value) for key, value in data.items()}
        brief = {key: data[key] for key in ('goal', 'repository', 'commit') if key in data}
        brief['files'] = [f.get('path') for f in data.get('files', []) if isinstance(f, dict)]
        event(role, 'model_input', '读取角色输入', {'call_id': call_id, 'schema': schema.__name__, 'input_summary': brief, 'input_hashes': inputs})
        try:
            output = await original(role, instruction, data, schema)
        except BaseException as exc:
            event(role, 'model_error', '角色调用未完成', {'call_id': call_id, 'error_type': type(exc).__name__})
            raise
        value = output.model_dump()
        event(role, 'model_output', '角色输出已保存', {'call_id': call_id, 'schema': schema.__name__, 'output_hash': digest(value), 'output': value})
        return output

    llm.ask = ask
    return llm
