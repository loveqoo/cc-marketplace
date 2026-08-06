"""Ctx 언팩 누락을 AST 로 전수 검사한다.

  usage: python3 ctx_check.py [repo-root]

왜 있나: 함수가 `ctx.root` 를 풀지 않고 `root` 를 참조하면 NameError 가 나고,
훅은 fail-open 이라 **조용히 죽는다**. 0.20.0 과 0.26.0 에서 두 번 겪었고
그때마다 테스트는 전부 초록이었다.

클로저를 이해해야 한다 — 중첩 함수는 바깥 함수의 이름을 본다. 그걸 모르면
오탐이 나고, 오탐이 나는 검사는 아무도 보지 않는다.
"""
import ast
import os
import sys

BUNDLE = {"con", "cfg", "root", "lid", "sid"}
REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                       else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "..", ".."))
SRC = os.path.join(REPO, "plugins/step-seven-harness/scripts/harness.py")


def own_scope(fn):
    """fn 자신의 스코프에 속한 노드만. **중첩 함수 안으로 내려가지 않는다.**

    `ast.walk` 로 전부 훑으면 중첩 함수의 대입을 바깥 함수의 바인딩으로 센다.
    그러면 아래가 통과했다 — 적대적 리뷰가 지적하고 실증했다.

        def hook_x(inp, ctx):
            def helper():
                root = "..."      # 여기서만 대입
            print(root)           # 바깥은 풀지 않았다 → NameError

    이 검사기가 잡는 것이 바로 그 부류(다섯 번 걸렸다)이므로 이 구멍은 치명적이다.
    """
    out = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        out.append(n)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue                      # 별도 스코프다
        stack.extend(ast.iter_child_nodes(n))
    return out


def names_bound(fn):
    """그 함수 **자신의 스코프**에서 이름이 묶이는 것들 (인자 + 대입 + 루프 변수)."""
    out = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    if fn.args.vararg:
        out.add(fn.args.vararg.arg)
    for n in own_scope(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
                elif isinstance(t, ast.Tuple):
                    out |= {e.id for e in t.elts if isinstance(e, ast.Name)}
        elif isinstance(n, (ast.For, ast.comprehension)):
            tgt = getattr(n, "target", None)
            if isinstance(tgt, ast.Name):
                out.add(tgt.id)
    return out


def main():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    top = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    top |= {t.id for n in tree.body if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)}

    chains = {}

    def walk(node, chain):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.FunctionDef):
                chains[ch] = list(chain)
                walk(ch, chain + [ch])
            else:
                walk(ch, chain)

    walk(tree, [])

    bad = []
    for fn, chain in chains.items():
        scope = set(top) | names_bound(fn)
        for anc in chain:
            scope |= names_bound(anc)          # 클로저 캡처
        used = {n.id for n in ast.walk(fn)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        miss = sorted((used & BUNDLE) - scope)
        if miss:
            bad.append("%s:%d 이(가) %s 를 풀지 않고 쓴다" % (fn.name, fn.lineno, miss))

    print("  함수 %d개 검사" % len(chains))
    if bad:
        print("  언팩 누락 %d건" % len(bad))
        for b in bad:
            print("    " + b)
        return 1
    print("  언팩 누락 없음")
    return 0


sys.exit(main())
