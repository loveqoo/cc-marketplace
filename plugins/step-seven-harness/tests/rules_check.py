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

print("\n실패 %d개: %s" % (len(FAILS), FAILS or "없음"))
con.close()
cleanup(root)
sys.exit(1 if FAILS else 0)
