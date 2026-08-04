#!/usr/bin/env python3
"""step-six-harness engine.

  harness.py hook     stdin 으로 훅 이벤트 JSON 을 받아 판정을 stdout 으로 낸다.
  harness.py <cmd>    모델/사람이 쓰는 CLI (status, advance, skip, allow, ...).

상태 모델
  SQLite DB(.claude/harness/harness.db) 는 **해시 발급기 + 현재 상태 머신**이다.
  루프가 닫히면 그 루프의 행은 버린다. 영구 기록은 각 폴더에 남은 파일이고,
  파일명이 루프 해시를 갖고 있어 그것이 시퀀스이자 인덱스다.
  DB 는 커밋하지 않으므로 워크트리 간 머지 문제가 원리적으로 없다.

설계 원칙
  - 하네스가 없는 프로젝트에서는 아무것도 출력하지 않고 즉시 종료한다.
  - 규칙이 확정적으로 걸릴 때만 deny 한다. 판단이 필요하면 ask 로 사람에게 넘긴다.
  - DB 나 설정이 깨졌으면 차단하지 않는다. 세션을 벽돌로 만드는 것보다 무력한 게 낫다.
  - 모든 쓰기는 트랜잭션 안에서 한다. 병렬 툴 호출로 훅이 동시 발화해도 안전하다.
"""

import calendar
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

HARNESS_DIR = os.path.join(".claude", "harness")
DB_REL = os.path.join(HARNESS_DIR, "harness.db")
CONFIG_REL = os.path.join(HARNESS_DIR, "stages.json")
WRAPPER_REL = os.path.join(HARNESS_DIR, "bin", "harness")
POLICY_REL = os.path.join(HARNESS_DIR, "POLICY.md")
RATIONALE_REL = os.path.join(HARNESS_DIR, "rationale.md")
LEARNED_REL = os.path.join(HARNESS_DIR, "LEARNED.md")

WRITE_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
}

# Bash 가 파일을 건드릴 가능성이 있는 명령. 이 경우에만 경로 토큰을 훑는다.
BASH_MUTATORS = re.compile(r"(^|[;&|]\s*)(rm|mv|cp|mkdir|touch|tee|dd|truncate)\b|>\s*\S|sed\s+-i")
# 두 번째 토큰까지 잡는다. `loop new` 는 루프를 닫고 새로 만들므로 모든 단계
# 게이트를 우회하는데, 첫 토큰만 보면 subcommand 가 'loop' 로 잡혀 동의 판정이
# 아예 일어나지 않았다 — 승격 게이트가 그 구멍으로 그대로 새어나갔다.
CTRL_RE = re.compile(r"harness(?:\.py)?[\"']?\s+([a-z-]+)(?:\s+([a-z-]+))?")
CTRL_SUB2 = {"loop": ("new", "adopt")}
CONSENT_CMDS = ("skip", "allow", "approve-plan", "loop new", "loop adopt")
CTRL_HEAD = {
    "skip": "단계 스킵 요청",
    "allow": "쓰기 금지 경로에 대한 예외 요청",
    "approve-plan": "계획 승인 요청",
    "loop new": "현재 작업을 닫고 새 작업을 시작하는 요청 — "
                "남은 단계와 승격 결정을 건너뛰게 된다",
    "loop adopt": "기존 루프 해시 재연결 요청",
    "auto-skip": "스킵 자동 승인 활성화 요청 — 이후 스킵은 다이얼로그 없이 통과한다",
}
EVIDENCE_STAGES = ("execution", "verification")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS loop (
  id TEXT PRIMARY KEY, intent TEXT, branch TEXT, created_at TEXT, closed_at TEXT,
  cycle INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS stage (
  loop_id TEXT, stage TEXT, status TEXT,
  entered_at TEXT, left_at TEXT, reason TEXT, authorized_by TEXT,
  PRIMARY KEY (loop_id, stage));
CREATE TABLE IF NOT EXISTS evidence (
  loop_id TEXT, stage TEXT, kind TEXT, item TEXT, at TEXT,
  PRIMARY KEY (loop_id, kind, item));
CREATE TABLE IF NOT EXISTS wgrant (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop_id TEXT, glob TEXT, reason TEXT, uses_left INTEGER, at TEXT);
CREATE TABLE IF NOT EXISTS stop_block (prompt_id TEXT, key TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS prompt_stage (
  prompt_id TEXT, stage TEXT, at TEXT, PRIMARY KEY (prompt_id, stage));
-- 관측 기록. 루프가 닫혀도 남는다. 복리의 원료이며 모델의 자기신고가 아니다.
CREATE TABLE IF NOT EXISTS event (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, loop_id TEXT, stage TEXT,
  kind TEXT, rule TEXT, target TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS event_kind_idx ON event(kind, rule);
CREATE INDEX IF NOT EXISTS event_target_idx ON event(target);
-- 승격 결정. 반복되는 실수를 산문으로 남기고 끝내지 않았다는 증거다.
-- event 처럼 루프가 닫혀도 남는다 — 작업 하나의 상태가 아니라 프로젝트의 자산이다.
CREATE TABLE IF NOT EXISTS promotion (
  key TEXT PRIMARY KEY, kind TEXT, decision TEXT, maturity TEXT,
  note TEXT, loop_id TEXT, at TEXT, recheck_at TEXT);
"""

EVENT_KINDS = {
    "block": "규칙 차단",
    "skip": "단계 스킵",
    "stop_gate": "종료 조건 미충족",
    "bypass": "게이트 우회",
    "tool_fail": "도구 실패",
    "edit": "파일 편집",
    "promote": "승격 결정",
    "promote_declined": "승격 보류",
    "promote_verify": "승격 시 변경 관측",
    "cycle_close": "회차 종료",
}

# 승격 결정의 종류. 'declined' 는 "안 한다 + 사유" — 결정이지 회피가 아니다.
PROMOTE_AS = {
    "hook": "훅/규칙으로 기계화 (stages.json 또는 플러그인)",
    "rule": "LEARNED.md 한 줄로 승격 (항상 로드된다)",
    "skill": "스킬/커맨드로 승격",
    "structure": "구조를 바꿔 원인을 제거",
    "declined": "승격하지 않는다 (사유 기록)",
}


# --------------------------------------------------------------------------- io

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def ts_epoch(s):
    """저장된 시각을 절대 시각(epoch 초)으로 바꾼다.

    시각 비교를 SQL 문자열 비교로 하면 안 된다. 오프셋이 섞이거나(`+0900` 과
    오프셋 없는 값) DST 전환이 있으면 사전순과 실제 순서가 어긋난다. 예:
    `2026-11-01T01:45:00-0400` 보다 `2026-11-01T01:30:00-0500` 이 실제로는
    나중인데 문자열로는 작다 — 재발 판정이 조용히 뒤집힌다.

    파싱에 실패하면 0.0 을 돌려주는데, 그건 "아주 오래됨"으로 취급되어 이벤트가
    측정 창에서 조용히 빠진다. 그래서 하네스가 쓰지 않는 형식까지 받아둔다 —
    공백 구분 형태는 SQLite `datetime()` 의 출력이고 그건 UTC 다.
    """
    if not s:
        return 0.0
    txt = str(s).strip()
    for fmt, utc in (("%Y-%m-%dT%H:%M:%S%z", False),
                     ("%Y-%m-%d %H:%M:%S%z", False),
                     ("%Y-%m-%dT%H:%M:%S", False),
                     ("%Y-%m-%d %H:%M:%S", True),
                     ("%Y-%m-%d", False)):
        try:
            tm = time.strptime(txt, fmt)
        except ValueError:
            continue
        off = getattr(tm, "tm_gmtoff", None)
        if off is not None:
            return calendar.timegm(tm) - off
        if utc:
            return calendar.timegm(tm)
        try:
            return time.mktime(tm)  # 오프셋이 없으면 로컬 시각으로 읽는다
        except (OverflowError, ValueError):
            return 0.0
    return 0.0


def jload(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def find_root(cwd):
    """harness.db 를 가진 가장 가까운 조상 디렉터리."""
    cands = []
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        cands.append(env)
    if cwd:
        cands.append(cwd)
    cands.append(os.getcwd())
    for cand in cands:
        d = os.path.abspath(os.path.expanduser(cand))
        while True:
            if os.path.isfile(os.path.join(d, DB_REL)):
                return d
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return None


def connect(root, create=False):
    path = os.path.join(root, DB_REL)
    if not create and not os.path.isfile(path):
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    return con


# ----------------------------------------------------------------------- config

def load_config(root, plugin_root_dir=None):
    cfg = jload(os.path.join(root, CONFIG_REL))
    if cfg is None and plugin_root_dir:
        cfg = jload(os.path.join(plugin_root_dir, "templates", "stages.json"))
    return cfg


def stage_ids(cfg):
    return [s["id"] for s in cfg["stages"]]


def stage_index(cfg, sid):
    ids = stage_ids(cfg)
    return ids.index(sid) if sid in ids else 0


def stage_obj(cfg, sid):
    return cfg["stages"][stage_index(cfg, sid)]


def label_of(cfg, sid):
    return "%d/%d %s" % (stage_index(cfg, sid) + 1, len(cfg["stages"]),
                         stage_obj(cfg, sid)["label"])


# ------------------------------------------------------------------- loop/stage

def new_loop_id():
    seed = "%d|%d|%s" % (time.time_ns(), os.getpid(), os.urandom(8).hex())
    return time.strftime("%y%m%d") + "-" + hashlib.sha256(seed.encode()).hexdigest()[:6]


def git_branch(root):
    try:
        with open(os.path.join(root, ".git", "HEAD"), encoding="utf-8") as fh:
            line = fh.read().strip()
        return line.split("refs/heads/", 1)[1] if "refs/heads/" in line else line[:12]
    except Exception:
        return None


def get_meta(con, k, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def set_meta(con, k, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def head_loop(con):
    return get_meta(con, "head")


def cycle_of(con, lid):
    """이 작업의 현재 회차. Compounding → Scaffolding 으로 돌 때마다 늘어난다."""
    row = con.execute("SELECT cycle FROM loop WHERE id=?", (lid,)).fetchone()
    try:
        return int(row["cycle"]) if row and row["cycle"] else 1
    except (TypeError, ValueError, IndexError):
        return 1


def file_prefix(con, lid):
    """`.dev/` 산출물 파일명 접두사. 앞단 해시로 grep 하면 한 작업이 모인다."""
    return "%s-%d-" % (lid, cycle_of(con, lid))


def create_loop(con, cfg, root, intent=None, loop_id=None):
    lid = loop_id or new_loop_id()
    con.execute("INSERT OR IGNORE INTO loop(id,intent,branch,created_at) VALUES(?,?,?,?)",
                (lid, intent, git_branch(root), now()))
    for i, s in enumerate(cfg["stages"]):
        con.execute("INSERT OR IGNORE INTO stage(loop_id,stage,status,entered_at) "
                    "VALUES(?,?,?,?)",
                    (lid, s["id"], "active" if i == 0 else "pending",
                     now() if i == 0 else None))
    set_meta(con, "head", lid)
    return lid


def close_loop(con, lid):
    """루프의 작업 상태를 버린다. 영구 기록은 폴더의 파일명이 갖고 있다.

    남기는 것: loop 인덱스(해시·의도·기간)와 event(관측 기록).
    event 를 버리면 복리의 원료가 사라지고, loop 인덱스가 없으면 event 가
    어느 작업의 것인지 알 수 없다. 버리는 것은 진행 중 상태뿐이다.
    """
    for tbl in ("stage", "evidence", "wgrant"):
        con.execute("DELETE FROM %s WHERE loop_id=?" % tbl, (lid,))
    con.execute("UPDATE loop SET closed_at=? WHERE id=?", (now(), lid))


def active_stage(con, lid):
    row = con.execute("SELECT stage FROM stage WHERE loop_id=? AND status='active'",
                      (lid,)).fetchone()
    return row["stage"] if row else None


def stage_rows(con, lid):
    return {r["stage"]: r for r in
            con.execute("SELECT * FROM stage WHERE loop_id=?", (lid,))}


def skips_of(con, lid):
    return con.execute("SELECT stage, reason, authorized_by FROM stage "
                       "WHERE loop_id=? AND status='skipped'", (lid,)).fetchall()


# --------------------------------------------------------------------- evidence

def record_event(con, lid, sid, kind, rule=None, target=None, detail=None):
    con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target,detail) "
                "VALUES(?,?,?,?,?,?,?)",
                (now(), lid, sid, kind, rule, target,
                 detail[:400] if detail else None))


def norm_cmd(cmd):
    """도구 실패를 세려면 명령을 안정된 키로 정규화해야 한다."""
    toks = re.findall(r"[^\s|;&<>]+", cmd or "")
    if not toks:
        return ""
    head = toks[0].split("/")[-1]
    multi = ("npm", "pnpm", "yarn", "bun", "git", "go", "cargo", "make",
             "python", "python3", "uv", "docker", "kubectl", "gh")
    if head in multi and len(toks) > 1 and not toks[1].startswith("-"):
        return "%s %s" % (head, toks[1])
    return head


def has_evidence(con, lid, kind):
    return con.execute("SELECT 1 FROM evidence WHERE loop_id=? AND kind=? LIMIT 1",
                       (lid, kind)).fetchone() is not None


def record_evidence(con, lid, sid, kind, item):
    con.execute("INSERT OR IGNORE INTO evidence(loop_id,stage,kind,item,at) "
                "VALUES(?,?,?,?,?)", (lid, sid, kind, item, now()))


def acceptance_of(con, lid):
    """이 작업의 완료 조건. 회차를 넘어 유지된다."""
    # rowid 순 = 입력 순. at 으로 정렬하면 같은 초에 넣은 조건들의 순서가 뒤섞인다.
    return [r["item"] for r in con.execute(
        "SELECT item FROM evidence WHERE loop_id=? AND kind='acceptance' ORDER BY rowid",
        (lid,))]


# ------------------------------------------------------------------- promotion
#
# 기록만 하고 승격하지 않으면 복리가 아니라 일기다. 100개 레포를 조사한 연구에서
# 가장 흔한 설정 냄새가 62% 의 "lint leakage" — 훅으로 막을 것을 산문으로 적어둔
# 것이었다. 우리는 반복을 세고 있으니, 세는 데서 멈추지 않는다.
#
# ExpeL 의 중요도 투표와 CODESKILL 의 add/merge/drop 이 공통으로 말하는 것:
# 저장소는 쌓이면 안 되고 수렴해야 하며, 살아남는 기준은 근거다.

def promo_cfg(cfg):
    return cfg.get("promotion") or {}


def promo_key(kind, rule, target):
    """승격 단위의 키. block 은 규칙이, tool_fail 은 정규화된 명령이 의미 있는 키다.

    stats 의 묶음 기준과 같아야 한다 — 다르면 "3개 작업에서 반복"이라고 보여준
    항목과 승격을 요구하는 항목이 어긋난다.
    """
    return "%s:%s" % (kind, (rule if kind == "block" else target) or "-")


def repeated_items(con, cfg):
    """여러 작업에서 반복된 항목. 한 작업 안의 반복은 우연일 수 있다."""
    pc = promo_cfg(cfg)
    kinds = pc.get("kinds") or ["block", "tool_fail"]
    try:
        min_loops = max(2, int(pc.get("min_loops", 3)))
    except (TypeError, ValueError):
        min_loops = 3
    # 승격할 수 없는 규칙은 후보에서 뺀다. `no_reason`·`bypass_mode`·`protected` 는
    # 모델이 게이트를 우회하려 한 기록이다 — 하네스가 제대로 동작한 증거이지
    # 기계화할 습관이 아니다. 이걸 승격 대상으로 올리면 "우회 시도를 승격하라"는
    # 뜻이 되고, 사용자가 봐야 할 규율 신호가 결정 절차로 세탁된다. stats 에는 남는다.
    skip = set(pc.get("exclude_rules") or
               ["no_reason", "bypass_mode", "protected", "protected_bash"])
    out = []
    for kind in kinds:
        key_col = "rule" if kind == "block" else "target"
        for r in con.execute(
                "SELECT IFNULL(%s,'-') k, COUNT(*) c, COUNT(DISTINCT loop_id) loops, "
                "MAX(at) last FROM event WHERE kind=? GROUP BY k "
                "HAVING loops >= ? ORDER BY loops DESC, c DESC" % key_col,
                (kind, min_loops)):
            if kind == "block" and r["k"] in skip:
                continue
            out.append({"key": "%s:%s" % (kind, r["k"]), "kind": kind,
                        "name": r["k"], "count": r["c"], "loops": r["loops"],
                        "last": r["last"]})
    out.sort(key=lambda d: (-d["loops"], -d["count"]))
    return out


def _events_since(con, item, since):
    """승격 이후 같은 항목이 다시 걸렸는지. 승격이 통했는지의 유일한 객관 증거다.

    비교는 Python 에서 절대 시각으로 한다 — SQL 문자열 비교는 오프셋·DST 에서
    순서가 뒤집힌다. 한 항목의 이벤트 수는 작으므로 행을 가져와도 싸다.
    또한 경계를 1초 뒤로 둔다: 승격을 기록한 그 초에 남은 이벤트는 승격 이전의
    사실이므로 재발로 세면 방금 내린 결정이 즉시 무효화된다.
    """
    key_col = "rule" if item["kind"] == "block" else "target"
    cutoff = ts_epoch(since) + 1 if since else 0.0
    rows = con.execute(
        "SELECT at, loop_id FROM event WHERE kind=? AND IFNULL(%s,'-')=?" % key_col,
        (item["kind"], item["name"])).fetchall()
    hits = [r for r in rows if ts_epoch(r["at"]) >= cutoff]
    return len(hits), len({r["loop_id"] for r in hits})


def _reopen_after(cfg):
    try:
        return max(1, int(promo_cfg(cfg).get("reopen_after_loops", 2)))
    except (TypeError, ValueError):
        return 2


def is_regressed(con, cfg, p):
    """저장된 maturity 를 믿지 않고 지금 계산한다.

    sync_promotions 를 아무도 실행하지 않은 세션에서도 게이트가 맞아야 한다.
    저장값만 보면 `stats`/`tidy`/`promote` 를 부르지 않은 채 Compounding 을
    통과할 수 있다 — 실제로 그렇게 새어나갔다.
    """
    if p["maturity"] == "regressed":
        return True
    item = {"kind": p["kind"], "name": p["key"].split(":", 1)[1]}
    _, loops = _events_since(con, item, p["recheck_at"] or p["at"])
    return loops >= _reopen_after(cfg)


def sync_promotions(con, cfg):
    """성숙도를 재계산한다. 결정론적이다 — LLM 을 끼우면 등급이 표류한다.

    established → proven: 승격 후 재발이 없고 그 사이 작업이 N개 지났다.
    established → regressed: 승격 후에도 M개 작업에서 다시 걸렸다. 승격이
    통하지 않았다는 뜻이므로 다시 결정 대상으로 돌린다.
    """
    try:
        proven_after = max(1, int(promo_cfg(cfg).get("proven_after_loops", 3)))
    except (TypeError, ValueError):
        proven_after = 3
    changed = []
    for p in con.execute("SELECT * FROM promotion").fetchall():
        if p["maturity"] == "regressed":
            continue
        item = {"kind": p["kind"], "name": p["key"].split(":", 1)[1]}
        _, loops = _events_since(con, item, p["recheck_at"] or p["at"])
        if loops >= _reopen_after(cfg):
            con.execute("UPDATE promotion SET maturity='regressed' WHERE key=?",
                        (p["key"],))
            changed.append((p["key"], "regressed"))
            continue
        if p["maturity"] == "proven" or p["decision"] == "declined":
            continue
        # 작업 수도 절대 시각으로 센다 — created_at 문자열 비교는 위와 같은 이유로 틀린다.
        base = ts_epoch(p["at"])
        since = sum(1 for r in con.execute("SELECT created_at FROM loop")
                    if ts_epoch(r["created_at"]) > base)
        if loops == 0 and since >= proven_after:
            con.execute("UPDATE promotion SET maturity='proven' WHERE key=?",
                        (p["key"],))
            changed.append((p["key"], "proven"))
    return changed


def pending_promotions(con, cfg, limit=None):
    """결정이 필요한 항목. regressed 는 결정이 무효화됐으므로 다시 포함한다."""
    decided = {r["key"]: r for r in con.execute("SELECT * FROM promotion")}
    out = []
    for item in repeated_items(con, cfg):
        p = decided.get(item["key"])
        if p is None:
            out.append(item)
        elif is_regressed(con, cfg, p):
            out.append(dict(item, regressed=p["decision"]))
    if limit is None:
        pc = promo_cfg(cfg)
        try:
            limit = max(1, int(pc.get("max_per_cycle", 3)))
        except (TypeError, ValueError):
            limit = 3
    return out[:limit]


# ---------------------------------------------------------------- measurement
#
# 측정할 수 있는 것은 **마찰**이고 알고 싶은 것은 **가치**다. 마찰은 대리 지표다.
# 그래서 여러 지표를 하나의 점수로 합치지 않는다 — 합치면 그 하나를 최적화하게 되고,
# 차단은 아무것도 시도하지 않거나 우회하는 것으로도 줄어든다. 나란히 놓고 사람이 읽는다.
#
# 스키마를 바꾸지 않는다. 두 기능 모두 새 event 종류로만 기록하므로 구버전 DB 에서
# "no such column" 으로 하네스가 죽는 일이 없다.

def cycle_window_start(con, lid):
    """이번 회차가 시작된 시각. 마지막 회차 종료 기록이 없으면 작업 생성 시각."""
    row = con.execute(
        "SELECT MAX(at) a FROM event WHERE loop_id=? AND kind='cycle_close'",
        (lid,)).fetchone()
    if row and row["a"]:
        return row["a"]
    row = con.execute("SELECT created_at FROM loop WHERE id=?", (lid,)).fetchone()
    return (row["created_at"] if row else None) or ""


def _window_events(con, lid, start, kinds=None):
    """[start, 지금) 안의 이 작업 이벤트. 비교는 절대 시각으로 한다."""
    lo = ts_epoch(start)
    sql = "SELECT at, kind, rule, target FROM event WHERE loop_id=?"
    params = [lid]
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        params += list(kinds)
    return [r for r in con.execute(sql, params) if ts_epoch(r["at"]) >= lo]


def cycle_counters(con, lid, start):
    """이 회차의 마찰 수치. 회피 지표를 반드시 함께 담는다 — 차단만 보면 속는다."""
    rows = _window_events(con, lid, start)
    tally = {}
    for r in rows:
        tally[r["kind"]] = tally.get(r["kind"], 0) + 1

    # 재편집 최대치: 한 파일을 몇 번 고쳤나. 구조 냄새의 대리 지표.
    edits = {}
    for r in rows:
        if r["kind"] == "edit":
            edits[r["target"]] = edits.get(r["target"], 0) + 1

    # 반복 실패: 이 회차의 실패 중 그 명령이 **이전에도** 실패한 적 있는 것.
    # 여기도 절대 시각으로 비교해야 한다. SQL 문자열 비교를 쓰면 형식이 조금만
    # 달라도(공백 구분 vs 'T', 오프셋 유무) 창 밖의 것이 안으로 들어온다.
    # 작업 경계를 넘어 센다 — 지난 작업에서 실패한 명령이 또 실패하는 것이 요점이다.
    lo = ts_epoch(start)
    seen_before = {r["target"] for r in con.execute(
        "SELECT at, target FROM event WHERE kind='tool_fail'")
        if ts_epoch(r["at"]) < lo}
    refails, seen_now = 0, set()
    for r in rows:
        if r["kind"] != "tool_fail":
            continue
        if r["target"] in seen_before or r["target"] in seen_now:
            refails += 1
        seen_now.add(r["target"])

    return {
        "dur": max(0, int(time.time() - lo)) if start else 0,
        "blocks": tally.get("block", 0),
        "fails": tally.get("tool_fail", 0),
        "refails": refails,
        "churn": max(edits.values()) if edits else 0,
        "edits": tally.get("edit", 0),
        "gates": tally.get("stop_gate", 0),
        "bypass": tally.get("bypass", 0),
        "skips": tally.get("skip", 0),
        "declines": tally.get("promote_declined", 0),
        "promotes": tally.get("promote", 0),
    }


def record_cycle_close(con, cfg, lid, sid):
    """회차 경계에서 그 회차의 집계를 한 줄로 남긴다.

    stage 행은 작업이 닫힐 때 삭제되므로 나중에 회차별 비용을 되살릴 수 없다.
    경계에서 스냅샷을 남기면 event 는 작업이 닫혀도 살아남아 측정이 가능해진다.
    """
    start = cycle_window_start(con, lid)
    c = cycle_counters(con, lid, start)
    c["cycle"] = cycle_of(con, lid)
    record_event(con, lid, sid, "cycle_close", str(c["cycle"]),
                 "%s-%d" % (lid, c["cycle"]), json.dumps(c, ensure_ascii=False))
    return c


def verify_globs(cfg, as_kind):
    v = (promo_cfg(cfg).get("verify_globs") or {}).get(as_kind)
    return v if isinstance(v, list) else None


def promote_change_seen(con, cfg, lid, as_kind):
    """승격 주장에 맞는 파일 변경이 이 회차에 실제로 있었는가.

    `--as hook` 이라고 써놓고 아무것도 고치지 않아도 게이트는 만족된다 —
    노트는 주장이다. 막지는 않는다(무엇을 고쳐야 하는지는 판단이므로). 대신
    관측 사실을 기록해서, 나중에 `metrics` 가 주장과 사실을 나란히 보여준다.
    None 은 '검증 대상 아님'이다 (rule 은 LEARNED.md 이고 하네스가 쓴다).
    """
    pats = verify_globs(cfg, as_kind)
    if not pats:
        return None
    excl = promo_cfg(cfg).get("verify_exclude") or []
    start = cycle_window_start(con, lid)
    for r in _window_events(con, lid, start, ("edit",)):
        rel = r["target"] or ""
        if any(glob_match(rel, p) for p in excl):
            continue
        if any(glob_match(rel, p) for p in pats):
            return True
    return False


def promotion_summary(con, cfg):
    rows = con.execute("SELECT maturity, COUNT(*) c FROM promotion "
                       "GROUP BY maturity").fetchall()
    return {r["maturity"]: r["c"] for r in rows}


def learned_lines(con, cfg):
    """LEARNED.md 에 실릴 규칙 줄. rule 로 승격되고 살아 있는 것만.

    저장된 maturity 가 아니라 실시간 판정을 쓴다 — 그러지 않으면 재발한 규칙이
    항상 로드되는 문서에 계속 남는다(실제로 남았다).
    """
    rows = con.execute(
        "SELECT * FROM promotion WHERE decision='rule' ORDER BY at").fetchall()
    return [r for r in rows if not is_regressed(con, cfg, r)]


def learned_budget(cfg):
    try:
        return max(1, int(promo_cfg(cfg).get("learned_max_lines", 20)))
    except (TypeError, ValueError):
        return 20


LEARNED_HEAD = """# 승격된 규칙

반복된 실수에서 승격된 규칙이다. 하네스가 생성하므로 직접 편집하지 마라 —
`harness promote` 로 올리고 `harness tidy` 로 내린다. 항상 로드되므로 예산이 있다
(최대 %d줄). 예산이 찬 상태에서 새 규칙을 올리려면 먼저 한 줄을 비워야 한다.
"""


def refresh_learned(con, cfg, root):
    """LEARNED.md 를 promotion 테이블에서 다시 생성한다.

    손으로 고친 내용이 성숙도와 어긋나지 않게 하려면 생성이 유일한 경로여야 한다.
    """
    rows = learned_lines(con, cfg)
    body = [LEARNED_HEAD % learned_budget(cfg)]
    if rows:
        for r in rows:
            body.append("- [%s] %s <!-- %s -->"
                        % (r["maturity"], (r["note"] or "").strip(), r["key"]))
    else:
        body.append("(아직 없다 — 반복된 실수가 승격되면 여기에 쌓인다.)")
    return _write_if_changed(os.path.join(root, LEARNED_REL),
                            "\n".join(body) + "\n")


def exit_blockers(con, cfg, lid, sid):
    return [k for k in stage_obj(cfg, sid).get("exit_criteria", [])
            if not criterion_met(con, cfg, lid, k)]


def criterion_met(con, cfg, lid, kind):
    """종료 조건 충족 여부.

    promotion_decided 는 evidence 행이 아니다 — 프로젝트 전체의 반복 항목에서
    계산되므로, 루프에 증거를 심어두면 다음 회차에 새로 생긴 반복을 놓친다.
    """
    if kind == "promotion_decided":
        return not pending_promotions(con, cfg)
    return has_evidence(con, lid, kind)


CRITERIA_HELP = {
    "intent_set": "이번에 할 작업을 `harness loop intent \"<작업>\"` 으로 기록해야 한다 "
                  "(Context 의 recall 이 이것을 기준으로 조회한다)",
    "acceptance": "무엇이 '끝'인지를 `harness loop done-when \"<조건>\" ...` 으로 "
                  "기록해야 한다 (Verification 이 대조할 기준이다)",
    "plan_file": "계획 파일을 .dev/plan/ 아래에 남겨야 한다",
    "plan_approved": "계획에 대한 사람의 승인이 필요하다 (harness approve-plan <file>)",
    "verification_evidence": "검증 증거가 없다 — 테스트 실행, 서브에이전트 검토, "
                             "브라우저 확인, dry-run 중 하나를 수행하라",
    "retro_file": "회고를 .dev/retrospect/ 또는 .dev/learning/ 아래에 기록해야 한다",
    "promotion_decided": "여러 작업에서 반복된 항목에 대한 승격 결정이 남았다 — "
                         "`harness promote` 로 목록을 보고 하나씩 결정하라. "
                         "승격하지 않기로 하는 것도 결정이다 (--decline --reason \"...\")",
}


# ------------------------------------------------------------------- path logic

def glob_match(rel, pat):
    """fnmatch 는 `**/` 를 '0개 이상의 디렉터리'로 다루지 못한다.
    `a/**/b.md` 가 `a/b.md` 에 매칭되지 않으므로 `**/` 를 뺀 형태도 함께 시도한다."""
    if fnmatch.fnmatch(rel, pat):
        return True
    if "**/" in pat and fnmatch.fnmatch(rel, pat.replace("**/", "", 1)):
        return True
    return False


def rel_to_root(root, path):
    if not path:
        return None
    p = path if os.path.isabs(path) else os.path.join(root, path)
    try:
        rel = os.path.relpath(os.path.normpath(p), root)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/")


def classify(rel, cfg):
    for cls, pats in cfg.get("path_classes", {}).items():
        for pat in pats:
            if glob_match(rel, pat):
                return cls
    return "source"


def find_grant(con, lid, rel):
    for g in con.execute("SELECT * FROM wgrant WHERE loop_id=? AND uses_left>0", (lid,)):
        if glob_match(rel, g["glob"]):
            return g
    return None


def new_toplevel_dir(root, rel):
    parts = rel.split("/")
    if len(parts) < 2:
        return None
    return None if os.path.isdir(os.path.join(root, parts[0])) else parts[0]


def check_write(con, cfg, root, lid, sid, rel):
    """(decision, reason). decision None 이면 판정하지 않음.

    차단은 event 에 적립한다 — 어떤 규칙에 몇 번 걸리는지가 복리의 원료다.
    """
    stage = stage_obj(cfg, sid)
    lbl = label_of(cfg, sid)
    cls = classify(rel, cfg)
    rules = cfg.get("folder_rules", {})
    grant = find_grant(con, lid, rel)

    def deny(rule, reason):
        record_event(con, lid, sid, "block", rule, rel, reason)
        return "deny", reason

    # 0. 하네스 자신을 보호한다. 예외(`allow`)로도 열리지 않는다.
    #    엔진을 고치면 게이트를 무력화할 수 있고, DB 를 덮어쓰면 손상 시 차단하지 않는
    #    설계(세션을 벽돌로 만들지 않기 위한 것) 때문에 게이트 전체가 조용히 꺼진다.
    for pat in rules.get("protected_paths", []):
        if glob_match(rel, pat):
            return deny("protected", (
                "하네스 자신은 수정할 수 없다 (%s). 규칙을 바꾸려면 "
                "`.claude/harness/stages.json` 을 고치고, 엔진을 바꾸려면 플러그인을 "
                "수정하라. 이 차단은 `allow` 로도 열리지 않는다." % rel))

    # 1. docs/ 는 인간 소유
    if cls == "docs" and not grant:
        return deny("docs_readonly", (
            "docs/ 는 사람이 기록하는 영역이라 하네스가 쓰기를 막는다 (%s). "
            "정말 필요하면 사용자에게 승인을 받아라: "
            "`.claude/harness/bin/harness allow \"%s\" --reason \"...\"`" % (rel, rel)))

    # 2. 단계별 쓰기 허용 클래스
    if cls not in stage.get("write", []) and not grant:
        return deny("stage_write", (
            "현재 단계 %s 에서는 '%s' 클래스 경로에 쓸 수 없다 (%s). 허용: %s. "
            "단계를 진행하려면 `.claude/harness/bin/harness advance`, "
            "건너뛰려면 `... skip <stage> --reason \"...\"`."
            % (lbl, cls, rel, ", ".join(stage.get("write", [])) or "(없음)")))

    # 3. 신규 최상위 폴더는 Scaffolding 에서만 (승인된 구조 영역은 제외)
    top = new_toplevel_dir(root, rel) if cls == "source" else None
    if top and sid not in rules.get("new_toplevel_dir_stages", ["scaffolding"]):
        return deny("new_toplevel", (
            "신규 최상위 폴더 '%s/' 는 Scaffolding 단계에서만 만들 수 있다 (현재 %s). "
            "구조 변경이 필요하면 Scaffolding 으로 되돌아가서 합의하라." % (top, lbl)))

    parts = rel.split("/")

    # 4. .dev 하위 폴더 규칙 + 작업 해시 파일명
    #    `.dev/` 직속 파일(예: .dev/INDEX.md)은 제약하지 않는다 — 누적 인덱스처럼
    #    특정 회차에 속하지 않는 문서의 자리가 필요하다. 폴더명 제약은 유지한다.
    if cls == "dev" and len(parts) >= 3:
        allowed = rules.get("dev_subdirs", [])
        if allowed and parts[1] not in allowed:
            return deny("dev_subdir",
                        ".dev/ 하위 폴더는 %s 만 허용한다. '%s' 는 규칙 위반이다. "
                        "특정 회차에 속하지 않는 누적 문서는 `.dev/` 직속에 둘 수 있다."
                        % ("/".join(allowed), parts[1]))
        if parts[1] in rules.get("loop_prefixed_dirs", []) \
                and parts[-1] not in rules.get("prefix_exempt_names", []):
            # 누적 문서(INDEX.md 등)는 특정 회차의 소유가 아니므로 접두사가 의미상 틀리다.
            # 파일이 쌓이면 이 인덱스가 361개를 대표하는 진입점이 된다.
            pre = file_prefix(con, lid)
            if not parts[-1].startswith(pre):
                return deny("loop_prefix", (
                    "이 작업의 산출물은 파일명이 '<작업해시>-<회차>-' 로 시작해야 한다. "
                    "'%s' 대신 '%s%s' 로 써라. 앞단 해시(%s)로 grep 하면 한 작업의 "
                    "산출물이 회차와 무관하게 모인다." % (parts[-1], pre, parts[-1], lid)))

    # 5. docs 하위 번호 명명 규칙 (grant 로 쓰기가 허용된 경우에만 도달)
    if cls == "docs" and len(parts) >= 3 and parts[1] in rules.get("docs_subdirs", []):
        pat = rules.get("numbered_name_pattern")
        if pat and not re.match(pat, parts[-1]):
            return deny("docs_naming",
                        "docs/%s/ 파일명은 NNN-name.md 형식이어야 한다. "
                        "'%s' 는 규칙 위반이다." % (parts[1], parts[-1]))

    if grant:
        con.execute("UPDATE wgrant SET uses_left=uses_left-1 WHERE id=?", (grant["id"],))
    return None, None


BASH_SPLIT = re.compile(r"\|\||&&|[;&|\n]")
# 이 프로그램들에 넘긴 경로는 '실행 대상'이다. 하네스 래퍼를 python3 로 돌리는 것은
# 정상 동작이므로 변경 시도로 오인해서는 안 된다.
BASH_INTERPRETERS = ("python", "python3", "sh", "bash", "zsh", "env", "exec", "node")
# 읽기만 하는 명령은 막지 않는다. 과잉 차단은 마찰이 되고, 마찰은 게이트를 끄게 만든다.
BASH_READERS = ("cat", "less", "more", "head", "tail", "grep", "rg", "wc", "file",
                "stat", "ls", "find", "diff", "shasum", "md5", "md5sum", "awk", "cut")


def bash_protected_hit(cfg, root, cmd):
    """Bash 명령이 보호 경로를 **대상으로** 삼는지. 실행하는 것은 대상이 아니다.

    check_write 는 Write/Edit 만 본다. Bash 는 `rm`, `sed -i`, 리다이렉트,
    `sqlite3 ... UPDATE` 로 같은 파일을 바꿀 수 있었고 그 경로는 검사되지 않았다.
    """
    pats = (cfg.get("folder_rules") or {}).get("protected_paths") or []
    if not pats:
        return None

    def protected(tok):
        rel = rel_to_root(root, tok.strip("\"'"))
        if not rel or rel == ".":
            return None
        return rel if any(glob_match(rel, p) for p in pats) else None

    for seg in BASH_SPLIT.split(cmd):
        toks = re.findall(r"\S+", seg)
        if not toks:
            continue
        head = os.path.basename(toks[0].strip("\"'"))
        # 리다이렉트가 있으면 읽기 명령도 쓰기가 된다 (`cat x > 엔진`).
        if head in BASH_READERS and ">" not in seg:
            continue
        skip = 2 if head in BASH_INTERPRETERS and len(toks) > 1 else 1
        for tok in toks[skip:]:
            hit = protected(tok)
            if hit:
                return hit
    return None


# ------------------------------------------------------------------ hook output

def emit(obj):
    if obj:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def pre_decision(decision, reason):
    out = {"hookEventName": "PreToolUse", "permissionDecision": decision}
    if reason:
        out["permissionDecisionReason"] = reason
    return {"hookSpecificOutput": out}


# ------------------------------------------------------------------------ hooks

def hook_session_start(inp, con, cfg, root, lid, sid):
    refresh_wrapper(root)
    # LEARNED.md 는 CLAUDE.md 앵커로 **이 순간** 로드된다. 그러니 여기서 맞춰야 한다.
    # promote 에서만 재생성하면 그 사이 재발한 규칙이 계속 실린다.
    try:
        with con:
            sync_promotions(con, cfg)
        refresh_learned(con, cfg, root)
    except Exception:
        pass  # 복리 장치가 세션 시작을 막지는 않는다
    stage = stage_obj(cfg, sid)
    lines = [
        "[harness] 작업 %s · 회차 %d · 단계 %s — %s"
        % (lid, cycle_of(con, lid), label_of(cfg, sid), stage["summary"]),
        "제어: `.claude/harness/bin/harness` {status | advance | skip <대상> --reason \"...\"} "
        "· 나머지 명령은 `harness help`",
        "이 단계 쓰기 허용: %s. `.dev/` 산출물 파일명은 `%s` 로 시작해야 한다. "
        "근거 문서: `.claude/harness/rationale.md`"
        % (", ".join(stage.get("write", [])) or "(없음)", file_prefix(con, lid)),
        "모든 답변 말머리에 [%s] 를 붙여라." % stage["label"],
    ]
    if stage.get("hint"):
        lines.append(stage["hint"])
    if auto_skip_on(con):
        lines.append("⚠ 스킵 자동 승인 ON (%s) — 스킵에 사용자 다이얼로그가 뜨지 않는다. "
                     "사유는 여전히 필수다. 끄려면 `harness auto-skip off`."
                     % auto_skip_scope_note(con))
    sk = skips_of(con, lid)
    if sk:
        lines.append("이 루프의 스킵: "
                     + "; ".join("%s(%s, %s)" % (r["stage"], r["reason"],
                                                 r["authorized_by"]) for r in sk))
    try:
        pend = pending_promotions(con, cfg)
    except Exception:
        pend = []
    if pend:
        lines.append("승격 결정 대기 %d개 (%s) — Compounding 의 종료 조건이다. "
                     "`harness promote` 로 결정하라."
                     % (len(pend), ", ".join(it["key"] for it in pend)))
    emit({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                 "additionalContext": "\n".join(lines)}})


def auto_skip_state(con):
    """(활성, 만료사유) — 범위·횟수 만료까지 반영한 실제 상태."""
    if get_meta(con, "auto_skip") != "on":
        return False, None
    scope = get_meta(con, "auto_skip_loop")
    if scope and scope != head_loop(con):
        return False, "작업 %s 범위였고 작업이 바뀌어 만료됐다" % scope
    uses = get_meta(con, "auto_skip_uses")
    if uses:
        try:
            if int(uses) <= 0:
                return False, "사용 횟수를 모두 소진해 만료됐다"
        except ValueError:
            pass
    return True, None


def auto_skip_on(con):
    return auto_skip_state(con)[0]


def auto_skip_uses_left(con):
    uses = get_meta(con, "auto_skip_uses")
    try:
        return int(uses) if uses else None
    except ValueError:
        return None


def consume_auto_skip(con):
    """자동 승인 1회 소진. 남은 횟수(무제한이면 None)를 돌려준다."""
    left = auto_skip_uses_left(con)
    if left is None:
        return None
    left = max(0, left - 1)
    set_meta(con, "auto_skip_uses", str(left))
    # 플래그는 사용자의 의도를 담고, 실효 상태는 auto_skip_state 가 계산한다.
    # 여기서 'off' 로 뒤집으면 "왜 꺼졌는지"를 잃는다.
    return left


def auto_skip_scope_note(con):
    bits = []
    scope = get_meta(con, "auto_skip_loop")
    if scope:
        bits.append("작업 %s 범위" % scope)
    left = auto_skip_uses_left(con)
    if left is not None:
        bits.append("남은 %d회" % left)
    return ", ".join(bits) or "무제한"


def ctrl_decision(con, sub, cmd, mode, lid, sid):
    """제어 명령에 대한 판정. 사람의 동의가 필요한 것만 ask 로 올린다."""
    if sub == "auto-skip":
        # off 는 게이트 복원이므로 동의 없이 허용한다. on 은 게이트를 무력화하므로
        # 반드시 사람의 동의를 받는다 — 그러지 않으면 모델이 스스로 켤 수 있다.
        if not re.search(r"auto-skip\s+on\b", cmd):
            return
    elif sub not in CONSENT_CMDS:
        return

    reason = arg_value(cmd, "reason")
    if sub != "approve-plan" and not reason:
        record_event(con, lid, sid, "block", "no_reason", sub, cmd[:200])
        return emit(pre_decision("deny",
            "사유 없이 %s 할 수 없다. --reason \"...\" 로 사유를 명시하라." % sub))

    if sub == "skip" and auto_skip_on(con):
        # 자동 승인이 켜져 있다. 다이얼로그는 생략하되 사실은 사용자에게 노출한다.
        out = pre_decision("defer", None)
        out["systemMessage"] = ("harness: 단계 스킵을 자동 승인했다 (사유: %s · %s). "
                                "끄려면 `harness auto-skip off`."
                                % (reason, auto_skip_scope_note(con)))
        return emit(out)

    detail = "%s: `%s`" % (CTRL_HEAD[sub], cmd.strip())
    if reason:
        detail += "\n사유: %s" % reason
    detail += "\n승인하면 하네스 상태에 기록된다."
    if mode == "bypassPermissions":
        record_event(con, lid, sid, "block", "bypass_mode", sub, cmd[:200])
        return emit(pre_decision("deny", detail +
            "\nbypassPermissions 모드에서는 사람의 동의를 받을 수 없어 거부한다. "
            "권한 모드를 낮추고 다시 시도하라."))
    return emit(pre_decision("ask", detail))


def hook_pre_tool_use(inp, con, cfg, root, lid, sid):
    tool = inp.get("tool_name") or ""
    ti = inp.get("tool_input") or {}
    mode = inp.get("permission_mode") or "default"

    if tool == "Bash":
        cmd = ti.get("command") or ""

        # 하네스 자신을 Bash 로 건드리는 것을 먼저 막는다. 제어 명령 판정보다
        # 앞이어야 한다 — `rm <엔진> && harness status` 처럼 뒤에 제어 명령을
        # 붙이면 그 앞의 파괴가 검사 없이 통과했다.
        hit = bash_protected_hit(cfg, root, cmd)
        if hit:
            with con:
                record_event(con, lid, sid, "block", "protected_bash", hit, cmd[:200])
            return emit(pre_decision("deny", (
                "하네스 자신(%s)은 Bash 로도 변경할 수 없다. 규칙을 바꾸려면 "
                "`.claude/harness/stages.json` 을, 엔진을 바꾸려면 플러그인을 "
                "수정하라. 내용을 보려면 Read 도구를 쓰라. "
                "이 차단은 `allow` 로도 열리지 않는다." % hit)))

        m = CTRL_RE.search(cmd)
        if m:
            sub = m.group(1)
            if m.group(2) and m.group(2) in CTRL_SUB2.get(sub, ()):
                sub = "%s %s" % (sub, m.group(2))
            with con:
                return ctrl_decision(con, sub, cmd, mode, lid, sid)

        if BASH_MUTATORS.search(cmd):
            for tok in re.findall(r"[\w./~-]+", cmd):
                rel = rel_to_root(root, tok)
                if rel and "/" in rel and classify(rel, cfg) == "docs" \
                        and not find_grant(con, lid, rel):
                    with con:
                        record_event(con, lid, sid, "block", "docs_readonly_bash",
                                     rel, cmd[:200])
                    return emit(pre_decision("deny",
                        "docs/ 는 사람이 기록하는 영역이다. 이 명령이 '%s' 를 변경할 수 "
                        "있어 막는다. 필요하면 `harness allow` 로 승인을 받아라." % rel))
        return

    field = WRITE_TOOLS.get(tool)
    if not field:
        return
    rel = rel_to_root(root, ti.get(field))
    if not rel:
        return
    with con:
        decision, reason = check_write(con, cfg, root, lid, sid, rel)
    if decision:
        emit(pre_decision(decision, reason))


def hook_post_tool_use(inp, con, cfg, root, lid, sid):
    """증거만 조용히 적립한다. 컨텍스트로 출력하지 않는다."""
    tool = inp.get("tool_name") or ""
    ti = inp.get("tool_input") or {}
    signals = cfg.get("evidence_signals", {})

    with con:
        field = WRITE_TOOLS.get(tool)
        if field:
            rel = rel_to_root(root, ti.get(field))
            if rel:
                # 편집 이력. 한 루프에서 같은 파일을 몇 번 고쳤는지가 구조 냄새다.
                record_event(con, lid, sid, "edit", None, rel)
                for kind, sig in signals.items():
                    for pat in sig.get("write_glob", []):
                        if glob_match(rel, pat):
                            record_evidence(con, lid, sid, kind, rel)
                            break
        if sid in EVIDENCE_STAGES:
            sig = signals.get("verification_evidence", {})
            if tool == "Bash":
                pat = sig.get("bash_pattern")
                cmd = ti.get("command") or ""
                if pat and re.search(pat, cmd):
                    record_evidence(con, lid, sid, "verification_evidence",
                                    cmd.strip()[:120])
            if tool in sig.get("tools", []):
                record_evidence(con, lid, sid, "verification_evidence", "agent:" + tool)
            tp = sig.get("tool_pattern")
            if tp and re.search(tp, tool):
                record_evidence(con, lid, sid, "verification_evidence", "tool:" + tool)


def hook_post_tool_use_failure(inp, con, cfg, root, lid, sid):
    """도구 실패를 적립한다. 같은 실패가 반복되는 것이 '동일한 실수'의 직접 증거다.

    오류 필드명이 문서에 명시돼 있지 않아 후보를 순서대로 시도한다.
    """
    tool = inp.get("tool_name") or ""
    ti = inp.get("tool_input") or {}
    err = ""
    for key in ("tool_error", "error", "tool_output", "tool_response", "message"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            err = val.strip()
            break
    target = norm_cmd(ti.get("command")) if tool == "Bash" else tool
    if tool in WRITE_TOOLS:
        target = "%s %s" % (tool, rel_to_root(root, ti.get(WRITE_TOOLS[tool])) or "")
    target = target or tool

    # 적립 **전에** 과거를 센다. 지금 실패를 넣고 세면 첫 실패가 2회로 보이고,
    # '이전 오류'가 방금 그 오류가 되어 아무 정보도 주지 않는다.
    prior = con.execute(
        "SELECT COUNT(*) c, COUNT(DISTINCT loop_id) loops FROM event "
        "WHERE kind='tool_fail' AND target=?", (target,)).fetchone()
    prev = con.execute(
        "SELECT detail FROM event WHERE kind='tool_fail' AND target=? "
        "AND detail IS NOT NULL AND detail != '' ORDER BY id DESC LIMIT 1",
        (target,)).fetchone()
    # 현재 작업이 과거 집합에 이미 있는지 봐야 한다. 무조건 +1 하면 한 작업에서
    # 세 번 실패한 것을 "작업 2개에서 반복"으로 세어 승격 요구가 거짓이 된다.
    seen_here = con.execute(
        "SELECT 1 FROM event WHERE kind='tool_fail' AND target=? AND loop_id=? LIMIT 1",
        (target, lid)).fetchone()
    with con:
        record_event(con, lid, sid, "tool_fail", tool, target, err)
    if not prior["c"]:
        return  # 첫 실패는 아직 배울 것이 없다

    # 실패한 순간이 회수의 최적 시점이다 — 무엇을 물어볼지 이미 알기 때문이다.
    # 여기서 알려주지 않으면 모델은 같은 벽을 다시 받는다.
    emit_failure_recall(con, cfg, root, target, prior["c"] + 1,
                        prior["loops"] + (0 if seen_here else 1), prev)


def emit_failure_recall(con, cfg, root, target, n, loops, prev):
    lines = ["[harness] '%s' 실패가 %d번째다%s."
             % (target, n, " (작업 %d개에 걸쳐)" % loops if loops > 1 else "")]

    p = con.execute("SELECT decision, maturity, note FROM promotion WHERE key=?",
                    ("tool_fail:%s" % target,)).fetchone()
    if p and p["decision"] == "declined":
        lines.append("이 항목은 승격을 보류한 적이 있다: %s — 또 걸린다면 그 판단이 "
                     "틀렸다는 증거다. 회고에 쓰라." % (p["note"] or "-"))
    elif p:
        lines.append("이 항목은 이미 %s(%s): %s — 승격이 통하지 않고 있다면 "
                     "회고에 그 사실을 쓰라."
                     % (PROMOTE_AS.get(p["decision"], p["decision"]),
                        p["maturity"], (p["note"] or "-")))

    if prev and prev["detail"]:
        lines.append("이전 오류: %s" % " ".join(prev["detail"].split())[:160])

    # 인덱스는 조건 없이 앞에 놓이므로, 그대로 쓰면 모든 실패에 같은 인덱스가 붙어
    # 벽지가 된다. 키워드에 실제로 걸린 파일을 우선하고 인덱스는 대체 경로로만 준다.
    indexes, files = _recall_files(root, [target], limit=4)
    if files:
        lines.append("관련 기록 — 같은 실수를 다시 하기 전에 읽어라: %s"
                     % ", ".join(files[:3]))
    elif indexes:
        lines.append("이 실패를 직접 다룬 기록은 없다. 인덱스에서 찾아보라: %s"
                     % ", ".join(indexes[:2]))
    else:
        lines.append("관련 기록이 없다. 이번에 해결하면 회고에 남겨라 "
                     "(다음에 이 자리에서 제시된다).")

    out = {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure",
                                  "additionalContext": "\n".join(lines)}}
    # 승격 임계에 닿으면 사용자에게도 보인다 — 모델이 같은 벽을 반복하는 것은
    # 사용자가 알아야 할 사실이다. 단 이미 결정된 항목에는 결정을 요구한다고
    # 말하지 않는다 — 결정이 있는데 또 요구하면 메시지가 거짓이 된다.
    try:
        thr = int(promo_cfg(cfg).get("min_loops", 3))
    except (TypeError, ValueError):
        thr = 3
    undecided = p is None or is_regressed(con, cfg, p)
    if loops >= thr and undecided:
        out["systemMessage"] = ("harness: '%s' 실패가 작업 %d개에서 반복된다 — "
                               "Compounding 에서 승격 결정을 요구한다." % (target, loops))
    elif loops >= thr:
        out["systemMessage"] = ("harness: '%s' 실패가 작업 %d개에서 반복된다 "
                               "(이미 %s 로 결정됨 — 재발이 이어지면 결정이 무효화된다)."
                               % (target, loops, p["decision"]))
    emit(out)


def hook_stop(inp, con, cfg, root, lid, sid):
    stage = stage_obj(cfg, sid)
    prompt_id = inp.get("prompt_id") or "-"
    msg = (inp.get("last_assistant_message") or "").strip()
    limits = cfg.get("stop_block_limits", {})

    problems = []
    if msg:
        # 단계 이름과 **정확히** 일치해야 한다. 번호 병기(`[4/6 Execution]`)는 이름에서
        # 도출되는 중복 정보라 허용하지 않는다. 정확 일치라서 한 이름이 다른 이름의
        # 부분 문자열일 때 생기는 오탐도 없다.
        # 이 프롬프트 동안 관여한 단계의 이름은 모두 허용한다 (턴 중 전이 대응).
        seen = [r["stage"] for r in con.execute(
            "SELECT stage FROM prompt_stage WHERE prompt_id=?", (prompt_id,))]
        allowed = {stage_obj(cfg, s)["label"].lower() for s in seen + [sid]}
        m = re.match(r"^\[([^\]]{1,60})\]", msg)
        inside = m.group(1).strip().lower() if m else ""
        if inside not in allowed:
            problems.append(("prefix",
                "말머리는 [%s] 여야 한다. 단계 이름만 대괄호로 감싸 맨 앞에 붙여라 "
                "(번호 병기 불가)." % stage["label"]))
    for key in stage.get("stop_requires", []):
        if not criterion_met(con, cfg, lid, key):
            problems.append((key, "%s 단계를 끝낼 수 없다: %s"
                             % (stage["label"], CRITERIA_HELP.get(key, key))))
    # 진행 유도 (opt-in, 기본 꺼짐). 남은 단계가 있으면 턴 종료를 막아 이어붙인다.
    # 하네스는 원래 반응만 하고 턴을 시작하지 않는다. 이건 그 한계를 Stop 훅으로 미는 실험이다.
    sc = cfg.get("stop_continue") or {}
    if not problems and sc.get("enabled"):
        left = con.execute("SELECT COUNT(*) c FROM stage WHERE loop_id=? "
                           "AND status IN ('pending','active')", (lid,)).fetchone()["c"]
        limit = int(sc.get("max_per_prompt", 3))
        n = con.execute("SELECT COUNT(*) c FROM stop_block WHERE prompt_id=? "
                        "AND key='continue'", (prompt_id,)).fetchone()["c"]
        if left and n < limit:
            with con:
                con.execute("INSERT INTO stop_block(prompt_id,key,at) VALUES(?,?,?)",
                            (prompt_id, "continue", now()))
            return emit({"decision": "block", "reason": (
                "작업이 아직 끝나지 않았다 (현재 %s, 남은 단계 %d). 멈추지 말고 이어서 진행하라 — "
                "`harness status` 로 이 단계의 종료 조건을 확인하고 채운 뒤 `harness advance`. "
                "작업이 정말 끝났으면 Compounding 에서 `harness advance --done` 으로 닫아라. "
                "(이어붙임 %d/%d)" % (stage["label"], left, n + 1, limit))})

    if not problems:
        return

    blocked, exhausted = [], []
    with con:
        for key, text in problems:
            n = con.execute("SELECT COUNT(*) c FROM stop_block "
                            "WHERE prompt_id=? AND key=?", (prompt_id, key)).fetchone()["c"]
            if n >= int(limits.get(key, 1)):
                exhausted.append(key)
                continue
            con.execute("INSERT INTO stop_block(prompt_id,key,at) VALUES(?,?,?)",
                        (prompt_id, key, now()))
            record_event(con, lid, sid, "stop_gate", key, stage["id"], text)
            blocked.append(text)
        for key in exhausted:
            record_event(con, lid, sid, "bypass", key, stage["id"],
                         "차단 상한 소진으로 미충족 상태 종료")

    if blocked:
        emit({"decision": "block", "reason": " / ".join(blocked)})
    elif exhausted:
        # 조용히 통과시키지 않는다 — 우회 사실을 사용자에게 노출한다
        emit({"systemMessage": "harness: %s 단계를 미충족 상태로 종료했다 (%s). "
                               "차단 상한 소진." % (stage["label"], ", ".join(exhausted))})


HOOKS = {
    "SessionStart": hook_session_start,
    "PreToolUse": hook_pre_tool_use,
    "PostToolUse": hook_post_tool_use,
    "PostToolUseFailure": hook_post_tool_use_failure,
    "Stop": hook_stop,
}


def run_hook():
    try:
        inp = json.load(sys.stdin)
    except Exception:
        return 0
    root = find_root(inp.get("cwd"))
    if not root:
        return 0  # 하네스 미설치 프로젝트 — 조용히 종료
    con = connect(root)
    if con is None:
        return 0
    try:
        cfg = load_config(root, plugin_root())
        if not isinstance(cfg, dict) or not cfg.get("stages"):
            return 0  # 설정이 깨졌으면 차단하지 않는다
        lid = head_loop(con)
        sid = active_stage(con, lid) if lid else None
        if not lid or not sid:
            return 0
        pid = inp.get("prompt_id")
        if pid:
            with con:
                con.execute("INSERT OR IGNORE INTO prompt_stage(prompt_id,stage,at) "
                            "VALUES(?,?,?)", (pid, sid, now()))
        fn = HOOKS.get(inp.get("hook_event_name"))
        if fn:
            fn(inp, con, cfg, root, lid, sid)
    except Exception as exc:
        # 차단하지 않는다. exit 1 은 Claude Code 가 훅 오류로 표면화하므로,
        # 무력해지더라도 조용히 빠진다 — 세션을 벽돌로 만드는 것보다 낫다.
        # 단 세션 시작 때는 한 번 알린다. 조용히 죽으면 고장을 모른다.
        sys.stderr.write("step-six-harness: %s\n" % exc)
        if inp.get("hook_event_name") == "SessionStart":
            emit({"systemMessage":
                  "harness: 하네스가 오류로 비활성 상태다 (%s). 스키마가 오래됐으면 "
                  "`.claude/harness/bin/harness init` 을 다시 실행하라." % exc})
        return 0
    finally:
        con.close()
    return 0


# -------------------------------------------------------------------------- cli

def plugin_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def arg_value(cmd, name):
    """따옴표가 살아 있는 raw 명령 문자열에서 --name 값을 뽑는다 (훅 경로 전용)."""
    m = re.search(r"--%s(?:=|\s+)(\"([^\"]*)\"|'([^']*)'|([^\s]+))" % name, cmd)
    return (m.group(2) or m.group(3) or m.group(4)) if m else None


def argv_value(argv, name):
    """쉘이 이미 분해한 argv 에서 --name 값을 뽑는다 (공백 있는 사유 보존)."""
    flag = "--" + name
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def argv_positional(argv):
    """--flag 의 값을 위치 인자로 오인하지 않게 분리한다."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a.startswith("--"):
            skip = "=" not in a
            continue
        out.append(a)
    return out


ENGINE_REL = os.path.join(HARNESS_DIR, "bin", "harness.py")

# 엔진 사본을 프로젝트 안에서 먼저 찾는다. 프로젝트 밖의 파일을 실행하면
# auto-mode 분류기와 샌드박스가 막는다 — 둘 다 실제로 겪은 문제다.
WRAPPER = """#!/bin/sh
# step-six-harness wrapper — 세션 시작마다 갱신된다. 직접 편집하지 마라.
D="$(cd "$(dirname "$0")" && pwd)"
P="$D/harness.py"
if [ ! -f "$P" ]; then P="%s"; fi
if [ ! -f "$P" ]; then
  P="$(ls -t "$HOME"/.claude/plugins/cache/*/step-six-harness/*/scripts/harness.py 2>/dev/null | head -1)"
fi
[ -f "$P" ] || { echo "step-six-harness: engine not found" >&2; exit 1; }
exec python3 "$P" "$@"
"""


def _write_if_changed(path, body, mode=None):
    try:
        if os.path.isfile(path) and open(path, encoding="utf-8").read() == body:
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        if mode is not None:
            os.chmod(path, mode)
        return True
    except Exception:
        return False


def refresh_engine(root):
    """엔진 사본을 프로젝트 안에 둔다.

    모델이 실행하는 명령이 작업 디렉터리 밖의 파일을 가리키면 분류기·샌드박스가
    막는다. 사본은 gitignore 되고 세션 시작마다 갱신되므로 버전이 어긋나지 않는다.
    """
    src = os.path.abspath(__file__)
    dst = os.path.join(root, ENGINE_REL)
    if src == os.path.abspath(dst):
        return False  # 사본 자신이 실행 중이면 덮어쓰지 않는다
    try:
        with open(src, encoding="utf-8") as fh:
            body = fh.read()
    except Exception:
        return False
    return _write_if_changed(dst, body, 0o644)


def refresh_wrapper(root):
    refresh_engine(root)
    _write_if_changed(os.path.join(root, WRAPPER_REL),
                      WRAPPER % os.path.abspath(__file__), 0o755)


def cli_status(con, cfg, root, lid, sid, argv):
    print("작업 %s · 회차 %d · 단계 %s"
          % (lid, cycle_of(con, lid), label_of(cfg, sid)))
    row = con.execute("SELECT intent FROM loop WHERE id=?", (lid,)).fetchone()
    if row and row["intent"]:
        print("  작업 내용: %s" % row["intent"])
    else:
        print("  작업 내용: (미정) — %s 단계에서 정하고 "
              "`harness loop intent \"...\"` 로 기록하라"
              % stage_obj(cfg, cfg["stages"][0]["id"])["label"])
    acc = acceptance_of(con, lid)
    if acc:
        print("  완료 조건 (%d개):" % len(acc))
        for i, t in enumerate(acc, 1):
            print("    %d. %s" % (i, t))
    else:
        print("  완료 조건: (미정) — `harness loop done-when \"<조건>\" ...` 으로 기록하라")
    print("  요약: %s" % stage_obj(cfg, sid)["summary"])
    print("  쓰기 허용: %s" % (", ".join(stage_obj(cfg, sid).get("write", [])) or "(없음)"))
    print("  .dev/ 산출물 파일명 접두사: %s" % file_prefix(con, lid))
    rows = stage_rows(con, lid)
    print("  단계: " + " → ".join(
        "%s(%s)" % (s["label"], rows[s["id"]]["status"] if s["id"] in rows else "?")
        for s in cfg["stages"]))
    crit = stage_obj(cfg, sid).get("exit_criteria")
    if crit:
        missing = exit_blockers(con, cfg, lid, sid)
        done = [k for k in crit if k not in missing]
        print("  종료 조건: 충족 %s / 미충족 %s"
              % (", ".join(done) or "-", ", ".join(missing) or "-"))
    ev = con.execute("SELECT kind, COUNT(*) c FROM evidence WHERE loop_id=? "
                     "GROUP BY kind", (lid,)).fetchall()
    if ev:
        print("  증거: %s" % ", ".join("%s×%d" % (r["kind"], r["c"]) for r in ev))
    for r in skips_of(con, lid):
        print("  스킵: %s — %s (승인: %s)" % (r["stage"], r["reason"], r["authorized_by"]))
    for g in con.execute("SELECT * FROM wgrant WHERE loop_id=? AND uses_left>0", (lid,)):
        print("  예외: %s (남은 %d회) — %s" % (g["glob"], g["uses_left"], g["reason"]))
    if auto_skip_on(con):
        print("  ⚠ 스킵 자동 승인 ON (%s) — 사유: %s. 끄려면 `harness auto-skip off`"
              % (auto_skip_scope_note(con), get_meta(con, "auto_skip_reason", "-")))
    pend = pending_promotions(con, cfg)
    if pend:
        print("  승격 결정 대기 %d개 (Compounding 의 종료 조건): %s"
              % (len(pend), ", ".join(it["key"] for it in pend)))
    mat = promotion_summary(con, cfg)
    if mat:
        print("  승격됨: %s" % ", ".join("%s %d" % (k, v) for k, v in sorted(mat.items())))
    head = tidy_headline(con, cfg, root)
    if head:
        print("  %s" % head)
    return 0


def _enter(con, cfg, root, lid, dest_idx):
    """dest_idx 단계를 active 로. 범위를 넘으면 루프를 닫고 새 루프를 만든다."""
    if dest_idx >= len(cfg["stages"]):
        close_loop(con, lid)
        return create_loop(con, cfg, root), cfg["stages"][0]["id"], True
    sid = cfg["stages"][dest_idx]["id"]
    con.execute("UPDATE stage SET status='active', entered_at=? "
                "WHERE loop_id=? AND stage=?", (now(), lid, sid))
    return lid, sid, False


def _hint_on_enter(con, cfg, root, lid, sid):
    """단계 진입 시의 안내.

    Context 는 **당겨가는** 단계다. 과거 기록을 밀어넣지 않는다 — 이번 task 와
    무관한 실수까지 컨텍스트를 먹기 때문이다. 조회 방법만 알려주고 무엇이
    관련 있는지는 모델이 판단한다.
    Compounding 은 반대다. 막 끝낸 루프 자신의 기록은 무조건 관련 있으니 밀어준다.
    """
    hint = stage_obj(cfg, sid).get("hint")
    if hint:
        print("\n%s" % hint)

    # Scaffolding 은 '줄이는' 단계다. 권고만으로는 아무도 줄이지 않았으므로 목록을 준다.
    if sid == "scaffolding":
        head = tidy_headline(con, cfg, root)
        if head:
            print("\n%s" % head)

    # 완료 조건은 Verification·Compounding 에서 무조건 관련 있으므로 밀어준다.
    if sid in ("verification", "compounding"):
        acc = acceptance_of(con, lid)
        if acc:
            print("\n이 작업의 완료 조건 (%d개):" % len(acc))
            for i, t in enumerate(acc, 1):
                print("  %d. %s" % (i, t))

    if sid != "compounding":
        return
    sk = skips_of(con, lid)
    if sk:
        print("\n이 루프에서 건너뛴 단계 — 회고에 사유와 함께 기록하라:")
        for r in sk:
            print("  - %s: %s (승인: %s)" % (r["stage"], r["reason"], r["authorized_by"]))
    rows = con.execute(
        "SELECT kind, rule, target, COUNT(*) c FROM event "
        "WHERE loop_id=? AND kind IN ('block','tool_fail','bypass') "
        "GROUP BY kind, rule, target HAVING c > 0 ORDER BY c DESC LIMIT 8",
        (lid,)).fetchall()
    if rows:
        print("\n이 루프에서 관측된 것 — 회고 대상:")
        for r in rows:
            print("  - %s/%s %s ×%d" % (r["kind"], r["rule"] or "-", r["target"], r["c"]))
    churn = con.execute(
        "SELECT target, COUNT(*) c FROM event WHERE loop_id=? AND kind='edit' "
        "GROUP BY target HAVING c >= 4 ORDER BY c DESC LIMIT 5", (lid,)).fetchall()
    if churn:
        print("\n재편집이 많은 파일 — 구조 문제일 수 있다:")
        for r in churn:
            print("  - %s ×%d" % (r["target"], r["c"]))

    # 여러 작업에서 반복된 것은 이 단계의 종료 조건이다. 산문으로 적고 끝내면
    # 다음 작업에서 같은 실수가 또 나오고, 그건 복리가 아니다.
    pend = pending_promotions(con, cfg)
    if pend:
        print("\n여러 작업에서 반복된 항목 — 이 단계를 끝내려면 결정해야 한다 "
              "(종료 조건 promotion_decided):")
        for it in pend:
            mark = "  ← %s 로 승격했는데 다시 걸렸다" % it["regressed"] \
                if it.get("regressed") else ""
            print("  - %s ×%d (작업 %d개)%s"
                  % (it["key"], it["count"], it["loops"], mark))
        print("  `harness promote` 로 목록과 결정 방법을 본다. "
              "승격하지 않기로 하는 것도 결정이다.")


def next_cycle(con, cfg, root, lid):
    """같은 작업의 다음 회차. Selection 은 유지하고 나머지 단계를 초기화한다.

    증거를 초기화하지 않으면 2회차 Planning 이 1회차 계획서로 통과한다.
    intent_set 만 남긴다 — 작업은 그대로이므로 다시 선정할 필요가 없다.
    이전 회차의 계획·회고 파일은 파일로 남고, 파일명의 회차로 구분된다.
    """
    ids = stage_ids(cfg)
    # 작업 정의(무엇을·무엇이 끝인지)는 회차를 넘어 유지한다. 회차마다 다시 선언하게
    # 하면 긴 작업에서 기준이 표류한다 — 그게 완료 조건을 두는 이유와 정면으로 어긋난다.
    con.execute("DELETE FROM evidence WHERE loop_id=? "
                "AND kind NOT IN ('intent_set','acceptance')", (lid,))
    con.execute("DELETE FROM wgrant WHERE loop_id=?", (lid,))
    con.execute("UPDATE stage SET status='pending', entered_at=NULL, left_at=NULL, "
                "reason=NULL, authorized_by=NULL WHERE loop_id=? AND stage != ?",
                (lid, ids[0]))
    con.execute("UPDATE loop SET cycle=cycle+1 WHERE id=?", (lid,))
    con.execute("UPDATE stage SET status='active', entered_at=? "
                "WHERE loop_id=? AND stage=?", (now(), lid, ids[1]))
    return ids[1]


def cli_advance(con, cfg, root, lid, sid, argv):
    last = cfg["stages"][-1]["id"]
    want_done = "--done" in argv
    want_cycle = "--cycle" in argv

    if sid != last and (want_done or want_cycle):
        print("--done / --cycle 은 마지막 단계(%s)에서만 쓴다."
              % stage_obj(cfg, last)["label"])
        return 2
    if sid == last:
        if want_done and want_cycle:
            print("--done 과 --cycle 은 함께 쓸 수 없다.")
            return 2
        if not (want_done or want_cycle):
            print("%s 단계에서는 두 갈래 중 하나를 골라야 한다. 스스로 판단하라:"
                  % stage_obj(cfg, last)["label"])
            print("  harness advance --done    작업이 끝났다 → %s (새 작업 선정)"
                  % stage_obj(cfg, cfg["stages"][0]["id"])["label"])
            print("  harness advance --cycle   후속 회차가 남았다 → %s (같은 작업 유지)"
                  % stage_obj(cfg, cfg["stages"][1]["id"])["label"])
            return 1

    missing = exit_blockers(con, cfg, lid, sid)
    if missing:
        print("advance 거부 — %s 단계의 종료 조건이 남았다:" % stage_obj(cfg, sid)["label"])
        for k in missing:
            print("  - %s: %s" % (k, CRITERIA_HELP.get(k, k)))
        if stage_obj(cfg, sid).get("skippable") is False:
            print("이 단계는 건너뛸 수 없다. 조건을 채워야 한다.")
        else:
            print("정당한 사유가 있으면 `harness skip %s --reason \"...\"` 로 "
                  "사람의 승인을 받아라." % sid)
        return 1

    snap = None
    with con:
        con.execute("UPDATE stage SET status='done', left_at=? "
                    "WHERE loop_id=? AND stage=?", (now(), lid, sid))
        # 회차 경계에서만 스냅샷을 남긴다. close_loop 이 stage 를 지우기 전에.
        if sid == last:
            snap = record_cycle_close(con, cfg, lid, sid)
        if sid == last and want_done:
            close_loop(con, lid)
            nlid = create_loop(con, cfg, root)
            nsid = cfg["stages"][0]["id"]
            done_task = True
        elif sid == last:
            nlid, nsid, done_task = lid, next_cycle(con, cfg, root, lid), False
        else:
            nlid, nsid, _ = _enter(con, cfg, root, lid, stage_index(cfg, sid) + 1)
            done_task = False

    if snap:
        print("회차 %d 기록: 차단 %d · 실패 %d(반복 %d) · 재편집 최대 %d · "
              "우회 %d · 스킵 %d"
              % (snap["cycle"], snap["blocks"], snap["fails"], snap["refails"],
                 snap["churn"], snap["bypass"], snap["skips"]))
    if done_task:
        print("작업 %s 종료 — 기록은 각 폴더의 파일에 남아 있다." % lid)
        print("새 작업 %s → 단계 %s" % (nlid, label_of(cfg, nsid)))
    else:
        if sid == last:
            print("작업 %s 회차 %d 시작 (같은 작업 유지)"
                  % (nlid, cycle_of(con, nlid)))
            print("   파일명 접두사: %s" % file_prefix(con, nlid))
        print("→ 단계 %s" % label_of(cfg, nsid))
        print("   %s" % stage_obj(cfg, nsid)["summary"])
    _hint_on_enter(con, cfg, root, nlid, nsid)
    return 0


def cli_skip(con, cfg, root, lid, sid, argv):
    """PreToolUse 가 사람의 승인을 받은 뒤에만 여기까지 온다."""
    pos = argv_positional(argv)
    target = pos[0] if pos else None
    reason = argv_value(argv, "reason")
    if not target or not reason:
        print("사용법: harness skip <stage-id|+N|until:<stage-id>> --reason \"...\"")
        return 2
    ids = stage_ids(cfg)
    cur = stage_index(cfg, sid)
    if target.startswith("+"):
        try:
            dest = min(cur + int(target[1:]) - 1, len(ids) - 1)
        except ValueError:
            print("잘못된 형식: %s" % target)
            return 2
    elif target.startswith("until:"):
        want = target.split(":", 1)[1]
        if want not in ids:
            print("알 수 없는 단계: %s" % want)
            return 2
        dest = ids.index(want) - 1
    elif target in ids:
        dest = ids.index(target)
    else:
        print("알 수 없는 대상: %s" % target)
        return 2
    if dest < cur:
        print("뒤로 갈 수는 없다. 현재 단계 %s 유지." % label_of(cfg, sid))
        return 1

    # 건너뛸 수 없는 단계 — 회차를 중단하더라도 회고는 남겨야 복리가 끊기지 않는다.
    # 이 검사가 먼저다. 아래 기록 검사보다 뒤에 두면 엉뚱한 이유를 내놓는다.
    locked = [ids[i] for i in range(cur, dest + 1)
              if cfg["stages"][i].get("skippable") is False]
    if locked:
        print("%s 단계는 건너뛸 수 없다." % ", ".join(stage_obj(cfg, s)["label"] for s in locked))
        print("이 회차를 중단하려면 `harness skip until:%s --reason \"...\"` 로 그 단계까지 "
              "이동한 뒤, 중단 사유를 회고로 남기고 `harness advance` 로 루프를 닫아라."
              % locked[0])
        return 1

    # 스킵은 **승인**을 면제하지만 **기록**을 면제하지 않는다.
    # 무인 실행으로 Planning 을 건너뛰어도 계획 파일은 남아야 한다 — 회차 10번을 돌았을 때
    # 계획 10개가 남는 것과 스킵 기록 10개가 남는 것은 복리의 재료로서 다르다.
    for i in range(cur, dest + 1):
        st = cfg["stages"][i]
        for key in st.get("skip_requires", []):
            if not has_evidence(con, lid, key):
                print("%s 를 건너뛰더라도 기록은 남겨야 한다: %s"
                      % (st["label"], CRITERIA_HELP.get(key, key)))
                print("먼저 그 기록을 남긴 뒤 다시 시도하라. 승인만 면제된다.")
                return 1

    # 자동 승인으로 통과한 스킵은 사람이 승인한 것과 구분해 기록한다
    by = "auto" if auto_skip_on(con) else "user"
    left = None
    skipped = []
    with con:
        # 현재 단계: 종료 조건을 충족했다면 done, 아니면 skipped 로 정직하게 기록한다
        if dest == cur or exit_blockers(con, cfg, lid, sid):
            con.execute("UPDATE stage SET status='skipped', left_at=?, reason=?, "
                        "authorized_by=? WHERE loop_id=? AND stage=?",
                        (now(), reason, by, lid, sid))
            skipped.append(sid)
        else:
            con.execute("UPDATE stage SET status='done', left_at=? "
                        "WHERE loop_id=? AND stage=?", (now(), lid, sid))
        for i in range(cur + 1, dest + 1):
            con.execute("UPDATE stage SET status='skipped', left_at=?, reason=?, "
                        "authorized_by=? WHERE loop_id=? AND stage=?",
                        (now(), reason, by, lid, ids[i]))
            skipped.append(ids[i])
        for s in skipped:
            record_event(con, lid, s, "skip", s, by, reason)
        if by == "auto":
            left = consume_auto_skip(con)
        nlid, nsid, cycled = _enter(con, cfg, root, lid, dest + 1)
    print("스킵(%s): %s" % ("자동 승인" if by == "auto" else "사용자 승인",
                            ", ".join(skipped) or "(없음)"))
    print("사유: %s" % reason)
    if by == "auto" and left is not None:
        print("자동 승인 남은 횟수: %d%s" % (left, " — 소진되어 OFF 로 돌아갔다" if left == 0 else ""))
    if cycled:
        print("작업 %s 종료 → 새 작업 %s, 단계 %s" % (lid, nlid, label_of(cfg, nsid)))
    else:
        print("→ 단계 %s" % label_of(cfg, nsid))
        _hint_on_enter(con, cfg, root, nlid, nsid)
    return 0


def cli_allow(con, cfg, root, lid, sid, argv):
    pos = argv_positional(argv)
    glob = pos[0] if pos else None
    reason = argv_value(argv, "reason")
    uses = argv_value(argv, "uses")
    if not glob or not reason:
        print("사용법: harness allow <glob> --reason \"...\" [--uses N]")
        return 2
    with con:
        con.execute("INSERT INTO wgrant(loop_id,glob,reason,uses_left,at) "
                    "VALUES(?,?,?,?,?)",
                    (lid, glob, reason, int(uses) if uses else 3, now()))
    print("예외 등록(사용자 승인): %s — %s" % (glob, reason))
    return 0


def cli_approve_plan(con, cfg, root, lid, sid, argv):
    pos = argv_positional(argv)
    path = pos[0] if pos else None
    if not path:
        print("사용법: harness approve-plan <plan-file>")
        return 2
    rel = rel_to_root(root, path)
    if not rel or not os.path.isfile(os.path.join(root, rel)):
        print("계획 파일을 찾을 수 없다: %s" % path)
        return 2
    with con:
        record_evidence(con, lid, sid, "plan_file", rel)
        record_evidence(con, lid, sid, "plan_approved", rel)
    print("계획 승인 기록: %s" % rel)
    return 0


RECALL_DIRS = ("retrospect", "learning", "troubleshooting")

# 작업 설명에서 키워드를 뽑을 때 걸러낼 저정보 단어. 이것들이 남으면 OR 조회가
# 거의 모든 기록에 걸려서 조회가 무의미해진다.
STOPWORDS = {
    "수정", "추가", "구현", "작업", "개선", "변경", "정리", "삭제", "제거", "적용",
    "처리", "로직", "기능", "부분", "관련", "리팩터링", "리팩토링", "버그", "이슈",
    "fix", "add", "update", "refactor", "change", "remove", "delete", "impl",
    "implement", "cleanup", "the", "and", "for", "with", "into",
    # 경로 상용 디렉터리명 — 이것만으로는 무관한 기록까지 걸린다
    "src", "lib", "app", "apps", "dist", "build", "packages", "pkg", "internal",
}


def _expand_keywords(keywords):
    """경로 키워드를 조각으로 넓힌다 — src/api.ts → {src/api.ts, src, api, ts}.

    이벤트 조회는 세는 것이라 정확해야 하지만, 파일은 읽을 후보를 고르는 것이라
    넓어야 한다. 'src/api' 로 조회했을 때 api 를 다룬 회고 산문이 걸려야 한다.
    """
    out = set()
    for kw in keywords:
        out.add(kw.lower())
        # 공백도 쪼갠다 — 실패 지점 주입은 'npm test' 같은 정규화된 명령을 키워드로
        # 넘기는데, 회고 파일이 그 두 낱말을 붙여 쓴 경우는 드물다.
        for part in re.split(r"[/\\.\-_\s]", kw):
            if len(part) >= 3:
                out.add(part.lower())
    return out


INDEX_NAMES = ("INDEX.md", "README.md")


def _recall_files(root, keywords, limit=6):
    """회고·학습·트러블슈팅 파일 중 키워드에 걸리는 것. 내용은 읽지 않고 경로만 준다.

    인덱스 파일은 키워드와 무관하게 항상 앞에 놓는다. 파일이 수백 개로 쌓이면
    개별 파일 6개를 보여주는 것보다 전체를 요약한 인덱스 하나가 낫다.
    """
    keywords = _expand_keywords(keywords) if keywords else set()
    indexes, hits = [], []
    for sub in RECALL_DIRS:
        d = os.path.join(root, ".dev", sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d), reverse=True):
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue
            rel = ".dev/%s/%s" % (sub, name)
            if name in INDEX_NAMES:
                indexes.append(rel)
                continue
            if not keywords:
                hits.append(rel)
                continue
            hay = name.lower()
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    hay += "\n" + fh.read(20000).lower()
            except Exception:
                pass
            if any(kw.lower() in hay for kw in keywords):
                hits.append(rel)
    return indexes, hits[:max(0, limit - len(indexes))]


def tidy_cfg(cfg):
    return cfg.get("tidy") or {}


def tidy_report(con, cfg, root):
    """정리 후보. 판정은 전부 파일시스템 사실이고 LLM 이 끼지 않는다.

    "정리하라"는 권고는 아무 일도 만들지 않았다. 무엇을 정리할지의 목록이라야
    행동이 된다. 삭제·병합 자체는 여전히 자율이다 — 후보만 제시한다.
    """
    tc = tidy_cfg(cfg)
    try:
        thr = max(1, int(tc.get("dir_file_threshold", 12)))
        age_days = max(1, int(tc.get("age_days", 30)))
        group_min = max(2, int(tc.get("merge_group", 3)))
    except (TypeError, ValueError):
        thr, age_days, group_min = 12, 30, 3
    cutoff = time.time() - age_days * 86400
    open_loops = {r["id"] for r in
                  con.execute("SELECT id FROM loop WHERE closed_at IS NULL")}

    out = {"dirs": [], "stale": [], "groups": [], "learned": None, "regressed": []}
    for sub in RECALL_DIRS:
        d = os.path.join(root, ".dev", sub)
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        files = [n for n in names
                 if os.path.isfile(os.path.join(d, n)) and n not in INDEX_NAMES]
        if not files:
            continue
        idx = os.path.join(d, "INDEX.md")
        has_idx = os.path.isfile(idx)

        def mtime(path):
            """목록을 읽은 뒤 파일이 사라질 수 있다 (병렬 작업, 끊어진 symlink)."""
            try:
                return os.path.getmtime(path)
            except OSError:
                return 0.0

        newest = max([mtime(os.path.join(d, n)) for n in files] or [0.0])
        note = None
        if len(files) >= thr and not has_idx:
            note = "파일 %d개인데 INDEX.md 가 없다" % len(files)
        elif has_idx and mtime(idx) < newest:
            note = "INDEX.md 가 최신 파일보다 낡았다 (파일 %d개)" % len(files)
        if note:
            out["dirs"].append((".dev/%s/" % sub, note))

        groups = {}
        for n in files:
            path = os.path.join(d, n)
            m = re.match(r"^(\d{6}-[0-9a-f]{6})-", n)
            lid_of = m.group(1) if m else None
            if lid_of:
                groups.setdefault(lid_of, []).append(".dev/%s/%s" % (sub, n))
            try:
                mt = os.path.getmtime(path)
            except OSError:
                continue
            # 열려 있는 작업의 파일은 후보가 아니다 — 아직 쓰이는 중이다.
            if mt < cutoff and lid_of not in open_loops:
                out["stale"].append((".dev/%s/%s" % (sub, n),
                                     int((time.time() - mt) // 86400)))
        for k, v in groups.items():
            if len(v) >= group_min and k not in open_loops:
                out["groups"].append((k, sorted(v)))

    budget = learned_budget(cfg)
    used = len(learned_lines(con, cfg))
    if used:
        out["learned"] = (used, budget)
    out["regressed"] = con.execute(
        "SELECT key, decision, note FROM promotion WHERE maturity='regressed'").fetchall()
    out["stale"].sort(key=lambda t: -t[1])
    return out


def tidy_headline(con, cfg, root):
    """Scaffolding 에서 한 줄로 보여줄 요약. 할 일이 없으면 None."""
    try:
        rep = tidy_report(con, cfg, root)
    except Exception:
        return None
    bits = []
    if rep["dirs"]:
        bits.append("인덱스 %d곳" % len(rep["dirs"]))
    if rep["stale"]:
        bits.append("오래된 파일 %d개" % len(rep["stale"]))
    if rep["groups"]:
        bits.append("병합 후보 %d묶음" % len(rep["groups"]))
    if rep["regressed"]:
        bits.append("재발한 승격 %d개" % len(rep["regressed"]))
    if rep["learned"] and rep["learned"][0] >= rep["learned"][1]:
        bits.append("LEARNED.md 예산 소진 %d/%d" % rep["learned"])
    if not bits:
        return None
    return "정리 후보: %s — `harness tidy` 로 목록을 본다" % ", ".join(bits)


def cli_tidy(con, cfg, root, lid, sid, argv):
    """줄이는 것도 일이다. 쌓이면 복잡해지는 시스템은 복리가 아니다."""
    with con:
        sync_promotions(con, cfg)
    refresh_learned(con, cfg, root)
    rep = tidy_report(con, cfg, root)
    limit = 12

    print("정리 후보 (Scaffolding 단계의 일이다. 삭제·병합 여부는 자율)")
    if rep["dirs"]:
        print("\n인덱스가 필요하거나 낡은 폴더")
        for d, note in rep["dirs"]:
            print("  %-26s %s" % (d, note))
    if rep["groups"]:
        print("\n한 작업이 여러 파일을 남겼다 — 하나로 병합할 후보")
        for k, files in rep["groups"][:limit]:
            print("  작업 %s — %d개" % (k, len(files)))
            for f in files[:4]:
                print("      %s" % f)
            if len(files) > 4:
                print("      ... +%d개" % (len(files) - 4))
    if rep["stale"]:
        print("\n닫힌 작업의 오래된 파일 — 인덱스에 요약하고 지울 후보")
        for f, days in rep["stale"][:limit]:
            print("  %-52s %d일" % (f[:52], days))
        if len(rep["stale"]) > limit:
            print("  ... +%d개" % (len(rep["stale"]) - limit))
    if rep["regressed"]:
        print("\n승격했는데 다시 걸린 항목 — 승격이 통하지 않았다")
        for r in rep["regressed"]:
            print("  %-30s %s: %s" % (r["key"][:30], r["decision"], r["note"] or "-"))
        print("  Compounding 에서 다시 결정하게 된다 (`harness promote`).")
    if rep["learned"]:
        used, budget = rep["learned"]
        print("\nLEARNED.md: %d/%d줄%s"
              % (used, budget, " — 예산 소진. 새 규칙을 올리려면 먼저 비워라"
                 if used >= budget else ""))
        print("  내리기: `harness promote <key> --decline --reason \"...\"`")
    if not any((rep["dirs"], rep["groups"], rep["stale"], rep["regressed"],
                rep["learned"])):
        print("  (없음 — 정리할 것이 없다)")
    return 0


def _pct(part, whole):
    return "  -  " if not whole else "%4.0f%%" % (100.0 * part / whole)


def _survival(con, cfg):
    """승격 종류별 재발률. 각 승격이 그 자체로 하나의 실험이다.

    "하네스 있기 전/후"는 비교할 수 없지만 "이 규칙을 승격한 전/후"는 비교할 수
    있다. 이것이 이 하네스에서 유일하게 엄밀한 측정이다.
    """
    verified = {}
    for r in con.execute("SELECT target, detail FROM event "
                         "WHERE kind='promote_verify' ORDER BY id"):
        verified[r["target"]] = (r["detail"] or "").endswith("yes")
    agg = {}
    for p in con.execute("SELECT * FROM promotion"):
        d = agg.setdefault(p["decision"], {"n": 0, "re": 0, "vn": 0, "vy": 0})
        d["n"] += 1
        if is_regressed(con, cfg, p):
            d["re"] += 1
        if p["key"] in verified:
            d["vn"] += 1
            d["vy"] += 1 if verified[p["key"]] else 0
    return agg


def _cycle_rows(con):
    out = []
    for r in con.execute("SELECT at, detail FROM event WHERE kind='cycle_close' "
                         "ORDER BY id"):
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            continue
        if isinstance(d, dict):
            d["at"] = r["at"]
            out.append(d)
    return out


def _bucket(rows, n=3):
    """회차를 n등분한다. 개별 비교는 작업 난이도에 교란되므로 구간으로만 읽는다."""
    if len(rows) < n * 2:
        return [(1, len(rows), rows)] if rows else []
    size = len(rows) // n
    out = []
    for i in range(n):
        lo = i * size
        hi = len(rows) if i == n - 1 else (i + 1) * size
        out.append((lo + 1, hi, rows[lo:hi]))
    return out


def cli_metrics(con, cfg, root, lid, sid, argv):
    """복리 측정. 점수를 만들지 않는다 — 합치면 그 하나를 최적화하게 된다."""
    with con:
        sync_promotions(con, cfg)
    lc = con.execute("SELECT COUNT(*) c, MIN(created_at) a, MAX(created_at) b "
                     "FROM loop").fetchone()
    cyc = _cycle_rows(con)
    print("복리 측정 — 작업 %d개, 기록된 회차 %d개%s"
          % (lc["c"], len(cyc),
             " (%s ~ %s)" % ((lc["a"] or "")[:10], (lc["b"] or "")[:10])
             if lc["a"] else ""))

    print("\n① 승격 생존율 — 무엇이 실제로 막았나")
    agg = _survival(con, cfg)
    if not agg:
        print("  (아직 승격이 없다. 여러 작업에서 반복된 항목이 생기면 쌓인다)")
    else:
        for k in sorted(agg, key=lambda x: -agg[x]["n"]):
            d = agg[k]
            vs = ("변경관측 %d/%d" % (d["vy"], d["vn"])) if d["vn"] else ""
            print("  %-10s %2d건 중 %2d건 재발 (%s)  %s"
                  % (k, d["n"], d["re"], _pct(d["re"], d["n"]).strip(), vs))
        small = sum(d["n"] for d in agg.values())
        if small < 20:
            print("  ⚠ 표본 %d건. 비율을 믿지 마라 — 20~30건은 있어야 한다." % small)
        print("  재발 = 승격 이후 같은 항목이 다시 걸린 것. 변경관측 = 그 회차에 "
              "주장에 맞는 파일 변경이 있었나.")

    print("\n② 회차 추세 — 마찰과 회피를 나란히 본다")
    if not cyc:
        print("  (회차 종료 기록이 없다. `advance --cycle` 또는 `--done` 때 쌓인다)")
    else:
        keys = [("blocks", "차단"), ("refails", "반복실패"), ("churn", "재편집"),
                ("bypass", "우회"), ("skips", "스킵"), ("declines", "보류")]
        print("  %-12s %s" % ("회차구간", " ".join("%8s" % t for _, t in keys)))
        buckets = _bucket(cyc)
        avgs = []
        for lo, hi, rows in buckets:
            a = {k: sum(r.get(k, 0) for r in rows) / float(len(rows)) for k, _ in keys}
            avgs.append(a)
            print("  %-12s %s" % ("%d-%d회" % (lo, hi),
                                  " ".join("%8.1f" % a[k] for k, _ in keys)))
        if len(avgs) >= 2:
            first, last = avgs[0], avgs[-1]
            friction = last["blocks"] + last["refails"] < first["blocks"] + first["refails"]
            evasion = (last["bypass"] + last["skips"] + last["declines"]
                       > first["bypass"] + first["skips"] + first["declines"])
            # Goodhart 가드. 차단은 아무것도 시도하지 않거나 우회해도 줄어든다.
            if friction and evasion:
                print("  ⚠ 마찰이 줄었지만 우회·스킵·보류가 늘었다 — 개선이 아니라 "
                      "회피일 수 있다. 게이트가 연극이 되고 있는지 보라.")
            elif friction:
                print("  ✓ 마찰이 줄고 우회는 늘지 않았다 — 개선 신호다 "
                      "(작업 난이도 차이는 통제하지 못한다).")
            elif evasion:
                print("  ⚠ 우회가 늘었는데 마찰은 줄지 않았다 — 규칙이 맞지 않는지 보라.")

    print("\n③ 반복 실패 비율 — 실패 주입이 겨냥한 것")
    # 누적 비율(전체 실패 중 첫 실패가 아닌 것)은 쓰지 않는다. 명령 종류는 적고
    # 실행은 많으니 시간이 지나면 무조건 100% 에 수렴한다 — 창이 없는 비율은
    # 아무것도 말해주지 않는다. 회차 구간별로만 읽는다.
    if not cyc:
        print("  (회차 종료 기록이 없다. `advance --cycle` 또는 `--done` 때 쌓인다)")
    else:
        for lo, hi, rows in _bucket(cyc):
            tf = sum(r.get("fails", 0) for r in rows)
            tr = sum(r.get("refails", 0) for r in rows)
            print("  %-12s 실패 %3d건 중 이전에도 실패한 것 %3d건 (%s)"
                  % ("%d-%d회" % (lo, hi), tf, tr, _pct(tr, tf).strip()))
        print("  '이전에도 실패한 것' = 그 회차 시작 전에 이미 같은 명령이 실패한 적 있음.")

    print("\n측정하지 못하는 것: 결과물의 품질, 그 회차가 필요했는지, 사람이 아낀 시간.")
    print("점수를 만들지 않는 이유: 하나로 합치면 그 하나를 최적화하게 된다.")
    return 0


def cli_promote(con, cfg, root, lid, sid, argv):
    """반복된 항목을 승격하거나, 승격하지 않기로 결정한다.

    승인 다이얼로그를 띄우지 않는다 — 이건 게이트 우회가 아니라 기록이기 때문이다.
    대신 결정은 전부 event 에 남아 `stats` 에 드러나고, 승격 후에도 같은 항목이
    다시 걸리면 결정이 무효화되어 다시 올라온다. 무성의한 보류는 되돌아온다.
    """
    with con:
        changed = sync_promotions(con, cfg)
    # 목록만 보는 경로에서도 갱신한다. 성숙도를 바꿨는데 파일을 그대로 두면
    # 재발한 규칙이 항상 로드되는 문서에 남는다.
    refresh_learned(con, cfg, root)
    if changed:
        # 20줄을 쏟으면 정작 결정해야 할 목록이 스크롤 밖으로 밀린다.
        if len(changed) <= 3:
            for key, mat in changed:
                print("성숙도 갱신: %s → %s" % (key, mat))
        else:
            agg = {}
            for _, mat in changed:
                agg[mat] = agg.get(mat, 0) + 1
            print("성숙도 갱신 %d건: %s"
                  % (len(changed), ", ".join("%s %d" % kv for kv in sorted(agg.items()))))
    pos = argv_positional(argv)
    as_kind = argv_value(argv, "as")
    decline = "--decline" in argv
    note = argv_value(argv, "note") or argv_value(argv, "reason")

    if not pos:
        pend = pending_promotions(con, cfg)
        print("승격 결정이 필요한 항목 (%d개)" % len(pend))
        if not pend:
            print("  (없음 — 여러 작업에서 반복된 항목이 아직 없다)")
        for it in pend:
            mark = ""
            if it.get("regressed"):
                mark = "  ← %s 로 승격했는데 다시 걸렸다" % it["regressed"]
            print("  %-34s ×%d, 작업 %d개%s"
                  % (it["key"][:34], it["count"], it["loops"], mark))
        if pend:
            print("\n결정 방법 (하나 고른다):")
            for k, desc in PROMOTE_AS.items():
                flag = "--decline --reason \"...\"" if k == "declined" \
                    else "--as %s --note \"...\"" % k
                print("  harness promote <key> %-32s %s" % (flag, desc))
        done = con.execute("SELECT key, decision, maturity, note FROM promotion "
                           "ORDER BY at DESC LIMIT 8").fetchall()
        if done:
            print("\n이미 결정된 항목")
            for r in done:
                print("  %-30s %-10s %-12s %s"
                      % (r["key"][:30], r["decision"], r["maturity"],
                         (r["note"] or "-")[:40]))
        return 0

    key = pos[0]
    known = {it["key"] for it in repeated_items(con, cfg)}
    if key not in known:
        print("반복 항목이 아니다: %s" % key)
        print("`harness promote` 로 목록을 확인하라 (키는 'block:<규칙>' 또는 "
              "'tool_fail:<명령>' 형식이다).")
        return 2
    if decline:
        as_kind = "declined"
    if not as_kind:
        print("무엇으로 승격할지 골라야 한다: --as %s, 또는 --decline."
              % "|".join(k for k in PROMOTE_AS if k != "declined"))
        return 2
    if as_kind not in PROMOTE_AS:
        print("알 수 없는 승격 종류: %s (가능: %s)"
              % (as_kind, ", ".join(PROMOTE_AS)))
        return 2
    if not note:
        print("사유/내용이 필요하다: %s"
              % ("--reason \"왜 승격하지 않는가\"" if as_kind == "declined"
                 else "--note \"무엇을 어떻게 바꿨는가\""))
        return 2

    if as_kind == "rule":
        used = len(learned_lines(con, cfg))
        existing = con.execute("SELECT decision FROM promotion WHERE key=?",
                               (key,)).fetchone()
        grows = not (existing and existing["decision"] == "rule")
        if grows and used >= learned_budget(cfg):
            print("LEARNED.md 예산이 찼다 (%d/%d줄). 항상 로드되는 문서라 상한이 있다."
                  % (used, learned_budget(cfg)))
            print("먼저 한 줄을 비워라: `harness promote <기존키> --decline "
                  "--reason \"...\"` (`harness tidy` 로 목록 확인)")
            return 1

    kind = key.split(":", 1)[0]
    # 보류의 성숙도는 'declined' 다. established 로 두면 "확립된 규칙"과 구분되지 않는다.
    maturity = "declined" if as_kind == "declined" else "established"
    seen = promote_change_seen(con, cfg, lid, as_kind)
    with con:
        con.execute(
            "INSERT INTO promotion(key,kind,decision,maturity,note,loop_id,at,recheck_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "decision=excluded.decision, maturity=excluded.maturity, "
            "note=excluded.note, loop_id=excluded.loop_id, at=excluded.at, "
            "recheck_at=excluded.recheck_at",
            (key, kind, as_kind, maturity, note, lid, now(), now()))
        record_event(con, lid, sid,
                     "promote_declined" if as_kind == "declined" else "promote",
                     as_kind, key, note)
        if seen is not None:
            # 주장과 사실을 따로 남긴다. metrics 가 나란히 보여준다.
            record_event(con, lid, sid, "promote_verify", as_kind, key,
                         "change_seen=%s" % ("yes" if seen else "no"))
    wrote = refresh_learned(con, cfg, root)

    print("%s: %s → %s" % ("보류 기록" if as_kind == "declined" else "승격 기록",
                           key, PROMOTE_AS[as_kind]))
    print("  %s" % note)
    if wrote:
        print("  %s 갱신 (%d/%d줄)"
              % (LEARNED_REL.replace(os.sep, "/"), len(learned_lines(con, cfg)),
                 learned_budget(cfg)))
    if seen is False:
        print("  ⚠ 이 회차에 그에 맞는 파일 변경이 관측되지 않았다 (%s). 막지는 않지만 "
              "기록된다 — `harness metrics` 가 주장과 사실을 나란히 보여준다."
              % ", ".join(verify_globs(cfg, as_kind) or []))
    elif seen:
        print("  설정/구조 변경이 이 회차에 관측됐다 — 주장이 사실로 뒷받침된다.")
    if as_kind == "declined":
        print("  보류도 결정이다. 이 항목이 앞으로 %s개 작업에서 다시 걸리면 "
              "결정이 무효화되어 다시 올라온다."
              % promo_cfg(cfg).get("reopen_after_loops", 2))
    else:
        print("  성숙도 established. 재발 없이 작업 %s개가 지나면 proven 이 된다."
              % promo_cfg(cfg).get("proven_after_loops", 3))
    left = pending_promotions(con, cfg)
    print("남은 결정: %d개" % len(left))
    return 0


def cli_recall(con, cfg, root, lid, sid, argv):
    """과거 관측 기록과 회고 파일을 조회한다 (pull). 무엇이 관련 있는지는 호출자가 판단한다."""
    keywords = argv_positional(argv)
    kind = argv_value(argv, "kind")
    rule = argv_value(argv, "rule")
    from_intent = False
    if not keywords:
        # Scaffolding 에서 정한 작업을 기본 키워드로 쓴다. 6→2 링크의 연결점.
        row = con.execute("SELECT intent FROM loop WHERE id=?", (lid,)).fetchone()
        intent = (row["intent"] if row else None) or ""
        picked = [t for t in re.split(r"[\s,·/]+", intent)
                  if len(t) >= 2 and t.lower() not in STOPWORDS][:6]
        if picked:
            keywords = picked
            from_intent = True
    try:
        limit = int(argv_value(argv, "limit") or 12)
    except ValueError:
        limit = 12

    # 괄호 필수 — "A OR B AND C" 는 "A OR (B AND C)" 로 파싱되어
    # 첫 조건만 만족하면 키워드 필터가 통째로 무시된다.
    where, params = ["(kind != 'edit' OR ? = 1)"], [1 if kind == "edit" else 0]
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if rule:
        where.append("rule = ?")
        params.append(rule)
    # 키워드는 OR 로 묶는다. AND 로 묶으면 "src/auth.ts 토큰 갱신 수정" 같은 작업
    # 설명에서 뽑은 키워드를 전부 만족하는 이벤트가 없어 항상 빈 결과가 나온다.
    kw_or = []
    for kw in keywords:
        like = "%" + kw.lower() + "%"
        kw_or.append("(LOWER(target) LIKE ? OR LOWER(IFNULL(rule,'')) LIKE ? "
                     "OR LOWER(IFNULL(detail,'')) LIKE ?)")
        params += [like, like, like]
    if kw_or:
        where.append("(" + " OR ".join(kw_or) + ")")
    rows = con.execute(
        "SELECT kind, rule, target, COUNT(*) c, MAX(at) last, "
        "COUNT(DISTINCT loop_id) loops FROM event WHERE %s "
        "GROUP BY kind, rule, target ORDER BY loops DESC, c DESC LIMIT ?"
        % " AND ".join(where), params + [limit]).fetchall()

    if from_intent:
        head = "이 루프의 작업에서 추출: %s" % " ".join(keywords)
    elif keywords:
        head = "키워드: %s" % " ".join(keywords)
    else:
        head = "전체 — 작업이 정해졌으면 `harness loop intent \"...\"` 로 기록하라"
    print("과거 관측 기록 (%s)" % head)
    if not rows:
        print("  (없음)")
    for r in rows:
        mark = "  ← 여러 작업에서 반복" if r["loops"] > 1 else ""
        print("  %-10s %-16s %-34s ×%d (작업 %d)%s"
              % (r["kind"], r["rule"] or "-", (r["target"] or "")[:34],
                 r["c"], r["loops"], mark))

    churn = con.execute(
        "SELECT target, COUNT(*) c, COUNT(DISTINCT loop_id) loops FROM event "
        "WHERE kind='edit' GROUP BY target HAVING c >= 5 "
        "ORDER BY c DESC LIMIT 5").fetchall()
    if churn and not kind:
        matched = [r for r in churn
                   if not keywords or any(k.lower() in (r["target"] or "").lower()
                                          for k in keywords)]
        if matched:
            print("\n재편집이 많은 파일")
            for r in matched:
                print("  %-40s ×%d (작업 %d)" % (r["target"][:40], r["c"], r["loops"]))

    indexes, files = _recall_files(root, keywords)
    if indexes:
        print("\n인덱스 — 쌓인 기록의 진입점 (먼저 읽어라)")
        for f in indexes:
            print("  %s" % f)
    print("\n관련 회고·학습 파일 (필요하면 읽어라)")
    if not files:
        print("  (없음)")
    for f in files:
        print("  %s" % f)
    return 0


def cli_stats(con, cfg, root, lid, sid, argv):
    """누적 수치. --loop 를 주면 현재 작업만."""
    only = "--loop" in argv
    cond, params = ("WHERE loop_id = ?", [lid]) if only else ("", [])
    print("범위: %s" % ("현재 작업 %s" % lid if only else "전체 누적"))

    lc = con.execute("SELECT COUNT(*) c, SUM(closed_at IS NOT NULL) closed "
                     "FROM loop").fetchone()
    print("작업: %d개 (완료 %d)" % (lc["c"], lc["closed"] or 0))

    rows = con.execute("SELECT kind, COUNT(*) c FROM event %s GROUP BY kind "
                       "ORDER BY c DESC" % cond, params).fetchall()
    print("이벤트: " + (", ".join("%s %d" % (EVENT_KINDS.get(r["kind"], r["kind"]), r["c"])
                                  for r in rows) or "(없음)"))

    # 반복 신호는 규칙 단위로 봐야 드러난다. (규칙, 대상) 으로 묶으면
    # 같은 규칙에 다른 파일로 계속 걸리는 패턴이 흩어져 보이지 않는다.
    # tool_fail 만 예외 — 정규화된 명령 자체가 의미 있는 키다.
    for kind, title, key in (("block", "차단된 규칙", "rule"),
                             ("tool_fail", "실패한 도구", "target"),
                             ("skip", "건너뛴 단계", "rule"),
                             ("stop_gate", "미충족 종료 조건", "rule"),
                             ("bypass", "우회한 게이트", "rule")):
        q = ("SELECT IFNULL(%s,'-') k, COUNT(*) c, COUNT(DISTINCT loop_id) loops, "
             "COUNT(DISTINCT target) targets FROM event WHERE kind=? %s "
             "GROUP BY k ORDER BY loops DESC, c DESC LIMIT 6"
             % (key, "AND loop_id=?" if only else ""))
        rs = con.execute(q, ([kind] + params)).fetchall()
        if not rs:
            continue
        print("\n%s" % title)
        for r in rs:
            bits = "×%d" % r["c"]
            if key == "rule" and r["targets"] > 1:
                bits += ", 대상 %d종" % r["targets"]
            if r["loops"] > 1:
                bits += " ← %d개 작업에서 반복" % r["loops"]
            print("  %-24s %s" % (r["k"][:24], bits))
    with con:
        sync_promotions(con, cfg)
    refresh_learned(con, cfg, root)
    rows = con.execute("SELECT key, decision, maturity, note FROM promotion "
                       "ORDER BY maturity, at").fetchall()
    if rows:
        print("\n승격 이력 — 반복을 기계화한 기록")
        for r in rows:
            print("  %-28s %-10s %-12s %s"
                  % (r["key"][:28], r["decision"], r["maturity"],
                     (r["note"] or "-")[:36]))
    pend = pending_promotions(con, cfg)
    if pend:
        print("\n승격 결정 대기 %d개 — `harness promote`" % len(pend))
    print("\n상세 조회: `harness recall <키워드|경로>`")
    return 0


def cli_auto_skip(con, cfg, root, lid, sid, argv):
    """스킵 자동 승인 토글. on 은 PreToolUse 가 사람의 동의를 받은 뒤에만 도달한다."""
    pos = argv_positional(argv)
    mode = pos[0] if pos else "status"
    if mode == "off":
        with con:
            set_meta(con, "auto_skip", "off")
            set_meta(con, "auto_skip_uses", "")
            set_meta(con, "auto_skip_loop", "")
            set_meta(con, "auto_skip_off_at", now())
        print("스킵 자동 승인 OFF — 이제 모든 스킵이 사용자 동의를 요구한다.")
        return 0

    if mode == "on":
        reason = argv_value(argv, "reason")
        uses = argv_value(argv, "uses")
        scope = argv_value(argv, "scope") or "project"
        if not reason:
            print("사용법: harness auto-skip on --reason \"...\" "
                  "[--uses N] [--scope loop|project]")
            return 2
        if scope not in ("loop", "project"):
            print("--scope 는 loop 또는 project 여야 한다.")
            return 2
        if uses is not None:
            try:
                if int(uses) < 1:
                    raise ValueError
            except ValueError:
                print("--uses 는 1 이상의 정수여야 한다.")
                return 2
        with con:
            set_meta(con, "auto_skip", "on")
            set_meta(con, "auto_skip_reason", reason)
            set_meta(con, "auto_skip_at", now())
            set_meta(con, "auto_skip_uses", str(int(uses)) if uses else "")
            set_meta(con, "auto_skip_loop", lid if scope == "loop" else "")
        print("스킵 자동 승인 ON (사용자 승인) — 사유: %s" % reason)
        print("범위: %s" % auto_skip_scope_note(con))
        print("사유는 계속 필수이고 기록에는 authorized_by=auto 로 남는다. "
              "끄려면 `harness auto-skip off`.")
        return 0

    if mode == "status":
        active, expired = auto_skip_state(con)
        if active:
            print("스킵 자동 승인: ON (since %s) — 사유: %s"
                  % (get_meta(con, "auto_skip_at", "-"),
                     get_meta(con, "auto_skip_reason", "-")))
            print("  범위: %s" % auto_skip_scope_note(con))
        else:
            print("스킵 자동 승인: OFF — 모든 스킵이 사용자 동의를 요구한다."
                  + (" (%s)" % expired if expired else ""))
        return 0

    print("사용법: harness auto-skip {on --reason \"...\" [--uses N] "
          "[--scope loop|project] | off | status}")
    return 2


def cli_loop(con, cfg, root, lid, sid, argv):
    pos = argv_positional(argv)
    sub = pos[0] if pos else "show"
    if sub == "new":
        intent = argv_value(argv, "intent")
        with con:
            close_loop(con, lid)
            nlid = create_loop(con, cfg, root, intent)
            if intent:
                record_evidence(con, nlid, cfg["stages"][0]["id"], "intent_set", intent)
        print("작업 %s 종료 → 새 작업 %s, 단계 %s"
              % (lid, nlid, label_of(cfg, cfg["stages"][0]["id"])))
        return 0
    if sub == "intent":
        text = " ".join(pos[1:]).strip() or (argv_value(argv, "intent") or "").strip()
        if not text:
            print("사용법: harness loop intent \"<이번 루프에서 할 작업>\"")
            return 2
        with con:
            con.execute("UPDATE loop SET intent=? WHERE id=?", (text, lid))
            # Scaffolding 의 종료 조건. 작업을 기록하지 않으면 단계를 넘어갈 수 없다.
            record_evidence(con, lid, sid, "intent_set", text)
        print("작업 %s 의 내용: %s" % (lid, text))
        print("Context 단계의 `harness recall` 이 이 작업을 기준으로 과거 기록을 찾는다.")
        return 0
    if sub == "done-when":
        items = [t for t in pos[1:] if t.strip()]
        if "--clear" in argv:
            with con:
                con.execute("DELETE FROM evidence WHERE loop_id=? AND kind='acceptance'",
                            (lid,))
            print("완료 조건을 비웠다. 다시 기록하라.")
            return 0
        if items:
            with con:
                for it in items:
                    record_evidence(con, lid, sid, "acceptance", it.strip())
        rows = acceptance_of(con, lid)
        if not rows:
            print("사용법: harness loop done-when \"<완료 조건>\" [\"<조건2>\" ...] [--clear]")
            print("무엇이 '끝'인지 기록한다. Verification 이 이것을 대조하고,")
            print("Compounding 이 작업 종료 판단의 근거로 쓴다. 회차가 바뀌어도 유지된다.")
            return 2
        print("작업 %s 의 완료 조건 (%d개):" % (lid, len(rows)))
        for i, t in enumerate(rows, 1):
            print("  %d. %s" % (i, t))
        return 0
    if sub == "adopt":
        want = pos[1] if len(pos) > 1 else None
        if not want:
            print("사용법: harness loop adopt <loop-id> --reason \"...\"")
            return 2
        with con:
            close_loop(con, lid)
            create_loop(con, cfg, root, argv_value(argv, "reason"), loop_id=want)
        print("작업 %s 재연결(사용자 승인). 단계는 1단계부터 다시 추적한다." % want)
        return 0
    row = con.execute("SELECT * FROM loop WHERE id=?", (lid,)).fetchone()
    print("loop %s · branch %s · created %s"
          % (lid, row["branch"] if row else "-", row["created_at"] if row else "-"))
    if row and row["intent"]:
        print("  intent: %s" % row["intent"])
    return 0


CLI = {
    "status": cli_status,
    "advance": cli_advance,
    "skip": cli_skip,
    "allow": cli_allow,
    "approve-plan": cli_approve_plan,
    "loop": cli_loop,
    "auto-skip": cli_auto_skip,
    "recall": cli_recall,
    "stats": cli_stats,
    "promote": cli_promote,
    "tidy": cli_tidy,
    "metrics": cli_metrics,
}


def run_cli(argv):
    cmd = argv[0]
    if cmd == "init":
        return cli_init(argv[1:])
    root = find_root(os.getcwd())
    if not root:
        print("이 프로젝트에는 하네스가 설치되지 않았다. `harness init` 또는 "
              "/step-six-harness:install 을 실행하라.", file=sys.stderr)
        return 1
    con = connect(root)
    cfg = load_config(root, plugin_root())
    if con is None or not isinstance(cfg, dict) or not cfg.get("stages"):
        print("DB 또는 설정이 손상되었다: %s" % os.path.join(root, HARNESS_DIR),
              file=sys.stderr)
        return 1
    try:
        lid = head_loop(con)
        sid = active_stage(con, lid) if lid else None
        if not lid or not sid:
            with con:
                lid = create_loop(con, cfg, root)
            sid = cfg["stages"][0]["id"]
            print("활성 루프가 없어 새로 만들었다: %s" % lid)
        fn = CLI.get(cmd)
        if not fn:
            print("알 수 없는 명령: %s\n사용 가능: %s\n전체 사용법은 `harness help`."
                  % (cmd, ", ".join(sorted(CLI))), file=sys.stderr)
            return 2
        try:
            return fn(con, cfg, root, lid, sid, argv[1:])
        except sqlite3.OperationalError as exc:
            # 스키마가 플러그인보다 오래됐을 때 traceback 대신 할 일을 알려준다.
            # 마이그레이션은 하지 않는다 — DB 는 커밋하지 않는 런타임 상태이고,
            # init 이 스키마를 다시 적용하는 것이 정해진 업그레이드 경로다.
            print("DB 스키마가 플러그인 버전과 맞지 않는다 (%s).\n"
                  "`.claude/harness/bin/harness init` 을 다시 실행하라 — 파일은 "
                  "덮어쓰지 않고 스키마만 갱신한다. 그래도 안 되면 %s 를 지우고 "
                  "init 을 실행하라 (진행 중 상태만 사라지고 기록 파일은 남는다)."
                  % (exc, DB_REL.replace(os.sep, "/")), file=sys.stderr)
            return 1
    finally:
        con.close()


WRAPPER_CMD = ".claude/harness/bin/harness"
# 읽기 전용·정상 진행 명령만 미리 허용한다. 동의가 필요한 명령
# (skip / allow / approve-plan / auto-skip on / loop new|adopt) 은 의도적으로 제외한다.
SAFE_PERMS = ["Bash(%s %s)" % (WRAPPER_CMD, c)
              for c in ("status", "advance", "loop", "help", "tidy", "promote", "metrics")] \
    + ["Bash(%s %s:*)" % (WRAPPER_CMD, c)
       for c in ("recall", "stats", "loop intent", "promote")] \
    + ["Bash(%s auto-skip status)" % WRAPPER_CMD]


def ensure_permissions(root):
    """하네스 조회 명령을 프로젝트 설정에 미리 허용한다.

    매번 권한 프롬프트를 요구하면 모델이 조회를 포기하고 파일을 직접 읽는
    우회로 간다 — 실제 세션에서 관측된 문제다. 반환값은 추가한 규칙 수.
    """
    path = os.path.join(root, ".claude", "settings.json")
    data = {}
    if os.path.isfile(path):
        data = jload(path)
        if not isinstance(data, dict):
            return -1  # 손상된 설정은 건드리지 않는다
    # 최상위가 dict 인 것만 확인하고 setdefault 를 연달아 호출하면, permissions 가
    # list/문자열인 정상 JSON 에서 AttributeError 로 init 이 중간에 죽어
    # 부분 설치 상태를 남긴다. 모양을 단계마다 확인한다.
    perms = data.get("permissions")
    if perms is None:
        perms = data["permissions"] = {}
    if not isinstance(perms, dict):
        return -1
    allow = perms.get("allow")
    if allow is None:
        allow = perms["allow"] = []
    if not isinstance(allow, list):
        return -1
    added = [p for p in SAFE_PERMS if p not in allow]
    if not added:
        return 0
    allow.extend(added)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return len(added)


def cli_init(argv):
    root = os.path.abspath(argv[0] if argv else os.getcwd())
    pr = plugin_root()
    created = []
    for rel, src in ((CONFIG_REL, "templates/stages.json"),
                     (POLICY_REL, "templates/POLICY.md"),
                     (RATIONALE_REL, "templates/rationale.md")):
        dst = os.path.join(root, rel)
        if os.path.exists(dst):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(os.path.join(pr, src), encoding="utf-8") as fh:
            body = fh.read()
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(body)
        created.append(rel)

    cfg = load_config(root, pr)
    fresh = not os.path.isfile(os.path.join(root, DB_REL))
    con = connect(root, create=True)
    con.executescript(SCHEMA)
    con.commit()
    lid = head_loop(con)
    if not lid or not active_stage(con, lid):
        with con:
            lid = create_loop(con, cfg, root)
        created.append("%s (loop %s)" % (DB_REL, lid))
    elif fresh:
        created.append(DB_REL)
    # 앵커가 가리키는 파일이 없으면 CLAUDE.md 임포트가 깨진다. 빈 상태로라도 만든다.
    if refresh_learned(con, cfg, root):
        created.append(LEARNED_REL)
    con.close()
    refresh_wrapper(root)

    nperm = ensure_permissions(root)
    if nperm > 0:
        created.append(".claude/settings.json (조회 명령 %d개 허용)" % nperm)
    elif nperm < 0:
        print("주의: .claude/settings.json 을 읽을 수 없어 권한 허용을 건너뛰었다.",
              file=sys.stderr)

    gi = os.path.join(root, ".gitignore")
    want = [".claude/harness/harness.db", ".claude/harness/harness.db-wal",
            ".claude/harness/harness.db-shm", ".claude/harness/bin/"]
    have = open(gi, encoding="utf-8").read() if os.path.isfile(gi) else ""
    # 행 단위로 본다. substring 으로 보면 주석에 경로가 언급된 것만으로
    # "이미 있다"고 판단해 실제 ignore 규칙을 넣지 않는다.
    have_lines = {ln.strip() for ln in have.splitlines()}
    add = [w for w in want if w not in have_lines]
    if add:
        with open(gi, "a", encoding="utf-8") as fh:
            if have and not have.endswith("\n"):
                fh.write("\n")
            fh.write("\n# step-six-harness (런타임 상태 — 커밋하지 않는다)\n")
            fh.write("\n".join(add) + "\n")
        created.append(".gitignore")

    # 앵커는 두 줄이다. POLICY 는 사람이 정한 원칙, LEARNED 는 하네스가 승격한 규칙.
    # 둘을 한 파일에 섞으면 생성 대상과 손으로 쓴 것이 구분되지 않는다.
    cm = os.path.join(root, "CLAUDE.md")
    body = open(cm, encoding="utf-8").read() if os.path.isfile(cm) else ""
    # 행 단위로 본다. 코드 예시나 설명문에 앵커 문자열이 있으면 substring 판정은
    # 실제 import 행이 없는데도 있다고 착각한다.
    body_lines = {ln.strip() for ln in body.splitlines()}
    add_anchors = [a for a in ("@%s" % POLICY_REL.replace(os.sep, "/"),
                               "@%s" % LEARNED_REL.replace(os.sep, "/"))
                   if a not in body_lines]
    if add_anchors:
        with open(cm, "a", encoding="utf-8") as fh:
            if body and not body.endswith("\n"):
                fh.write("\n")
            fh.write("\n" + "\n".join(add_anchors) + "\n")
        created.append("CLAUDE.md (앵커 %d줄)" % len(add_anchors))

    print("하네스 설치 완료: %s" % root)
    for c in created:
        print("  + %s" % c)
    if not created:
        print("  (변경 없음 — 이미 설치되어 있다)")
    print("활성 작업: %s" % lid)
    print("커밋 대상: .claude/harness/{POLICY.md,LEARNED.md,stages.json,rationale.md}, "
          "CLAUDE.md")
    return 0


USAGE = """step-six-harness — 작업 하네스

  0 Selection → 1 Scaffolding → 2 Context → 3 Planning
              → 4 Execution → 5 Verification → 6 Compounding
                                   ├─ 작업 끝    → 0 Selection
                                   └─ 회차 계속  → 1 Scaffolding

현재 상태
  status                       현재 작업·회차·단계·종료 조건·증거·스킵 기록·예외
  loop                         작업 해시와 브랜치 (.dev/ 파일명 접두사는 해시-회차)

과거 기록 조회 (Context 단계에서 쓴다)
  recall [키워드|경로 ...] [--kind K] [--rule R]
                               과거 차단·실패·재편집 기록과 관련 회고 파일을 찾는다.
                               이번 task 와 관련된 것만 골라 읽어라 — 전부 읽지 마라
  stats [--loop]               누적 수치. 어떤 규칙에 몇 번 걸렸는지, 무엇이 반복되는지

복리 (기록을 자산으로 바꾼다)
  promote                      여러 작업에서 반복된 항목과 승격 결정 목록
  promote <key> --as hook|rule|skill|structure --note "..."
                               반복을 기계화했다고 기록. --as rule 은 LEARNED.md 에
                               한 줄로 올라가 항상 로드된다 (예산 상한 있음)
  promote <key> --decline --reason "..."
                               승격하지 않기로 결정. 보류도 결정이므로 기록된다.
                               이후 다시 반복되면 결정이 무효화되어 다시 올라온다
  tidy                         정리 후보 — 낡은 인덱스, 오래된 파일, 병합 후보,
                               재발한 승격, LEARNED.md 예산 (Scaffolding 에서 쓴다)
  metrics                      복리 측정 — 승격 종류별 재발률(무엇이 실제로 막았나),
                               회차 추세와 그 짝인 우회 추세, 반복 실패 비율.
                               점수는 만들지 않는다

단계 진행
  advance                      다음 단계로. 종료 조건이 남으면 거부하고 무엇이 남았는지 알려준다
  advance --done               (Compounding 에서만) 작업 종료 → Selection 으로
  advance --cycle              (Compounding 에서만) 다음 회차 → Scaffolding 으로, 같은 작업 유지
  skip <대상> --reason "..."    단계 건너뛰기 ✋
                               대상: <stage-id> | +N (N단계 전진) | until:<stage-id>
  approve-plan <file>          계획에 대한 사람의 승인 기록 ✋

예외
  allow <glob> --reason "..." [--uses N]
                               쓰기 금지 경로(docs/ 등)에 예외 등록 ✋
  auto-skip on --reason "..." [--uses N] [--scope loop|project]
                               스킵 동의 다이얼로그를 끈다 ✋ (범위를 좁히는 것을 권한다)
  auto-skip off | status       자동 승인 해제 / 현재 상태

루프
  loop intent "<작업>"          이번에 할 작업을 기록 (Selection 에서)
                               recall 이 이 작업을 기본 키워드로 쓴다
  loop done-when "<조건>" ...   무엇이 '끝'인지 기록 (Selection 에서). 인자 없으면 목록,
                               --clear 로 비운다. 회차가 바뀌어도 유지된다
  loop new --reason "..." [--intent "..."]
                               루프를 닫고 새 해시로 시작 ✋ — 남은 단계와 승격 결정을
                               건너뛰게 되므로 스킵과 같은 승인을 받는다
  loop adopt <hash> --reason "..."
                               DB를 잃었을 때 기존 해시로 재연결 ✋

설치
  init [path]                  이 프로젝트에 하네스 설치

✋ = 사용자 승인 다이얼로그가 뜬다. 모델은 스스로 승인할 수 없다.

명령을 외울 필요는 없다 — 차단당하면 훅이 실행할 명령을 그대로 알려준다.
단계 정의·폴더 규칙: .claude/harness/stages.json
승격된 규칙 (하네스가 생성): .claude/harness/LEARNED.md
왜 이렇게 통제하는가: .claude/harness/rationale.md"""


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    if argv[0] == "hook":
        return run_hook()
    return run_cli(argv)


if __name__ == "__main__":
    sys.exit(main())
