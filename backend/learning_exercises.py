"""Project-specific focus selected by the agent; factual answer premises omitted."""
import re
from .models import Strict
from pydantic import Field


class ExerciseFocus(Strict):
    symbol: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z_$][A-Za-z0-9_.$:]*$')
    evidence_ids: list[int] = Field(min_length=1, max_length=3)


def exercise_from_focus(focus, index, catalog, snapshot):
    ids = focus['evidence_ids']
    if any(type(i) is not int or not 1 <= i <= len(catalog) for i in ids):
        raise ValueError('练习证据编号无效')
    evidence = [catalog[i-1] for i in dict.fromkeys(ids)]
    files = {f['path']: f['content'].splitlines() for f in snapshot['files']}
    symbol = focus['symbol']
    pattern = re.compile(r'(?<![A-Za-z0-9_$])' + re.escape(symbol) + r'(?![A-Za-z0-9_$])')
    def contains(e):
        return pattern.search('\n'.join(files[e['path']][e['start_line']-1:e['end_line']]))
    if not any(contains(e) for e in evidence):
        # A cited function body often omits its signature. Resolve the chosen
        # symbol only within the upstream-approved ranges in the same file;
        # never invent a line number or silently substitute a different topic.
        paths = {e['path'] for e in evidence}
        matches = [e for e in catalog if e['path'] in paths and contains(e)]
        if not matches:
            raise ValueError('练习焦点不在上游源码证据内：' + symbol)
        evidence = [min(matches, key=lambda e: e['end_line'] - e['start_line'])]
    templates = [
        ('追踪参数与数据来源', '区分已读代码中的定义、传入值和消费位置。',
         [f'在引用代码中定位 {symbol} 的定义或使用，记录实际出现的参数、实参或输入值；需要签名时打开完整源码快照核对。',
          '逐个说明这些值来自调用方、默认值、常量还是内部计算，并找到它们的消费位置；源码未显示的部分标为待确认。'],
         ['列出定义或使用位置、值的来源及消费位置，并各自标明代码依据。', '区分签名参数与调用实参，不补造不存在的参数或具体运行结果。']),
        ('核对条件与去向', '根据源码区分条件、优先级及对应执行路径。',
         [f'围绕 {symbol} 查找已读代码中的条件判断，列出完整前置条件及其去向；若未见条件分支，明确记录未见，不自行补造。',
          '核对相关默认值、配置来源及判断顺序；区分正常继续、提前返回和异常，缺少输入时不要断言实际会走哪条分支。'],
         ['每个路径判断都有条件和源码依据，不能从单个缺省参数推断整个运行结果。', '把已读事实与尚缺信息分别标明。']),
        ('区分返回数据与副作用', '区分被调用方的直接产出、调用方组装的数据以及状态修改。',
         [f'围绕 {symbol} 查找返回、yield 或结果消费位置，逐项说明数据由哪个函数产出、如何被调用方使用；未显示实现时注明无法确认。',
          '核对赋值与外部状态修改分别发生在哪个函数内；若未发现某类副作用则如实记录，不假定调用会报错或修改环境。'],
         ['分别标明直接产出与调用方组装的位置，不混淆返回对象的类型或所属函数。', '对副作用给出实际语句依据；练习只做静态阅读，不声称已执行。']),
    ]
    title, objective, instructions, acceptance = templates[index]
    return dict(title=f'{title}：{symbol}', objective=objective, instructions=instructions,
                acceptance=acceptance, evidence=evidence)
