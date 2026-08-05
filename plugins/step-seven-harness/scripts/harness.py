#!/usr/bin/env python3
"""step-seven-harness engine.

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
BASH_MUTATORS = re.compile(
    r"(^|[;&|]\s*)(rm|mv|cp|mkdir|touch|tee|dd|truncate|install|ln|shred)\b"
    r"|>\s*\S|sed\s+-i"
    # find 는 -delete/-exec 로 쓴다. 읽기 명령으로 분류했다가 통째로 통과했다.
    r"|\s-delete\b|\s-exec(dir)?\b")
# 두 번째 토큰까지 잡는다. `loop new` 는 루프를 닫고 새로 만들므로 모든 단계
# 게이트를 우회하는데, 첫 토큰만 보면 subcommand 가 'loop' 로 잡혀 동의 판정이
# 아예 일어나지 않았다 — 승격 게이트가 그 구멍으로 그대로 새어나갔다.
CTRL_SUB2 = {"loop": ("new", "adopt")}
CTRL_NAMES = ("harness", "harness.py")
# 따옴표 안은 값이다. 먼저 한 토큰으로 뭉개야 `--reason "a b"` 의 'b' 가 위치
# 인자로 오인되지 않는다 — 그 오인이 subcommand 판정을 틀리게 만들었다.
QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')
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
    "retro_keys": "회고 검색 키 확인",
    "plan_mode_exit": "plan mode 종료 관측",
    "stop_continue": "턴 이어붙임",
    "stop_stalled": "진전 없어 이어붙임 중단",
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

class Cfg(dict):
    """stages.json 접근자. 경로 문법과 형 강제를 한 곳에 모은다.

    dict 를 상속하므로 `cfg["stages"]` 나 `isinstance(cfg, dict)` 가 그대로 동작한다.
    이 클래스가 없을 때는 설정을 읽는 방식이 세 가지였고, `int()` 강제 변환
    try/except 가 일곱 곳에 복붙돼 있었다 — 기본값이 어긋나도 알 수 없는 상태였다.
    """

    def at(self, path, default=None):
        cur = self
        for part in path.split("."):
            if not isinstance(cur, dict):
                return default
            cur = cur.get(part)
            if cur is None:
                return default
        return cur

    def num(self, path, default, low=None):
        try:
            n = int(self.at(path, default))
        except (TypeError, ValueError):
            n = int(default)
        return n if low is None else max(low, n)

    def seq(self, path, default=()):
        v = self.at(path)
        return list(v) if isinstance(v, (list, tuple)) else list(default)

    def obj(self, path):
        v = self.at(path)
        return v if isinstance(v, dict) else {}


class Ctx(object):
    """한 번의 훅/명령 실행이 필요한 전부. 이전에는 이 다섯 개를 위치 인자로
    열일곱 함수에 꿰어 날랐다 — 어떤 함수가 무엇을 만지는지 알 수 없었다.

    각 함수는 첫 줄에서 **실제로 쓰는 것만** 풀어쓴다. 그 한 줄이 의존 범위를
    문서화한다: `cli_metrics` 는 con·cfg 만 쓰고 파일시스템을 건드리지 않는다.
    """

    __slots__ = ("con", "cfg", "root", "lid", "sid")

    def __init__(self, con, cfg, root, lid, sid):
        self.con, self.cfg, self.root = con, cfg, root
        self.lid, self.sid = lid, sid


def load_config(root, plugin_root_dir=None):
    """프로젝트의 stages.json. 없는 **최상위 영역만** 템플릿에서 채운다.

    왜 채우나: `install_templates` 는 기존 stages.json 을 덮지 않는다 — 사용자가
    고친 문서이기 때문이다. 그래서 새 설정 영역을 추가하면 **기존 설치는 영원히
    받지 못한다.** 규칙이 코드에 있을 때는 티가 안 났지만, 어휘를 설정으로 옮기는
    순간 그건 게이트가 조용히 어긋나는 것이다.

    왜 최상위 영역만인가: 사용자의 작업 방식이 "마찰이 크면 stages.json 에서
    덜어낸다"이고, 그건 영역 **안의 항목**을 지우는 일이다. 항목 단위로 병합하면
    지운 것이 되살아나 그 자유를 빼앗는다. 그래서 영역이 통째로 없을 때만 채우고,
    비워 둔 것(`[]`, `{}`)은 비운 대로 존중한다.
    """
    path = os.path.join(root, CONFIG_REL)
    cfg = jload(path)
    # **있는데 못 읽는 것과 없는 것은 다르다.** 손상된 문서를 템플릿으로 갈아치우면
    # 사용자가 덜어낸 규칙이 말없이 되살아나고, 그 사람은 이유 모를 차단만 본다.
    # 없으면(설치 전) 템플릿을 쓰고, 깨졌으면 그대로 알린다.
    if cfg is None and os.path.exists(path):
        return None
    tpl_dir = plugin_root_dir or plugin_root()
    tpl = jload(os.path.join(tpl_dir, "templates", "stages.json"))
    if cfg is None:
        cfg = tpl
    elif isinstance(cfg, dict) and isinstance(tpl, dict):
        had_criteria = "criteria" in cfg
        for k, v in tpl.items():
            cfg.setdefault(k, v)
        if not had_criteria:
            _adopt_evidence_signals(cfg)
    return Cfg(cfg) if isinstance(cfg, dict) else cfg


def _adopt_evidence_signals(cfg):
    """`evidence_signals` 를 커스터마이즈한 문서의 그 값을 `criteria` 로 옮긴다.

    0.31.0 에서 `evidence_signals` 가 `criteria` 로 흡수됐다. 템플릿 채움만 하면
    `criteria` 는 들어오지만 **사용자가 고쳐 둔 옛 값은 조용히 무시된다** —
    `bash_pattern` 에 자기 빌드 명령을 넣어 둔 사람은 그게 사라진 것을 모른 채
    검증 게이트가 안 열리는 것만 보게 된다. 이름을 바꿨으면 옮겨주는 것이 맞다.

    새 어휘 필드(`satisfied_by`, `help` 등)는 템플릿 값을 유지한다. 사용자가
    고친 것은 신호 필드뿐이므로 그것만 덮는다.
    """
    old = cfg.get("evidence_signals")
    crit = cfg.get("criteria")
    if not isinstance(old, dict) or not isinstance(crit, dict):
        return
    for kind, sig in old.items():
        if not isinstance(sig, dict):
            continue
        target = crit.setdefault(kind, {"satisfied_by": "cli"})
        if isinstance(target, dict):
            target.update(sig)


SATISFIED_BY = ("cli", "file", "observed", "no_pending_promotions")

# 아래 다섯 개는 **정책·내용**이라 설정이 정한다. 엔진에 박아 두면 프로젝트마다 다른
# 것을 하나로 강요하게 되고, 특히 recall_dirs 는 조용한 결함이었다 — 설정에 폴더를
# 더해도 recall 이 그 폴더를 못 봐서, 안내대로 고친 사람이 **다시 안 읽히는 기록**을
# 쌓게 됐다. 파일은 있고 키워드도 맞는데 영원히 안 나온다. 복리의 반대다.
RECALL_DIRS_DEFAULT = ("retrospect", "learning", "troubleshooting")
INDEX_NAMES_DEFAULT = ("INDEX.md", "README.md")
BASH_READERS_DEFAULT = ("cat", "less", "more", "head", "tail", "grep", "rg", "wc",
                        "file", "stat", "ls", "diff", "shasum", "md5", "md5sum", "cut")
BASH_INTERPRETERS_DEFAULT = ("python", "python3", "sh", "bash", "zsh", "env",
                             "exec", "node")


def recall_dirs(cfg):
    """`recall` 이 읽는 폴더. 이 밖의 기록은 **존재하지 않는 것과 같다.**

    `dev_subdirs` 전체를 읽지 않는 것은 의도다 — `plan`·`log`·`scratch` 는 교훈이
    아니라 산출물이고, 그것까지 끌어오면 조회가 소음으로 덮인다.
    """
    return tuple(cfg.seq("recall.dirs", RECALL_DIRS_DEFAULT))


def index_names(cfg):
    return tuple(cfg.seq("recall.index_names", INDEX_NAMES_DEFAULT))


def recall_read_bytes(cfg):
    return cfg.num("recall.read_bytes", 50000, low=1000)


def retro_questions(cfg):
    """회고에서 물을 것. 무엇을 묻는지가 회고의 값을 정하므로 사람이 정해야 한다."""
    out = []
    for it in cfg.seq("retro_questions"):
        if isinstance(it, dict) and it.get("q"):
            out.append((it["q"], it.get("why") or ""))
    return out


def consent_map(cfg):
    """사람의 승인이 필요한 하네스 명령 → 무엇을 승인하는지의 설명.

    설정이라는 것은 **줄일 수 있다는 뜻**이다. `allow` 다이얼로그가 시끄러우면
    하네스를 끄는 대신 그 항목을 덜어낼 수 있다.
    """
    m = cfg.obj("consent")
    return {k: v for k, v in m.items() if isinstance(v, str)} if m else {}


def bash_mutator_re(cfg):
    pat = cfg.at("bash.mutator_pattern")
    try:
        return re.compile(pat) if pat else BASH_MUTATORS
    except re.error:
        return BASH_MUTATORS       # 잘못된 정규식으로 게이트를 열지 않는다


def config_problems(cfg):
    """설정의 **오타**를 찾는다. 규칙 위반이 아니라 어휘 오류만 본다.

    왜 필요한가: 어휘를 설정으로 옮기면 오타가 새로운 실패 방식이 된다. 그리고 그
    실패는 조용하다 — `satisfied_by: "fille"` 은 파일 검사를 말없이 끄고,
    `panels: ["work_candidatez"]` 는 아무것도 하지 않는다. 사용자는 자기가 설정한
    것이 동작한다고 믿는다. 게이트가 조용히 어긋나는 것은 이 하네스가 반복해서
    잡아온 부류이고, 어휘화로 그 표면을 넓혔으니 함께 막아야 한다.

    막지는 않는다 — 설정이 조금 틀렸다고 세션을 벽돌로 만들면 그게 더 나쁘다.
    무엇이 무시되고 있는지 **말한다.**
    """
    out = []
    crit = cfg.obj("criteria")
    for name, spec in sorted(crit.items()):
        if not isinstance(spec, dict):
            out.append("criteria.%s 가 객체가 아니다 — 이 조건은 무시된다" % name)
            continue
        how = spec.get("satisfied_by")
        if how is None:
            out.append("criteria.%s 에 satisfied_by 가 없다 — cli 로 간주한다" % name)
        elif how not in SATISFIED_BY:
            out.append("criteria.%s.satisfied_by='%s' 는 모르는 값이다 (%s 중 하나) "
                       "— 판정이 cli 로 떨어진다"
                       % (name, how, "/".join(SATISFIED_BY)))
        if how == "file" and not spec.get("write_glob"):
            out.append("criteria.%s 는 satisfied_by=file 인데 write_glob 이 없다 "
                       "— 어떤 파일도 이 조건을 채우지 못한다" % name)
    # 쓰기 규칙. 여기 오타는 특히 조용하다 — 규칙이 아무것도 막지 않거나,
    # 반대로 아무 경로에도 해당하지 않아 통째로 죽는다. 둘 다 티가 안 난다.
    fr = cfg.obj("folder_rules")
    seen_ids = set()
    for i, r in enumerate(cfg.get("write_rules") or []):
        at = "write_rules[%d]" % i
        if not isinstance(r, dict):
            out.append("%s 가 객체가 아니다 — 이 규칙은 무시된다" % at)
            continue
        rid = r.get("id")
        if not rid:
            out.append("%s 에 id 가 없다 — 차단 기록이 '?' 로 남아 승격에 쓸 수 없다" % at)
        elif rid in seen_ids:
            out.append("%s 의 id '%s' 가 중복이다 — 통계가 두 규칙을 한 덩어리로 센다"
                       % (at, rid))
        else:
            seen_ids.add(rid)
        at = "write_rules[%s]" % (rid or i)
        if not r.get("deny"):
            out.append("%s 에 deny 메시지가 없다 — 막으면서 무엇을 하라는 말이 없다" % at)
        when, req = r.get("when") or {}, r.get("require") or {}
        for k in when:
            if k not in WRITE_SELECTORS:
                out.append("%s.when 의 '%s' 는 모르는 선택자다 (%s 중 하나) "
                           "— 이 조건은 무시된다" % (at, k, "/".join(WRITE_SELECTORS)))
        tests = [k for k in req if k in WRITE_TESTS]
        unknown = [k for k in req if k not in WRITE_TESTS]
        for k in unknown:
            out.append("%s.require 의 '%s' 는 모르는 판정이다 (%s 중 하나)"
                       % (at, k, "/".join(WRITE_TESTS)))
        if not tests:
            out.append("%s 에 판정이 없다 — 이 규칙은 아무것도 막지 않는다" % at)
        elif len(tests) > 1:
            out.append("%s 에 판정이 %d개다 (%s) — 하나만 쓴다. 첫 것만 적용된다"
                       % (at, len(tests), ", ".join(sorted(tests))))
        pname = req.get("predicate")
        if pname and pname not in WRITE_PREDICATES:
            out.append("%s.require.predicate '%s' 라는 파이썬 술어가 없다 "
                       "— 이 규칙은 아무것도 막지 않는다" % (at, pname))
        # folder_rules 의 어느 목록을 가리키는 자리들. 없는 이름을 가리키면 조용히 죽는다.
        for field, holder in (("subdir_in", when), ("basename_not_in", when),
                              ("subdir_in", req), ("not_matching", req),
                              ("stage_in", req)):
            name = holder.get(field)
            if isinstance(name, str) and name not in fr:
                out.append("%s 의 %s='%s' 가 folder_rules 에 없다 — %s"
                           % (at, field, name,
                              "이 규칙이 어떤 경로에도 해당하지 않는다"
                              if holder is when else "아무것도 막지 못한다"))
        name = req.get("basename_matches")
        if isinstance(name, str) and cfg.at(name) is None:
            out.append("%s.require.basename_matches='%s' 가 설정에 없다 "
                       "— 아무것도 막지 못한다" % (at, name))

    # recall 대상 폴더. 여기 오타가 나면 그 폴더의 기록은 **영원히 안 나온다** —
    # 파일은 있고 키워드도 맞는데 조회에 안 걸린다. 조용한 결함으로 실제로 있었다.
    dev_dirs = set(cfg.seq("folder_rules.dev_subdirs"))
    for d in cfg.seq("recall.dirs", RECALL_DIRS_DEFAULT):
        if dev_dirs and d not in dev_dirs:
            out.append("recall.dirs 의 '%s' 가 folder_rules.dev_subdirs 에 없다 "
                       "— 그 폴더에는 쓸 수 없으므로 조회할 것도 없다" % d)
    if not cfg.seq("recall.dirs", RECALL_DIRS_DEFAULT):
        out.append("recall.dirs 가 비어 있다 — 과거 회고를 하나도 찾지 못한다")
    if not retro_questions(cfg):
        out.append("retro_questions 가 비어 있다 — 회고에서 무엇을 물을지가 없다")
    for i, it in enumerate(cfg.seq("retro_questions")):
        if not isinstance(it, dict) or not it.get("q"):
            out.append("retro_questions[%d] 에 q 가 없다 — 이 질문은 무시된다" % i)
    pat = cfg.at("bash.mutator_pattern")
    if pat:
        try:
            re.compile(pat)
        except re.error as e:
            out.append("bash.mutator_pattern 이 잘못된 정규식이다 (%s) "
                       "— 기본 패턴으로 되돌아간다" % e)

    known = set(crit)
    for st in cfg.get("stages") or []:
        if not isinstance(st, dict):
            continue
        sid = st.get("id", "?")
        for field in ("exit_criteria", "stop_requires", "skip_requires"):
            for k in st.get(field) or []:
                if k not in known:
                    out.append("stages[%s].%s 의 '%s' 가 criteria 에 없다 "
                               "— 채울 방법이 없어 이 단계를 끝낼 수 없다"
                               % (sid, field, k))
        for p in st.get("panels") or []:
            if p not in PANELS:
                out.append("stages[%s].panels 의 '%s' 는 모르는 패널이다 (%s 중 하나) "
                           "— 조용히 무시된다"
                           % (sid, p, "/".join(sorted(PANELS))))
    return out


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


def events_where(con, kinds=None, loop_id=None, rule=None, target=None,
                 from_epoch=None, to_epoch=None):
    """이벤트를 고른다. 시각 경계는 **절대 시각**으로만 비교한다.

    이 함수가 없을 때는 같은 관용구가 네 곳에 복붙돼 있었고, 그중 두 곳이
    SQL 문자열 비교를 쓰고 있었다. 오프셋 유무나 공백 구분 형식이 섞이면
    사전순과 실제 순서가 어긋나서 창 밖의 이벤트가 안으로 들어온다 —
    두 릴리스에 걸쳐 같은 버그를 두 번 냈다. 경계 판정은 여기 한 곳에만 있다.
    """
    sql = ["SELECT at, loop_id, stage, kind, rule, target, detail FROM event WHERE 1=1"]
    params = []
    if kinds:
        sql.append("AND kind IN (%s)" % ",".join("?" * len(kinds)))
        params += list(kinds)
    for col, val in (("loop_id", loop_id), ("rule", rule), ("target", target)):
        if val is not None:
            sql.append("AND IFNULL(%s,'-') = ?" % col)
            params.append(val)
    rows = con.execute(" ".join(sql), params).fetchall()
    if from_epoch is None and to_epoch is None:
        return rows
    out = []
    for r in rows:
        t = ts_epoch(r["at"])
        if from_epoch is not None and t < from_epoch:
            continue
        if to_epoch is not None and t >= to_epoch:
            continue
        out.append(r)
    return out


def loops_created_after(con, epoch):
    """그 시각 이후에 만들어진 작업 수. created_at 도 문자열로 비교하면 안 된다."""
    return sum(1 for r in con.execute("SELECT created_at FROM loop")
               if ts_epoch(r["created_at"]) > epoch)


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

def promo_match(item):
    """block 은 규칙이, 나머지는 대상이 의미 있는 키다. events_where 용 필터."""
    col = "rule" if item["kind"] == "block" else "target"
    return {col: item["name"]}


def promo_key(kind, rule, target):
    """승격 단위의 키. block 은 규칙이, tool_fail 은 정규화된 명령이 의미 있는 키다.

    stats 의 묶음 기준과 같아야 한다 — 다르면 "3개 작업에서 반복"이라고 보여준
    항목과 승격을 요구하는 항목이 어긋난다.
    """
    return "%s:%s" % (kind, (rule if kind == "block" else target) or "-")


def repeated_items(con, cfg):
    """여러 작업에서 반복된 항목. 한 작업 안의 반복은 우연일 수 있다."""
    kinds = cfg.seq("promotion.kinds", ("block", "tool_fail"))
    min_loops = cfg.num("promotion.min_loops", 3, low=2)
    # 승격할 수 없는 규칙은 후보에서 뺀다. `no_reason`·`bypass_mode`·`protected` 는
    # 모델이 게이트를 우회하려 한 기록이다 — 하네스가 제대로 동작한 증거이지
    # 기계화할 습관이 아니다. 이걸 승격 대상으로 올리면 "우회 시도를 승격하라"는
    # 뜻이 되고, 사용자가 봐야 할 규율 신호가 결정 절차로 세탁된다. stats 에는 남는다.
    skip = set(cfg.seq("promotion.exclude_rules",
                       ("no_reason", "bypass_mode", "protected", "protected_bash")))
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


def recurrence(con, p):
    """승격 이후 같은 항목이 다시 걸렸는지 → (횟수, 작업 수).

    승격이 통했는지의 유일한 객관 증거다. 경계를 1초 뒤로 두는 이유: 승격을
    기록한 그 초에 남은 이벤트는 승격 **이전**의 사실이므로, 재발로 세면 방금
    내린 결정이 즉시 무효화된다.
    """
    item = {"kind": p["kind"], "name": p["key"].split(":", 1)[1]}
    base = p["recheck_at"] or p["at"]
    hits = events_where(con, kinds=(item["kind"],),
                        from_epoch=(ts_epoch(base) + 1) if base else 0.0,
                        **promo_match(item))
    return len(hits), len({r["loop_id"] for r in hits})


def is_regressed(con, cfg, p):
    """저장된 maturity 를 믿지 않고 지금 계산한다.

    sync_promotions 를 아무도 실행하지 않은 세션에서도 게이트가 맞아야 한다.
    저장값만 보면 `stats`/`tidy`/`promote` 를 부르지 않은 채 Compounding 을
    통과할 수 있다 — 실제로 그렇게 새어나갔다.
    """
    if p["maturity"] == "regressed":
        return True
    return recurrence(con, p)[1] >= cfg.num("promotion.reopen_after_loops", 2, low=1)


def sync_promotions(con, cfg):
    """성숙도를 재계산한다. 결정론적이다 — LLM 을 끼우면 등급이 표류한다.

    established → proven: 승격 후 재발이 없고 그 사이 작업이 N개 지났다.
    established → regressed: 승격 후에도 M개 작업에서 다시 걸렸다. 승격이
    통하지 않았다는 뜻이므로 다시 결정 대상으로 돌린다.
    """
    proven_after = cfg.num("promotion.proven_after_loops", 3, low=1)
    reopen_after = cfg.num("promotion.reopen_after_loops", 2, low=1)
    changed = []
    for p in con.execute("SELECT * FROM promotion").fetchall():
        if p["maturity"] == "regressed":
            continue
        _, loops = recurrence(con, p)
        if loops >= reopen_after:
            con.execute("UPDATE promotion SET maturity='regressed' WHERE key=?",
                        (p["key"],))
            changed.append((p["key"], "regressed"))
            continue
        if p["maturity"] == "proven" or p["decision"] == "declined":
            continue
        if loops == 0 and loops_created_after(con, ts_epoch(p["at"])) >= proven_after:
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
        limit = cfg.num("promotion.max_per_cycle", 3, low=1)
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
    """이번 회차 창의 시작 **epoch**. `>=` 로 비교한다.

    시각 정밀도가 초 단위라 경계 처리가 필요하다. 회차 종료 기록이 있으면 그
    시각을 **배타적**으로 둔다(+1초) — 같은 초에 남은 이벤트는 앞 회차의 사실이고,
    포함시키면 한 회차의 마지막 이벤트가 다음 회차 창에 겹쳐 두 번 세어진다.
    회차 종료 기록이 없으면(1회차) 작업 생성 시각을 포함한다.
    """
    row = con.execute(
        "SELECT MAX(at) a FROM event WHERE loop_id=? AND kind='cycle_close'",
        (lid,)).fetchone()
    if row and row["a"]:
        return ts_epoch(row["a"]) + 1
    row = con.execute("SELECT created_at FROM loop WHERE id=?", (lid,)).fetchone()
    return ts_epoch(row["created_at"] if row else None)


def cycle_counters(con, lid, lo):
    """이 회차의 마찰 수치. 회피 지표를 반드시 함께 담는다 — 차단만 보면 속는다.

    `lo` 는 cycle_window_start 가 준 epoch 다 (경계 처리가 거기 들어 있다).
    """
    rows = events_where(con, loop_id=lid, from_epoch=lo)
    tally = {}
    for r in rows:
        tally[r["kind"]] = tally.get(r["kind"], 0) + 1

    # 모드 사전 승인(bypassPermissions)은 **회피가 아니다.** 사용자가 고른 모드다.
    # 같이 세면 무인 실행이 곧바로 "게이트가 연극이 되고 있다" 로 판정된다 —
    # 실제로는 사람이 그렇게 하라고 지시한 것인데. 열을 갈라 둘 다 보이게 한다.
    preauth = sum(1 for r in rows
                  if r["kind"] == "bypass" and r["rule"] == "bypass_mode")

    # 재편집 최대치: 한 파일을 몇 번 고쳤나. 구조 냄새의 대리 지표.
    edits = {}
    for r in rows:
        if r["kind"] == "edit":
            edits[r["target"]] = edits.get(r["target"], 0) + 1

    # 반복 실패: 이 회차의 실패 중 그 명령이 **이전에도** 실패한 적 있는 것.
    # 작업 경계를 넘어 센다 — 지난 작업에서 실패한 명령이 또 실패하는 것이 요점이다.
    seen_before = {r["target"] for r in
                   events_where(con, kinds=("tool_fail",), to_epoch=lo)}
    refails, seen_now = 0, set()
    for r in rows:
        if r["kind"] != "tool_fail":
            continue
        if r["target"] in seen_before or r["target"] in seen_now:
            refails += 1
        seen_now.add(r["target"])

    return {
        "dur": max(0, int(time.time() - lo)) if lo else 0,
        "blocks": tally.get("block", 0),
        "fails": tally.get("tool_fail", 0),
        "refails": refails,
        "churn": max(edits.values()) if edits else 0,
        "edits": tally.get("edit", 0),
        "gates": tally.get("stop_gate", 0),
        "bypass": tally.get("bypass", 0) - preauth,
        "preauth": preauth,
        "skips": tally.get("skip", 0),
        "declines": tally.get("promote_declined", 0),
        "promotes": tally.get("promote", 0),
    }


def record_cycle_close(con, cfg, lid, sid):
    """회차 경계에서 그 회차의 집계를 한 줄로 남긴다.

    stage 행은 작업이 닫힐 때 삭제되므로 나중에 회차별 비용을 되살릴 수 없다.
    경계에서 스냅샷을 남기면 event 는 작업이 닫혀도 살아남아 측정이 가능해진다.
    """
    c = cycle_counters(con, lid, cycle_window_start(con, lid))
    c["cycle"] = cycle_of(con, lid)
    record_event(con, lid, sid, "cycle_close", str(c["cycle"]),
                 "%s-%d" % (lid, c["cycle"]), json.dumps(c, ensure_ascii=False))
    return c


def verify_globs(cfg, as_kind):
    return cfg.seq("promotion.verify_globs.%s" % as_kind) or None


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
    excl = cfg.seq("promotion.verify_exclude")
    for r in events_where(con, kinds=("edit",), loop_id=lid,
                          from_epoch=cycle_window_start(con, lid)):
        rel = r["target"] or ""
        if any(glob_match(rel, p) for p in excl):
            continue
        if any(glob_match(rel, p) for p in pats):
            return True
    return False


# ----------------------------------------------------------------- retrospect
#
# 회고는 파일이 있는지만 봤고 **무엇을 묻는지는 설계된 적이 없었다.** 밀어주던 것이
# 전부 하네스 내부 사정(어떤 규칙에 걸렸나, 어떤 파일을 다시 고쳤나)이라, "왜 내
# 규칙을 어겼나"를 묻고 "무엇을 배웠나"는 묻지 않았다.
#
# 그리고 형식이 기계적으로 중요하다. 회고는 나중에 **정규화된 명령·규칙 이름으로
# 텍스트 검색**되어 찾아진다. 같은 사실을 담아도 그 토큰이 글자 그대로 없으면
# 영원히 안 찾아진다 — 실험으로 확인했다. 그래서 키를 알려주고 들어갔는지 본다.
#
# 통찰의 질은 채점하지 않는다(판단이다). 찾아지는지만 확인한다(기계적 사실이다).

def cycle_search_keys(con, lid, lo, limit=6):
    """이 회차에 관측된 것들의 **검색 키**.

    나중에 실패 지점 주입과 `recall` 이 바로 이 문자열로 회고를 찾는다. 그러니
    회고에 이 문자열이 글자 그대로 들어 있어야 한다.
    """
    keys = []
    for r in events_where(con, kinds=("tool_fail", "block"), loop_id=lid,
                          from_epoch=lo):
        k = r["target"] if r["kind"] == "tool_fail" else r["rule"]
        if k and k not in keys:
            keys.append(k)
    return keys[:limit]


def retro_files_of_cycle(con, cfg, root, lid):
    """이 회차가 쓴 회고·학습 파일. 파일명 접두사로 가른다."""
    pre = file_prefix(con, lid)
    out = []
    for sub in recall_dirs(cfg):
        d = os.path.join(root, ".dev", sub)
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if n.startswith(pre) and os.path.isfile(os.path.join(d, n)):
                out.append(os.path.join(d, n))
    return out


def retro_key_report(con, cfg, root, lid, lo):
    """(키, 찾은 키, 못 찾은 키). 검색과 **같은 범위**를 읽어 확인한다."""
    keys = cycle_search_keys(con, lid, lo)
    if not keys:
        return [], [], []
    hay = ""
    for path in retro_files_of_cycle(con, cfg, root, lid):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                hay += "\n" + fh.read(recall_read_bytes(cfg)).lower()
        except OSError:
            continue
    found = [k for k in keys if k.lower() in hay]
    return keys, found, [k for k in keys if k not in found]


def work_candidates(con, cfg, root, limit=6):
    """하네스가 **자기 기록에서** 아는 할 일. Selection 의 작업 후보다.

    "새 작업이 없다" 가 "할 일이 없다" 는 뜻이 아니다. 승격 결정이 밀려 있고,
    통하지 않은 승격이 있고, 인덱스가 낡았고, 예산이 찼다면 그건 전부 복리를
    유지하는 일이다. 이걸 내놓지 않으면 무인 실행이 Selection 에서 멈춘다 —
    사람에게 물으라고 말해봐야 무인 실행에는 물을 사람이 없다.
    """
    out = []

    def add(kind, what, how):
        if len(out) < limit:
            out.append({"kind": kind, "what": what, "how": how})

    try:
        for it in pending_promotions(con, cfg):
            add("승격 결정", "'%s' 가 작업 %d개에서 반복된다 — 훅·구조로 올릴지 결정"
                % (it["key"], it["loops"]),
                "harness promote %s --as hook --note \"...\"" % it["key"])
    except Exception:
        pass
    try:
        for r in con.execute("SELECT key, decision, note FROM promotion "
                             "WHERE maturity='regressed'"):
            add("재발한 승격", "'%s' 는 %s 로 승격했는데 다시 걸렸다 — 그 방법이 통하지 "
                "않았다" % (r["key"], r["decision"]),
                "원인을 다시 보고 `harness promote %s` 로 다시 결정" % r["key"])
    except Exception:
        pass
    try:
        rep = tidy_report(con, cfg, root)
        for d, note in rep["dirs"]:
            add("기록 정리", "%s %s" % (d, note), "인덱스를 만들거나 갱신 (Scaffolding)")
        if rep["groups"]:
            add("기록 정리", "한 작업이 여러 파일을 남긴 묶음 %d개 — 하나로 병합"
                % len(rep["groups"]), "harness tidy 로 목록 확인 (Scaffolding)")
        if rep["stale"]:
            add("기록 정리", "닫힌 작업의 오래된 파일 %d개 — 인덱스에 요약하고 정리"
                % len(rep["stale"]), "harness tidy 로 목록 확인 (Scaffolding)")
        if rep["learned"] and rep["learned"][0] >= rep["learned"][1]:
            add("예산", "LEARNED.md 가 %d/%d 줄로 찼다 — 한 줄을 비워야 새 규칙이 들어간다"
                % rep["learned"], "harness promote <기존키> --decline --reason \"...\"")
    except Exception:
        pass
    return out


def render_work_candidates(items, mode_note=True):
    if not items:
        return
    print("\n하네스가 아는 할 일 (%d개) — 새 작업이 없다면 여기서 고를 수 있다:"
          % len(items))
    for i, it in enumerate(items, 1):
        print("  %d. [%s] %s" % (i, it["kind"], it["what"]))
        print("     → %s" % it["how"])
    if mode_note:
        print("  고르면 `harness loop intent \"...\"` 와 `harness loop done-when \"...\"` 로 "
              "기록하고 진행하라. 이것들도 정말 필요 없으면 그렇다고 말하고 멈춰라.")


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
    return cfg.num("promotion.learned_max_lines", 20, low=1)


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


def fs_evidence(cfg, root, prefix, kind):
    """그 산출물이 **디스크에 실제로 있나**. 있으면 상대 경로, 없으면 None.

    증거를 PostToolUse 관측에만 의존하면 훅이 없는 환경에서 조건이 영원히 안
    채워진다. 훅이 없는 환경은 예외가 아니라 다수다 — 다른 에이전트 도구
    (Codex 는 셸만 가로챈다), 사람이 직접 쓴 파일, 훅이 실패한 세션.
    파일 존재는 관측 없이도 알 수 있는 사실이므로 관측을 기다리지 않고 본다.

    이 회차의 접두사를 **요구한다**. 요구하지 않으면 지난 회차의 계획서가
    이번 회차의 게이트를 열고, 그건 사람 없이 게이트가 열리는 것이다.
    접두사가 면제된 누적 문서(INDEX.md)도 이 요구에 걸려 제외된다 — 맞다,
    인덱스는 이번 회차가 무엇을 했다는 증거가 아니다.
    """
    if not prefix:
        return None
    for pat in cfg.seq("criteria.%s.write_glob" % kind):
        # 글롭의 앞쪽 리터럴만 떼어 그 디렉터리부터 걷는다. 저장소 전체를 걷지 않는다.
        base = pat.split("*")[0].rstrip("/")
        d = os.path.join(root, base)
        if not os.path.isdir(d):
            continue
        for dirpath, _, names in os.walk(d):
            for n in sorted(names):
                if not n.startswith(prefix):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, n), root).replace(os.sep, "/")
                if glob_match(rel, pat):
                    return rel
    return None


def exit_blockers(con, cfg, root, lid, sid):
    return [k for k in stage_obj(cfg, sid).get("exit_criteria", [])
            if not criterion_met(con, cfg, root, lid, k)]


def criterion_met(con, cfg, root, lid, kind):
    """종료 조건 충족 여부. **어떻게 판정하는지는 어휘가 정한다.**

    `criteria.<이름>.satisfied_by` 가 판정 방식을 고른다. 예전에는 이 함수가
    조건 이름을 알고 있었고(`promotion_decided` 특수 분기), 그래서 조건을 더하거나
    이름을 바꾸려면 파이썬을 고쳐야 했다. 엔진이 알아야 하는 것은 **방식**이지
    이름이 아니다.

      cli                    사람·모델이 명령으로 기록한다 (evidence 행)
      file                   산출물이 디스크에 있으면 충족 (관측 불필요)
      observed               도구 사용을 관측해 적립한다 (실패한 실행은 제외)
      no_pending_promotions  프로젝트 전체의 미결 승격이 없으면 충족

    file·observed 도 evidence 행이 있으면 먼저 인정한다 — 관측이 잡혔다면 그게
    가장 이른 사실이다. 게이트는 **아무도 아무것도 미리 실행하지 않아도** 맞아야
    한다(is_regressed 와 같은 원칙). 여기서 evidence 행을 새로 쓰지는 않는다.
    판정 경로가 쓰기를 하면 훅의 트랜잭션 상태에 얹혀 실패할 수 있고, 판정은
    매번 다시 하면 되는 계산이다.
    """
    how = cfg.at("criteria.%s.satisfied_by" % kind, "cli")
    if how == "no_pending_promotions":
        # evidence 행으로 심어두면 안 된다 — 다음 회차에 새로 생긴 반복을 놓친다.
        return not pending_promotions(con, cfg)
    if has_evidence(con, lid, kind):
        return True
    if how == "file":
        return fs_evidence(cfg, root, file_prefix(con, lid), kind) is not None
    return False


def criterion_help(cfg, kind):
    return cfg.at("criteria.%s.help" % kind, kind)


def human_criteria(cfg):
    """사람만 채울 수 있는 조건. 이것이 남았으면 턴을 밀지 않는다 —
    밀면 모델이 만들 수 없는 것을 만들려 애쓰고, 그 시도가 매번 다이얼로그가 된다."""
    return tuple(k for k, v in (cfg.obj("criteria") or {}).items()
                 if isinstance(v, dict) and v.get("human"))


def evidence_stages(cfg, kind="verification_evidence"):
    """그 증거를 관측해 적립하는 단계들."""
    return tuple(cfg.seq("criteria.%s.stages" % kind))


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


class WriteReq(object):
    """쓰기 한 건에 대한 판정 요청. 규칙 함수들이 공유하는 계산을 한 번만 한다."""

    __slots__ = ("ctx", "rel", "cls", "stage", "lbl", "grant", "parts")

    def __init__(self, ctx, rel):
        self.ctx, self.rel = ctx, rel
        self.cls = classify(rel, ctx.cfg)
        self.stage = stage_obj(ctx.cfg, ctx.sid)
        self.lbl = label_of(ctx.cfg, ctx.sid)
        self.grant = find_grant(ctx.con, ctx.lid, rel)
        self.parts = rel.split("/")

    def seq(self, name, default=()):
        return self.ctx.cfg.seq("folder_rules." + name, default)


# 규칙 하나는 "위반 사유 문자열 또는 None" 을 돌려준다. 순서가 의미를 갖는다 —
# 아래 WRITE_RULES 의 나열 순서가 곧 우선순위이고, 그게 이 표의 존재 이유다.
# 이전에는 92줄 if-체인이어서 순서가 코드 흐름에 숨어 있었다.

# --------------------------------------------------------------- 쓰기 규칙 평가기
#
# 규칙 일곱 개가 전부 같은 모양이었다:
#
#   가드:  class == X  [· 깊이 ≥ N]  [· parts[1] ∈ <설정목록>]  [· basename ∉ <면제>]
#   판정:  일곱 가지 테스트 중 하나
#
# 그래서 파이썬 함수가 아니라 데이터로 쓴다(`stages.json` 의 `write_rules`).
# **배열 순서가 우선순위다** — 그게 이 표의 존재 이유이고, 예전 92줄 if-체인에서는
# 순서가 코드 흐름에 숨어 있었다.
#
# 거부 메시지도 데이터다. 우리 원칙 중 "막을 때는 최적 행동을 함께 준다"가 스키마로
# 강제되고, 부수적으로 번역이 데이터 파일 하나를 고치는 일이 된다.

WRITE_SELECTORS = ("class", "min_depth", "subdir_in", "basename_not_in",
                   "creates_new_toplevel")
WRITE_TESTS = ("never", "not_matching", "class_in_stage_write", "stage_in",
               "subdir_in", "basename_starts_with", "basename_matches", "predicate")

# 탈출 해치. 어휘로 표현할 수 없는 규칙은 이름 붙인 파이썬으로 남긴다 — 어휘를 억지로
# 늘리면 결국 튜링 완전해지고, 그러면 파이썬으로 쓰는 것과 같아진다. 비어 있는 것이
# 정상이고, 여기 무언가 들어가면 그것이 다음 어휘 후보다.
WRITE_PREDICATES = {}

FIELD_RE = re.compile(r"\{([a-z_]+)\}")


def _subst(template, fields):
    """`{rel}` 같은 자리만 채운다. 모르는 이름은 글자 그대로 남긴다 —
    사용자가 쓴 메시지에 중괄호가 있어도 터지지 않아야 한다."""
    return FIELD_RE.sub(lambda m: str(fields.get(m.group(1), m.group(0))), template)


def _w_selects(w, when):
    """가드. 이 규칙이 이 쓰기에 해당하나."""
    cls = when.get("class")
    if cls is not None and w.cls != cls:
        return False
    md = when.get("min_depth")
    if md is not None and len(w.parts) < int(md):
        return False
    for key, want_in in (("subdir_in", True), ("basename_not_in", False)):
        name = when.get(key)
        if name is None:
            continue
        if len(w.parts) < 2:
            return False
        part = w.parts[1] if key == "subdir_in" else w.parts[-1]
        if (part in w.seq(name)) is not want_in:
            return False
    if when.get("creates_new_toplevel") and not new_toplevel_dir(w.ctx.root, w.rel):
        return False
    return True


def _w_violates(w, req):
    """판정. True 면 위반이다."""
    if req.get("never"):
        return True
    name = req.get("not_matching")
    if name is not None:
        return any(glob_match(w.rel, p) for p in w.seq(name))
    if req.get("class_in_stage_write"):
        return w.cls not in (w.stage.get("write") or [])
    name = req.get("stage_in")
    if name is not None:
        return w.ctx.sid not in w.seq(name, ("scaffolding",))
    name = req.get("subdir_in")
    if name is not None:
        allowed = w.seq(name)
        # 목록이 비어 있으면 제약이 없다 — 덜어낸 것을 제약으로 읽으면 안 된다.
        return bool(allowed) and w.parts[1] not in allowed
    val = req.get("basename_starts_with")
    if val is not None:
        if val == "<loop_prefix>":
            val = file_prefix(w.ctx.con, w.ctx.lid)
        return not w.parts[-1].startswith(val)
    name = req.get("basename_matches")
    if name is not None:
        pat = w.ctx.cfg.at(name)
        return bool(pat) and not re.match(pat, w.parts[-1])
    name = req.get("predicate")
    if name:
        fn = WRITE_PREDICATES.get(name)
        return bool(fn) and bool(fn(w))
    return False


def _w_fields(w, req):
    f = {
        "rel": w.rel,
        "cls": w.cls,
        "stage": w.lbl,
        "loop": w.ctx.lid,
        "basename": w.parts[-1],
        "subdir": w.parts[1] if len(w.parts) > 1 else "",
        "prefix": file_prefix(w.ctx.con, w.ctx.lid),
        "top": new_toplevel_dir(w.ctx.root, w.rel) or "",
    }
    if req.get("class_in_stage_write"):
        f["allowed"] = ", ".join(w.stage.get("write") or []) or "(없음)"
    name = req.get("subdir_in")
    if name:
        f["allowed"] = "/".join(w.seq(name))
    name = req.get("stage_in")
    if name:
        f["stages"] = ", ".join(
            stage_obj(w.ctx.cfg, s)["label"] for s in w.seq(name, ("scaffolding",))
            if s in stage_ids(w.ctx.cfg)) or "(없음)"
    return f


def write_rules(cfg):
    return [r for r in (cfg.get("write_rules") or []) if isinstance(r, dict)]


def _first_violation(w, rules):
    """(규칙 id, 사유) 또는 (None, None). 배열 순서가 우선순위다."""
    for r in rules:
        if w.grant and r.get("grant_opens"):
            continue          # 예외로 열리는 규칙. protected 는 이 표시가 없다.
        if not _w_selects(w, r.get("when") or {}):
            continue
        if not _w_violates(w, r.get("require") or {}):
            continue
        return r.get("id") or "?", _subst(r.get("deny") or "규칙 위반이다: {rel}",
                                          _w_fields(w, r.get("require") or {}))
    return None, None


def check_write(ctx, rel):
    """(decision, reason). decision None 이면 판정하지 않음.

    차단은 event 에 적립한다 — 어떤 규칙에 몇 번 걸리는지가 복리의 원료다.
    """
    w = WriteReq(ctx, rel)
    rules = write_rules(ctx.cfg)
    name, reason = _first_violation(w, rules)
    if reason:
        record_event(ctx.con, ctx.lid, ctx.sid, "block", name, rel, reason)
        return "deny", reason
    if w.grant:
        # 조건부 UPDATE 로 소비한다. `with con:` 은 첫 DML 에서야 트랜잭션을
        # 시작하므로 find_grant 의 SELECT 는 트랜잭션 밖이다 — 병렬 훅 넷이
        # `--uses 1` 예외를 넷 다 쓰고 uses_left 가 -3 이 되는 것을 재현했다.
        # rowcount 로 '먼저 소진한 쪽'을 판정한다.
        used = ctx.con.execute(
            "UPDATE wgrant SET uses_left=uses_left-1 WHERE id=? AND uses_left>0",
            (w.grant["id"],)).rowcount
        if not used:
            # 다른 훅이 먼저 썼다. 예외 없이도 통과하는지 다시 판정한다.
            w.grant = None
            name, reason = _first_violation(w, rules)
            if reason:
                record_event(ctx.con, ctx.lid, ctx.sid, "block", name, rel, reason)
                return "deny", reason
    return None, None


BASH_SPLIT = re.compile(r"\|\||&&|[;&|\n]")
# 이 프로그램들에 넘긴 경로는 '실행 대상'이다. 하네스 래퍼를 python3 로 돌리는 것은
# 정상 동작이므로 변경 시도로 오인해서는 안 된다.
# 읽기만 하는 명령은 막지 않는다. 과잉 차단은 마찰이 되고, 마찰은 게이트를 끄게 만든다.
# find 는 없다 — `-delete`/`-exec` 로 파일을 지운다.


def bash_protected_hit(cfg, root, cmd):
    """Bash 명령이 보호 경로를 **대상으로** 삼는지. 실행하는 것은 대상이 아니다.

    check_write 는 Write/Edit 만 본다. Bash 는 `rm`, `sed -i`, 리다이렉트,
    `sqlite3 ... UPDATE` 로 같은 파일을 바꿀 수 있었고 그 경로는 검사되지 않았다.
    """
    pats = (cfg.get("folder_rules") or {}).get("protected_paths") or []
    if not pats:
        return None

    mutating = bool(bash_mutator_re(cfg).search(cmd))
    # `>|경로` 의 `|` 를 BASH_SPLIT 이 파이프로 보고 쪼개면 경로가 다음 세그먼트의
    # **명령어 자리**로 밀려 '실행 대상' 으로 건너뛰어진다. 먼저 떼어놓는다.
    cmd = cmd.replace(">|", "> ")

    def candidates(tok):
        """`of=경로`, `>|경로` 처럼 붙어 오는 형태까지 경로로 본다.

        둘 다 실제로 통과했다: `dd if=/dev/null of=<db>`, `printf x >|<LEARNED>`.
        """
        raw = tok.strip("\"'")
        out = [raw.lstrip("<>|&")]
        if "=" in raw:
            out.append(raw.split("=", 1)[1].lstrip("<>|&"))
        return [t for t in out if t]

    def protected(tok):
        for cand in candidates(tok):
            rel = rel_to_root(root, cand)
            if not rel or rel == ".":
                continue
            if any(glob_match(rel, p) for p in pats):
                return rel
            # 보호 경로를 **담고 있는** 디렉터리도 변경 명령의 대상이 될 수 없다.
            # `find .claude/harness -delete` 나 `rm -rf .claude` 가 그 경우다.
            if mutating and any(p.startswith(rel + "/") for p in pats):
                return rel
        return None

    for seg in BASH_SPLIT.split(cmd):
        toks = re.findall(r"\S+", seg)
        if not toks:
            continue
        head = os.path.basename(toks[0].strip("\"'"))
        # 리다이렉트가 있으면 읽기 명령도 쓰기가 된다 (`cat x > 엔진`).
        if head in cfg.seq("bash.readers", BASH_READERS_DEFAULT) and ">" not in seg:
            continue
        skip = 2 if head in cfg.seq("bash.interpreters", BASH_INTERPRETERS_DEFAULT) and len(toks) > 1 else 1
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

def hook_session_start(inp, ctx):
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
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
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                  "additionalContext": "\n".join(lines)}}
    # 설정 오타는 **사람에게** 알린다. 모델에게 컨텍스트로 주면 모델이 고치려 들고,
    # stages.json 은 사람의 문서다. 조용히 무시되는 설정이 있다는 사실 자체가 정보다.
    probs = config_problems(cfg)
    if probs:
        out["systemMessage"] = ("harness: stages.json 에서 무시되는 설정이 %d건 있다\n  - %s"
                                % (len(probs), "\n  - ".join(probs[:5])))
    emit(out)


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


def skip_block_reason(cfg, sid, target):
    """skip 이 **불가능한** 이유. 가능하면 None.

    훅과 CLI 가 같은 함수를 쓴다. 다른 규칙을 쓰면 사용자가 승인한 뒤에 거부되고,
    모델은 안내받은 명령을 다시 시도해 **다이얼로그가 무한 반복된다** — 실제로
    도그푸딩에서 그렇게 됐다.
    """
    ids = stage_ids(cfg)
    cur = stage_index(cfg, sid)
    last = cfg["stages"][-1]["id"]
    if target.startswith("+"):
        try:
            dest = min(cur + int(target[1:]) - 1, len(ids) - 1)
        except ValueError:
            return "잘못된 형식: %s" % target
    elif target.startswith("until:"):
        want = target.split(":", 1)[1]
        if want not in ids:
            return "알 수 없는 단계: %s" % want
        dest = ids.index(want) - 1
    elif target in ids:
        dest = ids.index(target)
    else:
        return "알 수 없는 대상: %s" % target

    if dest < cur:
        return ("뒤로 갈 수는 없다 — 이미 %s 단계이거나 그보다 뒤다. "
                "단계는 항상 앞으로만 간다." % label_of(cfg, sid))

    locked = [ids[i] for i in range(cur, dest + 1)
              if cfg["stages"][i].get("skippable") is False]
    if not locked:
        return None

    names = ", ".join(stage_obj(cfg, x)["label"] for x in locked)
    if ids[cur] == ids[0]:
        # 여기서 예전에는 `skip until:selection` 을 안내했다. 그건 dest 가 -1 이 되어
        # **항상 실패하는 명령**이고, 모델이 그대로 반복해 승인 요청이 무한히 떴다.
        return ("%s 단계는 건너뛸 수 없다 — 작업을 정하지 않고 넘어가면 이후 모든 단계가 "
                "기준 없이 돌아간다. 스킵이 아니라 **작업을 고르는 것**이 다음 행동이다: "
                "`harness status` 가 하네스가 아는 할 일을 후보로 보여준다(승격 결정, "
                "재발한 승격, 낡은 인덱스, 예산 소진). 고르면 "
                "`harness loop intent \"...\"` 와 `harness loop done-when \"...\"` 로 "
                "기록하고 진행하라. 그 후보들까지 정말 필요 없으면 그렇다고 말하고 "
                "멈춰라 — 그건 교착이 아니라 정상 종료다." % names)
    return ("%s 단계는 건너뛸 수 없다. 이 회차를 중단하려면 "
            "`harness skip until:%s --reason \"...\"` 로 %s 까지 이동한 뒤, 중단 사유를 "
            "회고로 남기고 `harness advance --cycle` (또는 `--done`) 으로 닫아라."
            % (names, last, stage_obj(cfg, last)["label"]))


PLAN_PREVIEW_LINES = 24
PLAN_PREVIEW_CHARS = 1400


def plan_preview(root, cmd):
    """승인 다이얼로그에 실을 계획 본문.

    계획 승인은 이 하네스에서 사람이 방해받는 유일하게 값있는 자리인데, 다이얼로그가
    **파일 이름만** 보여주고 있었다. 읽지 않고 찍는 도장은 마찰만 있고 정보가 없다 —
    그렇게 남은 `plan_approved` 기록은 가짜다. 무엇을 승인하는지 보여준다.
    """
    pos = [t for t in QUOTED_RE.sub(" ", cmd).split() if not t.startswith("--")]
    path = None
    for i, t in enumerate(pos):
        if t == "approve-plan" and i + 1 < len(pos):
            path = pos[i + 1].strip("\"'")
            break
    if not path:
        return "계획 파일 경로가 없다. `approve-plan <파일>` 형식으로 지정하라."
    full = path if os.path.isabs(path) else os.path.join(root, path)
    if not os.path.isfile(full):
        return "⚠ 계획 파일이 없다: %s — 승인하기 전에 확인하라." % path
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            body = fh.read(PLAN_PREVIEW_CHARS * 2)
    except OSError as exc:
        return "⚠ 계획 파일을 읽을 수 없다 (%s): %s" % (path, exc)
    lines = body.splitlines()
    shown = lines[:PLAN_PREVIEW_LINES]
    text = "\n".join(shown)[:PLAN_PREVIEW_CHARS]
    more = []
    if len(lines) > len(shown):
        more.append("이하 %d줄 생략" % (len(lines) - len(shown)))
    if len(text) < len("\n".join(shown)):
        more.append("길이 잘림")
    tail = " (%s — 전문은 %s)" % (", ".join(more), path) if more else ""
    return "%s%s\n%s" % (path, tail, text)


def ctrl_requests(cmd):
    """명령 문자열의 하네스 제어 호출을 **전부** 찾아 (subcommand, 세그먼트) 로 준다.

    두 결함을 함께 고친다.
    1. 이전에는 `CTRL_RE.search` 로 **첫 매치만** 봤다. `harness status;
       harness loop new` 는 status 로 판정되고 뒤의 loop new 는 검사되지 않았다.
    2. 훅은 정규식의 두 번째 토큰을 subcommand 로 봤고 CLI 는 플래그를 걷어낸
       위치 인자를 봤다. 두 파서가 다른 답을 내서 `loop --reason=x new` 가
       게이트를 지나쳤다. 이제 훅도 `argv_positional` 과 같은 규칙을 쓴다.
    """
    out = []
    for seg in BASH_SPLIT.split(cmd):
        toks = QUOTED_RE.sub("_", seg).split()
        for i, tok in enumerate(toks):
            if os.path.basename(tok.strip("\"'")) not in CTRL_NAMES:
                continue
            pos, skip = [], False
            for a in toks[i + 1:]:
                if skip:
                    skip = False
                    continue
                if a.startswith("--"):
                    skip = "=" not in a  # `--flag value` 는 값도 건너뛴다
                    continue
                pos.append(a)
            if pos:
                sub = pos[0]
                if len(pos) > 1 and pos[1] in CTRL_SUB2.get(sub, ()):
                    sub = "%s %s" % (sub, pos[1])
                out.append((sub, seg))
            break
    return out


def ctrl_decision(con, cfg, root, sub, cmd, mode, lid, sid):
    """제어 명령에 대한 판정을 **돌려준다**(emit 하지 않는다).

    한 Bash 명령에 제어 호출이 여러 개 있을 수 있어서, 호출자가 전부 순회하며
    동의가 필요한 것을 찾아야 한다. 이전에는 첫 매치만 보고 즉시 emit 해서
    `harness status; harness loop new` 가 통째로 통과했다.
    """
    if sub == "auto-skip":
        # off 는 게이트 복원이므로 동의 없이 허용한다. on 은 게이트를 무력화하므로
        # 반드시 사람의 동의를 받는다 — 그러지 않으면 모델이 스스로 켤 수 있다.
        if not re.search(r"auto-skip\s+on\b", cmd):
            return
    elif sub not in consent_map(cfg):
        return

    if sub == "skip":
        # 불가능한 스킵은 **묻지 않고** 거부한다. 승인을 받아봐야 거부되고,
        # 그러면 모델이 다시 시도해 다이얼로그만 반복된다.
        pos = [t for t in QUOTED_RE.sub("_", cmd).split()[1:] if not t.startswith("--")]
        tgt = None
        for i, t in enumerate(pos):
            if t == "skip" and i + 1 < len(pos):
                tgt = pos[i + 1]
                break
        if tgt:
            why = skip_block_reason(cfg, sid, tgt)
            if why:
                record_event(con, lid, sid, "block", "skip_impossible", tgt, why)
                return pre_decision("deny", why)

    reason = raw_flag(cmd, "reason")
    if sub != "approve-plan" and not reason:
        record_event(con, lid, sid, "block", "no_reason", sub, cmd[:200])
        return pre_decision("deny",
            "사유 없이 %s 할 수 없다. --reason \"...\" 로 사유를 명시하라." % sub)

    if sub == "skip" and auto_skip_on(con):
        # 자동 승인이 켜져 있다. 다이얼로그는 생략하되 사실은 사용자에게 노출한다.
        out = pre_decision("defer", None)
        out["systemMessage"] = ("harness: 단계 스킵을 자동 승인했다 (사유: %s · %s). "
                                "끄려면 `harness auto-skip off`."
                                % (reason, auto_skip_scope_note(con)))
        return out

    detail = "%s: `%s`" % (consent_map(cfg).get(sub, sub + " 요청"), cmd.strip())
    if reason:
        detail += "\n사유: %s" % reason
    if sub == "approve-plan":
        # 무엇을 승인하는지 보여준다. 이름만 보고 찍는 승인은 기록으로도 가짜다.
        detail += "\n\n─── 계획 ───\n%s\n───────────" % plan_preview(root, cmd)
    detail += "\n승인하면 하네스 상태에 기록된다."
    if mode == "bypassPermissions" and sub == "auto-skip":
        # 하나의 예외. `auto-skip on` 의 효과는 **세션을 넘어 지속된다**(meta 에 저장되고
        # scope 를 project 로 두면 이후 세션에도 남는다). 세션 단위 사전 승인으로
        # 세션을 넘는 결정을 덮을 수는 없다. 이건 진짜 사람의 판단이 필요하다.
        record_event(con, lid, sid, "block", "bypass_mode", sub, cmd[:200])
        return pre_decision("deny", detail +
            "\nbypassPermissions 는 이 세션의 사전 승인이지만 `auto-skip on` 은 효과가 "
            "세션을 넘어 남는다. 그래서 이것만은 거부한다 — 권한 모드를 낮추고 사람의 "
            "판단을 받아라. 이 세션의 스킵은 이미 사전 승인으로 통과한다.")

    if mode == "bypassPermissions":
        # `--dangerously-skip-permissions` 는 **사람이 세션 단위로 미리 승인한** 상태다.
        # "동의를 받을 수 없는 상태" 로 읽고 거부했더니, 무인 실행 전용 모드에서
        # `approve-plan` 이 불가능해져 Planning 이 교착됐다 — 모델은 산문으로
        # "승인하면 진행한다" 며 사람을 기다리고, 루프가 멈춘다.
        #
        # 승인은 면제하되 **기록은 면제하지 않는다.** auto-skip on 과 같은 취급이다.
        # 우회 사실은 bypass 이벤트로 남아 `stats` 와 `metrics` 의 회피 열에 드러난다.
        record_event(con, lid, sid, "bypass", "bypass_mode", sub, cmd[:200])
        out = pre_decision("defer", None)
        out["systemMessage"] = (
            "harness: %s 을 bypassPermissions 사전 승인으로 통과시켰다%s. "
            "기록은 남는다 — `harness stats` 의 '게이트 우회'."
            % (sub, " (사유: %s)" % reason if reason else ""))
        return out
    return pre_decision("ask", detail)


def hook_pre_tool_use(inp, ctx):
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
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

        reqs = ctrl_requests(cmd)
        for req_sub, seg in reqs:
            with con:
                out = ctrl_decision(con, cfg, root, req_sub, seg, mode, lid, sid)
            if out:
                return emit(out)
        if reqs:
            return  # 제어 명령이지만 동의가 필요 없다

        if bash_mutator_re(cfg).search(cmd):
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
        decision, reason = check_write(ctx, rel)
    if decision:
        emit(pre_decision(decision, reason))


def hook_post_tool_use(inp, ctx):
    """증거만 조용히 적립한다. 컨텍스트로 출력하지 않는다."""
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    tool = inp.get("tool_name") or ""

    if tool == "ExitPlanMode":
        # **관측만 한다. 아직 plan_approved 증거로 쓰지 않는다.**
        # 사용자가 계획을 거절했을 때도 이 훅이 뜨는지 모른다. 뜬다면 거절된 계획이
        # 승인으로 기록되고, 그건 게이트가 사람 없이 열리는 것이다 — 두 번 찾아낸
        # 부류의 구멍이다. 응답의 모양을 기록해 두고, 한 번 실제로 돌려본 뒤에
        # 안전하게 배선한다. 그때까지 승인은 `harness approve-plan` 이 담당한다.
        shape = {k: str(v)[:120] for k, v in sorted(inp.items())
                 if k not in ("tool_input", "cwd", "session_id", "transcript_path")}
        with con:
            record_event(con, lid, sid, "plan_mode_exit", "observed",
                         "ExitPlanMode", json.dumps(shape, ensure_ascii=False)[:400])
        return

    ti = inp.get("tool_input") or {}
    signals = cfg.obj("criteria")

    with con:
        field = WRITE_TOOLS.get(tool)
        if field:
            rel = rel_to_root(root, ti.get(field))
            if rel:
                # 편집 이력. 한 루프에서 같은 파일을 몇 번 고쳤는지가 구조 냄새다.
                record_event(con, lid, sid, "edit", None, rel)
                # write_glob 이 없는 조건(cli·observed·no_pending)은 그냥 지나간다.
                for kind, sig in signals.items():
                    for pat in (sig.get("write_glob") or []) if isinstance(sig, dict) else []:
                        if glob_match(rel, pat):
                            record_evidence(con, lid, sid, kind, rel)
                            break
        if sid in evidence_stages(cfg) and not tool_failed(inp):
            sig = signals.get("verification_evidence") or {}
            if not isinstance(sig, dict):
                sig = {}
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


def tool_failed(inp):
    """이 도구 호출이 실패했나. 판단이 안 되면 False (실패라고 단정하지 않는다).

    왜 필요한가: `bash_pattern` 은 **명령 문자열만** 봤다. 그래서 `pytest` 를
    돌려 3개가 깨져도 verification_evidence 가 적립되고 Verification 게이트가
    열렸다. 실제로 그렇게 통과하는 것을 확인했다. 테스트를 돌린 것과 통과한
    것은 다른 사실인데 같은 것으로 세고 있었다.

    실패가 PostToolUse 로 오는지 PostToolUseFailure 로만 오는지는 문서에 없다.
    어느 쪽이든 안전하게 두 곳 다 이 검사를 통과해야 적립된다 — 실패가 저쪽으로
    간다면 이 검사는 한 번도 걸리지 않을 뿐이고, 이쪽으로 온다면 구멍이 닫힌다.
    """
    resp = inp.get("tool_response")
    if isinstance(resp, dict):
        if resp.get("isError") or resp.get("interrupted") or resp.get("is_error"):
            return True
        code = resp.get("exit_code", resp.get("exitCode"))
        if isinstance(code, int) and code != 0:
            return True
    for key in ("tool_error", "error"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            return True
        if v is True:
            return True
    return False


def hook_post_tool_use_failure(inp, ctx):
    """도구 실패를 적립한다. 같은 실패가 반복되는 것이 '동일한 실수'의 직접 증거다.

    오류 필드명이 문서에 명시돼 있지 않아 후보를 순서대로 시도한다.
    """
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
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

    # 전체 행을 가져온다. is_regressed 가 kind/key/recheck_at 을 쓰므로 일부 열만
    # 고르면 sqlite3.Row 가 IndexError 를 낸다 — 훅이 fail-open 이라 조용히 죽었다.
    p = con.execute("SELECT * FROM promotion WHERE key=?",
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
    indexes, files = _recall_files(cfg, root, [target], limit=4)
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
    thr = cfg.num("promotion.min_loops", 3, low=2)
    undecided = p is None or is_regressed(con, cfg, p)
    if loops >= thr and undecided:
        out["systemMessage"] = ("harness: '%s' 실패가 작업 %d개에서 반복된다 — "
                               "Compounding 에서 승격 결정을 요구한다." % (target, loops))
    elif loops >= thr:
        out["systemMessage"] = ("harness: '%s' 실패가 작업 %d개에서 반복된다 "
                               "(이미 %s 로 결정됨 — 재발이 이어지면 결정이 무효화된다)."
                               % (target, loops, p["decision"]))
    emit(out)


def hook_stop(inp, ctx):
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    stage = stage_obj(cfg, sid)
    # prompt_id 가 없는 환경에서 "-" 로 뭉치면 서로 다른 프롬프트가 이어붙임
    # 예산을 공유한다. 세션으로 대체하고, 그것도 없으면 작업 해시로 가둔다.
    prompt_id = (inp.get("prompt_id") or inp.get("session_id")
                 or "loop:%s" % lid)
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
        if not criterion_met(con, cfg, root, lid, key):
            problems.append((key, "%s 단계를 끝낼 수 없다: %s"
                             % (stage["label"], criterion_help(cfg, key))))
    if not problems:
        return continue_or_stop(con, cfg, root, lid, sid, stage, prompt_id)

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


# 하네스가 **스스로** 남기는 기록은 진전이 아니다. 이걸 빼지 않으면 이어붙임
# 이벤트가 이벤트 수를 늘려 지문이 매번 바뀌고, 진전 감지가 자기 자신을 진전으로
# 세면서 영원히 발동하지 않는다 — 실제로 그렇게 만들어서 5회 헛돌았다.
FP_IGNORE_KINDS = ("stop_continue", "stop_stalled", "stop_gate", "bypass", "cycle_close")


def progress_fingerprint(con, lid, sid):
    """'하네스가 아는 진전'의 지문.

    읽기만 한 턴은 지문이 그대로다 — 그건 의도한 것이다. 단계 종료 조건은
    증거·모델 활동·단계 전이로만 채워지므로, 그 셋이 그대로면 종료에 가까워지지
    않았다. 지문이 연속으로 같으면 이어붙여도 같은 자리를 돈다.
    """
    ev = con.execute("SELECT COUNT(*) c FROM evidence WHERE loop_id=?",
                     (lid,)).fetchone()["c"]
    n = con.execute(
        "SELECT COUNT(*) c FROM event WHERE loop_id=? AND kind NOT IN (%s)"
        % ",".join("?" * len(FP_IGNORE_KINDS)),
        (lid,) + FP_IGNORE_KINDS).fetchone()["c"]
    return "%s:%d:%d" % (sid, ev, n)


def stalled_rounds(con, lid, prompt_id, fp):
    """이 프롬프트에서 지문이 연속 몇 번 그대로였나.

    작업(loop_id)으로도 가둔다. prompt_id 가 없는 환경에서 과거 기록이 새 작업의
    예산을 깎는 것을 막는다.
    """
    seen = [r["detail"] for r in con.execute(
        "SELECT detail FROM event WHERE kind='stop_continue' AND target=? "
        "AND loop_id=? ORDER BY id", (prompt_id, lid))]
    stalled = 0
    for d in reversed(seen):
        if d != fp:
            break
        stalled += 1
    return stalled, len(seen)


def continue_or_stop(con, cfg, root, lid, sid, stage, prompt_id):
    """종료 조건을 다 채웠는데 단계가 남았으면 턴 종료를 막아 이어붙인다.

    하네스는 원래 반응만 하고 턴을 시작하지 않는다. 이건 그 한계를 Stop 훅으로
    미는 것이고, 무인 실행의 유일한 추진 장치다.

    **진전 감지가 이 기능을 켤 수 있게 만든 조건이다.** 없이 켰을 때 첫 e2e 에서
    모델이 불가능한 명령을 4회 반복하며 헛돌았다. 진전이 없으면 이어붙이지
    않으므로, 진전이 있을 때는 상한을 넉넉히 줄 수 있다.
    """
    if not cfg.at("stop_continue.enabled"):
        return
    left = con.execute("SELECT COUNT(*) c FROM stage WHERE loop_id=? "
                       "AND status IN ('pending','active')", (lid,)).fetchone()["c"]
    if not left:
        return
    # 사람만 채울 수 있는 조건이 남았으면 **밀지 않는다.** 여기서 밀면 모델이
    # 만들 수 없는 것을 만들려 애쓰고, 그 시도가 매번 승인 다이얼로그가 된다.
    # Selection 에 작업이 없는 것은 교착이 아니라 **사람을 기다리는 상태**다.
    waiting = [k for k in exit_blockers(con, cfg, root, lid, sid)
               if k in human_criteria(cfg)]
    if waiting:
        note = ""
        if "intent_set" in waiting:
            try:
                n = len(work_candidates(con, cfg, root))
            except Exception:
                n = 0
            if n:
                note = (" 다만 하네스가 아는 할 일이 %d개 있다 — `harness status` 로 "
                        "확인하고 고를 수 있다." % n)
        return emit({"systemMessage":
                     "harness: %s 단계에서 사람의 입력을 기다린다 (%s). 턴을 끝낸다.%s"
                     % (stage["label"], ", ".join(waiting), note)})
    limit = cfg.num("stop_continue.max_per_prompt", 6, low=1)
    no_prog = cfg.num("stop_continue.no_progress_limit", 2, low=1)
    fp = progress_fingerprint(con, lid, sid)
    stalled, used = stalled_rounds(con, lid, prompt_id, fp)

    if stalled >= no_prog:
        # 조용히 놓아주지 않는다. 헛돈 사실이 기록되고 사용자에게 보인다.
        with con:
            record_event(con, lid, sid, "stop_stalled", stage["id"], prompt_id,
                         "지문 %s 가 %d회 연속 그대로 — 이어붙이기를 멈춘다" % (fp, stalled))
        return emit({"systemMessage": (
            "harness: %d회 이어붙였으나 진전이 없어 멈춘다 (%s 단계). 같은 지시를 "
            "반복하는 대신 무엇이 막고 있는지 사람에게 물어라." % (used, stage["label"]))})
    if used >= limit:
        with con:
            record_event(con, lid, sid, "bypass", "continue_limit", stage["id"],
                         "이어붙임 상한 %d 소진" % limit)
        return emit({"systemMessage":
                     "harness: 이어붙임 상한 %d회를 소진해 턴을 끝낸다 (%s 단계)."
                     % (limit, stage["label"])})

    with con:
        record_event(con, lid, sid, "stop_continue", str(used + 1), prompt_id, fp)
    missing = exit_blockers(con, cfg, root, lid, sid)
    todo = ("이 단계의 남은 종료 조건: %s" % ", ".join(missing) if missing
            else "이 단계의 종료 조건은 채웠다 — `harness advance` 로 넘어가라")
    return emit({"decision": "block", "reason": (
        "작업이 아직 끝나지 않았다 (현재 %s, 남은 단계 %d). 멈추지 말고 이어서 진행하라. "
        "%s. 작업이 정말 끝났으면 Compounding 에서 `harness advance --done` 으로 닫아라. "
        "(이어붙임 %d/%d)" % (stage["label"], left, todo, used + 1, limit))})


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
    con = None
    try:
        root = find_root(inp.get("cwd"))
        if not root:
            return 0  # 하네스 미설치 프로젝트 — 조용히 종료
        # connect 가 try 밖에 있었다. 손상된 SQLite 파일은 connect 나
        # `PRAGMA journal_mode=WAL` 에서 DatabaseError 를 던지고, 그게
        # fail-open 처리 밖이라 traceback + exit 1 이 됐다 — 재현했다.
        con = connect(root)
        if con is None:
            return 0
        cfg = load_config(root, plugin_root())
        if not isinstance(cfg, dict) or not cfg.get("stages"):
            # 차단하지는 않는다. 다만 **조용히 꺼지지도 않는다** — 설정이 깨진 채로
            # 하네스가 없는 것처럼 동작하면 사용자는 게이트가 사라진 것을 모른다.
            # 손상된 문서를 템플릿으로 갈아치우는 것도 답이 아니다(덜어낸 규칙이
            # 되살아난다). 그래서 끄고, 끈 사실을 세션 시작에 말한다.
            if inp.get("hook_event_name") == "SessionStart":
                emit({"systemMessage":
                      "harness: `%s` 를 읽을 수 없어 하네스가 비활성 상태다. "
                      "JSON 문법을 확인하라 — 고치기 전까지 어떤 게이트도 동작하지 "
                      "않는다. 되돌리려면 `git checkout -- %s` 또는 그 파일을 지우고 "
                      "`.claude/harness/bin/harness init`."
                      % (CONFIG_REL, CONFIG_REL)})
            return 0
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
            fn(inp, Ctx(con, cfg, root, lid, sid))
    except Exception as exc:
        # 차단하지 않는다. exit 1 은 Claude Code 가 훅 오류로 표면화하므로,
        # 무력해지더라도 조용히 빠진다 — 세션을 벽돌로 만드는 것보다 낫다.
        # 단 세션 시작 때는 한 번 알린다. 조용히 죽으면 고장을 모른다.
        sys.stderr.write("step-seven-harness: %s\n" % exc)
        if inp.get("hook_event_name") == "SessionStart":
            emit({"systemMessage":
                  "harness: 하네스가 오류로 비활성 상태다 (%s). 스키마가 오래됐으면 "
                  "`.claude/harness/bin/harness init` 을 다시 실행하라." % exc})
        return 0
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    return 0


# -------------------------------------------------------------------------- cli

def plugin_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def raw_flag(cmd, name):
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
# step-seven-harness wrapper — 세션 시작마다 갱신된다. 직접 편집하지 마라.
D="$(cd "$(dirname "$0")" && pwd)"
P="$D/harness.py"
if [ ! -f "$P" ]; then P="%s"; fi
if [ ! -f "$P" ]; then
  # 정확한 이름을 먼저, 그다음 이름에 덜 묶인 glob. 플러그인 이름이 바뀌어도
  # (6->7 단계처럼) 마지막 폴백이 살아 있다. 여러 개면 최신 것을 쓴다.
  P="$(ls -t "$HOME"/.claude/plugins/cache/*/step-seven-harness/*/scripts/harness.py \
             "$HOME"/.claude/plugins/cache/*/*harness*/*/scripts/harness.py \
             2>/dev/null | head -1)"
fi
[ -f "$P" ] || { echo "step-seven-harness: engine not found" >&2; exit 1; }
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


def status_report(ctx):
    """현재 상태. 출력하지 않는다 — 테스트가 값을 검사할 수 있어야 한다."""
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    row = con.execute("SELECT intent FROM loop WHERE id=?", (lid,)).fetchone()
    rows = stage_rows(con, lid)
    stage = stage_obj(cfg, sid)
    missing = exit_blockers(con, cfg, root, lid, sid)
    crit = stage.get("exit_criteria") or []
    return {
        "loop": lid,
        "cycle": cycle_of(con, lid),
        "stage": sid,
        "stage_label": label_of(cfg, sid),
        "summary": stage["summary"],
        "intent": (row["intent"] if row else None) or None,
        "acceptance": acceptance_of(con, lid),
        "write": list(stage.get("write", [])),
        "prefix": file_prefix(con, lid),
        "stages": [{"id": s["id"], "label": s["label"],
                    "status": rows[s["id"]]["status"] if s["id"] in rows else "?"}
                   for s in cfg["stages"]],
        "exit_met": [k for k in crit if k not in missing],
        "exit_missing": missing,
        "evidence": {r["kind"]: r["c"] for r in con.execute(
            "SELECT kind, COUNT(*) c FROM evidence WHERE loop_id=? GROUP BY kind",
            (lid,))},
        "skips": [dict(r) for r in skips_of(con, lid)],
        "grants": [{"glob": g["glob"], "uses_left": g["uses_left"],
                    "reason": g["reason"]} for g in con.execute(
            "SELECT * FROM wgrant WHERE loop_id=? AND uses_left>0", (lid,))],
        "auto_skip": (auto_skip_scope_note(con) if auto_skip_on(con) else None),
        "auto_skip_reason": get_meta(con, "auto_skip_reason", "-"),
        "pending_promotions": [it["key"] for it in pending_promotions(con, cfg)],
        "promoted": promotion_summary(con, cfg),
        "tidy": tidy_headline(con, cfg, root),
        # 작업이 정해지지 않았을 때만. 정해졌으면 후보는 소음이다.
        "candidates": ([] if (row and row["intent"])
                       else work_candidates(con, cfg, root)),
    }


def render_status(d, cfg):
    # 무시되는 설정이 있으면 **맨 위**에 말한다. 아래에 묻으면 안 읽는다.
    if d.get("config_problems"):
        print("⚠ stages.json 에서 무시되는 설정 %d건:" % len(d["config_problems"]))
        for p in d["config_problems"]:
            print("  - %s" % p)
        print()
    print("작업 %s · 회차 %d · 단계 %s" % (d["loop"], d["cycle"], d["stage_label"]))
    if d["intent"]:
        print("  작업 내용: %s" % d["intent"])
    else:
        print("  작업 내용: (미정) — %s 단계에서 정하고 "
              "`harness loop intent \"...\"` 로 기록하라"
              % stage_obj(cfg, cfg["stages"][0]["id"])["label"])
    if d["acceptance"]:
        print("  완료 조건 (%d개):" % len(d["acceptance"]))
        for i, t in enumerate(d["acceptance"], 1):
            print("    %d. %s" % (i, t))
    else:
        print("  완료 조건: (미정) — `harness loop done-when \"<조건>\" ...` 으로 기록하라")
    print("  요약: %s" % d["summary"])
    print("  쓰기 허용: %s" % (", ".join(d["write"]) or "(없음)"))
    print("  .dev/ 산출물 파일명 접두사: %s" % d["prefix"])
    print("  단계: " + " → ".join("%s(%s)" % (s["label"], s["status"])
                                  for s in d["stages"]))
    if d["exit_met"] or d["exit_missing"]:
        print("  종료 조건: 충족 %s / 미충족 %s"
              % (", ".join(d["exit_met"]) or "-", ", ".join(d["exit_missing"]) or "-"))
    if d["evidence"]:
        print("  증거: %s" % ", ".join("%s×%d" % kv for kv in d["evidence"].items()))
    for r in d["skips"]:
        print("  스킵: %s — %s (승인: %s)" % (r["stage"], r["reason"], r["authorized_by"]))
    for g in d["grants"]:
        print("  예외: %s (남은 %d회) — %s" % (g["glob"], g["uses_left"], g["reason"]))
    if d["auto_skip"]:
        print("  ⚠ 스킵 자동 승인 ON (%s) — 사유: %s. 끄려면 `harness auto-skip off`"
              % (d["auto_skip"], d["auto_skip_reason"]))
    if d["pending_promotions"]:
        print("  승격 결정 대기 %d개 (Compounding 의 종료 조건): %s"
              % (len(d["pending_promotions"]), ", ".join(d["pending_promotions"])))
    if d["promoted"]:
        print("  승격됨: %s"
              % ", ".join("%s %d" % kv for kv in sorted(d["promoted"].items())))
    if d["tidy"]:
        print("  %s" % d["tidy"])
    if d.get("candidates"):
        render_work_candidates(d["candidates"])


def cli_status(ctx, argv):
    data = status_report(ctx)
    probs = config_problems(ctx.cfg)
    if probs:
        data["config_problems"] = probs
    dump_json(data) if "--json" in argv else render_status(data, ctx.cfg)
    return 0


def _enter(ctx, dest_idx):
    """dest_idx 단계를 active 로. 범위를 넘으면 루프를 닫고 새 루프를 만든다."""
    con, cfg, root, lid = ctx.con, ctx.cfg, ctx.root, ctx.lid
    if dest_idx >= len(cfg["stages"]):
        close_loop(con, lid)
        return create_loop(con, cfg, root), cfg["stages"][0]["id"], True
    sid = cfg["stages"][dest_idx]["id"]
    con.execute("UPDATE stage SET status='active', entered_at=? "
                "WHERE loop_id=? AND stage=?", (now(), lid, sid))
    return lid, sid, False


def _panel_work_candidates(ctx, lid):
    """작업이 정해지지 않았으면 하네스가 아는 할 일을 후보로 내놓는다.
    무인 실행에는 물을 사람이 없으므로, 고를 것을 주지 않으면 거기서 멈춘다."""
    row = ctx.con.execute("SELECT intent FROM loop WHERE id=?", (lid,)).fetchone()
    if not (row and row["intent"]):
        render_work_candidates(work_candidates(ctx.con, ctx.cfg, ctx.root))


def _panel_tidy(ctx, lid):
    """'줄이는' 것도 일이다. 권고만으로는 아무도 줄이지 않았으므로 목록을 준다."""
    head = tidy_headline(ctx.con, ctx.cfg, ctx.root)
    if head:
        print("\n%s" % head)


def _panel_acceptance(ctx, lid):
    """완료 조건. 대조해야 하는 자리에서는 찾아오게 하지 않고 밀어준다."""
    acc = acceptance_of(ctx.con, lid)
    if acc:
        print("\n이 작업의 완료 조건 (%d개):" % len(acc))
        for i, t in enumerate(acc, 1):
            print("  %d. %s" % (i, t))


PANELS = {
    "work_candidates": _panel_work_candidates,
    "tidy": _panel_tidy,
    "acceptance": _panel_acceptance,
    "retro": None,   # 분량이 커서 _hint_on_enter 안에 남긴다
}


def _hint_on_enter(ctx, lid, sid):
    """단계 진입 시의 안내. **무엇을 보여줄지는 단계가 선언한다.**

    예전에는 이 함수가 단계 id 를 알고 있었다(`if sid == "scaffolding"`). 그래서
    단계를 더하거나 이름을 바꾸면 파이썬을 고쳐야 했고, 실제로 0.10.0 에서
    Selection 을 신설했을 때 이 부류가 낡았다. 엔진이 알아야 하는 것은 **패널의
    종류**이지 어느 단계가 그것을 쓰는지가 아니다 — 그건 `stages[].panels` 가 정한다.

    Context 는 **당겨가는** 단계라 패널이 없다. 과거 기록을 밀어넣으면 이번 task 와
    무관한 실수까지 컨텍스트를 먹는다. 조회 방법만 알려주고 판단은 모델이 한다.
    Compounding 은 반대다. 막 끝낸 루프 자신의 기록은 무조건 관련 있으니 밀어준다.
    """
    con, cfg = ctx.con, ctx.cfg
    hint = stage_obj(cfg, sid).get("hint")
    if hint:
        print("\n%s" % hint)

    panels = stage_obj(cfg, sid).get("panels") or []
    for name in panels:
        fn = PANELS.get(name)
        if fn:
            fn(ctx, lid)

    if "retro" not in panels:
        return

    # 무엇을 물을지가 회고의 값을 정한다. 관측을 나열하기 **전에** 질문을 둔다 —
    # 순서를 뒤집으면 "규칙에 걸린 목록"이 회고의 전부가 된다.
    print("\n회고에 답할 것:")
    for i, (q, why) in enumerate(retro_questions(cfg), 1):
        print("  %d. **%s** — %s" % (i, q, why))

    keys = cycle_search_keys(con, lid, cycle_window_start(con, lid))
    if keys:
        print("\n이 회차의 검색 키 — 회고 **앞부분**에 이 문자열을 그대로 넣어라:")
        print("  " + "  ".join("`%s`" % k for k in keys))
        print("  나중에 이 키로 찾는다. 없으면 그 회고는 다시 찾아지지 않는다 "
              "(내용이 같아도 그렇다).")

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


def next_cycle(ctx):
    """같은 작업의 다음 회차. Selection 은 유지하고 나머지 단계를 초기화한다.

    증거를 초기화하지 않으면 2회차 Planning 이 1회차 계획서로 통과한다.
    intent_set 만 남긴다 — 작업은 그대로이므로 다시 선정할 필요가 없다.
    이전 회차의 계획·회고 파일은 파일로 남고, 파일명의 회차로 구분된다.
    """
    con, cfg, lid = ctx.con, ctx.cfg, ctx.lid
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


def cli_advance(ctx, argv):
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
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

    missing = exit_blockers(con, cfg, root, lid, sid)
    if missing:
        print("advance 거부 — %s 단계의 종료 조건이 남았다:" % stage_obj(cfg, sid)["label"])
        for k in missing:
            print("  - %s: %s" % (k, criterion_help(cfg, k)))
        if stage_obj(cfg, sid).get("skippable") is False:
            print("이 단계는 건너뛸 수 없다. 조건을 채워야 한다.")
        else:
            print("정당한 사유가 있으면 `harness skip %s --reason \"...\"` 로 "
                  "사람의 승인을 받아라." % sid)
        return 1

    # 회고가 나중에 찾아지는지 확인한다. 통찰의 질은 채점하지 않지만 찾아지는지는
    # 기계적 사실이라 확인할 수 있다. 막지는 않는다 — 무엇을 쓸지는 판단이다.
    retro_note = None
    if sid == last:
        try:
            keys, found, missing = retro_key_report(
                con, cfg, root, lid, cycle_window_start(con, lid))
            if keys:
                retro_note = (keys, found, missing)
                with con:
                    record_event(con, lid, sid, "retro_keys", str(len(found)),
                                 "%s-%d" % (lid, cycle_of(con, lid)),
                                 "found=%d/%d missing=%s"
                                 % (len(found), len(keys), ",".join(missing) or "-"))
        except Exception:
            pass

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
            nlid, nsid, done_task = lid, next_cycle(ctx), False
        else:
            nlid, nsid, _ = _enter(ctx, stage_index(cfg, sid) + 1)
            done_task = False

    if retro_note:
        keys, found, missing = retro_note
        if missing:
            print("회고 확인: 검색 키 %d개 중 %d개가 빠졌다 — %s"
                  % (len(keys), len(missing), ", ".join("`%s`" % k for k in missing)))
            print("   그 문자열이 회고에 없으면 다음에 같은 일이 생겨도 찾아지지 않는다. "
                  "다음 회차 회고에는 넣어라.")
        else:
            print("회고 확인: 검색 키 %d개 전부 들어 있다 — 다시 찾아진다." % len(keys))
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
    _hint_on_enter(ctx, nlid, nsid)
    return 0


def cli_skip(ctx, argv):
    """PreToolUse 가 사람의 승인을 받은 뒤에만 여기까지 온다."""
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    pos = argv_positional(argv)
    target = pos[0] if pos else None
    reason = argv_value(argv, "reason")
    if not target or not reason:
        print("사용법: harness skip <stage-id|+N|until:<stage-id>> --reason \"...\"")
        return 2
    # 훅과 **같은 함수**로 판정한다. 훅이 이미 막았으므로 보통 여기 오지 않지만,
    # 셸 간접 호출로 훅을 우회해 들어온 경우에도 같은 답을 내야 한다.
    why = skip_block_reason(cfg, sid, target)
    if why:
        print(why)
        return 1
    ids = stage_ids(cfg)
    cur = stage_index(cfg, sid)
    if target.startswith("+"):
        dest = min(cur + int(target[1:]) - 1, len(ids) - 1)
    elif target.startswith("until:"):
        dest = ids.index(target.split(":", 1)[1]) - 1
    else:
        dest = ids.index(target)

    # 스킵은 **승인**을 면제하지만 **기록**을 면제하지 않는다.
    # 무인 실행으로 Planning 을 건너뛰어도 계획 파일은 남아야 한다 — 회차 10번을 돌았을 때
    # 계획 10개가 남는 것과 스킵 기록 10개가 남는 것은 복리의 재료로서 다르다.
    for i in range(cur, dest + 1):
        st = cfg["stages"][i]
        for key in st.get("skip_requires", []):
            # advance 와 **같은 판정**을 써야 한다. has_evidence 만 보면 계획 파일이
            # 디스크에 있는데도 "계획 파일을 남겨야 한다"고 거부한다 — 이미 한 일을
            # 하라는 말이고, 사용자는 빠져나갈 길이 없다. 실제로 그렇게 막혔다.
            if not criterion_met(con, cfg, root, lid, key):
                print("%s 를 건너뛰더라도 기록은 남겨야 한다: %s"
                      % (st["label"], criterion_help(cfg, key)))
                print("먼저 그 기록을 남긴 뒤 다시 시도하라. 승인만 면제된다.")
                return 1

    # 자동 승인으로 통과한 스킵은 사람이 승인한 것과 구분해 기록한다
    by = "auto" if auto_skip_on(con) else "user"
    left = None
    skipped = []
    with con:
        # 현재 단계: 종료 조건을 충족했다면 done, 아니면 skipped 로 정직하게 기록한다
        if dest == cur or exit_blockers(con, cfg, root, lid, sid):
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
        nlid, nsid, cycled = _enter(ctx, dest + 1)
    print("스킵(%s): %s" % ("자동 승인" if by == "auto" else "사용자 승인",
                            ", ".join(skipped) or "(없음)"))
    print("사유: %s" % reason)
    if by == "auto" and left is not None:
        print("자동 승인 남은 횟수: %d%s" % (left, " — 소진되어 OFF 로 돌아갔다" if left == 0 else ""))
    if cycled:
        print("작업 %s 종료 → 새 작업 %s, 단계 %s" % (lid, nlid, label_of(cfg, nsid)))
    else:
        print("→ 단계 %s" % label_of(cfg, nsid))
        _hint_on_enter(ctx, nlid, nsid)
    return 0


SHELL_META = set(";|&<>$`(){}\n")


def cli_verify(ctx, argv):
    """검증을 **하네스가 직접 돌리고 종료 코드로** 판정한다.

      harness verify -- pytest tests/

    왜 있나: verification_evidence 는 PostToolUse 관측으로만 채워졌다. 훅이
    없는 환경 — 다른 에이전트 도구, 사람이 직접 돌릴 때, 훅이 실패한 세션 —
    에서는 `skip` 밖에 길이 없었고, 그건 '검증했다'가 아니라 '검증을 건너뛰었다'로
    기록된다. 정직한 기록을 남길 방법이 없으면 사람은 부정직한 기록을 남긴다.

    자기 보고는 받지 않는다. "테스트 돌렸습니다"를 증거로 받으면 그 순간 이
    게이트는 장식이 된다 — 하네스가 실행하고 결과를 본다.

    돌릴 수 있는 명령을 검증 패턴으로 **제한한다.** 제한하지 않으면 이 명령이
    PreToolUse 를 우회하는 셸이 된다. 셸 메타문자도 거부한다 —
    `pytest; rm -rf /` 는 패턴에 걸리지만 앞부분만 검증 명령이다.
    """
    import shlex
    import subprocess
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    cmd = " ".join(argv[argv.index("--") + 1:]).strip() if "--" in argv else ""
    if not cmd:
        print("사용법: harness verify -- <검증 명령>")
        print("  예: harness verify -- pytest tests/")
        return 2
    stages = evidence_stages(cfg)
    if sid not in stages:
        print("verify 는 %s 단계에서 쓴다 (현재 %s)."
              % (", ".join(stages), label_of(cfg, sid)))
        return 2
    if set(cmd) & SHELL_META:
        print("셸 메타문자가 있는 명령은 거부한다 — 검증 명령 하나만 넘겨라.")
        return 2
    pat = cfg.at("criteria.verification_evidence.bash_pattern")
    if pat and not re.search(pat, cmd):
        print("검증 명령으로 보이지 않는다: %s" % cmd)
        print("이 자리는 검증을 돌리는 곳이다. 임의의 명령을 돌리는 곳이 아니다.")
        return 2
    try:
        rc = subprocess.call(shlex.split(cmd), cwd=root)
    except OSError as e:
        print("실행할 수 없다: %s" % e)
        return 2
    if rc != 0:
        # 실패도 사실이므로 적립한다. 증거로는 세지 않는다.
        with con:
            record_event(con, lid, sid, "tool_fail", "verify", norm_cmd(cmd),
                         "exit %d" % rc)
        print("\n검증 실패 (exit %d) — 증거로 기록하지 않았다. 고치고 다시 돌려라." % rc)
        return 1
    with con:
        record_evidence(con, lid, sid, "verification_evidence",
                        ("verify: " + cmd)[:120])
    print("\n검증 통과 — 증거로 기록했다: %s" % cmd)
    return 0


def cli_allow(ctx, argv):
    con, lid = ctx.con, ctx.lid
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


def cli_approve_plan(ctx, argv):
    con, root, lid, sid = ctx.con, ctx.root, ctx.lid, ctx.sid
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


# 회고 파일에서 읽는 범위. 이 밖의 내용은 `recall` 이 못 본다 — 즉 존재하지 않는
# 것과 같다. 회고 키 확인도 **같은 상수**를 써야 확인이 거짓말을 하지 않는다.

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




def _recall_files(cfg, root, keywords, limit=6):
    """회고·학습·트러블슈팅 파일 중 키워드에 걸리는 것. 내용은 읽지 않고 경로만 준다.

    인덱스 파일은 키워드와 무관하게 항상 앞에 놓는다. 파일이 수백 개로 쌓이면
    개별 파일 6개를 보여주는 것보다 전체를 요약한 인덱스 하나가 낫다.
    """
    keywords = _expand_keywords(keywords) if keywords else set()
    indexes, hits = [], []
    for sub in recall_dirs(cfg):
        d = os.path.join(root, ".dev", sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d), reverse=True):
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue
            rel = ".dev/%s/%s" % (sub, name)
            if name in index_names(cfg):
                indexes.append(rel)
                continue
            if not keywords:
                hits.append(rel)
                continue
            hay = name.lower()
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    hay += "\n" + fh.read(recall_read_bytes(cfg)).lower()
            except Exception:
                pass
            if any(kw.lower() in hay for kw in keywords):
                hits.append(rel)
    return indexes, hits[:max(0, limit - len(indexes))]


def tidy_report(con, cfg, root):
    """정리 후보. 판정은 전부 파일시스템 사실이고 LLM 이 끼지 않는다.

    "정리하라"는 권고는 아무 일도 만들지 않았다. 무엇을 정리할지의 목록이라야
    행동이 된다. 삭제·병합 자체는 여전히 자율이다 — 후보만 제시한다.
    """
    thr = cfg.num("tidy.dir_file_threshold", 12, low=1)
    age_days = cfg.num("tidy.age_days", 30, low=1)
    group_min = cfg.num("tidy.merge_group", 3, low=2)
    cutoff = time.time() - age_days * 86400
    open_loops = {r["id"] for r in
                  con.execute("SELECT id FROM loop WHERE closed_at IS NULL")}

    out = {"dirs": [], "stale": [], "groups": [], "learned": None, "regressed": []}
    for sub in recall_dirs(cfg):
        d = os.path.join(root, ".dev", sub)
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        files = [n for n in names
                 if os.path.isfile(os.path.join(d, n)) and n not in index_names(cfg)]
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


def cli_tidy(ctx, argv):
    """줄이는 것도 일이다. 쌓이면 복잡해지는 시스템은 복리가 아니다."""
    con, cfg, root = ctx.con, ctx.cfg, ctx.root
    with con:
        sync_promotions(con, cfg)
    refresh_learned(con, cfg, root)
    rep = tidy_report(con, cfg, root)
    if "--json" in argv:
        dump_json({k: ([dict(r) for r in v] if k == "regressed" else v)
                   for k, v in rep.items()})
        return 0
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


# 사전승인(preauth)은 표에는 보이지만 **판정에는 들어가지 않는다.** 무인 실행을
# 회피로 세면 그 판정은 모드 선택을 비난하는 것이 된다.
TREND_KEYS = (("blocks", "차단"), ("refails", "반복실패"), ("churn", "재편집"),
              ("bypass", "우회"), ("skips", "스킵"), ("declines", "보류"),
              ("preauth", "사전승인"))


def trend_verdict(avgs):
    """Goodhart 가드. 차단은 아무것도 시도하지 않거나 우회해도 줄어든다.

    그래서 마찰과 회피를 **함께** 판정한다. 둘을 한 점수로 합치면 이 구분이
    사라지고, 회피가 개선으로 보인다.
    """
    if len(avgs) < 2:
        return None
    # preauth 는 의도적으로 빠져 있다 — 아래 evasion 계산을 보라.
    first, last = avgs[0], avgs[-1]
    friction = last["blocks"] + last["refails"] < first["blocks"] + first["refails"]
    evasion = (last["bypass"] + last["skips"] + last["declines"]
               > first["bypass"] + first["skips"] + first["declines"])
    if friction and evasion:
        return "evasion"
    if friction:
        return "improving"
    if evasion:
        return "mismatch"
    return "flat"


VERDICT_TEXT = {
    "evasion": "⚠ 마찰이 줄었지만 우회·스킵·보류가 늘었다 — 개선이 아니라 회피일 수 "
               "있다. 게이트가 연극이 되고 있는지 보라.",
    "improving": "✓ 마찰이 줄고 우회는 늘지 않았다 — 개선 신호다 "
                 "(작업 난이도 차이는 통제하지 못한다).",
    "mismatch": "⚠ 우회가 늘었는데 마찰은 줄지 않았다 — 규칙이 맞지 않는지 보라.",
}


def metrics_report(ctx):
    """복리 측정 데이터. 출력하지 않는다."""
    con, cfg = ctx.con, ctx.cfg
    with con:
        sync_promotions(con, cfg)
    lc = con.execute("SELECT COUNT(*) c, MIN(created_at) a, MAX(created_at) b "
                     "FROM loop").fetchone()
    cyc = _cycle_rows(con)
    buckets, avgs = [], []
    for lo, hi, rows in _bucket(cyc):
        avg = {k: sum(r.get(k, 0) for r in rows) / float(len(rows))
               for k, _ in TREND_KEYS}
        avgs.append(avg)
        buckets.append({
            "from": lo, "to": hi, "avg": avg,
            "fails": sum(r.get("fails", 0) for r in rows),
            "refails": sum(r.get("refails", 0) for r in rows),
        })
    return {
        "loops": lc["c"],
        "cycles": len(cyc),
        "span": [(lc["a"] or "")[:10], (lc["b"] or "")[:10]] if lc["a"] else None,
        "survival": _survival(con, cfg),
        "buckets": buckets,
        "verdict": trend_verdict(avgs),
    }


NO_CYCLES = "  (회차 종료 기록이 없다. `advance --cycle` 또는 `--done` 때 쌓인다)"


def render_metrics(d):
    print("복리 측정 — 작업 %d개, 기록된 회차 %d개%s"
          % (d["loops"], d["cycles"],
             " (%s ~ %s)" % tuple(d["span"]) if d["span"] else ""))

    print("\n① 승격 생존율 — 무엇이 실제로 막았나")
    agg = d["survival"]
    if not agg:
        print("  (아직 승격이 없다. 여러 작업에서 반복된 항목이 생기면 쌓인다)")
    else:
        for k in sorted(agg, key=lambda x: -agg[x]["n"]):
            v = agg[k]
            seen = ("변경관측 %d/%d" % (v["vy"], v["vn"])) if v["vn"] else ""
            print("  %-10s %2d건 중 %2d건 재발 (%s)  %s"
                  % (k, v["n"], v["re"], _pct(v["re"], v["n"]).strip(), seen))
        total = sum(v["n"] for v in agg.values())
        if total < 20:
            print("  ⚠ 표본 %d건. 비율을 믿지 마라 — 20~30건은 있어야 한다." % total)
        print("  재발 = 승격 이후 같은 항목이 다시 걸린 것. 변경관측 = 그 회차에 "
              "주장에 맞는 파일 변경이 있었나.")

    print("\n② 회차 추세 — 마찰과 회피를 나란히 본다")
    if not d["buckets"]:
        print(NO_CYCLES)
    else:
        print("  %-12s %s" % ("회차구간", " ".join("%8s" % t for _, t in TREND_KEYS)))
        for b in d["buckets"]:
            print("  %-12s %s" % ("%d-%d회" % (b["from"], b["to"]),
                                  " ".join("%8.1f" % b["avg"][k] for k, _ in TREND_KEYS)))
        if d["verdict"] in VERDICT_TEXT:
            print("  " + VERDICT_TEXT[d["verdict"]])

    print("\n③ 반복 실패 비율 — 실패 주입이 겨냥한 것")
    # 누적 비율(전체 실패 중 첫 실패가 아닌 것)은 쓰지 않는다. 명령 종류는 적고
    # 실행은 많으니 시간이 지나면 무조건 100% 에 수렴한다 — 창이 없는 비율은
    # 아무것도 말해주지 않는다. 회차 구간별로만 읽는다.
    if not d["buckets"]:
        print(NO_CYCLES)
    else:
        for b in d["buckets"]:
            print("  %-12s 실패 %3d건 중 이전에도 실패한 것 %3d건 (%s)"
                  % ("%d-%d회" % (b["from"], b["to"]), b["fails"], b["refails"],
                     _pct(b["refails"], b["fails"]).strip()))
        print("  '이전에도 실패한 것' = 그 회차 시작 전에 이미 같은 명령이 실패한 적 있음.")

    print("\n측정하지 못하는 것: 결과물의 품질, 그 회차가 필요했는지, 사람이 아낀 시간.")
    print("점수를 만들지 않는 이유: 하나로 합치면 그 하나를 최적화하게 된다.")


def cli_metrics(ctx, argv):
    """복리 측정. 점수를 만들지 않는다 — 합치면 그 하나를 최적화하게 된다."""
    data = metrics_report(ctx)
    dump_json(data) if "--json" in argv else render_metrics(data)
    return 0


def cli_promote(ctx, argv):
    """반복된 항목을 승격하거나, 승격하지 않기로 결정한다.

    승인 다이얼로그를 띄우지 않는다 — 이건 게이트 우회가 아니라 기록이기 때문이다.
    대신 결정은 전부 event 에 남아 `stats` 에 드러나고, 승격 후에도 같은 항목이
    다시 걸리면 결정이 무효화되어 다시 올라온다. 무성의한 보류는 되돌아온다.
    """
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
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
        data = promote_report(ctx)
        dump_json(data) if "--json" in argv else render_promote(data)
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
              % cfg.num("promotion.reopen_after_loops", 2, low=1))
    else:
        print("  성숙도 established. 재발 없이 작업 %s개가 지나면 proven 이 된다."
              % cfg.num("promotion.proven_after_loops", 3, low=1))
    left = pending_promotions(con, cfg)
    print("남은 결정: %d개" % len(left))
    return 0


def cli_recall(ctx, argv):
    """과거 관측 기록과 회고 파일을 조회한다 (pull). 무엇이 관련 있는지는 호출자가 판단한다."""
    con, cfg, root, lid = ctx.con, ctx.cfg, ctx.root, ctx.lid
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

    indexes, files = _recall_files(cfg, root, keywords)
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


def cli_stats(ctx, argv):
    """누적 수치. --loop 를 주면 현재 작업만."""
    con, cfg, root, lid = ctx.con, ctx.cfg, ctx.root, ctx.lid
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
                             ("bypass", "우회한 게이트 (사전승인 포함)", "rule")):
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


def cli_auto_skip(ctx, argv):
    """스킵 자동 승인 토글. on 은 PreToolUse 가 사람의 동의를 받은 뒤에만 도달한다."""
    con, lid = ctx.con, ctx.lid
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


def cli_loop(ctx, argv):
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
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
    "verify": cli_verify,
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
              "/step-seven-harness:install 을 실행하라.", file=sys.stderr)
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
            return fn(Ctx(con, cfg, root, lid, sid), argv[1:])
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


def ensure_permissions(root, tries=3):
    """하네스 조회 명령을 프로젝트 설정에 미리 허용한다.

    매번 권한 프롬프트를 요구하면 모델이 조회를 포기하고 파일을 직접 읽는
    우회로 간다 — 실제 세션에서 관측된 문제다.

    **비교-교환으로 쓴다.** 이 파일은 Claude Code 도 쓴다(`enabledPlugins` 등).
    읽고-고치고-쓰는 사이에 저쪽이 쓰면 우리가 그걸 덮어 없앤다 — 플러그인
    활성화가 통째로 사라지는 방향이다. 쓰기 직전에 다시 읽어 우리가 읽었던 것과
    같은지 확인하고, 다르면 새 내용으로 다시 병합한다.

    반환값: 추가한 규칙 수 / 0 = 더할 것 없음 / -1 = 손상되어 건드리지 않음
            / -2 = 다른 쪽이 계속 쓰고 있어 포기
    """
    path = os.path.join(root, ".claude", "settings.json")

    def read_raw():
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return False          # 읽을 수 없음 (None 과 구분한다)

    for _ in range(max(1, tries)):
        raw = read_raw()
        if raw is False:
            return -1
        if raw is None:
            data = {}
        else:
            try:
                data = json.loads(raw) if raw.strip() else {}
            except ValueError:
                return -1         # 손상된 설정은 건드리지 않는다
            if not isinstance(data, dict):
                return -1

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

        # 쓰기 직전에 다시 읽는다. 우리가 읽은 뒤 누가 바꿨으면 그 내용으로 다시 한다.
        if read_raw() != raw:
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return len(added)
    return -2


# 설치는 여섯 가지 서로 다른 일이다. 한 함수에 154줄로 뭉쳐 있으면 하나가
# 예외를 던졌을 때 어디까지 됐는지 알 수 없고(실제로 부분 설치 상태가 남았다),
# 각각을 따로 테스트할 수도 없다. 단계마다 "무엇을 만들었나"를 돌려준다.

def install_templates(root, pr):
    """원칙·근거·설정 문서. **이미 있으면 덮어쓰지 않는다** — 사용자가 고친 것이다."""
    made = []
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
        made.append(rel)
    return made


def install_db(root, cfg):
    """스키마를 적용하고 활성 작업을 보장한다. 업그레이드 경로가 이 함수다 —
    `CREATE TABLE IF NOT EXISTS` 라서 재실행이 곧 스키마 갱신이다."""
    made = []
    fresh = not os.path.isfile(os.path.join(root, DB_REL))
    con = connect(root, create=True)
    try:
        con.executescript(SCHEMA)
        con.commit()
        lid = head_loop(con)
        if not lid or not active_stage(con, lid):
            with con:
                lid = create_loop(con, cfg, root)
            made.append("%s (loop %s)" % (DB_REL, lid))
        elif fresh:
            made.append(DB_REL)
        # 앵커가 가리키는 파일이 없으면 CLAUDE.md 임포트가 깨진다. 빈 상태로라도 만든다.
        if refresh_learned(con, cfg, root):
            made.append(LEARNED_REL)
    finally:
        con.close()
    return made, lid


def install_gitignore(root):
    """런타임 상태를 커밋 대상에서 뺀다."""
    gi = os.path.join(root, ".gitignore")
    want = [".claude/harness/harness.db", ".claude/harness/harness.db-wal",
            ".claude/harness/harness.db-shm", ".claude/harness/bin/"]
    have = open(gi, encoding="utf-8").read() if os.path.isfile(gi) else ""
    # 행 단위로 본다. substring 으로 보면 주석에 경로가 언급된 것만으로
    # "이미 있다"고 판단해 실제 ignore 규칙을 넣지 않는다.
    lines = {ln.strip() for ln in have.splitlines()}
    add = [w for w in want if w not in lines]
    if not add:
        return []
    with open(gi, "a", encoding="utf-8") as fh:
        if have and not have.endswith("\n"):
            fh.write("\n")
        fh.write("\n# step-seven-harness (런타임 상태 — 커밋하지 않는다)\n")
        fh.write("\n".join(add) + "\n")
    return [".gitignore"]


AGENTS_MARK = "<!-- step-seven-harness -->"
AGENTS_BLOCK = """%s
## 작업 절차 — 이 저장소는 하네스로 절차를 강제한다

먼저 읽어라. 이 둘이 이 저장소의 규칙이다.

- `%s` — 사람이 정한 원칙
- `%s` — 반복된 실수에서 승격된 규칙

일을 시작하기 전에 현재 단계를 확인하고, **단계를 건너뛰지 마라.**

```
%s status              # 현재 단계, 남은 종료 조건, 이번 작업의 완료 조건
%s advance             # 종료 조건을 채웠으면 다음 단계로
%s verify -- <검증 명령>  # 검증은 하네스가 직접 돌리고 종료 코드로 판정한다
```

`advance` 는 종료 조건이 남아 있으면 **거부하고** 무엇이 남았는지 말한다.
"검증했습니다" 같은 자기 보고는 증거가 아니다 — `verify` 로 실제로 돌려라.
산출물은 `.dev/` 아래에 `<작업해시>-<회차>-` 로 시작하는 이름으로 쓴다.

이 게이트는 훅 없이 CLI 만으로 동작하므로 어떤 에이전트 도구에서도 같다.
""" % (AGENTS_MARK, POLICY_REL.replace(os.sep, "/"), LEARNED_REL.replace(os.sep, "/"),
       WRAPPER_CMD, WRAPPER_CMD, WRAPPER_CMD)


def install_agents_md(root):
    """AGENTS.md 에 절차 안내를 한 번 붙인다.

    왜 CLAUDE.md 와 따로 다루나: AGENTS.md 는 `@import` 를 모른다. 그건 Claude
    Code 의 기능이고, Codex·Cursor·Copilot·Gemini CLI·Aider·Windsurf·Zed 는
    이 파일을 **그냥 마크다운으로 읽는다.** 그러니 앵커 한 줄이 아니라 읽으면
    바로 쓸 수 있는 문장이어야 한다.

    이미 표시가 있으면 **아무것도 하지 않는다.** 다시 써서 갱신하는 편이
    깔끔하겠지만, 사용자가 이 블록 안을 고쳤을 때 그것을 지운다. 남의 글을
    잃는 것은 낡은 안내보다 나쁘다 — CLAUDE.md 앵커와 같은 규칙이다.
    """
    p = os.path.join(root, "AGENTS.md")
    body = open(p, encoding="utf-8").read() if os.path.isfile(p) else ""
    if AGENTS_MARK in body:
        return []
    with open(p, "a", encoding="utf-8") as fh:
        if body and not body.endswith("\n"):
            fh.write("\n")
        fh.write(("\n" if body else "") + AGENTS_BLOCK)
    return ["AGENTS.md (%s)" % ("절차 안내 추가" if body else "새로 만듦")]


def install_anchors(root):
    """CLAUDE.md 에 앵커 두 줄. POLICY 는 사람이 정한 원칙, LEARNED 는 하네스가
    승격한 규칙 — 한 파일에 섞으면 생성 대상과 손으로 쓴 것이 구분되지 않는다."""
    cm = os.path.join(root, "CLAUDE.md")
    body = open(cm, encoding="utf-8").read() if os.path.isfile(cm) else ""
    # 행 단위로 본다. 코드 예시나 설명문에 앵커 문자열이 있으면 substring 판정은
    # 실제 import 행이 없는데도 있다고 착각한다.
    lines = {ln.strip() for ln in body.splitlines()}
    add = [a for a in ("@%s" % POLICY_REL.replace(os.sep, "/"),
                       "@%s" % LEARNED_REL.replace(os.sep, "/"))
           if a not in lines]
    if not add:
        return []
    with open(cm, "a", encoding="utf-8") as fh:
        if body and not body.endswith("\n"):
            fh.write("\n")
        fh.write("\n" + "\n".join(add) + "\n")
    return ["CLAUDE.md (앵커 %d줄)" % len(add)]


def cli_init(argv):
    root = os.path.abspath(argv[0] if argv else os.getcwd())
    pr = plugin_root()
    created = install_templates(root, pr)
    db_made, lid = install_db(root, load_config(root, pr))
    created += db_made
    refresh_wrapper(root)

    nperm = ensure_permissions(root)
    if nperm > 0:
        created.append(".claude/settings.json (조회 명령 %d개 허용)" % nperm)
    elif nperm == -2:
        print("주의: .claude/settings.json 을 다른 쪽이 동시에 쓰고 있어 권한 허용을 "
              "건너뛰었다. 남의 변경을 덮지 않으려고 포기한 것이다 — `harness init` 을 "
              "다시 실행하면 된다.", file=sys.stderr)
    elif nperm < 0:
        print("주의: .claude/settings.json 을 읽을 수 없어 권한 허용을 건너뛰었다.",
              file=sys.stderr)

    created += install_gitignore(root)
    created += install_anchors(root)
    created += install_agents_md(root)
    label = None
    try:
        con2 = connect(root)
        if con2 is not None:
            try:
                sid2 = active_stage(con2, lid)
                if sid2:
                    label = label_of(load_config(root, pr), sid2)
            finally:
                con2.close()
    except Exception:
        pass
    render_init(root, created, lid, label)
    return 0


# ----------------------------------------------------------------------- render
#
# 계산과 출력을 가른다. 섞여 있을 때는 테스트가 stdout 을 grep 할 수밖에 없었고,
# 그래서 내가 `'^[^g]*$'` 처럼 **실패할 수 없는** 정규식을 썼다 — LEARNED.md
# 표류가 초록 상태로 출하된 직접 원인이다. 보고 명령은 `--json` 으로 구조를
# 그대로 내보내므로, 테스트가 산문이 아니라 값을 검사할 수 있다.

def promote_report(ctx):
    """승격 결정 현황. 계산만 한다."""
    return {
        "pending": pending_promotions(ctx.con, ctx.cfg),
        "options": [{"as": k, "why": v} for k, v in PROMOTE_AS.items()],
        "decided": [dict(r) for r in ctx.con.execute(
            "SELECT key, decision, maturity, note FROM promotion "
            "ORDER BY at DESC LIMIT 8")],
    }


def render_promote(d):
    pend = d["pending"]
    print("승격 결정이 필요한 항목 (%d개)" % len(pend))
    if not pend:
        print("  (없음 — 여러 작업에서 반복된 항목이 아직 없다)")
    for it in pend:
        mark = ("  ← %s 로 승격했는데 다시 걸렸다" % it["regressed"]
                if it.get("regressed") else "")
        print("  %-34s ×%d, 작업 %d개%s"
              % (it["key"][:34], it["count"], it["loops"], mark))
    if pend:
        print("\n결정 방법 (하나 고른다):")
        for o in d["options"]:
            flag = ("--decline --reason \"...\"" if o["as"] == "declined"
                    else "--as %s --note \"...\"" % o["as"])
            print("  harness promote <key> %-32s %s" % (flag, o["why"]))
    if d["decided"]:
        print("\n이미 결정된 항목")
        for r in d["decided"]:
            print("  %-30s %-10s %-12s %s"
                  % (r["key"][:30], r["decision"], r["maturity"],
                     (r["note"] or "-")[:40]))


def render_init(root, created, lid, stage_label=None):
    print("하네스 설치 완료: %s" % root)
    for c in created:
        print("  + %s" % c)
    if not created:
        print("  (변경 없음 — 이미 설치되어 있다)")
    # 단계를 **여기서** 말한다. 문서에 적어두면 단계 구성이 바뀔 때 뒤처지고,
    # 모델은 그 낡은 문장을 정확히 따라 틀린 말을 한다 — 실제로 그렇게 됐다
    # (0.10.0 에서 Selection 을 신설한 뒤에도 스킬 문서가 `1/6 Scaffolding` 이었다).
    print("활성 작업: %s%s" % (lid, " · 단계 %s" % stage_label if stage_label else ""))
    print("커밋 대상: .claude/harness/{POLICY.md,LEARNED.md,stages.json,rationale.md}, "
          "CLAUDE.md, AGENTS.md")


def dump_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


USAGE = """step-seven-harness — 작업 하네스

  0 Selection → 1 Scaffolding → 2 Context → 3 Planning
              → 4 Execution → 5 Verification → 6 Compounding
                                   ├─ 작업 끝    → 0 Selection
                                   └─ 회차 계속  → 1 Scaffolding

현재 상태
  status                       현재 작업·회차·단계·종료 조건·증거·스킵 기록·예외

  보고 명령(status·metrics·tidy·promote)은 `--json` 으로 구조를 그대로 낸다.
  산문 대신 값을 검사해야 할 때 쓴다.
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
  verify -- <검증 명령>        하네스가 직접 돌려 종료 코드로 판정한다. 통과해야
                               증거가 된다. 훅이 없는 도구에서도 이 길은 열려 있다

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
