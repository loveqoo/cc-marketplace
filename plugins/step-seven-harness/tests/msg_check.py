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


# LAZY 상수는 두 형태를 인정한다. 상수 전체를 감싸거나(`t(USAGE)`), 원소를 감싸거나
# (`{k: t(v) for k, v in PROMOTE_AS_DEFAULT.items()}`). 후자는 상수 이름이 t() 안에
# 없으므로 **그 상수를 쓰는 문장에 t() 호출이 있는지**로 본다.
# 한계: 그 문장이 다른 것에 t() 를 쓰고 있어도 통과한다. 정확한 보장은 위의 전수
# 검사이고 이건 보조 검사다.
lazy_wrapped = set()
for n in ast.walk(tree):
    if not (isinstance(n, ast.Name) and n.id in LAZY):
        continue
    st = stmt_of(n)
    if st is None:
        continue
    if isinstance(st, ast.Assign) and any(
            isinstance(tg, ast.Name) and tg.id == n.id for tg in st.targets):
        continue                      # 정의 자체는 세지 않는다
    for c in ast.walk(st):
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "t":
            lazy_wrapped.add(n.id)
            break

for name in sorted(lazy_used - lazy_wrapped):
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
