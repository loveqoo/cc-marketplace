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
  id TEXT PRIMARY KEY, intent TEXT, branch TEXT, created_at TEXT);
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
"""


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
    """루프의 모든 행을 버린다. 영구 기록은 폴더의 파일명이 갖고 있다."""
    for tbl in ("stage", "evidence", "wgrant"):
        con.execute("DELETE FROM %s WHERE loop_id=?" % tbl, (lid,))
    con.execute("DELETE FROM loop WHERE id=?", (lid,))


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
    """(decision, reason). decision None 이면 판정하지 않음."""
    stage = stage_obj(cfg, sid)
    lbl = label_of(cfg, sid)
    cls = classify(rel, cfg)
    rules = cfg.get("folder_rules", {})
    grant = find_grant(con, lid, rel)

    # 1. docs/ 는 인간 소유
    if cls == "docs" and not grant:
        return "deny", (
            "docs/ 는 사람이 기록하는 영역이라 하네스가 쓰기를 막는다 (%s). "
            "정말 필요하면 사용자에게 승인을 받아라: "
            "`.claude/harness/bin/harness allow \"%s\" --reason \"...\"`" % (rel, rel))

    # 2. 단계별 쓰기 허용 클래스
    if cls not in stage.get("write", []) and not grant:
        return "deny", (
            "현재 단계 %s 에서는 '%s' 클래스 경로에 쓸 수 없다 (%s). 허용: %s. "
            "단계를 진행하려면 `.claude/harness/bin/harness advance`, "
            "건너뛰려면 `... skip <stage> --reason \"...\"`."
            % (lbl, cls, rel, ", ".join(stage.get("write", [])) or "(없음)"))

    # 3. 신규 최상위 폴더는 Scaffolding 에서만 (승인된 구조 영역은 제외)
    top = new_toplevel_dir(root, rel) if cls == "source" else None
    if top and sid not in rules.get("new_toplevel_dir_stages", ["scaffolding"]):
        return "deny", (
            "신규 최상위 폴더 '%s/' 는 Scaffolding 단계에서만 만들 수 있다 (현재 %s). "
            "구조 변경이 필요하면 Scaffolding 으로 되돌아가서 합의하라." % (top, lbl))

    parts = rel.split("/")

    # 4. .dev 하위 폴더 규칙 + 루프 해시 파일명
    if cls == "dev" and len(parts) >= 2:
        allowed = rules.get("dev_subdirs", [])
        if allowed and parts[1] not in allowed:
            return "deny", (".dev/ 하위는 %s 만 허용한다. '%s' 는 규칙 위반이다."
                            % ("/".join(allowed), parts[1]))
        if len(parts) >= 3 and parts[1] in rules.get("loop_prefixed_dirs", []):
            if not parts[-1].startswith(lid + "-"):
                return "deny", (
                    "이 루프의 산출물은 파일명이 루프 해시로 시작해야 한다. "
                    "'%s' 대신 '%s-%s' 로 써라. 해시가 시퀀스이자 워크트리 간 "
                    "충돌 방지 장치다." % (parts[-1], lid, parts[-1]))

    # 5. docs 하위 번호 명명 규칙 (grant 로 쓰기가 허용된 경우에만 도달)
    if cls == "docs" and len(parts) >= 3 and parts[1] in rules.get("docs_subdirs", []):
        pat = rules.get("numbered_name_pattern")
        if pat and not re.match(pat, parts[-1]):
            return "deny", ("docs/%s/ 파일명은 NNN-name.md 형식이어야 한다. "
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
        "[harness] loop %s · 단계 %s — %s" % (lid, label_of(cfg, sid), stage["summary"]),
        "제어: `.claude/harness/bin/harness` {status | advance | "
        "skip <stage|+N|until:<stage>> --reason \"...\" | allow <glob> --reason \"...\" | "
        "approve-plan <file>}",
        "이 단계 쓰기 허용: %s. `.dev/` 산출물 파일명은 `%s-` 로 시작해야 한다. "
        "근거 문서: `.claude/harness/rationale.md`"
        % (", ".join(stage.get("write", [])) or "(없음)", lid),
        "모든 답변 말머리에 [%s] 를 붙여라." % label_of(cfg, sid),
    ]
    if auto_skip_on(con):
        lines.append("⚠ 스킵 자동 승인이 켜져 있다 — 스킵에 사용자 다이얼로그가 뜨지 않는다. "
                     "사유는 여전히 필수다. 끄려면 `harness auto-skip off`.")
    sk = skips_of(con, lid)
    if sk:
        lines.append("이 루프의 스킵: "
                     + "; ".join("%s(%s, %s)" % (r["stage"], r["reason"],
                                                 r["authorized_by"]) for r in sk))
    emit({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                 "additionalContext": "\n".join(lines)}})


def auto_skip_on(con):
    return get_meta(con, "auto_skip") == "on"


def ctrl_decision(con, sub, cmd, mode):
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
        return emit(pre_decision("deny",
            "사유 없이 %s 할 수 없다. --reason \"...\" 로 사유를 명시하라." % sub))

    if sub == "skip" and auto_skip_on(con):
        # 자동 승인이 켜져 있다. 다이얼로그는 생략하되 사실은 사용자에게 노출한다.
        out = pre_decision("defer", None)
        out["systemMessage"] = ("harness: 단계 스킵을 자동 승인했다 (사유: %s). "
                                "끄려면 `harness auto-skip off`." % reason)
        return emit(out)

    detail = "%s: `%s`" % (CTRL_HEAD[sub], cmd.strip())
    if reason:
        detail += "\n사유: %s" % reason
    detail += "\n승인하면 하네스 상태에 기록된다."
    if mode == "bypassPermissions":
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
            return ctrl_decision(con, m.group(1), cmd, mode)

        if BASH_MUTATORS.search(cmd):
            for tok in re.findall(r"[\w./~-]+", cmd):
                rel = rel_to_root(root, tok)
                if rel and "/" in rel and classify(rel, cfg) == "docs" \
                        and not find_grant(con, lid, rel):
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


def hook_stop(inp, con, cfg, root, lid, sid):
    stage = stage_obj(cfg, sid)
    prompt_id = inp.get("prompt_id") or "-"
    msg = (inp.get("last_assistant_message") or "").strip()
    limits = cfg.get("stop_block_limits", {})

    problems = []
    if msg:
        # 이 프롬프트 동안 관여한 단계의 말머리는 모두 허용한다 (턴 중 전이 대응)
        seen = [r["stage"] for r in con.execute(
            "SELECT stage FROM prompt_stage WHERE prompt_id=?", (prompt_id,))]
        ok = {stage_index(cfg, s) + 1 for s in seen + [sid]}
        m = re.match(r"^\[\s*(\d+)\s*/\s*\d+", msg)
        if not m or int(m.group(1)) not in ok:
            problems.append(("prefix",
                "말머리에 [%s] 를 표시하지 않았다. 현재 단계를 말머리에 붙여 다시 답하라."
                % label_of(cfg, sid)))
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
            blocked.append(text)

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
    print("loop %s · 단계 %s" % (lid, label_of(cfg, sid)))
    print("  요약: %s" % stage_obj(cfg, sid)["summary"])
    print("  쓰기 허용: %s" % (", ".join(stage_obj(cfg, sid).get("write", [])) or "(없음)"))
    print("  .dev/ 산출물 파일명 접두사: %s-" % lid)
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
        print("  ⚠ 스킵 자동 승인 ON (since %s) — 사유: %s. 끄려면 `harness auto-skip off`"
              % (get_meta(con, "auto_skip_at", "-"),
                 get_meta(con, "auto_skip_reason", "-")))
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
    """Compounding 진입 시 스킵 기록을 노출해 회고 md 로 옮기게 한다.
    루프가 닫히면 DB 행은 버려지므로, 여기서 옮기지 않으면 그 사실만 소실된다."""
    if sid != "compounding":
        return
    sk = skips_of(con, lid)
    if not sk:
        return
    print("\n이 루프에서 건너뛴 단계 — 회고에 사유와 함께 기록하라 "
          "(루프 종료 시 DB 기록은 버려진다):")
    for r in sk:
        print("  - %s: %s (승인: %s)" % (r["stage"], r["reason"], r["authorized_by"]))


def cli_advance(con, cfg, root, lid, sid, argv):
    missing = exit_blockers(con, cfg, lid, sid)
    if missing:
        print("advance 거부 — %s 단계의 종료 조건이 남았다:" % stage_obj(cfg, sid)["label"])
        for k in missing:
            print("  - %s: %s" % (k, CRITERIA_HELP.get(k, k)))
        print("정당한 사유가 있으면 `harness skip %s --reason \"...\"` 로 "
              "사람의 승인을 받아라." % sid)
        return 1
    with con:
        con.execute("UPDATE stage SET status='done', left_at=? "
                    "WHERE loop_id=? AND stage=?", (now(), lid, sid))
        nlid, nsid, cycled = _enter(con, cfg, root, lid, stage_index(cfg, sid) + 1)
    if cycled:
        print("루프 %s 종료 — 기록은 각 폴더의 파일에 남아 있다." % lid)
        print("새 루프 %s 시작 → 단계 %s" % (nlid, label_of(cfg, nsid)))
    else:
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

    # 자동 승인으로 통과한 스킵은 사람이 승인한 것과 구분해 기록한다
    by = "auto" if auto_skip_on(con) else "user"
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
        nlid, nsid, cycled = _enter(con, cfg, root, lid, dest + 1)
    print("스킵(%s): %s" % ("자동 승인" if by == "auto" else "사용자 승인",
                            ", ".join(skipped) or "(없음)"))
    print("사유: %s" % reason)
    if cycled:
        print("루프 %s 종료 → 새 루프 %s, 단계 %s" % (lid, nlid, label_of(cfg, nsid)))
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


def cli_auto_skip(con, cfg, root, lid, sid, argv):
    """스킵 자동 승인 토글. on 은 PreToolUse 가 사람의 동의를 받은 뒤에만 도달한다."""
    pos = argv_positional(argv)
    mode = pos[0] if pos else "status"
    if mode == "off":
        with con:
            set_meta(con, "auto_skip", "off")
            set_meta(con, "auto_skip_off_at", now())
        print("스킵 자동 승인 OFF — 이제 모든 스킵이 사용자 동의를 요구한다.")
        return 0
    if mode == "on":
        reason = argv_value(argv, "reason")
        if not reason:
            print("사용법: harness auto-skip on --reason \"...\"")
            return 2
        with con:
            set_meta(con, "auto_skip", "on")
            set_meta(con, "auto_skip_reason", reason)
            set_meta(con, "auto_skip_at", now())
        print("스킵 자동 승인 ON (사용자 승인) — 사유: %s" % reason)
        print("이후 스킵은 다이얼로그 없이 통과하지만, 사유는 계속 필수이고")
        print("기록에는 authorized_by=auto 로 남는다. 끄려면 `harness auto-skip off`.")
        return 0
    if mode == "status":
        if auto_skip_on(con):
            print("스킵 자동 승인: ON (since %s) — 사유: %s"
                  % (get_meta(con, "auto_skip_at", "-"),
                     get_meta(con, "auto_skip_reason", "-")))
        else:
            print("스킵 자동 승인: OFF — 모든 스킵이 사용자 동의를 요구한다.")
        return 0
    print("사용법: harness auto-skip {on --reason \"...\" | off | status}")
    return 2


def cli_loop(con, cfg, root, lid, sid, argv):
    pos = argv_positional(argv)
    sub = pos[0] if pos else "show"
    if sub == "new":
        with con:
            close_loop(con, lid)
            nlid = create_loop(con, cfg, root, argv_value(argv, "intent"))
        print("루프 %s 종료 → 새 루프 %s, 단계 %s"
              % (lid, nlid, label_of(cfg, cfg["stages"][0]["id"])))
        return 0
    if sub == "adopt":
        want = pos[1] if len(pos) > 1 else None
        if not want:
            print("사용법: harness loop adopt <loop-id> --reason \"...\"")
            return 2
        with con:
            close_loop(con, lid)
            create_loop(con, cfg, root, argv_value(argv, "reason"), loop_id=want)
        print("루프 %s 재연결(사용자 승인). 단계는 1단계부터 다시 추적한다." % want)
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
            print("알 수 없는 명령: %s (%s)" % (cmd, ", ".join(sorted(CLI))),
                  file=sys.stderr)
            return 2
        return fn(con, cfg, root, lid, sid, argv[1:])
    finally:
        con.close()


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
    print("활성 루프: %s" % lid)
    print("커밋 대상: .claude/harness/{POLICY.md,stages.json,rationale.md}, CLAUDE.md")
    return 0


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 0
    if argv[0] == "hook":
        return run_hook()
    return run_cli(argv)


if __name__ == "__main__":
    sys.exit(main())
