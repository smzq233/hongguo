from __future__ import annotations
import html as html_lib
import json
import re
from urllib.parse import unquote

URL_RE = re.compile(r'https?://[^\s<>"\']+', re.I)
ID_RE = re.compile(r'^\d{15,20}$')


def first_url(text: str) -> str:
    m = URL_RE.search(text or "")
    if not m:
        return ""
    return m.group(0).rstrip("。！!，,；;）)]}")


def safe_filename(name: str, max_len: int = 100) -> str:
    name = html_lib.unescape(name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip(' .')
    return (name or '未命名短剧')[:max_len]


def decode_many(value: str, rounds: int = 5) -> str:
    old = value or ""
    for _ in range(rounds):
        try:
            new = unquote(old)
        except Exception:
            break
        if new == old:
            break
        old = new
    return old


def valid_id(value) -> bool:
    return bool(ID_RE.fullmatch(str(value or '')))


def extract_balanced(text: str, start: int, opening='{', closing='}') -> str:
    i = text.find(opening, start)
    if i < 0:
        return ''
    depth = 0
    in_string = False
    escaped = False
    quote = ''
    for pos in range(i, len(text)):
        ch = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == quote:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            quote = ch
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return text[i:pos+1]
    return ''


def parse_embedded_json(text: str, marker: str):
    pos = text.find(marker)
    if pos < 0:
        return None
    eq = text.find('=', pos)
    if eq < 0:
        return None
    raw = extract_balanced(text, eq+1)
    if not raw:
        return None
    raw = raw.replace(r'\/', '/')
    try:
        return json.loads(raw)
    except Exception:
        return None


def recursive_values(obj, keys):
    keys = set(keys)
    out = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in keys:
                    out.append(v)
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    return out


def recursive_first(obj, keys, default=None):
    vals = recursive_values(obj, keys)
    for v in vals:
        if v not in (None, '', [], {}):
            return v
    return default


def normalize_id_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        if valid_id(s):
            return [s]
        try:
            return normalize_id_list(json.loads(s))
        except Exception:
            return []
    if isinstance(value, list):
        out=[]
        for x in value:
            if valid_id(x): out.append(str(x))
            elif isinstance(x, dict):
                for k in ('vid','chapter_id','chapterId','video_id','videoId','id'):
                    if valid_id(x.get(k)):
                        out.append(str(x[k])); break
        return out
    if isinstance(value, dict):
        tmp=[]
        for k,v in value.items():
            try: order=int(k)
            except Exception: order=10**9
            if valid_id(v): tmp.append((order,str(v)))
            elif isinstance(v,dict):
                vv=next((str(v[k]) for k in ('vid','chapter_id','chapterId','video_id','videoId','id') if valid_id(v.get(k))), '')
                if vv: tmp.append((order,vv))
        return [x[1] for x in sorted(tmp)]
    return []
