import csv
import io
import json
import math
import re
from html import escape
from typing import Literal
from pydantic import Field
from .models import Strict


class ChartSpec(Strict):
    title: str = Field(max_length=100)
    kind: Literal['line','bar']
    x: str
    y: list[str] = Field(min_length=1,max_length=4)
    unit: str = Field(max_length=30)


def parse_data(text):
    block=re.search(r'```(?:csv|json|tsv)?\s*\n([\s\S]*?)```',text,re.I)
    raw=(block.group(1).strip() if block else text.strip()).lstrip('\ufeff')
    try:
        if raw.startswith('['): rows=json.loads(raw)
        elif '|' in raw.splitlines()[0]:
            lines=[line.strip().strip('|') for line in raw.splitlines() if '|' in line]
            lines=[line for line in lines if not re.fullmatch(r'[\s|:\-]+',line)]
            values=[[v.strip() for v in line.split('|')] for line in lines]
            rows=[dict(zip(values[0],r,strict=True)) for r in values[1:]]
        else:
            dialect=csv.Sniffer().sniff(raw[:4096],delimiters=',\t;')
            reader=csv.DictReader(io.StringIO(raw),dialect=dialect)
            if not reader.fieldnames or len(set(reader.fieldnames))!=len(reader.fieldnames): raise ValueError('列名必须唯一')
            rows=list(reader)
        if not isinstance(rows,list) or not 1<=len(rows)<=200: raise ValueError('数据需为 1–200 行的表格')
        columns=list(rows[0])
        if not 2<=len(columns)<=20 or any(not isinstance(c,str) or not c.strip() for c in columns): raise ValueError('数据需包含 2–20 个具名字段')
        if any(not isinstance(r,dict) or set(r)!=set(columns) or any(isinstance(v,(dict,list,bool)) for v in r.values()) for r in rows): raise ValueError('数据行字段不一致或包含嵌套值')
        return {'columns':columns,'rows':rows,'raw':raw}
    except (ValueError,TypeError,IndexError,csv.Error):
        raise ValueError('请提供 CSV、JSON 数组或 Markdown 表格，2–20 列、最多 200 行；缺失值可留空。') from None


def numeric(value):
    if value is None or str(value).strip()=='': return None
    try: result=float(value)
    except (ValueError,TypeError): raise ValueError('所选纵轴包含非数值内容') from None
    if not math.isfinite(result): raise ValueError('数值必须有限')
    return result


def render_chart(data,spec):
    if spec.x not in data['columns'] or any(y not in data['columns'] or y==spec.x for y in spec.y): raise ValueError('图表字段不在输入数据中')
    series=[{'name':y,'values':[numeric(r[y]) for r in data['rows']]} for y in spec.y]
    values=[v for s in series for v in s['values'] if v is not None]
    if not values: raise ValueError('没有可绘制的数值')
    lo=min(0,min(values));hi=max(0,max(values))
    if lo==hi: hi=lo+1
    width,height,left,top,pw,ph=960,560,90,80,810,340
    y=lambda v:top+ph-(v-lo)/(hi-lo)*ph
    n=len(data['rows']);step=pw/max(n,1)
    x=lambda i:left+step*(i+.5)
    colors=['#4f766b','#6a79a3','#ad8059','#9a7298']
    esc=lambda v:escape(str(v),quote=True)
    out=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img"><rect width="100%" height="100%" fill="white"/><g font-family="Segoe UI,Microsoft YaHei,sans-serif" fill="#444"><text x="40" y="34" font-size="26">{esc(spec.title)}</text><text x="40" y="58" font-size="18">{esc(spec.unit)}</text>']
    for i in range(6):
        v=lo+(hi-lo)*i/5;py=y(v)
        out.append(f'<path d="M{left},{py}h{pw}" stroke="#eee"/><text x="{left-12}" y="{py+4}" text-anchor="end" font-size="18">{v:.4g}</text>')
    for sidx,s in enumerate(series):
        color=colors[sidx];segment=[]
        for i,v in enumerate(s['values']):
            if v is None:
                if segment: out.append(f'<polyline points="{" ".join(segment)}" fill="none" stroke="{color}" stroke-width="2"/>');segment=[]
                continue
            px,py=x(i),y(v);label=f"{data['rows'][i][spec.x]} · {s['name']}: {v:g} {spec.unit}"
            if spec.kind=='line':
                segment.append(f'{px},{py}')
                out.append(f'<circle cx="{px}" cy="{py}" r="3" fill="{color}"><title>{esc(label)}</title></circle>')
            else:
                bw=step*.75/len(series);bx=px-step*.375+sidx*bw
                out.append(f'<rect x="{bx}" y="{min(py,y(0))}" width="{bw*.9}" height="{max(abs(py-y(0)),.5)}" fill="{color}"><title>{esc(label)}</title></rect>')
        if segment: out.append(f'<polyline points="{" ".join(segment)}" fill="none" stroke="{color}" stroke-width="2"/>')
        out.append(f'<rect x="{left+sidx*195}" y="510" width="10" height="10" fill="{color}"/><text x="{left+16+sidx*195}" y="520" font-size="18">{esc(s["name"])}</text>')
    for i,row in enumerate(data['rows']):
        if i%max(1,math.ceil(n/12))==0:
            out.append(f'<text transform="translate({x(i)},440) rotate(25)" font-size="18">{esc(str(row[spec.x])[:24])}</text>')
    out.append('</g></svg>')
    return ''.join(out),series
