#!/usr/bin/env python3
"""step-six-harness engine.

두 가지 모드로 동작한다.

  harness.py hook     stdin 으로 훅 이벤트 JSON 을 받아 판정 결과를 stdout 으로 낸다.
  harness.py <cmd>    모델/사람이 쓰는 CLI (status, advance, skip, allow, ...).

설계 원칙
  - 하네스가 설치되지 않은 프로젝트에서는 아무것도 출력하지 않고 즉시 종료한다.
    (플러그인은 유저 전역에 설치되므로 무관한 레포에서 조용해야 한다)
  - 규칙이 확정적으로 걸릴 때만 deny 한다. 판단이 필요한 지점은 ask 로 사람에게 넘긴다.
  - 상태/설정이 깨졌으면 차단하지 않는다. 세션을 벽돌로 만드는 것보다 무력한 게 낫다.
"""

import fnmatch
import json
import os
import re
import sys
import time

HARNESS_DIR = os.path.join(".claude", "harness")
STATE_REL = os.path.join(HARNESS_DIR, "state.json")
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
CTRL_RE = re.compile(r"harness(?:\.py)?[\"']?\s+(status|advance|skip|allow|approve-plan|cycle|init)\b")
CONSENT_CMDS = ("skip", "allow", "approve-plan")

EVIDENCE_STAGES = ("execution", "verification")


# --------------------------------------------------------------------------- io

def jload(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def jdump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def find_root(cwd):
    """state.json 을 가진 가장 가까운 조상 디렉터리."""
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
            if os.path.isfile(os.path.join(d, STATE_REL)):
                return d
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return None


# ----------------------------------------------------------------- state/config

def default_state():
    return {
        "version": 1,
        "cycle": 1,
        "stage": "scaffolding",
        "started_at": now(),
        "updated_at": now(),
        "evidence": {},
        "skips": [],
        "grants": [],
        "stop_blocks": {},
        "history": [],
    }


def load_config(root, plugin_root=None):
    cfg = jload(os.path.join(root, CONFIG_REL))
    if cfg is None and plugin_root:
        cfg = jload(os.path.join(plugin_root, "templates", "stages.json"))
    return cfg


def stage_index(cfg, stage_id):
    for i, st in enumerate(cfg["stages"]):
        if st["id"] == stage_id:
            return i
    return 0


def stage_obj(cfg, stage_id):
    return cfg["stages"][stage_index(cfg, stage_id)]


def ev_bucket(state, cycle=None):
    key = str(cycle if cycle is not None else state.get("cycle", 1))
    return state.setdefault("evidence", {}).setdefault(key, {})


# ------------------------------------------------------------------- path logic

def glob_match(rel, pat):
    if fnmatch.fnmatch(rel, pat):
        return True
    if pat.startswith("**/") and fnmatch.fnmatch(rel, pat[3:]):
        return True
    return False


def rel_to_root(root, path):
    if not path:
        return None
    p = path if os.path.isabs(path) else os.path.join(root, path)
    p = os.path.normpath(p)
    try:
        rel = os.path.relpath(p, root)
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


def granted(state, rel):
    for g in state.get("grants", []):
        if g.get("uses_left", 0) > 0 and glob_match(rel, g.get("glob", "")):
            return g
    return None


def consume_grant(root, state, grant):
    grant["uses_left"] = grant.get("uses_left", 1) - 1
    state["updated_at"] = now()
    jdump(os.path.join(root, STATE_REL), state)


def new_toplevel_dir(root, rel):
    """rel 이 아직 존재하지 않는 최상위 디렉터리 안에 만들어지는가."""
    parts = rel.split("/")
    if len(parts) < 2:
        return None
    top = parts[0]
    if os.path.isdir(os.path.join(root, top)):
        return None
    return top


def check_write(root, state, cfg, rel):
    """(decision, reason) 을 돌려준다. decision None 이면 판정하지 않음."""
    stage_id = state.get("stage", "scaffolding")
    stage = stage_obj(cfg, stage_id)
    idx = stage_index(cfg, stage_id) + 1
    label = "%d/6 %s" % (idx, stage["label"])
    cls = classify(rel, cfg)
    rules = cfg.get("folder_rules", {})
    grant = granted(state, rel)

    # 1. docs/ 는 인간 소유
    if cls == "docs" and not grant:
        return "deny", (
            "docs/ 는 사람이 기록하는 영역이라 하네스가 쓰기를 막는다 (%s). "
            "정말 필요하면 사용자에게 승인을 받아라: "
            "`.claude/harness/bin/harness allow \"%s\" --reason \"...\"`" % (rel, rel)
        )

    # 2. 단계별 쓰기 허용 클래스
    if cls not in stage.get("write", []) and not grant:
        allowed = ", ".join(stage.get("write", [])) or "(없음)"
        return "deny", (
            "현재 단계 %s 에서는 '%s' 클래스 경로에 쓸 수 없다 (%s). 허용: %s. "
            "단계를 진행하려면 `.claude/harness/bin/harness advance`, "
            "건너뛰려면 `... skip <stage> --reason \"...\"`."
            % (label, cls, rel, allowed)
        )

    # 3. 신규 최상위 폴더는 Scaffolding 에서만.
    #    path_classes 로 분류된 영역(.dev, docs, tests, .claude)은 이미 승인된 구조이므로 제외한다.
    top = new_toplevel_dir(root, rel) if cls == "source" else None
    if top and stage_id not in rules.get("new_toplevel_dir_stages", ["scaffolding"]):
        return "deny", (
            "신규 최상위 폴더 '%s/' 는 Scaffolding 단계에서만 만들 수 있다 (현재 %s). "
            "구조 변경이 필요하면 Scaffolding 으로 되돌아가서 합의하라." % (top, label)
        )

    # 4. .dev 하위 폴더 규칙
    if cls == "dev":
        parts = rel.split("/")
        allowed = rules.get("dev_subdirs", [])
        if len(parts) >= 2 and allowed and parts[1] not in allowed:
            return "deny", (
                ".dev/ 하위는 %s 만 허용한다. '%s' 는 규칙 위반이다."
                % ("/".join(allowed), parts[1])
            )

    # 5. docs 하위 번호 명명 규칙 (grant 로 쓰기가 허용된 경우에만 도달)
    if cls == "docs":
        parts = rel.split("/")
        pat = rules.get("numbered_name_pattern")
        subs = rules.get("docs_subdirs", [])
        if pat and len(parts) >= 3 and parts[1] in subs:
            if not re.match(pat, parts[-1]):
                return "deny", (
                    "docs/%s/ 파일명은 %s 형식이어야 한다. '%s' 는 규칙 위반이다."
                    % (parts[1], "NNN-name.md", parts[-1])
                )

    if grant:
        consume_grant(root, state, grant)
    return None, None


# ------------------------------------------------------------------- evidence

def has_evidence(state, cfg, key):
    return bool(ev_bucket(state).get(key))


def record_evidence(root, state, cfg, key, item):
    bucket = ev_bucket(state)
    lst = bucket.setdefault(key, [])
    if item not in lst:
        lst.append(item)
        state["updated_at"] = now()
        jdump(os.path.join(root, STATE_REL), state)


def exit_blockers(state, cfg, stage):
    missing = []
    for key in stage.get("exit_criteria", []):
        if not has_evidence(state, cfg, key):
            missing.append(key)
    return missing


CRITERIA_HELP = {
    "plan_file": "계획 파일을 .dev/plan/ 아래에 남겨야 한다",
    "plan_approved": "계획에 대한 사람의 승인이 필요하다 (`harness approve-plan <file>`)",
    "verification_evidence": "검증 증거가 없다 — 테스트 실행, 서브에이전트 검토, 브라우저 확인, dry-run 중 하나를 수행하라",
    "retro_file": "회고를 .dev/retrospect/ 또는 .dev/learning/ 아래에 기록해야 한다",
}


# ----------------------------------------------------------------- hook: output

def emit(obj):
    if obj:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def pre_decision(decision, reason):
    out = {"hookEventName": "PreToolUse", "permissionDecision": decision}
    if reason:
        out["permissionDecisionReason"] = reason
    return {"hookSpecificOutput": out}


# --------------------------------------------------------------------- hooks

def hook_session_start(inp, root, state, cfg):
    refresh_wrapper(root)
    stage_id = state.get("stage", "scaffolding")
    stage = stage_obj(cfg, stage_id)
    idx = stage_index(cfg, stage_id) + 1
    pending = [s for s in state.get("skips", []) if s.get("cycle") == state.get("cycle")]
    lines = [
        "[harness] cycle %s · 단계 %d/6 %s — %s"
        % (state.get("cycle", 1), idx, stage["label"], stage["summary"]),
        "제어: `.claude/harness/bin/harness` {status | advance | skip <stage|+N|until:<stage>> --reason \"...\" | allow <glob> --reason \"...\" | approve-plan <file>}",
        "이 단계 쓰기 허용: %s. 위반은 훅이 차단한다. 근거 문서: `.claude/harness/rationale.md`",
        "모든 답변 말머리에 [%d/6 %s] 를 붙여라. 없으면 턴 종료가 차단된다."
        % (idx, stage["label"]),
    ]
    lines[2] = lines[2] % (", ".join(stage.get("write", [])) or "(없음)")
    if pending:
        lines.append(
            "이번 사이클 스킵 기록: "
            + "; ".join("%s(%s)" % (s.get("stage"), s.get("reason", "")[:40]) for s in pending)
        )
    emit({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    })


def hook_pre_tool_use(inp, root, state, cfg):
    tool = inp.get("tool_name") or ""
    ti = inp.get("tool_input") or {}
    mode = inp.get("permission_mode") or "default"

    # 제어 명령: 사람의 동의가 필요한 것은 ask 로 올린다
    if tool == "Bash":
        cmd = ti.get("command") or ""
        m = CTRL_RE.search(cmd)
        if m:
            sub = m.group(1)
            if sub in CONSENT_CMDS:
                reason = arg_value(cmd, "reason")
                if sub != "approve-plan" and not reason:
                    return emit(pre_decision("deny", (
                        "사유 없이 %s 할 수 없다. --reason \"...\" 로 사유를 명시하라." % sub)))
                head = {
                    "skip": "단계 스킵 요청",
                    "allow": "쓰기 금지 경로에 대한 예외 요청",
                    "approve-plan": "계획 승인 요청",
                }[sub]
                detail = "%s: `%s`" % (head, cmd.strip())
                if reason:
                    detail += "\n사유: %s" % reason
                detail += "\n승인하면 하네스 상태에 기록된다."
                if mode == "bypassPermissions":
                    # 이 모드에서 ask 가 자동 승인될 수 있으므로 동의 게이트를 닫는다
                    return emit(pre_decision("deny", (
                        detail + "\nbypassPermissions 모드에서는 사람의 동의를 받을 수 없어 거부한다. "
                        "권한 모드를 낮추고 다시 시도하라.")))
                return emit(pre_decision("ask", detail))
            return  # status / advance / cycle / init 는 통과

        if BASH_MUTATORS.search(cmd):
            for tok in re.findall(r"[\w./~-]+", cmd):
                rel = rel_to_root(root, tok)
                if not rel or "/" not in rel:
                    continue
                if classify(rel, cfg) == "docs" and not granted(state, rel):
                    return emit(pre_decision("deny", (
                        "docs/ 는 사람이 기록하는 영역이다. 이 명령이 '%s' 를 변경할 수 있어 막는다. "
                        "필요하면 `harness allow` 로 승인을 받아라." % rel)))
        return

    field = WRITE_TOOLS.get(tool)
    if not field:
        return
    rel = rel_to_root(root, ti.get(field))
    if not rel:
        return
    decision, reason = check_write(root, state, cfg, rel)
    if decision:
        emit(pre_decision(decision, reason))


def hook_post_tool_use(inp, root, state, cfg):
    """증거만 조용히 적립한다. 컨텍스트로 출력하지 않는다."""
    tool = inp.get("tool_name") or ""
    ti = inp.get("tool_input") or {}
    stage_id = state.get("stage", "scaffolding")
    signals = cfg.get("evidence_signals", {})

    field = WRITE_TOOLS.get(tool)
    if field:
        rel = rel_to_root(root, ti.get(field))
        if rel:
            for key, sig in signals.items():
                for pat in sig.get("write_glob", []):
                    if glob_match(rel, pat):
                        record_evidence(root, state, cfg, key, rel)
                        break

    if stage_id in EVIDENCE_STAGES:
        sig = signals.get("verification_evidence", {})
        if tool == "Bash":
            cmd = ti.get("command") or ""
            pat = sig.get("bash_pattern")
            if pat and re.search(pat, cmd):
                record_evidence(root, state, cfg, "verification_evidence", cmd.strip()[:120])
        if tool in sig.get("tools", []):
            record_evidence(root, state, cfg, "verification_evidence", "agent:" + tool)
        tp = sig.get("tool_pattern")
        if tp and re.search(tp, tool):
            record_evidence(root, state, cfg, "verification_evidence", "tool:" + tool)


def hook_stop(inp, root, state, cfg):
    stage_id = state.get("stage", "scaffolding")
    stage = stage_obj(cfg, stage_id)
    idx = stage_index(cfg, stage_id) + 1
    prompt_id = inp.get("prompt_id") or "-"
    already = state.setdefault("stop_blocks", {}).setdefault(prompt_id, [])
    msg = (inp.get("last_assistant_message") or "").strip()

    problems = []
    if msg and not re.match(r"^\[\s*%d\s*/\s*6\b" % idx, msg):
        problems.append(("prefix",
                         "말머리에 [%d/6 %s] 를 표시하지 않았다. 현재 단계를 말머리에 붙여 다시 답하라."
                         % (idx, stage["label"])))
    for key in stage.get("stop_requires", []):
        if not has_evidence(state, cfg, key):
            problems.append((key, "%s 단계를 끝낼 수 없다: %s"
                             % (stage["label"], CRITERIA_HELP.get(key, key))))

    problems = [p for p in problems if p[0] not in already]
    if not problems:
        return

    # 같은 프롬프트에서 같은 이유로 두 번 막지 않는다 (무한 루프 방지)
    already.extend(p[0] for p in problems)
    if len(state["stop_blocks"]) > 40:
        for k in list(state["stop_blocks"])[:-20]:
            del state["stop_blocks"][k]
    state["updated_at"] = now()
    jdump(os.path.join(root, STATE_REL), state)
    emit({"decision": "block", "reason": " / ".join(p[1] for p in problems)})


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
    state = jload(os.path.join(root, STATE_REL))
    cfg = load_config(root, plugin_root())
    if not isinstance(state, dict) or not isinstance(cfg, dict) or not cfg.get("stages"):
        return 0  # 설정이 깨졌으면 차단하지 않는다
    fn = HOOKS.get(inp.get("hook_event_name"))
    if fn:
        try:
            fn(inp, root, state, cfg)
        except Exception as exc:  # 훅이 세션을 막지 않게
            sys.stderr.write("step-six-harness: %s\n" % exc)
            return 1
    return 0


# ------------------------------------------------------------------------- cli

def plugin_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def arg_value(cmd, name):
    """따옴표가 살아 있는 raw 명령 문자열에서 --name 값을 뽑는다 (훅 경로 전용)."""
    m = re.search(r"--%s(?:=|\s+)(\"([^\"]*)\"|'([^']*)'|([^\s]+))" % name, cmd)
    if not m:
        return None
    return m.group(2) or m.group(3) or m.group(4)


def argv_value(argv, name):
    """이미 쉘이 분해한 argv 에서 --name 값을 뽑는다.

    CLI 경로에서 argv 를 다시 join 해 arg_value 로 파싱하면 공백이 든 사유가
    첫 단어에서 잘린다. 사유 전문을 보존하는 것이 이 하네스의 핵심이라 분리했다.
    """
    flag = "--" + name
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def argv_positional(argv):
    """--flag 의 값을 위치 인자로 오인하지 않게 분리한다."""
    out = []
    skip = False
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


def cli_status(root, state, cfg, argv):
    stage_id = state.get("stage", "scaffolding")
    idx = stage_index(cfg, stage_id) + 1
    stage = stage_obj(cfg, stage_id)
    print("cycle %s · 단계 %d/6 %s" % (state.get("cycle", 1), idx, stage["label"]))
    print("  요약: %s" % stage["summary"])
    print("  쓰기 허용: %s" % (", ".join(stage.get("write", [])) or "(없음)"))
    missing = exit_blockers(state, cfg, stage)
    if stage.get("exit_criteria"):
        done = [k for k in stage["exit_criteria"] if k not in missing]
        print("  종료 조건: 충족 %s / 미충족 %s"
              % (", ".join(done) or "-", ", ".join(missing) or "-"))
    ev = ev_bucket(state)
    if ev:
        print("  증거: %s" % ", ".join("%s×%d" % (k, len(v)) for k, v in ev.items()))
    for s in state.get("skips", []):
        if s.get("cycle") == state.get("cycle"):
            print("  스킵: %s — %s (%s)" % (s.get("stage"), s.get("reason"), s.get("at")))
    for g in state.get("grants", []):
        if g.get("uses_left", 0) > 0:
            print("  예외: %s (남은 %d회) — %s" % (g["glob"], g["uses_left"], g.get("reason")))
    return 0


def _set_stage(root, state, cfg, new_id, kind, reason=None):
    prev = state.get("stage")
    state["stage"] = new_id
    state["updated_at"] = now()
    state.setdefault("history", []).append(
        {"from": prev, "to": new_id, "kind": kind, "reason": reason,
         "cycle": state.get("cycle", 1), "at": now()})
    jdump(os.path.join(root, STATE_REL), state)


def cli_advance(root, state, cfg, argv):
    stage_id = state.get("stage", "scaffolding")
    stage = stage_obj(cfg, stage_id)
    missing = exit_blockers(state, cfg, stage)
    if missing:
        print("advance 거부 — %s 단계의 종료 조건이 남았다:" % stage["label"])
        for k in missing:
            print("  - %s: %s" % (k, CRITERIA_HELP.get(k, k)))
        print("정당한 사유가 있으면 `harness skip %s --reason \"...\"` 로 사람의 승인을 받아라." % stage_id)
        return 1
    idx = stage_index(cfg, stage_id)
    if idx + 1 >= len(cfg["stages"]):
        return cli_cycle(root, state, cfg, argv)
    nxt = cfg["stages"][idx + 1]
    _set_stage(root, state, cfg, nxt["id"], "advance")
    print("→ 단계 %d/6 %s" % (idx + 2, nxt["label"]))
    print("   %s" % nxt["summary"])
    return 0


def cli_skip(root, state, cfg, argv):
    """PreToolUse 가 이미 사람의 승인을 받은 뒤에만 여기까지 온다."""
    pos = argv_positional(argv)
    target = pos[0] if pos else None
    reason = argv_value(argv, "reason")
    if not target or not reason:
        print("사용법: harness skip <stage-id|+N|until:<stage-id>> --reason \"...\"")
        return 2
    ids = [s["id"] for s in cfg["stages"]]
    cur = stage_index(cfg, state.get("stage", "scaffolding"))
    if target.startswith("+"):
        dest = min(cur + int(target[1:]), len(ids) - 1)
    elif target.startswith("until:"):
        want = target.split(":", 1)[1]
        if want not in ids:
            print("알 수 없는 단계: %s" % want)
            return 2
        dest = ids.index(want)
    elif target in ids:
        dest = min(ids.index(target) + 1, len(ids) - 1)
    else:
        print("알 수 없는 대상: %s" % target)
        return 2
    if dest <= cur:
        print("뒤로 갈 때는 skip 이 아니라 `harness back` 이 필요하다 (미구현). 현재 단계 유지.")
        return 1
    skipped = ids[cur:dest]
    for sid in skipped:
        state.setdefault("skips", []).append(
            {"stage": sid, "reason": reason, "authorized_by": "user",
             "cycle": state.get("cycle", 1), "at": now()})
    _set_stage(root, state, cfg, ids[dest], "skip", reason)
    print("스킵(사용자 승인): %s" % ", ".join(skipped))
    print("사유: %s" % reason)
    print("→ 단계 %d/6 %s" % (dest + 1, cfg["stages"][dest]["label"]))
    return 0


def cli_allow(root, state, cfg, argv):
    pos = argv_positional(argv)
    glob = pos[0] if pos else None
    reason = argv_value(argv, "reason")
    uses = argv_value(argv, "uses")
    if not glob or not reason:
        print("사용법: harness allow <glob> --reason \"...\" [--uses N]")
        return 2
    state.setdefault("grants", []).append(
        {"glob": glob, "reason": reason, "uses_left": int(uses) if uses else 3,
         "cycle": state.get("cycle", 1), "at": now()})
    state["updated_at"] = now()
    jdump(os.path.join(root, STATE_REL), state)
    print("예외 등록(사용자 승인): %s — %s" % (glob, reason))
    return 0


def cli_approve_plan(root, state, cfg, argv):
    pos = argv_positional(argv)
    path = pos[0] if pos else None
    if not path:
        print("사용법: harness approve-plan <plan-file>")
        return 2
    rel = rel_to_root(root, path)
    if not rel or not os.path.isfile(os.path.join(root, rel)):
        print("계획 파일을 찾을 수 없다: %s" % path)
        return 2
    record_evidence(root, state, cfg, "plan_file", rel)
    record_evidence(root, state, cfg, "plan_approved", rel)
    print("계획 승인 기록: %s" % rel)
    return 0


def cli_cycle(root, state, cfg, argv):
    state["cycle"] = state.get("cycle", 1) + 1
    _set_stage(root, state, cfg, cfg["stages"][0]["id"], "cycle")
    print("새 사이클 %s 시작 → 단계 1/6 %s" % (state["cycle"], cfg["stages"][0]["label"]))
    return 0


CLI = {
    "status": cli_status,
    "advance": cli_advance,
    "skip": cli_skip,
    "allow": cli_allow,
    "approve-plan": cli_approve_plan,
    "cycle": cli_cycle,
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
    state = jload(os.path.join(root, STATE_REL))
    cfg = load_config(root, plugin_root())
    if not isinstance(state, dict) or not isinstance(cfg, dict):
        print("상태 또는 설정이 손상되었다: %s" % os.path.join(root, HARNESS_DIR), file=sys.stderr)
        return 1
    fn = CLI.get(cmd)
    if not fn:
        print("알 수 없는 명령: %s (%s)" % (cmd, ", ".join(sorted(CLI))), file=sys.stderr)
        return 2
    return fn(root, state, cfg, argv[1:])


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
    sp = os.path.join(root, STATE_REL)
    if not os.path.exists(sp):
        jdump(sp, default_state())
        created.append(STATE_REL)
    refresh_wrapper(root)

    gi = os.path.join(root, ".gitignore")
    want = [".claude/harness/state.json", ".claude/harness/bin/"]
    have = open(gi, encoding="utf-8").read() if os.path.isfile(gi) else ""
    add = [w for w in want if w not in have]
    if add:
        with open(gi, "a", encoding="utf-8") as fh:
            if have and not have.endswith("\n"):
                fh.write("\n")
            fh.write("\n# step-six-harness (기계 상태 — 커밋하지 않는다)\n")
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
