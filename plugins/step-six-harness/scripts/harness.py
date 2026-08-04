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

WRITE_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
}

# Bash 가 파일을 건드릴 가능성이 있는 명령. 이 경우에만 경로 토큰을 훑는다.
BASH_MUTATORS = re.compile(r"(^|[;&|]\s*)(rm|mv|cp|mkdir|touch|tee|dd|truncate)\b|>\s*\S|sed\s+-i")
CTRL_RE = re.compile(r"harness(?:\.py)?[\"']?\s+([a-z-]+)")
CONSENT_CMDS = ("skip", "allow", "approve-plan", "adopt")
CTRL_HEAD = {
    "skip": "단계 스킵 요청",
    "allow": "쓰기 금지 경로에 대한 예외 요청",
    "approve-plan": "계획 승인 요청",
    "adopt": "기존 루프 해시 재연결 요청",
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
"""

EVENT_KINDS = {
    "block": "규칙 차단",
    "skip": "단계 스킵",
    "stop_gate": "종료 조건 미충족",
    "bypass": "게이트 우회",
    "tool_fail": "도구 실패",
    "edit": "파일 편집",
}


# --------------------------------------------------------------------------- io

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


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


def exit_blockers(con, cfg, lid, sid):
    return [k for k in stage_obj(cfg, sid).get("exit_criteria", [])
            if not has_evidence(con, lid, k)]


CRITERIA_HELP = {
    "intent_set": "이번에 할 작업을 `harness loop intent \"<작업>\"` 으로 기록해야 한다 "
                  "(Context 의 recall 이 이것을 기준으로 조회한다)",
    "plan_file": "계획 파일을 .dev/plan/ 아래에 남겨야 한다",
    "plan_approved": "계획에 대한 사람의 승인이 필요하다 (harness approve-plan <file>)",
    "verification_evidence": "검증 증거가 없다 — 테스트 실행, 서브에이전트 검토, "
                             "브라우저 확인, dry-run 중 하나를 수행하라",
    "retro_file": "회고를 .dev/retrospect/ 또는 .dev/learning/ 아래에 기록해야 한다",
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
        m = CTRL_RE.search(cmd)
        if m:
            with con:
                return ctrl_decision(con, m.group(1), cmd, mode, lid, sid)

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
    with con:
        record_event(con, lid, sid, "tool_fail", tool, target or tool, err)


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
        if not has_evidence(con, lid, key):
            problems.append((key, "%s 단계를 끝낼 수 없다: %s"
                             % (stage["label"], CRITERIA_HELP.get(key, key))))
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
        sys.stderr.write("step-six-harness: %s\n" % exc)
        return 1
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


WRAPPER = """#!/bin/sh
# step-six-harness wrapper — 세션 시작마다 갱신된다. 직접 편집하지 마라.
P="%s"
if [ ! -f "$P" ]; then
  P="$(ls -t "$HOME"/.claude/plugins/cache/*/step-six-harness/*/scripts/harness.py 2>/dev/null | head -1)"
fi
[ -f "$P" ] || { echo "step-six-harness: engine not found" >&2; exit 1; }
exec python3 "$P" "$@"
"""


def refresh_wrapper(root):
    path = os.path.join(root, WRAPPER_REL)
    body = WRAPPER % os.path.abspath(__file__)
    try:
        if not os.path.isfile(path) or open(path, encoding="utf-8").read() != body:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.chmod(path, 0o755)
    except Exception:
        pass


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


def _hint_on_enter(con, cfg, lid, sid):
    """단계 진입 시의 안내.

    Context 는 **당겨가는** 단계다. 과거 기록을 밀어넣지 않는다 — 이번 task 와
    무관한 실수까지 컨텍스트를 먹기 때문이다. 조회 방법만 알려주고 무엇이
    관련 있는지는 모델이 판단한다.
    Compounding 은 반대다. 막 끝낸 루프 자신의 기록은 무조건 관련 있으니 밀어준다.
    """
    hint = stage_obj(cfg, sid).get("hint")
    if hint:
        print("\n%s" % hint)

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


def next_cycle(con, cfg, root, lid):
    """같은 작업의 다음 회차. Selection 은 유지하고 나머지 단계를 초기화한다.

    증거를 초기화하지 않으면 2회차 Planning 이 1회차 계획서로 통과한다.
    intent_set 만 남긴다 — 작업은 그대로이므로 다시 선정할 필요가 없다.
    이전 회차의 계획·회고 파일은 파일로 남고, 파일명의 회차로 구분된다.
    """
    ids = stage_ids(cfg)
    con.execute("DELETE FROM evidence WHERE loop_id=? AND kind != 'intent_set'", (lid,))
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

    with con:
        con.execute("UPDATE stage SET status='done', left_at=? "
                    "WHERE loop_id=? AND stage=?", (now(), lid, sid))
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
    _hint_on_enter(con, cfg, nlid, nsid)
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

    # 건너뛸 수 없는 단계 — 루프를 중단하더라도 회고는 남겨야 복리가 끊기지 않는다.
    locked = [ids[i] for i in range(cur, dest + 1)
              if cfg["stages"][i].get("skippable") is False]
    if locked:
        print("%s 단계는 건너뛸 수 없다." % ", ".join(stage_obj(cfg, s)["label"] for s in locked))
        print("이 회차를 중단하려면 `harness skip until:%s --reason \"...\"` 로 그 단계까지 "
              "이동한 뒤, 중단 사유를 회고로 남기고 `harness advance` 로 루프를 닫아라."
              % locked[0])
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
        _hint_on_enter(con, cfg, nlid, nsid)
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
        for part in re.split(r"[/\\.\-_]", kw):
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
        return fn(con, cfg, root, lid, sid, argv[1:])
    finally:
        con.close()


WRAPPER_CMD = ".claude/harness/bin/harness"
# 읽기 전용·정상 진행 명령만 미리 허용한다. 동의가 필요한 명령
# (skip / allow / approve-plan / auto-skip on / loop new|adopt) 은 의도적으로 제외한다.
SAFE_PERMS = ["Bash(%s %s)" % (WRAPPER_CMD, c) for c in ("status", "advance", "loop", "help")] \
    + ["Bash(%s %s:*)" % (WRAPPER_CMD, c) for c in ("recall", "stats", "loop intent")] \
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
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
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
    add = [w for w in want if w not in have]
    if add:
        with open(gi, "a", encoding="utf-8") as fh:
            if have and not have.endswith("\n"):
                fh.write("\n")
            fh.write("\n# step-six-harness (런타임 상태 — 커밋하지 않는다)\n")
            fh.write("\n".join(add) + "\n")
        created.append(".gitignore")

    anchor = "@%s" % POLICY_REL.replace(os.sep, "/")
    cm = os.path.join(root, "CLAUDE.md")
    body = open(cm, encoding="utf-8").read() if os.path.isfile(cm) else ""
    if anchor not in body:
        with open(cm, "a", encoding="utf-8") as fh:
            if body and not body.endswith("\n"):
                fh.write("\n")
            fh.write("\n%s\n" % anchor)
        created.append("CLAUDE.md (앵커 1줄)")

    print("하네스 설치 완료: %s" % root)
    for c in created:
        print("  + %s" % c)
    if not created:
        print("  (변경 없음 — 이미 설치되어 있다)")
    print("활성 작업: %s" % lid)
    print("커밋 대상: .claude/harness/{POLICY.md,stages.json,rationale.md}, CLAUDE.md")
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
  loop intent "<작업>"          이번 루프에서 할 작업을 기록 (Scaffolding 에서)
                               recall 이 이 작업을 기본 키워드로 쓴다
  loop new [--intent "..."]    루프를 닫고 새 해시로 시작
  loop adopt <hash> --reason "..."
                               DB를 잃었을 때 기존 해시로 재연결 ✋

설치
  init [path]                  이 프로젝트에 하네스 설치

✋ = 사용자 승인 다이얼로그가 뜬다. 모델은 스스로 승인할 수 없다.

명령을 외울 필요는 없다 — 차단당하면 훅이 실행할 명령을 그대로 알려준다.
단계 정의·폴더 규칙: .claude/harness/stages.json
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
