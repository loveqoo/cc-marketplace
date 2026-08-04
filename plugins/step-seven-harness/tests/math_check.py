"""cycle_counters 와 _survival 을 손계산과 대조한다.

  usage: python3 math_check.py [repo-root]


값은 미리 종이에 세고, 코드가 그 값을 내는지 본다. 코드를 읽고 기대값을 만들면
같은 오해를 두 번 하게 되므로 순서가 중요하다.
"""
import os
import subprocess
import sys
import tempfile

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                       else os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "plugins/step-seven-harness/scripts"))
import harness as h  # noqa: E402

root = os.path.join(tempfile.mkdtemp(), "p")
os.makedirs(root)
subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)
subprocess.run([sys.executable, os.path.join(REPO, "plugins/step-seven-harness/scripts/harness.py"),
                "init"], cwd=root, check=True, stdout=subprocess.DEVNULL)
con = h.connect(root)
cfg = h.load_config(root, os.path.join(REPO, "plugins/step-seven-harness"))
LID = h.head_loop(con)
OTHER = "260101-aaaaaa"

T0 = "2026-03-01T00:00:00+0900"          # 창 시작
BEFORE = "2026-02-01T00:00:00+0900"      # 창 이전
IN = "2026-03-05T00:00:00+0900"          # 창 안

def ev(at, lid, kind, rule=None, target=None):
    con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target) VALUES(?,?,?,?,?,?)",
                (at, lid, "execution", kind, rule, target))

# --- 창 이전 (집계에서 빠지되 refails 의 'seen_before' 는 만든다)
ev(BEFORE, LID, "tool_fail", "Bash", "npm test")
ev(BEFORE, LID, "block", "old_rule", "z.md")
ev(BEFORE, OTHER, "tool_fail", "Bash", "go test")   # 다른 작업에서 실패한 이력

# --- 창 안, 우리 작업
for r in ("a", "b", "a"):
    ev(IN, LID, "block", r, "f.md")                 # blocks 3
for _ in range(2):
    ev(IN, LID, "tool_fail", "Bash", "npm test")    # 둘 다 재발(창 이전에 있었음)
for _ in range(3):
    ev(IN, LID, "tool_fail", "Bash", "cargo test")  # 첫 건 제외 -> 재발 2
ev(IN, LID, "tool_fail", "Bash", "go test")         # 다른 작업 이력 -> 재발 1
for _ in range(4):
    ev(IN, LID, "edit", None, "x.py")               # churn 후보 4
for _ in range(2):
    ev(IN, LID, "edit", None, "y.py")
ev(IN, LID, "stop_gate", "retro_file", "compounding")
for _ in range(2):
    ev(IN, LID, "bypass", "prefix", "compounding")
ev(IN, LID, "skip", "context", "context")
for _ in range(2):
    ev(IN, LID, "promote_declined", "declined", "block:x")
ev(IN, LID, "promote", "hook", "block:y")

# --- 창 안, **다른** 작업 (우리 집계에 섞이면 안 된다)
for _ in range(9):
    ev(IN, OTHER, "block", "noise", "n.md")
    ev(IN, OTHER, "edit", None, "n.py")
con.commit()

EXPECT = {                # 손계산
    "blocks": 3,
    "fails": 6,           # npm2 + cargo3 + go1
    "refails": 5,         # npm2 + cargo2 + go1
    "churn": 4,           # x.py 4회
    "edits": 6,           # x.py4 + y.py2
    "gates": 1,
    "bypass": 2,
    "skips": 1,
    "declines": 2,
    "promotes": 1,
}
got = h.cycle_counters(con, LID, T0)
print("cycle_counters")
bad = []
for k, want in sorted(EXPECT.items()):
    ok = got[k] == want
    if not ok:
        bad.append(k)
    print("  %-10s 기대 %2d  실제 %2d  %s" % (k, want, got[k], "ok" if ok else "FAIL"))

# --- _survival
def promo(key, kind, decision, at, recheck=None):
    con.execute("INSERT OR REPLACE INTO promotion VALUES(?,?,?,?,?,?,?,?)",
                (key, kind, decision, "established", "n", LID, at, recheck or at))

def verify(key, decision, yes):
    ev_at = "2026-03-06T00:00:00+0900"
    con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target,detail) "
                "VALUES(?,?,?,?,?,?,?)",
                (ev_at, LID, "compounding", "promote_verify", decision, key,
                 "change_seen=%s" % ("yes" if yes else "no")))

con.execute("DELETE FROM promotion")
con.execute("DELETE FROM event WHERE kind='promote_verify'")
promo("block:A", "block", "hook", "2026-03-10T00:00:00+0900")
verify("block:A", "hook", False)
verify("block:A", "hook", True)    # 나중 결정이 이긴다 -> yes
promo("block:B", "block", "hook", "2026-03-10T00:00:00+0900")
verify("block:B", "hook", True)
verify("block:B", "hook", False)   # 나중 결정이 이긴다 -> no
promo("block:C", "block", "rule", "2026-03-10T00:00:00+0900")   # 검증 대상 아님
# D: 승격 후 두 작업에서 재발 -> regressed
promo("block:D", "block", "hook", "2026-03-10T00:00:00+0900")
for lid2 in ("260401-bbbbbb", "260402-cccccc"):
    con.execute("INSERT OR IGNORE INTO loop(id,created_at) VALUES(?,?)",
                (lid2, "2026-04-01T00:00:00+0900"))
    ev("2026-04-05T00:00:00+0900", lid2, "block", "D", "d.md")
con.commit()

SURV = {   # 손계산: hook = A,B,D / rule = C
    "hook": {"n": 3, "re": 1, "vn": 2, "vy": 1},
    "rule": {"n": 1, "re": 0, "vn": 0, "vy": 0},
}
agg = h._survival(con, cfg)
print("\n_survival")
for dec, want in sorted(SURV.items()):
    for k, wv in sorted(want.items()):
        gv = agg.get(dec, {}).get(k)
        ok = gv == wv
        if not ok:
            bad.append("%s.%s" % (dec, k))
        print("  %-6s %-3s 기대 %d  실제 %s  %s" % (dec, k, wv, gv, "ok" if ok else "FAIL"))

print("\n실패 %d개: %s" % (len(bad), bad or "없음"))
sys.exit(1 if bad else 0)
