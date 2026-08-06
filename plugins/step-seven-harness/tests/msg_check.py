"""출력 문자열이 전부 번역 가능한지 전수 검사한다.

  usage: python3 msg_check.py [repo-root]

왜 있나: 출력 문자열이 300개를 넘는다. "다 감쌌다"를 사람이 세는 것은 불가능하고,
하나 빠지면 영어 버전에 한국어 한 줄이 섞여 나온다 — 그리고 그건 아무도 모른다.
숫자를 세는 일은 기계가 해야 한다.

검사하는 것
  1. 한글이 든 문자열 리터럴이 `t(...)` 안에 있는지 (docstring·주석은 제외)
  2. 정의 시점에 감쌀 수 없는 모듈 상수(LAZY_MSG_NAMES)는 **쓰는 자리**에서
     감싸졌는지 — 목록에만 넣고 실제로 안 감싸면 조용히 번역이 빠진다
  3. 카탈로그가 코드·설정의 문자열과 맞는지 (남거나 빠진 키)

추출 기준은 _msgs.py 한 곳에 있다. 생성기와 검사기가 각자 모으다 어긋난 적이 있다.
"""
import ast
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _msgs  # noqa: E402

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                       else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "..", ".."))
PLUGIN = os.path.join(REPO, "plugins/step-seven-harness")
ENGINE = os.path.join(PLUGIN, "scripts/harness.py")
STAGES = os.path.join(PLUGIN, "templates/stages.json")
CATALOG = os.path.join(PLUGIN, "templates/messages.ko.json")

src, tree, parent, _docs = _msgs.parse(ENGINE)
LAZY = _msgs.lazy_names(src)
found = _msgs.engine_strings(ENGINE)
bad = []

missing = [(ln, s) for s, ln, ok, name in found
           if not ok and not (name and name in LAZY)]
lazy_used = {name for s, ln, ok, name in found
             if not ok and name and name in LAZY}

print("  엔진 출력 문자열 %d개 (고유 %d개)" % (len(found), len({s for s, _, _, _ in found})))
print("  t() 로 감싸지 않은 것 %d개" % len(missing))
for ln, txt in missing[:12]:
    bad.append("harness.py:%d 이(가) t() 밖에 있다 — %s"
               % (ln, txt[:70].replace("\n", " ")))
if len(missing) > 12:
    bad.append("... 그리고 %d개 더" % (len(missing) - 12))


def stmt_of(node):
    cur = node
    for _ in range(20):
        p = parent.get(id(cur))
        if p is None:
            return None
        if isinstance(p, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            return cur
        cur = p
    return None


# LAZY 상수는 **사용 지점마다** 검사한다.
#
# 예전에는 상수 단위로 봤다 — 어딘가 한 번 `t(USAGE)` 가 있으면 다른 곳의
# `print(USAGE)` 가 숨었다. 적대적 리뷰가 지적하고 실증했다. 영어 버전에서 한국어
# 한 줄이 새어 나오는데 아무도 모르는 상태다.
#
# 인정하는 두 형태:
#   (a) `t(USAGE)`                     — 상수 전체를 감싼다
#   (b) `{k: t(v) for k, v in X.items()}` — 순회하며 원소를 감싼다
def in_t_call(node):
    """조상 중 t(...) 호출의 인자인가. 다른 호출의 인자로 들어가면 거기서 멈춘다."""
    cur = node
    for _ in range(8):
        p = parent.get(id(cur))
        if p is None:
            return False
        if isinstance(p, ast.Call):
            return isinstance(p.func, ast.Name) and p.func.id == "t"
        cur = p
    return False


def iterated_with_t(node):
    """이 이름이 순회 대상이고, 그 순회 본문에서 t() 를 쓰나."""
    cur = node
    for _ in range(6):
        p = parent.get(id(cur))
        if p is None:
            return False
        if isinstance(p, (ast.comprehension, ast.For)) and p.iter is cur:
            holder = parent.get(id(p))
            if holder is None:
                return False
            for c in ast.walk(holder):
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) \
                   and c.func.id == "t":
                    return True
            return False
        cur = p
    return False


LAZY_WITH_TEXT = {name for _s, _ln, _ok, name in found if name and name in LAZY}

# **스칼라와 컨테이너를 가른다.** 이 구분이 없으면 검사가 오탐을 낸다.
#   스칼라(`USAGE = """..."""`)  — 값 자체가 문장이다. raw 사용은 곧 누출이다.
#   컨테이너(`TREND_KEYS = ((k, "라벨"), ...)`) — **키만 읽는 것이 정상**이다
#     (`for k, _ in TREND_KEYS`). 그 자리를 지적하면 오탐이고, 오탐이 나는 검사는
#     아무도 보지 않는다. 그래서 컨테이너는 "그 문장에 t() 가 있는지"만 본다.
SCALAR_TEXT = set()
for n in ast.walk(tree):
    if isinstance(n, ast.Assign) and len(n.targets) == 1 \
       and isinstance(n.targets[0], ast.Name) and n.targets[0].id in LAZY_WITH_TEXT \
       and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
        SCALAR_TEXT.add(n.targets[0].id)

for n in ast.walk(tree):
    if not (isinstance(n, ast.Name) and n.id in LAZY_WITH_TEXT
            and isinstance(n.ctx, ast.Load)):
        continue
    st = stmt_of(n)
    if st is None:
        continue
    if isinstance(st, ast.Assign) and any(
            isinstance(tg, ast.Name) and tg.id == n.id for tg in st.targets):
        continue                      # 정의 자체는 세지 않는다
    if in_t_call(n) or iterated_with_t(n):
        continue
    if n.id in SCALAR_TEXT:
        bad.append("harness.py:%d 의 %s 사용이 t() 밖에 있다 — 이 자리만 번역이 "
                   "조용히 빠진다" % (n.lineno, n.id))

# 컨테이너는 **상수 단위**로만 본다. 어딘가에서 원소를 감싸면 통과다.
#
# 한계를 정직하게 적어둔다: `TREND_KEYS` 의 값을 감싸지 않고 출력하는 자리가 새로
# 생기면 이 검사는 놓친다. 사용 지점마다 보려고 했더니 `for k, _ in TREND_KEYS`
# (키만 읽는 정상 코드)까지 지적하는 오탐이 났고, 오탐이 나는 검사는 아무도 보지
# 않는다. 정확한 보장은 위의 리터럴 전수 검사이고 이건 보조 검사다.
for name in sorted(lazy_used - SCALAR_TEXT):
    if not any(isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)
               and (in_t_call(n) or iterated_with_t(n)) for n in ast.walk(tree)):
        bad.append("%s 는 LAZY_MSG_NAMES 에 있으나 t() 로 감싸는 자리가 없다 "
                   "— 번역이 조용히 빠진다" % name)

want = {s for s, _, _, _ in found} | set(_msgs.config_strings(STAGES))
if os.path.isfile(CATALOG):
    cat = json.load(open(CATALOG, encoding="utf-8"))
    have = set(cat)
    print("  카탈로그 %d개 · 빠짐 %d · 남음 %d"
          % (len(cat), len(want - have), len(have - want)))
    for k in sorted(want - have)[:6]:
        bad.append("카탈로그에 없는 문자열 — %s" % k[:64].replace("\n", " "))
    for k in sorted(have - want)[:6]:
        bad.append("코드에 없는 카탈로그 항목 (지워야 한다) — %s"
                   % k[:64].replace("\n", " "))
    empty = [k for k, v in cat.items() if not isinstance(v, str) or not v.strip()]
    for k in empty[:4]:
        bad.append("카탈로그 값이 비어 있다 — %s" % k[:56].replace("\n", " "))
else:
    bad.append("카탈로그 파일이 없다: %s — `python3 tests/gen_catalog.py` 로 만든다"
               % os.path.relpath(CATALOG, REPO))

if bad:
    print("\n문제 %d건" % len(bad))
    for b in bad:
        print("  " + b)
    sys.exit(1)
print("\n문제 없음")
