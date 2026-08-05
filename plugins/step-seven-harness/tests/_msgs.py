"""메시지 추출 — msg_check.py 와 gen_catalog.py 가 **같은 기준**을 쓰게 한다.

처음에는 두 스크립트가 각자 모았고, 기준이 달라 카탈로그가 코드와 어긋났다
(생성기는 `t(...)` 안만 봤고 검사기는 리터럴 전수를 봤다). 같은 것을 두 곳에서
계산하면 어긋나고, 어긋난 것을 아무도 모른다 — 이 저장소에서 반복해 겪은 부류다.
"""
import ast
import json
import re

KO = re.compile(r"[가-힣]")

# 사람에게 나가지 않는 문자열. 번역 대상이 아니다.
#   SCHEMA        SQL. 주석에 한글이 있지만 출력되지 않는다.
#   EVENT_KINDS   DB 에 저장되는 종류 이름의 설명 (스키마 문서)
#   STOPWORDS     한국어 불용어. 번역하면 기능이 깨진다
EXEMPT_ASSIGN = ("SCHEMA", "EVENT_KINDS", "STOPWORDS")


def parse(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    parent = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parent[id(c)] = n
    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Module)):
            b = getattr(n, "body", None)
            if b and isinstance(b[0], ast.Expr) \
               and isinstance(b[0].value, ast.Constant) \
               and isinstance(b[0].value.value, str):
                docs.add(id(b[0].value))
    return src, tree, parent, docs


def lazy_names(src):
    m = re.search(r"LAZY_MSG_NAMES = \(([^)]*)\)", src, re.S)
    return set(re.findall(r'"([A-Z_]+)"', m.group(1))) if m else set()


def assigned_name(node, parent):
    cur = node
    for _ in range(8):
        p = parent.get(id(cur))
        if p is None:
            return None
        if isinstance(p, ast.Assign) and p.targets:
            tgt = p.targets[0]
            return tgt.id if isinstance(tgt, ast.Name) else None
        cur = p
    return None


def wrapped(node, parent):
    """조상 중에 t(...) 호출이 있나. 다른 호출의 인자로 들어가면 거기서 멈춘다."""
    cur = node
    for _ in range(8):
        p = parent.get(id(cur))
        if p is None:
            return False
        if isinstance(p, ast.Call) and isinstance(p.func, ast.Name) and p.func.id == "t":
            return True
        if isinstance(p, ast.Call):
            return False
        cur = p
    return False


def engine_strings(engine_path):
    """엔진의 출력 문자열 전부. (문자열, 줄번호, 감싸졌나, 대입된 상수명)."""
    src, tree, parent, docs = parse(engine_path)
    out = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)
                and KO.search(n.value) and id(n) not in docs):
            continue
        name = assigned_name(n, parent)
        if name in EXEMPT_ASSIGN:
            continue
        out.append((n.value, n.lineno, wrapped(n, parent), name))
    return out


def config_strings(stages_path):
    """stages.json 에서 사용자·모델에게 나가는 문자열. 엔진이 t() 로 통과시킨다."""
    cfg = json.load(open(stages_path, encoding="utf-8"))
    out = []

    def add(v):
        if isinstance(v, str) and KO.search(v):
            out.append(v)

    for st in cfg.get("stages") or []:
        if isinstance(st, dict):
            add(st.get("summary"))
            add(st.get("hint"))
    for spec in (cfg.get("criteria") or {}).values():
        if isinstance(spec, dict):
            add(spec.get("help"))
    for r in cfg.get("write_rules") or []:
        if isinstance(r, dict):
            add(r.get("deny"))
    for v in (cfg.get("consent") or {}).values():
        add(v)
    for it in cfg.get("retro_questions") or []:
        if isinstance(it, dict):
            add(it.get("q"))
            add(it.get("why"))
    for v in ((cfg.get("promotion") or {}).get("as_kinds") or {}).values():
        add(v)
    return out
