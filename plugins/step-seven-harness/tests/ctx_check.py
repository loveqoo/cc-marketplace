"""Ctx 언팩 누락을 전수 검사한다 — **파이썬 자신의 스코프 분석기로.**

  usage: python3 ctx_check.py [repo-root]

왜 있나: 함수가 `ctx.root` 를 풀지 않고 `root` 를 참조하면 NameError 가 나고,
훅은 fail-open 이라 **조용히 죽는다**. 0.20.0·0.26.0·0.31.0·0.33.0·0.34.0 에서
다섯 번 겪었고 그때마다 테스트는 전부 초록이었다.

## 왜 AST 손계산을 버렸나

처음에는 대입·인자·루프 변수를 직접 모아 스코프를 흉내냈다. 그 흉내는 두 번
틀렸고, 둘 다 적대적 리뷰가 찾았다.

  1. `ast.walk` 가 중첩 함수까지 내려가 **중첩 함수의 대입을 바깥 함수의
     바인딩으로** 셌다 — `def helper(): root = ...` 이 있으면 바깥의 `print(root)`
     가 통과했다.
  2. 고친 뒤에도 comprehension 변수를 바깥 바인딩으로 셌다 — 파이썬 3 에서
     `[root for root in ()]` 의 `root` 는 바깥으로 새지 않으므로
     `return root` 는 NameError 인데 통과했다.

**패턴이 보인다: 나는 파이썬의 스코프 규칙을 재구현하고 있었고, 규칙은 내 예상보다
많다.** 특수 사례를 하나씩 더하는 대신 파이썬에게 물어본다 — `symtable` 은
컴파일러가 쓰는 그 분석기다. 중첩 함수, comprehension, `global`/`nonlocal`,
walrus, except 별칭의 **스코프**를 전부 정확히 안다.

## 이 검사가 잡지 못하는 것 (정확히 적어둔다)

`symtable` 은 **스코프** 분석기이지 **확정 초기화(definite assignment)** 분석기가
아니다. `is_assigned()` 는 "이 스코프 어딘가에서 묶인다"는 뜻이지 "쓰기 전에 반드시
묶인다"가 아니다. 그래서 아래를 놓친다.

    def f(inp, ctx):
        if inp.get("x"):
            root = ctx.root      # 조건부로만 묶인다
        return open(root)        # x 가 없으면 UnboundLocalError

이건 dataflow 분석이 필요하고 stdlib 에는 없다. **근사를 하나 더 얹지 않는다** —
그게 이 파일이 두 번 틀린 이유였다.

대신 뿌리를 고쳤다. 이 검사가 존재한 이유는 훅의 NameError 가 **조용히** 죽었기
때문인데, 이제 훅은 내부 오류를 사용자에게 알린다(`inactive()`). 그러니 이 검사는
'최후의 방어선'이 아니라 **빨리 잡는 도구**다. 못 잡는 것은 실행하면 보인다.
"""
import ast
import os
import sys
import symtable

BUNDLE = {"con", "cfg", "root", "lid", "sid"}
REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                       else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "..", ".."))
SCRIPTS = os.path.join(REPO, "plugins/step-seven-harness/scripts")


def sources():
    """엔진을 이루는 파일 **전부**. 목록을 손으로 적으면 새 파일이 기본 '미검사' 다."""
    import glob
    found = sorted(glob.glob(os.path.join(SCRIPTS, "**", "*.py"), recursive=True))
    if not found:
        raise SystemExit("엔진 파일을 찾지 못했다: %s" % SCRIPTS)
    return found


def walk_scopes(table, path=()):
    """(이름경로, 스코프) 를 전부. 함수 스코프만 검사 대상이다."""
    for child in table.get_children():
        yield path + (child.get_name(),), child
        for it in walk_scopes(child, path + (child.get_name(),)):
            yield it


def cross_names(files, bad):
    """엔진과 조각이 **합쳐진 이름공간**에서 없는 이름을 쓰는가.

    구현을 `parts/` 로 가르면서 이름을 런타임에 합치게 했다(`parts/__init__.py`).
    그 대가로 `pyflakes` 가 무력해진다 — 파일 하나만 보면 `record_event` 가
    정의되지 않은 것으로 보인다. **오타를 잡아 주던 그물이 사라진 것**이므로
    같은 일을 구조에 맞게 다시 만든다.

    합집합에도 없으면 그것은 진짜 오타다.
    """
    import builtins
    known = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__path__"}
    trees = {}
    for f in files:
        tr = ast.parse(open(f, encoding="utf-8").read())
        trees[f] = tr
        for n in ast.walk(tr):
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                known.add(n.name)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    known.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, (ast.Assign, ast.AugAssign, ast.For,
                                ast.comprehension, ast.With, ast.ExceptHandler)):
                if isinstance(n, ast.ExceptHandler) and n.name:
                    known.add(n.name)      # `except X as e` 의 e
                for t2 in ast.walk(n):
                    if isinstance(t2, ast.Name) and isinstance(t2.ctx, ast.Store):
                        known.add(t2.id)
                    elif isinstance(t2, ast.arg):
                        known.add(t2.arg)
            elif isinstance(n, ast.arg):
                known.add(n.arg)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                known.add(n.id)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                known.add(n.name)          # `except X as e` 의 e
    n = 0
    for f, tr in trees.items():
        for x in ast.walk(tr):
            if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load):
                n += 1
                if x.id not in known:
                    bad.append("%s:%d 이(가) 어디에도 없는 '%s' 를 쓴다"
                               % (os.path.basename(f), x.lineno, x.id))
    return n


def arity(files, bad):
    """엔진 안의 호출이 **정의된 인자 개수와 맞는가.**

    파이썬에는 컴파일러가 없어서 시그니처를 바꾸고 호출부 하나를 빠뜨려도
    그 줄을 실제로 밟기 전까지 조용하다. 실제로 그랬다: `close_loop` 에
    `kind` 를 필수로 더하면서 호출 두 곳 중 하나만 고쳤고, 그 자리는 기본
    설정에서 **도달 불가**(마지막 단계가 `skippable: false`)라 검사 여덟 개와
    테스트 766개가 전부 초록이었다. `stages[6].skippable: true` 한 줄이면
    `TypeError` 로 죽었다(5회차 M2).

    "등록 시점에 터지게 한다"(`Gate` 의 `abc`)와 같은 생각을 호출에 적용한다.
    기본값·`*args`·`**kwargs` 가 있으면 판정하지 않는다 — 모르면 조용히 넘긴다.
    """
    defs, calls, n = {}, [], 0
    for src_path in files:
        tree = ast.parse(open(src_path, encoding="utf-8").read())
        for fn in [x for x in ast.walk(tree) if isinstance(x, ast.FunctionDef)]:
            a = fn.args
            if a.vararg or a.kwarg or a.kwonlyargs:
                continue
            lo = len(a.args) - len(a.defaults)
            defs.setdefault(fn.name, set()).add((lo, len(a.args)))
        for c in [x for x in ast.walk(tree) if isinstance(x, ast.Call)]:
            if isinstance(c.func, ast.Name) and not c.keywords \
                    and not any(isinstance(x, ast.Starred) for x in c.args):
                calls.append((os.path.basename(src_path), c.lineno,
                              c.func.id, len(c.args)))
    for fname, line, name, got in calls:
        want = defs.get(name)
        if not want or len(want) != 1:      # 이름이 겹치면 판정하지 않는다
            continue
        lo, hi = next(iter(want))
        n += 1
        if not (lo <= got <= hi):
            bad.append("%s:%d 이(가) %s 를 인자 %d개로 부른다 (정의는 %d~%d개)"
                       % (fname, line, name, got, lo, hi))
    return n


def main():
    checked, bad = 0, []
    files = sources()
    for src_path in files:
        checked += scan(src_path, bad)
    ncall = arity(files, bad)
    nname = cross_names(files, bad)
    print("  파일 %d개 · 스코프 %d개 · 호출 %d개 인자 · 이름 %d개 대조"
          % (len(files), checked, ncall, nname))
    if ncall < 200:
        print("  대조한 호출이 너무 적다 (%d) — 추출이 깨졌다" % ncall)
        return 1
    # 숫자를 **출력만** 하고 단정하지 않았다. 추출이 죽어도 "누락 없음" 이었다.
    if checked < 250:
        print("  훑은 스코프가 너무 적다 (%d) — 추출이 깨졌다" % checked)
        return 1
    if bad:
        print("  문제 %d건" % len(bad))
        for b in sorted(set(bad)):
            print("    " + b)
        return 1
    print("  문제 없음")
    return 0


def scan(src_path, bad):
    src = open(src_path, encoding="utf-8").read()
    top = symtable.symtable(src, os.path.basename(src_path), "exec")
    module_names = {s.get_name() for s in top.get_symbols()}
    checked = 0
    for path, sc in walk_scopes(top):
        if sc.get_type() != "function":
            continue
        checked += 1
        for sym in sc.get_symbols():
            name = sym.get_name()
            if name not in BUNDLE or not sym.is_referenced():
                continue
            # 이 스코프에 묶였으면(인자·대입) 정상.
            if sym.is_assigned() or sym.is_parameter():
                continue
            # 바깥 함수에서 캡처했으면(클로저) 정상 — symtable 이 free 로 표시한다.
            if sym.is_free():
                continue
            # 모듈 전역에 그 이름이 있으면 정상 (엔진에는 없지만 규칙은 지킨다).
            if name in module_names and not sym.is_local():
                continue
            bad.append("%s의 %s:%d 이(가) '%s' 를 풀지 않고 쓴다"
                       % (os.path.basename(src_path), ".".join(path),
                          sc.get_lineno(), name))
    return checked


sys.exit(main())
