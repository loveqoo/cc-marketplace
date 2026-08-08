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
import abc
import contextlib
import fnmatch
import glob as globlib
import hashlib
import json
import os
import re
import shlex
import sqlite3
import sys
import time

HARNESS_DIR = os.path.join(".claude", "harness")
WRAPPER_CMD = ".claude/harness/bin/harness"
# **엔진 파일의 위치는 여기서 한 번만 정한다.**
# `__file__` 은 그 코드가 **어느 모듈에 있는지**를 말한다. 구현을 `parts/` 로
# 가르자 `refresh_engine` 이 `parts/setup.py` 로 따라가면서 자기 위치를
# `scripts/parts/` 로 계산했고, 사본이 납작해지고 `harness.py` 가 빠졌다.
# 조용히 깨지는 종류다 — 사본은 만들어지는데 엔진이 없다.
ENGINE_FILE = os.path.abspath(__file__)
DB_REL = os.path.join(HARNESS_DIR, "harness.db")
CONFIG_REL = os.path.join(HARNESS_DIR, "stages.json")
POLICY_REL = os.path.join(HARNESS_DIR, "POLICY.md")
RATIONALE_REL = os.path.join(HARNESS_DIR, "rationale.md")
LEARNED_REL = os.path.join(HARNESS_DIR, "LEARNED.md")

# --------------------------------------------------------------------- 메시지
#
# 출력 문자열을 데이터로 뺀다. **원문이 곧 키다**(gettext 방식) — 키를 따로 발명하면
# 키와 문장이 어긋나고, 어긋난 것을 아무도 모른다. 카탈로그에 없으면 원문을 그대로
# 돌려주므로 번역이 없거나 일부만 있어도 동작이 바뀌지 않는다.
#
# 서식 문자열은 리터럴 자리에서 감싼다: `t("현재 %s") % x` — 보간 뒤에는 원문을
# 찾을 수 없으므로 출력 지점에서 번역할 수 없다.
#
# 모듈 상수(USAGE 등)는 정의 시점에 카탈로그가 없으므로 **쓰는 자리**에서 감싼다.
# tests/msg_check.py 가 감싸기 누락을 전수 검사한다 — 사람이 세는 일이 아니다.

_MESSAGES = {}
LANG_ENV = "HARNESS_LANG"
# 정의 시점에 번역할 수 없어 사용 지점에서 감싸는 상수들. 검사기가 이 목록을 안다.
LAZY_MSG_NAMES = ("USAGE", "AGENTS_BLOCK", "LEARNED_HEAD", "PROMOTE_AS_DEFAULT",
                  "UNRELATED", "NOT_VERIFICATION",
                  "INNOCUOUS_TOOLS",
                  "SELF_LOCK_MSG", "TREND_KEYS", "VERDICT_TEXT",
                  "NO_CYCLES", "WRAPPER", "RECALL_DIRS_DEFAULT",
                  "INDEX_NAMES_DEFAULT", "BASH_READERS_DEFAULT",
                  "BASH_INTERPRETERS_DEFAULT", "SHELL_META", "BASH_MUTATORS",
                  "BASH_OPAQUE", "NEVER_BENIGN")


_LANG = ""


def t(s):
    """번역. 없으면 원문. 번역 누락이 동작을 바꾸지 않는 것이 이 함수의 계약이다."""
    return _MESSAGES.get(s, s)


def message_status(root):
    """(요청 언어, 번역된 수, 전체 수). 부분 번역이 조용하지 않게 하려고 있다.

    `language: en` 을 켰는데 카탈로그가 없으면 **전부 한국어로 나온다** — 그리고 켠
    사람은 그걸 '번역이 안 된 것'이 아니라 '설정이 안 먹은 것'으로 읽는다. 어느
    쪽이든 말해줘야 한다.
    """
    if not _LANG or _LANG == "ko":
        return "", 0, 0
    total = 0
    for cand in (os.path.join(root, HARNESS_DIR, "bin", "messages.ko.json"),
                 os.path.join(plugin_root(), "templates", "messages.ko.json")):
        spec = jload(cand)
        if isinstance(spec, dict):
            total = len(spec)
            break
    return _LANG, len(_MESSAGES), total


def load_messages(root, lang=None):
    """카탈로그를 읽는다. 실패하면 조용히 원문으로 간다 — 번역 때문에 게이트가
    멈추는 것은 어떤 번역 누락보다 나쁘다."""
    global _MESSAGES, _LANG
    _MESSAGES = {}
    # 환경변수가 설정을 이긴다. 파일을 고치지 않고 시험해 보려고 쓰는 것이므로,
    # 설정에 language 가 적혀 있다고 무시하면 그 용도가 사라진다.
    lang = os.environ.get(LANG_ENV) or lang or ""
    _LANG = lang
    if not lang or lang == "ko":
        return 0        # 원문이 한국어다. 조회 자체를 하지 않는다.
    for cand in (os.path.join(root, HARNESS_DIR, "bin", "messages.%s.json" % lang),
                 os.path.join(plugin_root(), "templates", "messages.%s.json" % lang)):
        data = jload(cand)
        if isinstance(data, dict):
            _MESSAGES = {k: v for k, v in data.items()
                         if isinstance(k, str) and isinstance(v, str) and v}
            return len(_MESSAGES)
    return 0


WRITE_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
}


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
-- 회차 한정 경로 노드. stages.json 은 안정된 틀로 남고, 진행 중 더한 노드는
-- 여기 담겨 **이번 회차에만** 산다 — 회차가 닫히면 스코프 밖으로 나가고,
-- 이력은 event(path_add/path_remove) 로 남는다.
CREATE TABLE IF NOT EXISTS path_node (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop_id TEXT, cycle INTEGER, node TEXT, label TEXT, summary TEXT,
  write TEXT, after_node TEXT, reason TEXT, at TEXT,
  UNIQUE(loop_id, cycle, node));
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
    "cycle_adopt": "회차 포기 (재연결) — 측정에는 남고 회고 창은 그대로",
    "cycle_adopt_reason": "재연결 사유",
    "retro_keys": "회고 검색 키 확인",
    "plan_mode_exit": "plan mode 종료 관측",
    "stop_continue": "턴 이어붙임",
    "stop_stalled": "진전 없어 이어붙임 중단",
    "path_add": "경로 노드 추가 (회차 한정)",
    "path_remove": "경로 노드 삭제",
    "branch": "분기 선택",
}

# 승격 결정의 종류. 'declined' 는 "안 한다 + 사유" — 결정이지 회피가 아니다.
PROMOTE_AS_DEFAULT = {
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
    """설치된 프로젝트의 루트. **DB 가 없어도 설치는 설치다.**

    예전에는 `harness.db` 만 찾았다. 그래서 DB 파일이 지워지면 하네스는 이 프로젝트를
    '설치 안 함' 으로 보고 **아무 말 없이 모든 게이트를 껐다.** 설치하지 않은 것과
    고장 난 것은 다르고, 둘을 같게 다루면 고장이 침묵이 된다.

    설치의 표식은 사람의 문서(`stages.json`)다 — 그것이 있으면 이 프로젝트는 하네스를
    쓰기로 한 것이고, DB 가 없다는 사실은 `inactive()` 가 소리 내어 말한다.
    """
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
            if any(os.path.isfile(os.path.join(d, m)) for m in (DB_REL, CONFIG_REL)):
                return d
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return None


# 나중에 더해진 열. `CREATE TABLE IF NOT EXISTS` 는 **기존 표에 열을 더하지 못한다.**
# 여기 적고 연결할 때마다 맞춘다 — "재실행이 곧 스키마 갱신" 이라는 성질을 유지한다.
# 훅 하나에 허용된 시간. `hooks/hooks.json` 의 timeout 과 같아야 한다 (doc_check 가 본다).
HOOK_TIMEOUT_S = 10
# SQLite 잠금 대기. **훅 예산보다 넉넉히 작아야 한다** — 잠금을 기다리다 프로세스가
# 강제 종료되면 fail-open 경고조차 내지 못하고, 사용자는 게이트가 꺼진 줄도 모른다.
DB_WAIT_S = 4


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


class Refuse(Exception):
    """CLI 가 거절한다. **설명과 종료 코드를 함께 들고 나간다.**

    예전에는 `print(설명)` 과 `return 코드` 가 두 문장이었다. 둘을 잇는 것은
    규약이고, 규약은 지켜지지 않는다 — 하나를 빼먹으면 검사가 조용히 통과한다.

    **값으로 돌려주지 않는 이유:** Result 를 돌려주면 호출자가 그것을 버릴 수
    있고, 버려진 거절은 통과와 구분되지 않는다. 파이썬에는 "이 반환값을 반드시
    써라"를 강제할 방법이 없다. **무시할 수 없는 것은 예외뿐이다.**
    """

    def __init__(self, *lines, **kw):
        super(Refuse, self).__init__(lines[0] if lines else "")
        self.lines = [ln for ln in lines if ln]
        self.code = kw.pop("code", 2)
        if kw:
            raise TypeError("Refuse: %s" % ", ".join(sorted(kw)))


def sub_table():
    """`<명령> <서브명령>` 표 하나와 그 등록 데코레이터를 만든다.

    두 명령이 각자 if 체인으로 dispatch 를 다시 쓰면 **답이 갈린다.** 실제로
    갈려 있었다 — 모르는 이름에 `loop` 는 rc=0 으로 조용히 `show` 를 했고
    `auto-skip` 은 rc=2 로 거절했다. 같은 종류에는 답이 하나여야 한다.
    """
    table = {}

    def register(name):
        def deco(fn):
            table[name] = fn
            return fn
        return deco

    return table, register


LOOP_SUBS, loop_sub = sub_table()
AUTO_SKIP_SUBS, auto_skip_sub = sub_table()
PATH_SUBS, path_sub = sub_table()


def dispatch(table, cmd, sub):
    """서브명령을 표에서 찾는다. **없으면 거절한다.**

    `if sub == "new": ... if sub == "intent": ...` 은 dispatch 를 손으로 다시
    쓴 것이다. 손으로 쓰면 빠진 가지가 조용히 마지막 기본값으로 떨어진다 —
    `harness loop inetnt "작업 내용"` 이 **rc=0 으로 아무 일도 하지 않았다.**
    사용자는 기록됐다고 믿는다. 표는 모르는 이름을 알아본다.
    """
    fn = table.get(sub)
    if fn is None:
        raise Refuse(t("알 수 없는 %s 서브명령: %s") % (cmd, sub),
                     t("가능: %s") % " | ".join(sorted(table)))
    return fn


# 아래 다섯 개는 **정책·내용**이라 설정이 정한다. 엔진에 박아 두면 프로젝트마다 다른
# 것을 하나로 강요하게 되고, 특히 recall_dirs 는 조용한 결함이었다 — 설정에 폴더를
# 더해도 recall 이 그 폴더를 못 봐서, 안내대로 고친 사람이 **다시 안 읽히는 기록**을
# 쌓게 됐다. 파일은 있고 키워드도 맞는데 영원히 안 나온다. 복리의 반대다.
RECALL_DIRS_DEFAULT = ("retrospect", "learning", "troubleshooting")
INDEX_NAMES_DEFAULT = ("INDEX.md", "README.md")


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


# ------------------------------------------------------------------- loop/stage

def new_loop_id():
    seed = "%d|%d|%s" % (time.time_ns(), os.getpid(), os.urandom(8).hex())
    return time.strftime("%y%m%d") + "-" + hashlib.sha256(seed.encode()).hexdigest()[:6]


def git_branch(root):
    """현재 브랜치. **워크트리의 `.git` 은 디렉터리가 아니라 파일**이라 예전에는 늘 None 이었다."""
    try:
        dot = os.path.join(root, ".git")
        if os.path.isfile(dot):                      # `gitdir: <실제 경로>`
            with open(dot, encoding="utf-8") as fh:
                dot = fh.read().split("gitdir:", 1)[1].strip()
            if not os.path.isabs(dot):
                dot = os.path.join(root, dot)
        with open(os.path.join(dot, "HEAD"), encoding="utf-8") as fh:
            line = fh.read().strip()
        return line.split("refs/heads/", 1)[1] if "refs/heads/" in line else line[:12]
    except Exception:
        return None


def stage_rows(con, lid):
    return {r["stage"]: r for r in
            con.execute("SELECT * FROM stage WHERE loop_id=?", (lid,))}


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


# 곁다리 작업이 실패한 사실. `swallow` 가 채우고 `status` 가 보여준다.
SWALLOWED = []
# 삼킨 사실을 적는 곳. `run_hook`/`run_cli` 가 root 를 알게 되면 채운다.
SWALLOW_LOG = None
SWALLOW_LOG_REL = os.path.join(HARNESS_DIR, "swallowed.log")
SWALLOW_KEEP = 50          # 최근 N 줄만 남긴다. 무한히 자라지 않는다.


def swallow_note(line):
    """삼킨 사실을 **프로세스 밖으로** 남긴다.

    `SWALLOWED` 는 모듈 전역 리스트고 훅은 매번 새 프로세스로 떠서 즉시 죽는다.
    그래서 훅에서 삼킨 것은 **어디에도 남지 않았다** — 읽기 전용 FS 에서 모든
    관측 기록이 사라지는 중인데 `status` 는 초록이었다(5회차 D-H1·C⑤).

    DB 가 아니라 파일에 적는다. 삼킴이 가장 많이 일어나는 때가 바로 DB 를 쓸 수
    없을 때이기 때문이다. 파일도 못 쓰면 그때는 정말 할 수 있는 것이 없다 —
    그 실패는 조용히 넘긴다(중첩 삼킴).
    """
    if not SWALLOW_LOG:
        return
    try:
        # **append 로 쓴다.** 읽고-자르고-덮어쓰기는 동시 훅 둘이 삼키면 한쪽
        # 기록을 지웠다 — 삼킴이 몰리는 상황(읽기 전용 FS 등)이 정확히 훅이
        # 몰리는 상황이다(6회차). 자르기는 파일이 충분히 컸을 때만 하므로
        # 경합 창이 드문 경로로 좁혀진다. 상한 자체는 유지된다.
        entry = "%s  %s\n" % (now(), line)
        if (os.path.isfile(SWALLOW_LOG)
                and os.path.getsize(SWALLOW_LOG) > SWALLOW_KEEP * 200):
            with open(SWALLOW_LOG, encoding="utf-8") as fh:
                old = fh.read().splitlines()[-(SWALLOW_KEEP - 1):]
            with open(SWALLOW_LOG, "w", encoding="utf-8") as fh:
                fh.write("\n".join(old) + "\n" + entry)
        else:
            with open(SWALLOW_LOG, "a", encoding="utf-8") as fh:
                fh.write(entry)
    except OSError:
        pass


def swallowed_recent(root):
    """파일에 남은 삼킨 사실 — 최근 SWALLOW_KEEP 개.

    쓰기는 append 라(경합 유실을 막으려고) 줄 수 상한을 즉시 강제하지 않는다.
    상한은 두 겹으로 지킨다 — 파일 크기는 `swallow_note` 의 자르기가, 보이는
    개수는 여기가.
    """
    try:
        with open(os.path.join(root, SWALLOW_LOG_REL), encoding="utf-8") as fh:
            return [x for x in fh.read().splitlines()
                    if x.strip()][-SWALLOW_KEEP:]
    except OSError:
        return []


@contextlib.contextmanager
def swallow(what):
    """**곁다리 작업의 실패를 삼킨다 — 그러나 조용히는 아니다.**

    본 판정을 망가뜨리면 안 되는 부수 작업(스냅샷 기록, 후보 수집, 임시 파일
    정리)이 열한 곳에서 `try/except Exception: pass` 로 쓰여 있었다. 그 열한
    곳은 **실패해도 아무도 모르는 자리**다 — 이 플러그인이 스스로 최악이라고
    적어 둔 실패 모드(조용한 실패)를 자기 코드가 열한 번 하고 있었다.

    게이트가 꺼지는 출구를 `inactive()` 하나로 모은 것과 같은 이유로 모은다.
    삼키되 **사실은 남긴다.**
    """
    try:
        yield
    except Exception as exc:                  # noqa: BLE001 - 삼키는 것이 목적이다
        line = "%s: %s: %s" % (what, type(exc).__name__, exc)
        SWALLOWED.append(line)
        swallow_note(line)


def has_evidence(con, lid, kind):
    return con.execute("SELECT 1 FROM evidence WHERE loop_id=? AND kind=? LIMIT 1",
                       (lid, kind)).fetchone() is not None


def promotion_summary(con, cfg):
    rows = con.execute("SELECT maturity, COUNT(*) c FROM promotion "
                       "GROUP BY maturity").fetchall()
    return {r["maturity"]: r["c"] for r in rows}


LEARNED_HEAD = """# 승격된 규칙

반복된 실수에서 승격된 규칙이다. 하네스가 생성하므로 직접 편집하지 마라 —
`harness promote` 로 올리고 `harness tidy` 로 내린다. 항상 로드되므로 예산이 있다
(최대 %d줄). 예산이 찬 상태에서 새 규칙을 올리려면 먼저 한 줄을 비워야 한다.
"""


def human_criteria(cfg):
    """사람만 채울 수 있는 조건. 이것이 남았으면 턴을 밀지 않는다 —
    밀면 모델이 만들 수 없는 것을 만들려 애쓰고, 그 시도가 매번 다이얼로그가 된다."""
    return tuple(k for k, v in (cfg.obj("criteria") or {}).items()
                 if isinstance(v, dict) and v.get("human"))


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


def rel_aliases(root, path):
    """이 경로가 가리키는 것을 root 기준 상대경로 **여러 개**로 준다.

    ## 왜 하나로는 안 되나

    바닥값(SELF_LOCK)은 경로 **문자열**을 본다. 그런데 symlink 는 같은 파일에 다른
    문자열을 붙인다. `ln -s . alias` 를 만들고 `alias/.claude/harness/bin/harness` 를
    쓰면 문자열이 안 맞아 통과했다 — 5차 리뷰가 찾았고 재현했다. 그 파일은 사전
    승인된 래퍼이므로 결과는 **승인 없는 임의 코드 실행**이다.

    그래서 판정 단위를 '문자열 하나'에서 '이 경로가 가리킬 수 있는 문자열 전부'로
    바꾼다. 문자열 그대로와, symlink 를 푼 뒤가 **둘 다** 후보다.

      - 그대로가 필요한 이유: 바닥값 자체가 symlink 여서 밖을 가리키면(`bin` ->
        `/tmp/x`) 푼 결과는 root 밖이라 잡히지 않는다. 문자열은 잡힌다.
      - 푼 것이 필요한 이유: 위의 별칭 경로.

    아직 없는 파일도 판정한다(쓰기 **전**이다). 존재하는 조상까지만 풀고 나머지는
    이어 붙인다. root 자신이 symlink 인 경우(macOS 의 `/tmp` -> `/private/tmp`)를
    위해 root 도 푼 것과 안 푼 것 둘 다에 대해 상대화한다.
    """
    out = []
    lit = rel_to_root(root, path)
    if lit:
        out.append(lit)
    if not path:
        return out
    p = os.path.normpath(path if os.path.isabs(path) else os.path.join(root, path))
    try:
        tail, cur = [], p
        while not os.path.lexists(cur):
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            tail.append(os.path.basename(cur))
            cur = parent
        real = os.path.join(os.path.realpath(cur), *reversed(tail))
    except OSError:
        return out
    for base in (root, os.path.realpath(root)):
        r = rel_to_root(base, real)
        if r and r not in out:
            out.append(r)
    return out


def classify(rel, cfg):
    for cls, pats in cfg.get("path_classes", {}).items():
        for pat in pats:
            if glob_match(rel, pat):
                return cls
    return "source"


def find_grant(con, lid, rel):
    for g in live_grants(con, lid):
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
        f["allowed"] = ", ".join(w.stage.get("write") or []) or t("(없음)")
    name = req.get("subdir_in")
    if name:
        f["allowed"] = "/".join(w.seq(name))
    name = req.get("stage_in")
    if name:
        f["stages"] = ", ".join(
            stage_obj(w.ctx.cfg, s)["label"] for s in w.seq(name, ("scaffolding",))
            if s in stage_ids(w.ctx.cfg)) or t("(없음)")
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
        return r.get("id") or "?", _subst(t(r.get("deny")) or t("규칙 위반이다: {rel}"),
                                          _w_fields(w, r.get("require") or {}))
    return None, None


# 하네스 자신을 지키는 **바닥값**. 설정은 여기에 더할 수만 있고 뺄 수 없다.
#
# 왜 코드에 있나: 이것만 데이터로 두면 자기 잠금이 협상 가능해진다. `stages.json` 은
# `context` 클래스라 여러 단계에서 모델이 쓸 수 있고, 거기서 `protected` 규칙을 지우거나
# `protected_paths: []` 로 두거나 `write_rules: []` 로 비우면 엔진과 DB가 열렸다.
# 엔진을 쓸 수 있으면 **모든 게이트가 끝난다.** 적대적 리뷰에서 여섯 가지 모양으로
# 확인했고, 0.30.0 엔진에서도 뚫렸다 — 새 결함이 아니라 원래 있던 구조 문제다.
#
# "규칙이 틀렸으면 stages.json 을 고쳐라"는 그대로 유지된다. 단 하나, **잠금을 푸는
# 방향으로는** 고칠 수 없다. 근거 문서가 이미 "이 차단은 allow 로도 열리지 않는다"고
# 못박은 그 예외를 설정에도 적용하는 것이다.
SELF_LOCK = (".claude/harness/bin",
             ".claude/harness/bin/**",
             ".claude/harness/harness.db*")
SELF_LOCK_MSG = ("하네스 자신은 수정할 수 없다 (%s). 이 차단은 `allow` 로도, "
                 "`stages.json` 을 고쳐서도 열 수 없다 — 엔진과 상태를 바꿀 수 있으면 "
                 "모든 게이트가 무의미해진다. 규칙을 바꾸려면 `stages.json` 의 "
                 "규칙을, 엔진을 바꾸려면 플러그인을 수정하라.")


def self_lock_hit(rel):
    """바닥값에 걸리나. 설정을 보지 않는다 — 그게 요점이다.

    **대소문자를 무시하고 본다.** macOS·Windows 의 기본 파일시스템은 대소문자를
    구분하지 않으므로 `.claude/harness/BIN/harness.py` 는 `bin/harness.py` 와
    **같은 파일**이다. glob_match 는 대소문자를 구분해서 그 경로가 Write·Bash 양쪽에서
    통과했다 — 직접 확인했다. 대소문자만 다른 경로를 하네스 자기 영역에 만드는
    정당한 이유는 없으므로, 구분하는 파일시스템에서 과잉 차단이 되는 것을 받아들인다.
    """
    low = rel.lower()
    for pat in SELF_LOCK:
        if glob_match(rel, pat) or glob_match(low, pat.lower()):
            return True
    return False


def check_write(ctx, rel):
    """(decision, reason). decision None 이면 판정하지 않음.

    차단은 event 에 적립한다 — 어떤 규칙에 몇 번 걸리는지가 복리의 원료다.
    """
    w = WriteReq(ctx, rel)
    # 바닥값은 설정보다 앞이고 grant 도 보지 않는다. write_rules 가 비어 있어도 남는다.
    # **symlink 별칭까지 본다** — 문자열만 보면 `alias/.claude/harness/bin/harness` 로
    # 사전 승인된 래퍼를 덮어쓸 수 있었다.
    aliased = floor_hit(ctx.root, rel)
    if aliased:
        reason = t(SELF_LOCK_MSG) % aliased
        record_event(ctx.con, ctx.lid, ctx.sid, "block", "self_lock", rel, reason)
        return "deny", reason
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


# 이 프로그램들에 넘긴 경로는 '실행 대상'이다. 하네스 래퍼를 python3 로 돌리는 것은
# 정상 동작이므로 변경 시도로 오인해서는 안 된다.
# 읽기만 하는 명령은 막지 않는다. 과잉 차단은 마찰이 되고, 마찰은 게이트를 끄게 만든다.
# find 는 없다 — `-delete`/`-exec` 로 파일을 지운다.


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
    with swallow(t("세션 시작 갱신")):
        with con:
            sync_promotions(con, cfg)
        refresh_learned(con, cfg, root)
    stage = stage_obj(cfg, sid)
    lines = [
        t("[harness] 작업 %s · 회차 %d · 단계 %s — %s")
        % (lid, cycle_of(con, lid), label_of(cfg, sid), stage["summary"]),
        t("제어: `.claude/harness/bin/harness` {status | advance | skip <대상> --reason \"...\"} "
        "· 나머지 명령은 `harness help`"),
        t("이 단계 쓰기 허용: %s. `.dev/` 산출물 파일명은 `%s` 로 시작해야 한다. "
        "근거 문서: `.claude/harness/rationale.md`")
        % (", ".join(stage.get("write", [])) or t("(없음)"), file_prefix(con, lid)),
        t("모든 답변 말머리에 [%s] 를 붙여라.") % stage["label"],
    ]
    if stage.get("hint"):
        lines.append(stage["hint"])
    if auto_skip_on(con):
        lines.append(t("⚠ 스킵 자동 승인 ON (%s) — 스킵에 사용자 다이얼로그가 뜨지 않는다. "
                     "사유는 여전히 필수다. 끄려면 `harness auto-skip off`.")
                     % auto_skip_scope_note(con))
    sk = skips_of(con, lid)
    if sk:
        lines.append(t("이 루프의 스킵: ")
                     + "; ".join("%s(%s, %s)" % (r["stage"], r["reason"],
                                                 r["authorized_by"]) for r in sk))
    try:
        pend = pending_promotions(con, cfg)
    except Exception:
        pend = []
    if pend:
        lines.append(t("승격 결정 대기 %d개 (%s) — Compounding 의 종료 조건이다. "
                     "`harness promote` 로 결정하라.")
                     % (len(pend), ", ".join(it["key"] for it in pend)))
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                  "additionalContext": "\n".join(lines)}}
    # 설정 오타는 **사람에게** 알린다. 모델에게 컨텍스트로 주면 모델이 고치려 들고,
    # stages.json 은 사람의 문서다. 조용히 무시되는 설정이 있다는 사실 자체가 정보다.
    probs = (config_problems(cfg) + drift_problems(cfg, root)
             + install_problems(root) + language_problems(root))
    if probs:
        out["systemMessage"] = (t("harness: stages.json 에서 무시되는 설정이 %d건 있다\n  - %s")
                                % (len(probs), "\n  - ".join(probs[:5])))
    emit(out)


def consume_auto_skip(con):
    """자동 승인 1회 소진. (차지했나, 남은 횟수) — 무제한이면 (True, None).

    플래그는 사용자의 의도를 담고 실효 상태는 `auto_skip_state` 가 계산한다.
    여기서 'off' 로 뒤집으면 "왜 꺼졌는지"를 잃는다.
    """
    if auto_skip_uses_left(con) is None:
        return True, None            # 무제한 — 차지할 것이 없다
    won = claim(con, "UPDATE meta SET v = CAST(CAST(v AS INTEGER) - 1 AS TEXT) "
                     "WHERE k='auto_skip_uses' AND CAST(v AS INTEGER) > 0", ())
    return won, auto_skip_uses_left(con)


PLAN_PREVIEW_LINES = 24
PLAN_PREVIEW_CHARS = 1400


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
        decision, hit, why = floor_verdict(cfg, root, cmd)
        if decision == "deny":
            with con:
                record_event(con, lid, sid, "block", "protected_bash", hit, cmd[:200])
            return emit(pre_decision("deny", (
                t("하네스 자신(%s)은 Bash 로도 변경할 수 없다. 규칙을 바꾸려면 "
                "`.claude/harness/stages.json` 을, 엔진을 바꾸려면 플러그인을 "
                "수정하라. 내용을 보려면 Read 도구를 쓰라. "
                "이 차단은 `allow` 로도 열리지 않는다.") % hit)))
        if decision == "ask":
            # 사유 문구는 `floor_verdict` 가 만든다 — 원문에만 보이는 것과
            # 바닥값 부모가 대상인 것은 다른 설명이 필요하다.
            with con:
                record_event(con, lid, sid, "ask", "floor_named", hit, cmd[:200])
            return emit(pre_decision("ask", why))

        # 래퍼는 사전 승인돼 있으므로 **내용이 우리 것일 때만** 실행돼야 한다.
        # 이 검사를 모든 Bash 앞에 두는 이유: 변조는 앞선 호출에서 이미 끝나 있고,
        # 이 명령이 래퍼를 부르는지 아닌지를 정확히 아는 것도 셸 파싱이다.
        wrap = wrapper_intact(root)
        if wrap is not True:
            restored = (t("원본으로 복구했다 — 다시 실행하면 통한다.")
                        if wrap is not None
                        else t("복구도 하지 못했다 (쓰기 실패). 권한이나 파일시스템을 "
                               "확인하라 — 고칠 때까지 이 경로는 실행되지 않는다."))
            with con:
                record_event(con, lid, sid, "block", "wrapper_tampered",
                             WRAPPER_CMD, cmd[:200])
            return emit(pre_decision("deny", (
                t("`%s` 의 내용이 하네스가 쓴 것과 다르다. 이 경로는 "
                  "`.claude/settings.json` 에 **사전 승인**돼 있어서, 내용이 바뀌면 "
                  "승인 없이 임의 코드가 실행된다. 그래서 실행하지 않았다. %s")
                % (WRAPPER_CMD, restored))))

        for req_sub, req_pos, direct, seg in ctrl_requests(cmd):
            if not ctrl_known(req_sub):
                if not direct:
                    # 산문 속 `harness <낱말>` — 커밋 메시지("docs: explain harness
                    # install flow")가 커밋할 때마다 ask 를 받았다(6회차 실측).
                    # 실행 자리(머리)도 아니고 아는 하위 명령도 아니면 판정할
                    # 제어 호출이 없다. 인라인 실행(`sh -c '…'`)은 인터프리터
                    # 인라인 검사가 따로 묻고, 셸 치환은 아래 opaque 검사가 잡는다.
                    continue
                # 하네스를 부르는데 하위 명령을 읽을 수 없다(`$(echo skip)`, `$C`).
                # **모르면 통과가 아니라 물음이다** — 이 판정이 세 번 뚫린 이유가
                # 전부 "모르면 통과" 였다.
                return emit(pre_decision("ask", t(
                    "이 명령이 하네스를 부르는데 어떤 하위 명령인지 읽을 수 없다 "
                    "(`%s`). 셸 치환이 섞이면 실행 시점에야 정해지므로 사람이 봐야 "
                    "한다. 하위 명령을 그대로 적으면 하네스가 판정한다.")
                    % cmd.strip()[:200]))
            with con:
                out = ctrl_decision(con, cfg, root, req_sub, req_pos, direct,
                                    seg, mode, lid, sid)
            if out:
                return emit(out)
        # **제어 명령이라고 뒤 검사를 건너뛰지 않는다.** 건너뛰었더니 명령 어딘가에
        # `harness` 라는 낱말과 위치 인자 하나만 있으면 쓰기 규칙과 opaque 검사가
        # 통째로 꺼졌다 — `echo "harness 관련" > docs/x.md` 가 그대로 통과했다.

        # Bash 쓰기도 **같은 규칙 엔진**을 지난다. 예전에는 `docs_readonly` 규칙 하나만
        # 여기 손으로 베껴져 있었고, 나머지 여섯 규칙은 Bash 에 없었다 — `stage_write`
        # 까지 없었으므로 `sed -i` 한 줄로 단계 게이트가 통째로 우회됐다.
        for rel in bash_writes(cfg, root, cmd):
            with con:
                decision, reason = check_write(ctx, rel)
            if decision:
                return emit(pre_decision(decision, t(
                    "이 명령이 '%s' 를 바꾼다. %s") % (rel, reason)))

        # 대상을 특정할 수 없는 파괴는 **모른다고 말하고 넘긴다.** 막으면 정상 정리
        # 작업이 막히고, 통과시키면 구멍이다. 자세한 근거는 `BASH_OPAQUE` 에 적었다.
        why = bash_opaque(cmd) or bash_unresolved(cfg, cmd)
        if why:
            with con:
                record_event(con, lid, sid, "ask", "opaque_write", "", cmd[:200])
            return emit(pre_decision("ask", t(
                "이 명령이 무엇을 바꿀지 하네스가 알 수 없다 — %s. 단계별 쓰기 규칙을 "
                "적용할 대상을 정할 수 없으므로 사람이 봐야 한다. 대상이 분명한 형태로 "
                "바꿔 쓰면(예: 경로를 직접 적으면) 규칙이 대신 판정한다.")
                % why))
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
        # **차단은 Bash 도 세면서 편집은 안 셌다.** PreToolUse 는 `bash_writes` 로
        # 대상을 뽑아 같은 규칙 엔진에 넣는데, PostToolUse 는 Write/Edit 도구만
        # 봤다. 그래서 `sed -i`·리다이렉트로 고친 것은 재편집(churn) 지표에
        # 안 잡히고, 승격 검증이 "변경 관측 0/1" 로 **정상 승격을 무고**했다
        # (4회차 C⑦ 실측). "무엇을 쓰나" 의 답은 이미 하나 있다 — 그것을 쓴다.
        field = WRITE_TOOLS.get(tool)
        # 실패한 쓰기는 Bash 만 걸렀다. Write/Edit 도 걸러야 한다 — 실패한
        # Write 의 경로가 파일 증거(plan_file 등)로 적립되고, 파일이 없으면
        # digest 가 NULL 이라 그 증거는 만료조차 안 됐다(6회차 실측).
        if field and not tool_failed(inp):
            rels = [rel_to_root(root, ti.get(field))]
        elif tool == "Bash" and not tool_failed(inp):
            rels = bash_writes(cfg, root, ti.get("command") or "")
        else:
            rels = []
        for rel in [r for r in rels if r]:
            # 편집 이력. 한 루프에서 같은 파일을 몇 번 고쳤는지가 구조 냄새다.
            record_event(con, lid, sid, "edit", None, rel)
            # write_glob 이 없는 조건(cli·observed·no_pending)은 그냥 지나간다.
            for kind, sig in signals.items():
                # `write_glob` 은 `satisfied_by: file` 의 어휘다. 방식을 보지 않고
                # glob 만 보면, `human: true` 인 조건에 glob 을 더하는 것만으로
                # **파일을 쓰는 행위가 사람의 승인이 된다.**
                if not isinstance(sig, dict) or sig.get("satisfied_by") != "file":
                    continue
                for pat in sig.get("write_glob") or []:
                    if glob_match(rel, pat):
                        record_evidence(con, lid, sid, kind, rel, root)
                        break
        if sid in evidence_stages(cfg) and not tool_failed(inp):
            sig = signals.get("verification_evidence") or {}
            if not isinstance(sig, dict):
                sig = {}
            if tool == "Bash":
                cmd = ti.get("command") or ""
                # 원문 검색이 아니라 **실행 위치**를 본다 (`verification_hit`).
                if verification_hit(cfg, cmd):
                    record_evidence(con, lid, sid, "verification_evidence",
                                    cmd.strip()[:120])
            if tool in sig.get("tools", []):
                record_evidence(con, lid, sid, "verification_evidence", "agent:" + tool)
            tp = sig.get("tool_pattern")
            if tp and re.search(tp, tool):
                record_evidence(con, lid, sid, "verification_evidence", "tool:" + tool)


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
    lines = [t("[harness] '%s' 실패가 %d번째다%s.")
             % (target, n, t(" (작업 %d개에 걸쳐)") % loops if loops > 1 else "")]

    # 전체 행을 가져온다. is_regressed 가 kind/key/recheck_at 을 쓰므로 일부 열만
    # 고르면 sqlite3.Row 가 IndexError 를 낸다 — 훅이 fail-open 이라 조용히 죽었다.
    p = promotion_rows(con, key="tool_fail:%s" % target)
    if p and p["decision"] == "declined":
        lines.append(t("이 항목은 승격을 보류한 적이 있다: %s — 또 걸린다면 그 판단이 "
                     "틀렸다는 증거다. 회고에 쓰라.") % (p["note"] or "-"))
    elif p:
        lines.append(t("이 항목은 이미 %s(%s): %s — 승격이 통하지 않고 있다면 "
                     "회고에 그 사실을 쓰라.")
                     % (promote_as(cfg).get(p["decision"], p["decision"]),
                        p["maturity"], (p["note"] or "-")))

    if prev and prev["detail"]:
        lines.append(t("이전 오류: %s") % " ".join(prev["detail"].split())[:160])

    # 인덱스는 조건 없이 앞에 놓이므로, 그대로 쓰면 모든 실패에 같은 인덱스가 붙어
    # 벽지가 된다. 키워드에 실제로 걸린 파일을 우선하고 인덱스는 대체 경로로만 준다.
    indexes, files = _recall_files(cfg, root, [target], limit=4)
    if files:
        lines.append(t("관련 기록 — 같은 실수를 다시 하기 전에 읽어라: %s")
                     % ", ".join(files[:3]))
    elif indexes:
        lines.append(t("이 실패를 직접 다룬 기록은 없다. 인덱스에서 찾아보라: %s")
                     % ", ".join(indexes[:2]))
    else:
        lines.append(t("관련 기록이 없다. 이번에 해결하면 회고에 남겨라 "
                     "(다음에 이 자리에서 제시된다)."))

    out = {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure",
                                  "additionalContext": "\n".join(lines)}}
    # 승격 임계에 닿으면 사용자에게도 보인다 — 모델이 같은 벽을 반복하는 것은
    # 사용자가 알아야 할 사실이다. 단 이미 결정된 항목에는 결정을 요구한다고
    # 말하지 않는다 — 결정이 있는데 또 요구하면 메시지가 거짓이 된다.
    thr = cfg.num("promotion.min_loops", 3, low=2)
    undecided = p is None or is_regressed(con, cfg, p)
    if loops >= thr and undecided:
        out["systemMessage"] = (t("harness: '%s' 실패가 작업 %d개에서 반복된다 — "
                               "Compounding 에서 승격 결정을 요구한다.") % (target, loops))
    elif loops >= thr:
        out["systemMessage"] = (t("harness: '%s' 실패가 작업 %d개에서 반복된다 "
                               "(이미 %s 로 결정됨 — 재발이 이어지면 결정이 무효화된다).")
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
                t("말머리는 [%s] 여야 한다. 단계 이름만 대괄호로 감싸 맨 앞에 붙여라 "
                "(번호 병기 불가).") % stage["label"]))
    for key in stage.get("stop_requires", []):
        if not criterion_met(con, cfg, root, lid, key):
            problems.append((key, t("%s 단계를 끝낼 수 없다: %s")
                             % (stage["label"], criterion_why(con, cfg, root, lid, key))))
    if not problems:
        return continue_or_stop(con, cfg, root, lid, sid, stage, prompt_id)

    # **예산을 적지 못해도 답은 안다.** 이 트랜잭션이 터지면 예외가 `inactive()`
    # 로 떨어져 Stop 게이트가 통째로 열렸다 — 읽기 전용 FS 에서 `prompt_id` 유무와
    # 무관하게 재현됐다(5회차 D-C2). 기록은 곁다리이고 판정은 이미 손에 있다.
    #
    # 적지 못하면 **예산이 안 줄었다는 뜻**이므로 전부 막는 쪽이 옳다. 우회 예산은
    # 사용자가 관대해지려고 둔 것이지 DB 사고로 얻는 것이 아니다.
    blocked, exhausted = [t for _k, t in problems], []
    with swallow(t("턴 종료 예산")):
        blocked, exhausted = self_stop_budget(con, cfg, lid, sid, stage,
                                              prompt_id, problems, limits)

    if blocked:
        emit({"decision": "block", "reason": " / ".join(blocked)})
    elif exhausted:
        # 조용히 통과시키지 않는다 — 우회 사실을 사용자에게 노출한다
        emit({"systemMessage": t("harness: %s 단계를 미충족 상태로 종료했다 (%s). "
                               "차단 상한 소진.") % (stage["label"], ", ".join(exhausted))})


HOOKS = {
    "SessionStart": hook_session_start,
    "PreToolUse": hook_pre_tool_use,
    "PostToolUse": hook_post_tool_use,
    "PostToolUseFailure": hook_post_tool_use_failure,
    "Stop": hook_stop,
}


def init_hint(root):
    """`init` 을 어떻게 부를지. **있는 명령만 안내한다.**

    설치 표식이 커밋되는 `stages.json` 이 되면서 새 클론·`git worktree add` 도
    "게이트가 꺼졌다" 를 내게 됐다. 그런데 그 상태에는 `.claude/harness/bin/` 이
    없다(gitignore). 없는 명령을 안내하면 그것이 막다른 길이다.
    """
    if os.path.isfile(os.path.join(root, WRAPPER_REL)):
        return "%s init" % WRAPPER_CMD
    return "python3 %s init" % ENGINE_FILE


def inactive(why, fix=None):
    """**게이트가 꺼진 채로 빠져나가는 유일한 출구.**

    왜 하나로 모았나: `run_hook` 에 조용한 `return 0` 이 네 개 있었고, 라운드마다
    새 출구가 발견됐다 — DB 를 못 읽는 경우를 막으면 `stages.json` 손상이 남았고,
    그걸 막으면 head 없음이 남았다. **출구가 여러 개인 구조에서는 "다음 출구"가
    항상 남는다.** 하나로 모으면 새 출구를 더할 때 알림이 딸려 온다.

    판정은 열어 준다(세션을 벽돌로 만들지 않는다). 꺼진 사실만 반드시 말한다.
    """
    msg = t("harness: %s — **게이트가 꺼졌다.** 단계·쓰기 규칙·종료 조건이 지금 "
            "아무것도 막지 않는다.") % why
    if fix:
        msg += t(" 복구: %s") % fix
    emit({"systemMessage": msg})
    return 0


def run_hook():
    try:
        inp = json.load(sys.stdin)
    except Exception:
        return 0
    con = None
    try:
        root = find_root(inp.get("cwd"))
        if not root:
            return 0  # 하네스 미설치 프로젝트 — 조용히 종료 (설치 안 한 것은 고장이 아니다)
        # connect 가 try 밖에 있었다. 손상된 SQLite 파일은 connect 나
        # `PRAGMA journal_mode=WAL` 에서 DatabaseError 를 던지고, 그게
        # fail-open 처리 밖이라 traceback + exit 1 이 됐다 — 재현했다.
        con = connect(root)
        if con is None:
            # 진짜 자산은 상태 DB 다. 못 읽으면 모든 게이트가 한꺼번에 꺼진다.
            return inactive(t("상태 DB(%s)를 읽을 수 없다") % DB_REL,
                            t("`%s` (기록은 .dev/ 의 파일에 남아 있다)")
                            % init_hint(root))
        gone = missing_gates()
        if gone:
            # 게이트 구현이 안 실렸다. 그 게이트가 막던 것이 **전부** 통과한다 —
            # 파일이 빠졌든 import 가 터졌든, 결과는 게이트 해제와 같다.
            return inactive(t("게이트 %s 가 실리지 않았다 — 그 게이트가 막던 것이 "
                              "지금 아무것도 막지 않는다%s")
                            % (", ".join(gone), gate_load_why()),
                            t("플러그인 설치가 온전한지 확인하라 (`scripts/` 아래 "
                              "파일이 빠졌을 수 있다)"))
        globals()["SWALLOW_LOG"] = os.path.join(root, SWALLOW_LOG_REL)
        cfg = load_config(root, plugin_root())
        load_messages(root, cfg.at("language") if isinstance(cfg, dict) else None)
        # 손상된 문서를 템플릿으로 갈아치우지는 않는다 — 덜어낸 규칙이 되살아난다.
        fix = (t("`git checkout -- %s`, 또는 그 파일을 지우고 `%s`")
               % (CONFIG_REL, init_hint(root)))
        if not isinstance(cfg, dict):
            return inactive(t("`%s` 를 읽을 수 없다 (JSON 문법을 확인하라)")
                            % CONFIG_REL, fix)
        if not cfg.get("stages"):
            # 문법은 멀쩡한데 비어 있는 경우다. "문법을 확인하라" 고 하면 없는 오타를
            # 찾게 만든다 — 무엇이 비었는지 그대로 말한다.
            return inactive(t("`%s` 의 stages 가 비어 있다 — 단계가 없으면 단계 게이트도, "
                              "종료 조건도, 단계별 쓰기 규칙도 없다") % CONFIG_REL, fix)
        lid = head_loop(con)
        if lid:
            # 이번 회차의 실행 그래프 = 설정 단계 + 회차 한정 노드. 활성 단계가
            # 회차 한정 노드일 수 있으므로 `stage_known` **앞**에서 결합해야 한다 —
            # 뒤에 두면 그 노드에 서 있는 것만으로 게이트가 꺼진다.
            with swallow(t("경로 노드 결합")):
                graph_splice(con, cfg, lid)
        sid = active_stage(con, lid) if lid else None
        if lid and sid and not stage_known(cfg, sid):
            # DB 의 활성 단계가 `stages.json` 에 없다. 예전에는 `stage_index` 가 0 을
            # 돌려줘 **1단계로 조용히 읽혔고** 그 단계의 종료 조건이 통째로 사라졌다.
            return inactive(
                t("활성 단계 '%s' 가 `%s` 에 없다 — 상태와 설정이 어긋났다. "
                  "그 단계의 종료 조건이 아무것도 확인되지 않는다") % (sid, CONFIG_REL),
                t("단계 id 를 되돌리거나, 새 구성으로 작업을 다시 시작하라 "
                  "(`%s loop new --reason \"단계 구성 변경\"`)") % WRAPPER_CMD)
        if not lid or not sid:
            probs = (config_problems(cfg) + drift_problems(cfg, root)
                     + install_problems(root) + language_problems(root))
            extra = (t(" 무시되는 설정 %d건: %s")
                     % (len(probs), "; ".join(probs[:3]))) if probs else ""
            return inactive(t("활성 작업이 없다 (head 또는 활성 단계 없음)%s") % extra,
                            t("`%s` 또는 `%s status`") % (init_hint(root), WRAPPER_CMD))
        pid = inp.get("prompt_id")
        if pid:
            # **부기가 판정을 막지 않는다.** 이건 말머리 검사용 순수 기록인데
            # `swallow` 밖에 있었다. 읽기 전용 FS·디스크 참·잠금 경합에서 이
            # INSERT 가 터지면 예외가 `inactive()` 로 떨어져 **그 뒤의 판정이
            # 아예 돌지 않았다** — `rm -rf .claude` 가 통과했다(5회차 C④·D-C2,
            # 두 리뷰어가 각자 재현). Claude Code 는 `prompt_id` 를 항상 보낸다.
            #
            # `record_event` 만 감싼 것은 열한 곳 중 하나를 고친 것이었다.
            with swallow(t("프롬프트-단계 기록")):
                with con:
                    con.execute("INSERT OR IGNORE INTO prompt_stage(prompt_id,stage,at) "
                                "VALUES(?,?,?)", (pid, sid, now()))
        fn = HOOKS.get(inp.get("hook_event_name"))
        if fn:
            fn(inp, Ctx(con, cfg, root, lid, sid))
    except Exception as exc:
        # 차단하지 않는다. exit 1 은 Claude Code 가 훅 오류로 표면화하므로,
        # 무력해지더라도 조용히 빠진다 — 세션을 벽돌로 만드는 것보다 낫다.
        #
        # **그러나 조용히 빠지지도 않는다.** 예전에는 세션 시작에서만 알렸고, 그래서
        # 세션 중간에 상태가 깨지면 남은 세션 전체가 게이트 없이 돌면서 아무 말이
        # 없었다 — 손상된 DB 는 예외로 오므로 바로 이 경로다. 판정은 열어 주고
        # 사실은 매번 말한다. 소음이 은폐보다 낫다.
        sys.stderr.write("step-seven-harness: %s\n" % exc)
        # 여기 오는 것은 대개 **엔진 내부 오류**다(스키마 불일치, NameError 등).
        # "상태를 읽을 수 없다"고 뭉뚱그리면 사용자가 DB를 의심하며 엉뚱한 곳을 고친다.
        #
        # 이 경로가 소리를 내게 만든 것이 `ctx_check` 보다 중요하다. 정적 검사는
        # 조건부 대입 뒤의 UnboundLocalError 를 잡지 못한다(symtable 의 한계).
        # 그런 결함의 진짜 방어선은 **실패가 보이는 것**이다.
        return inactive(t("하네스 내부 오류: %s") % exc,
                        t("`%s init` 으로 스키마를 갱신하거나, 계속되면 이 메시지를 "
                          "그대로 보고하라") % WRAPPER_CMD)
    finally:
        if con is not None:
            with swallow(t("DB 닫기")):
                con.close()
    return 0


# -------------------------------------------------------------------------- cli

def plugin_root():
    return os.path.dirname(os.path.dirname(ENGINE_FILE))


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
# 어휘 기본값. 엔진 사본 옆에 두어 같은 수명을 갖는다 (`bin/` 은 이미 gitignore 된다).
DEFAULTS_REL = os.path.join(HARNESS_DIR, "bin", "defaults.json")

# 엔진 사본을 프로젝트 안에서 먼저 찾는다. 프로젝트 밖의 파일을 실행하면
# auto-mode 분류기와 샌드박스가 막는다 — 둘 다 실제로 겪은 문제다.
WRAPPER = """#!/bin/sh
# step-seven-harness wrapper — 세션 시작마다 갱신된다. 직접 편집하지 마라.
#
# **플러그인 원본을 먼저 쓴다.** 프로젝트 사본(`$D/harness.py`)을 먼저 쓰면,
# 그 사본이 `.claude/settings.json` 에 사전 승인돼 있으므로(SAFE_PERMS) 사본을
# 덮는 것이 곧 **승인 없는 임의 코드 실행**이 된다. 적대적 리뷰가 지적했고 맞다.
# 사본은 원본이 없을 때의 폴백일 뿐이다 — 모델이 치는 명령은 여전히 작업 디렉터리
# 안의 이 래퍼이므로 분류기·샌드박스 문제도 생기지 않는다.
D="$(cd "$(dirname "$0")" && pwd)"
# 1) 이 래퍼를 쓴 그 엔진. 2) 없으면 플러그인 캐시의 최신 것. 3) 그래도 없으면 사본.
#
# **사본은 마지막이다.** 순서를 2와 3 사이에 두면, 플러그인이 업데이트돼 고정 경로가
# 사라진 순간 사본이 먼저 실행된다 — 그 사본은 사전 승인돼 있으므로 승인 없는 임의
# 코드 실행이 된다. 4차 리뷰가 지적했고 맞다. 캐시 탐색을 먼저 한다.
P="%s"
if [ ! -f "$P" ]; then
  # **정확한 이름만 찾는다.** 예전에는 `*harness*` 로도 찾았다 — 이름이 바뀌어도
  # (6->7 단계처럼) 살아 있게 하려던 것인데, 이 래퍼는 `.claude/settings.json` 에
  # 사전 승인돼 있으므로 그 glob 은 **이름에 harness 가 들어간 아무 플러그인의
  # Python 을 승인 없이 실행하는 길**이 된다. 5차 리뷰가 찾았고 재현했다.
  # 이름이 바뀌면 폴백이 없어 CLI 가 안 되는 대신, 사용자가 그 사실을 즉시 안다.
  P="$(ls -t "$HOME"/.claude/plugins/cache/*/step-seven-harness/*/scripts/harness.py \
             2>/dev/null | head -1)"
fi
if [ ! -f "$P" ]; then P="$D/harness.py"; fi
[ -f "$P" ] || { echo "step-seven-harness: engine not found" >&2; exit 1; }
exec python3 "$P" "$@"
"""


ENGINE_LINE_RE = re.compile(r'^P="[^"]*"$', re.M)


def known_classes(cfg):
    """존재하는 경로 클래스. `source` 는 어디에도 안 걸릴 때의 기본값이다."""
    return set(cfg.obj("path_classes") or {}) | {"source"}


def rule_reachable(cfg, rule):
    """이 규칙이 **어떤 경로에도 해당할 수 있나.**

    선택자의 키만 보고 값을 안 봤더니, `when.class` 를 없는 클래스로 두면 규칙이
    조용히 죽었다. 직접 확인했다 — 일곱 규칙을 다 죽여도 아무 말이 없었다.
    """
    when = rule.get("when") or {}
    if not isinstance(when, dict):
        return False
    cls = when.get("class")
    if cls is not None and cls not in known_classes(cfg):
        return False
    req = rule.get("require") or {}
    if not isinstance(req, dict) or not [k for k in req if k in WRITE_TESTS]:
        return False
    name = req.get("predicate")
    if name and name not in WRITE_PREDICATES:
        return False
    return True


# --------------------------------------------------------------------- 자기검사
#
# **대리 지표를 버린다.**
#
# "강제가 실제로 작동하나" 를 세 번 다른 방식으로 추정했고 세 번 다 거짓말했다.
#   ① 무력화 모양을 예측해 진단  → 리뷰가 새 모양을 계속 찾았다
#   ② 규칙·조건 **개수**를 세어 보여줌 → 있지만 죽은 규칙이 7로 세어졌다
#   ③ "발동 가능" 만 세도록 보정   → 거짓값 조건이 여전히 발동 가능으로 세어졌다
#
# 셋 다 설정을 **들여다보고** 결론을 추정한 것이다. 추정은 항상 한 발 늦는다.
# 이미 두 번 옳은 답을 냈는데 끝까지 밀지 않았다 — `bash_pattern` 은 실제 명령으로
# **탐침**했고, 스코프는 파이썬의 **실제 분석기**를 썼다.
#
# 그 원칙을 여기서 완성한다. **실제 판정 함수에 대표 입력을 넣고 결과를 본다.**
# 이건 대리 지표가 아니라 강제 그 자체를 돌려 본 것이므로 거짓말할 수 없다.

# **탐침마다 단계를 고정한다.** 고정하지 않으면 결과가 "지금 어느 단계인가"에
# 좌우된다 — 예컨대 Selection 에서는 `stage_write` 가 거의 전부를 막아, 다른 규칙이
# 죽어도 탐침이 통과한다(막히긴 하되 **다른 이유로**). 규칙마다 그 규칙이 유일한
# 차단자인 단계를 골라야 "이 규칙이 살아 있나"를 실제로 묻게 된다.
#
# (설명, 종류, 대상, 막혀야 하나, 어느 단계에서 볼까 — None 이면 현재 단계)


# ============================================================ 게이트
#
# ## 왜 이 골격이 있나
#
# 게이트 하나에는 네 가지 책임이 있다 — **무엇을 막나 / 지금 켜져 있나 / 그것을 어떻게
# 증명하나 / 설정이 옳은가.** 이 넷이 파일 곳곳에 흩어져 있었다(쓰기 게이트는 15곳,
# 줄 539~3754). 그래서 게이트를 고칠 때 한 자리만 고치게 되고, **나머지가 빠져도
# 아무 소리가 나지 않았다.** 적대적 리뷰 3회가 찾은 결함의 대부분이 그 침묵이다:
#
#   `promotion.kinds` 는 진단하는데 같은 게이트의 `min_loops` 는 침묵
#   판정을 고쳤는데 `강제 중:` 줄은 그대로 (판정과 요약이 3,200줄 떨어져 있다)
#   탐침 목록이 게이트 목록보다 좁아서 22/22 가 CRITICAL 을 가려 줌
#
# ## 강제 장치
#
# 컴파일러가 있는 언어라면 "네 짝 중 하나가 없으면 컴파일이 안 된다"로 끝난다.
# 파이썬에는 그것이 없다. 대신 이 파일에는 **실행**이 있다 — 자기검사는 이미
# "주장하지 말고 대표 조작을 실제 판정에 넣어 본다"는 원칙으로 돌고 있다.
#
# 그래서 강제를 실행에 건다: **게이트는 자기가 막는 것과 통과시키는 것을 둘 다
# 내놓아야 한다.** 한쪽만 내놓으면 그 게이트는 자기를 증명하지 못한 것이고,
# 자기검사가 그 사실 자체를 실패로 보고한다(`gate_probes`). 설정으로 게이트를 끄면
# "막아야 할 것"이 통과하면서 드러나고, 과잉 차단이 되면 "통과해야 할 것"이 막히면서
# 드러난다 — 오늘 세 회차가 양방향으로 실패한 그 두 가지가 같은 장치에 걸린다.
#
# 확장은 열려 있다: 게이트를 더하려면 `Gate` 하나를 만들어 `GATES` 에 넣는다.
# 요약·자기검사·설정 진단이 전부 `GATES` 를 순회하므로 **빠뜨릴 자리가 없다.**


class Gate(abc.ABC):
    """게이트 하나. 네 책임을 **한 곳에서** 소유한다.

    `abc` 를 쓰는 이유: 넷 중 하나를 빠뜨리면 **등록 시점에** 터진다. 파이썬에는
    컴파일러가 없지만 추상 클래스가 그 자리를 일부 대신한다 — 나머지(탐침이 실제로
    구분력이 있는가)는 `gate_probes` 가 실행으로 본다.
    """

    knobs = ()          # 이 게이트를 끄는 설정 경로들

    # **훅이 실제로 부르는 함수 이름들.** 탐침은 반드시 여기를 지나야 한다.
    #
    # 4회차 리뷰가 이 규칙 없이는 자기증명이 공허하다는 것을 증명했다:
    # `check_write`·`ctrl_decision`·`hook_stop`·`pending_promotions` 네 함수의
    # 본문을 첫 줄에서 잘라내도 **자기검사 42/42 통과**, 요약 줄 바이트 동일,
    # 훅은 아무것도 막지 않았다. 탐침들이 진입점보다 한 층 아래를 부르거나
    # 자기 자신의 헬퍼를 부르고 있었기 때문이다.
    #
    # 예전 규칙은 "막는 기대와 통과하는 기대를 둘 다 **선언**했는가"만 물었다.
    # 선언은 증명이 아니다. `gate_probes` 가 이 이름들을 계수기로 감싸고,
    # **한 번도 지나지 않은 탐침을 실패로 낸다.**
    entry = ()

    @property
    @abc.abstractmethod
    def key(self):
        """번역되지 않는 안정된 식별자. **레지스트리가 이것으로 소실을 감지한다** —
        사람이 보는 이름은 언어에 따라 바뀌므로 그것으로는 셀 수 없다."""

    @property
    @abc.abstractmethod
    def name(self):
        """사람이 보는 이름. **읽을 때** 번역한다 — 클래스 정의 시점에는 아직
        프로젝트의 `language` 를 모른다."""

    @abc.abstractmethod
    def state(self, cfg):
        """(켜진 수, 전체). 둘이 다르면 사용자가 그 차이를 본다."""

    @abc.abstractmethod
    def probes(self, ctx):
        """[(설명, 호출, 막혀야 하나)]. `호출` 은 인자 없이 불러 (막혔나, 설명) 을 준다."""

    def problems(self, cfg):
        return []


# **여기 있어야 하는 게이트.** 구현이 파일로 갈라지면 파일 하나가 안 실려도 조용히
# 사라질 수 있다 — 그때 `강제 중:` 줄에서 그 게이트만 빠지고 아무도 모른다.
# 그래서 레지스트리가 아니라 **엔진이** 목록을 선언하고, 없으면 소리를 낸다.
REQUIRED_GATES = ("write", "consent", "criteria", "stop", "promotion", "graph")
GATES = []


def gate(cls):
    """게이트를 등록한다. 데코레이터 하나가 요약·자기검사·진단 전부에 꽂는다.

    추상 메서드가 하나라도 비어 있으면 여기서 `TypeError` 가 난다 — 등록 시점이다.
    """
    GATES.append(cls())
    return cls


def gate_load_why():
    """왜 안 실렸는지. 없으면 빈 문자열.

    `GATE_LOAD_FAILS` 를 채우기만 하고 아무도 읽지 않으면 그 값은 없는 것과 같다.
    "실리지 않았다" 만 말하고 원인을 삼키면 사용자는 고칠 수 없다(4회차 D-M10).
    """
    if not GATE_LOAD_FAILS:
        return ""
    return t(" (%s)") % "; ".join("%s — %s" % nm_why for nm_why in GATE_LOAD_FAILS)


def missing_gates():
    """실려야 하는데 없는 게이트. 비어 있으면 정상."""
    return [k for k in REQUIRED_GATES if k not in {g.key for g in GATES}]


# 탐침이 도는 동안 훅이 낸 것. `probe_run` 이 채우고 탐침이 읽는다 —
# 훅의 **출력**을 보지 않으면 "막았다" 를 게이트 밖에서 확인할 방법이 없다.
PROBE_EMITS = []


class probe_loop(object):
    """탐침 전용 작업 id. 안에서 쓴 것은 **전부 되돌린다.**

    예전에는 살아 있는 작업(`ctx.lid`)에 대고 물었다. 그래서 사용자가
    `harness loop intent "..."` 를 기록하는 순간 — **하네스가 시키는 첫 행동이다** —
    탐침이 뒤집혀 매 세션 거짓 경보가 상주했다(4회차). 더 나쁜 것은 그 경보의
    문자열이 `criterion_met` 이 통째로 죽었을 때와 **정확히 같았다**는 점이다.
    신호와 소음이 구분되지 않으면 사람은 둘 다 무시한다.

    탐침은 고정된 입력에 대고 물어야 한다. 그래서 격리한다.

    **사본에 대고 묻는다.** 처음에는 savepoint 로 되돌리려 했는데 진입점 안에
    `with con:` 이 있고 그것이 커밋하면서 savepoint 를 없앴다 (`no such
    savepoint: probe`). 진짜 진입점을 부르는 이상 그 안에서 무엇이 커밋될지
    탐침은 알 수 없다 — 알려고 하면 그게 또 하나의 모형이다. 사본에 물으면
    그 질문 자체가 사라진다.
    """

    LID = "__probe__"

    def __init__(self, con):
        self.src, self.mem = con, None

    def __enter__(self):
        self.mem = sqlite3.connect(":memory:")
        self.mem.row_factory = self.src.row_factory
        self.src.backup(self.mem)
        return self.mem, self.LID

    def __exit__(self, *exc):
        self.mem.close()
        return False


def probe_hook(ctx, inp):
    """훅이 **실제로 지나는 길**로 판정을 받아온다 — `HOOKS` 표에서 찾아 부른다.

    4회차에 "탐침이 진입점을 지났는지 계수한다" 로 고쳤고, 그 규칙은 자기가 겨눈
    다섯 함수를 실제로 지킨다(비우면 3~12개 탐침이 실패한다). **그런데 그 다섯
    위에 훅 디스패처가 있었다.** `HOOKS = {}` 한 줄이면 게이트가 전부 죽는데
    자기검사 34/34 와 `강제 중:` 줄이 바이트 하나 안 바뀌었다(5회차 D-C1).

    같은 실수의 한 층 위 판이다. 그래서 탐침을 **표에서** 출발시킨다 — 표가
    비면 탐침이 돌지 못하고, 돌지 못한 것도 결과다(`ValueError`).

    돌려주는 것은 훅이 낸 것(`PROBE_EMITS`)이다. `probe_run` 이 `emit` 을
    가로채므로 stdout 은 더럽혀지지 않는다.
    """
    name = inp.get("hook_event_name")
    fn = HOOKS.get(name)
    if fn is None:
        raise ValueError(t("훅 표에 %s 가 없다 — 그 이벤트는 아무 판정도 받지 못한다")
                         % name)
    fn(inp, ctx)
    return list(PROBE_EMITS)


def probe_decision(ctx, inp):
    """훅의 판정 `(decision, reason)`. 아무 말이 없으면 `(None, "")`."""
    for out in probe_hook(ctx, inp):
        got = (out or {}).get("hookSpecificOutput") or {}
        if got.get("permissionDecision"):
            return got["permissionDecision"], got.get("permissionDecisionReason", "")
    return None, ""


class probe_run(object):
    """탐침 한 번을 **격리해서** 돌리고, 진입점을 지났는지 센다.

    두 가지를 동시에 한다.

    ① **계수** — `entry` 로 선언된 함수를 감싸 호출 수를 센다. 0 이면 그 탐침은
       훅이 지나는 문을 지나지 않은 것이고, 무엇도 증명하지 못한다.

    ② **출력 가로채기** — `hook_stop` 은 stdout 으로 JSON 을 낸다. 자기검사가
       그것을 그대로 흘리면 훅 채널이 깨진다. `emit` 을 여기서 잡아 `PROBE_EMITS`
       에 모으고, 탐침이 그것을 읽어 "무엇으로 막았나" 를 확인한다.
       DB 오염은 `probe_loop` 의 메모리 사본이 막는다 — 장치는 하나면 된다.

    되돌리는 것까지 한 덩어리다. 예외가 나도 `__exit__` 이 원래 함수를 되돌린다.
    """

    def __init__(self, mod, names):
        self.mod, self.names, self.hits = mod, names, 0
        self._orig, self._tables = {}, []

    def _count(self, fn):
        def go(*a, **kw):
            self.hits += 1
            return fn(*a, **kw)
        return go

    def __enter__(self):
        for nm in self.names:
            fn = getattr(self.mod, nm)
            self._orig[nm] = fn
            wrapped = self._count(fn)
            setattr(self.mod, nm, wrapped)
            # **표가 잡아둔 참조도 바꾼다.** `HOOKS` 는 정의 시점에 함수 객체를
            # 직접 담으므로 모듈 속성만 감싸면 계수기가 안 닿는다 — 탐침이
            # 진입점을 지나고도 "지나지 않았다" 로 나왔다(5회차 D-H3 의 우회를
            # 내가 그대로 밟았다). 참조를 들고 있는 곳을 함께 갈아 끼운다.
            for key, val in list(HOOKS.items()):
                if val is fn:
                    self._tables.append((HOOKS, key, fn))
                    HOOKS[key] = wrapped
        del PROBE_EMITS[:]
        self._orig["emit"] = self.mod.emit
        self.mod.emit = PROBE_EMITS.append
        return self

    def __exit__(self, *exc):
        for tbl, key, fn in self._tables:
            tbl[key] = fn
        for nm, fn in self._orig.items():
            setattr(self.mod, nm, fn)
        return False


def gate_probes(ctx):
    """모든 게이트의 탐침을 돌린다. 실리지 않은 게이트가 있으면 그것이 첫 결과다. **자기를 증명하지 못하는 게이트도 결과다.**

    막는 탐침과 통과하는 탐침을 둘 다 갖지 못한 게이트는 구분력이 없다 — 설정으로
    꺼져도, 과잉 차단이 되어도 그 게이트만은 조용하다. 그것을 실패로 낸다.
    """
    out = [(t("게이트 %s 가 실려 있다") % k, False,
            t("실리지 않았다 — 그 게이트가 막던 것이 통과한다%s") % gate_load_why())
           for k in missing_gates()]
    for g in GATES:
        try:
            ps = list(g.probes(ctx))
        except Exception as exc:
            out.append((t("%s 게이트의 탐침이 터졌다") % g.name, False, str(exc)))
            continue
        wants = {want for _d, _c, want in ps}
        if wants != {True, False}:
            out.append((t("%s 게이트가 자기를 증명한다") % g.name, False,
                        t("막는 탐침과 통과하는 탐침을 둘 다 내놓아야 한다 (지금 %s)")
                        % (t("막는 것만") if wants == {True} else
                           t("통과하는 것만") if wants == {False} else t("없음"))))
            continue
        if not g.entry:
            out.append((t("%s 게이트가 진입점을 밝힌다") % g.name, False,
                        t("`entry` 가 비어 있다 — 탐침이 무엇을 지나는지 잴 수 없다")))
            continue
        mod = sys.modules[__name__]
        for desc, call, want in ps:
            try:
                with probe_run(mod, g.entry) as run:
                    blocked, why = call()
            except ValueError as exc:
                out.append((desc, False, str(exc)))   # 돌릴 수 없다는 것도 결과다
                continue
            except Exception as exc:
                out.append((desc, False, t("판정 중 예외: %s") % exc))
                continue
            # **지나지 않았으면 증명이 아니다.** 이 검사가 없을 때 네 게이트의
            # 진입점을 통째로 비워도 42/42 가 나왔다.
            if not run.hits:
                out.append((desc, False,
                            t("진입점(%s)을 지나지 않았다 — 자기 자신을 재고 있다")
                            % ", ".join(g.entry)))
                continue
            out.append((desc, blocked == want,
                        (t("막힘(%s)") % why) if blocked else t("통과")))
    return out


def gate_states(cfg):
    """[(이름, 켜진 수, 전체)] — `강제 중:` 줄의 재료."""
    return [(g.name,) + tuple(g.state(cfg)) for g in GATES]


def gate_problems(cfg):
    out = []
    for g in GATES:
        try:
            out += list(g.problems(cfg))
        except Exception as exc:
            out.append(t("%s 게이트의 설정 진단이 터졌다: %s") % (g.name, exc))
    return out


def _bind(fn, *a):
    """인자를 묶어 둔다. 탐침은 **나중에** 돌아야 한다 (돌려 본 결과가 결과다)."""
    return lambda: fn(*a)


def selftest(ctx):
    """게이트가 스스로를 증명한다. **주장이 아니라 실행이다.**

    예전에는 여기 35줄짜리 표(`SELFTEST`)와 48줄짜리 kind 분기가 있었다. 게이트를
    더할 때 표에 행을 넣는 것을 잊어도 조용했고, 실제로 동의 게이트가 통째로 빠진 채
    22/22 를 냈다. 이제 탐침은 게이트가 소유하므로 빠뜨릴 자리가 없다.
    """
    return gate_probes(ctx)


def enforcing_summary(cfg):
    """**지금 실제로 강제되고 있는 것.**

    예측은 끝이 없다(빈 목록, 아무것도 안 맞는 정규식, 이름 바꾸기…). 대신 결과를
    보여준다. 그 결과는 **게이트가 스스로 말한다** — 여기서 게이트별로 세면 게이트를
    더할 때 이 자리를 잊게 되고, 잊어도 조용하다.

    남는 것은 게이트의 `n/m` 으로 표현되지 않는 것뿐이다. 보호 경로는 바닥값 ∪ 설정
    이라 '전체' 가 없고, 언어는 게이트가 아니다.
    """
    return {
        "protected_paths": len(protected_pats(cfg)),
        "gates": gate_states(cfg),
        "language": cfg.at("language") or "ko",
    }


def _panel_work_candidates(ctx, lid):
    """작업이 정해지지 않았으면 하네스가 아는 할 일을 후보로 내놓는다.
    무인 실행에는 물을 사람이 없으므로, 고를 것을 주지 않으면 거기서 멈춘다."""
    row = loop_row(ctx.con, lid)
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
        print(t("\n이 작업의 완료 조건 (%d개):") % len(acc))
        for i, it in enumerate(acc, 1):
            print("  %d. %s" % (i, it))


PANELS = {
    "work_candidates": _panel_work_candidates,
    "tidy": _panel_tidy,
    "acceptance": _panel_acceptance,
    "retro": None,   # 분량이 커서 _hint_on_enter 안에 남긴다
}


SHELL_META = set(";|&<>$`(){}\n")


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


def tidy_headline(con, cfg, root):
    """Scaffolding 에서 한 줄로 보여줄 요약. 할 일이 없으면 None."""
    try:
        rep = tidy_report(con, cfg, root)
    except Exception:
        return None
    bits = []
    if rep["dirs"]:
        bits.append(t("인덱스 %d곳") % len(rep["dirs"]))
    if rep["stale"]:
        bits.append(t("오래된 파일 %d개") % len(rep["stale"]))
    if rep["groups"]:
        bits.append(t("병합 후보 %d묶음") % len(rep["groups"]))
    if rep["regressed"]:
        bits.append(t("재발한 승격 %d개") % len(rep["regressed"]))
    if rep["learned"] and rep["learned"][0] >= rep["learned"][1]:
        bits.append(t("LEARNED.md 예산 소진 %d/%d") % rep["learned"])
    if not bits:
        return None
    return t("정리 후보: %s — `harness tidy` 로 목록을 본다") % ", ".join(bits)


# 사전승인(preauth)은 표에는 보이지만 **판정에는 들어가지 않는다.** 무인 실행을
# 회피로 세면 그 판정은 모드 선택을 비난하는 것이 된다.
TREND_KEYS = (("blocks", "차단"), ("refails", "반복실패"), ("churn", "재편집"),
              ("bypass", "우회"), ("skips", "스킵"), ("declines", "보류"),
              ("preauth", "사전승인"))


VERDICT_TEXT = {
    "evasion": "⚠ 마찰이 줄었지만 우회·스킵·보류가 늘었다 — 개선이 아니라 회피일 수 "
               "있다. 게이트가 연극이 되고 있는지 보라.",
    "improving": "✓ 마찰이 줄고 우회는 늘지 않았다 — 개선 신호다 "
                 "(작업 난이도 차이는 통제하지 못한다).",
    "mismatch": "⚠ 우회가 늘었는데 마찰은 줄지 않았다 — 규칙이 맞지 않는지 보라.",
}


NO_CYCLES = "  (회차 종료 기록이 없다. `advance --cycle` 또는 `--done` 때 쌓인다)"


def run_cli(argv):
    cmd = argv[0]
    if cmd == "init":
        # 설치 경로에서도 삼킨 사실은 남아야 한다. `SWALLOW_LOG` 를 아래(설치된
        # 프로젝트 기준)에서만 채우면 init 중 삼킨 실패(엔진 사본 갱신·설치 후
        # 점검)가 파일에도 화면에도 안 남아 설치가 성공처럼 보였다(6회차).
        target = os.path.abspath(argv[1]) if len(argv) > 1 else os.getcwd()
        globals()["SWALLOW_LOG"] = os.path.join(target, SWALLOW_LOG_REL)
        rc = cli_init(argv[1:])
        for note in SWALLOWED:
            print(t("주의(삼킨 실패): %s") % note, file=sys.stderr)
        return rc
    root = find_root(os.getcwd())
    if not root:
        print(t("이 프로젝트에는 하네스가 설치되지 않았다. `harness init` 또는 "
              "/step-seven-harness:install 을 실행하라."), file=sys.stderr)
        return 1
    try:
        con = connect(root)
    except sqlite3.Error as exc:
        # 훅은 "복구: init" 이라고 안내한다. 그 안내가 가리키는 CLI 가 여기서
        # traceback 으로 죽으면 게이트가 **영구히** 꺼진다.
        print(t("상태 DB를 열 수 없다 (%s). `%s` 로 복구하라 — 읽을 수 없는 파일은 "
                "지우지 않고 옆으로 옮긴다.") % (exc, init_hint(root)), file=sys.stderr)
        return 1
    gone = missing_gates()
    if gone:
        print(t("게이트 %s 가 실리지 않았다 — 그 게이트가 막던 것이 지금 아무것도 "
                "막지 않는다%s. 플러그인 설치가 온전한지 확인하라.")
              % (", ".join(gone), gate_load_why()), file=sys.stderr)
        return 1
    globals()["SWALLOW_LOG"] = os.path.join(root, SWALLOW_LOG_REL)
    cfg = load_config(root, plugin_root())
    load_messages(root, cfg.at("language") if isinstance(cfg, dict) else None)
    if con is None or not isinstance(cfg, dict) or not cfg.get("stages"):
        print(t("DB 또는 설정이 손상되었다: %s\n복구: `%s`")
              % (os.path.join(root, HARNESS_DIR), init_hint(root)), file=sys.stderr)
        return 1
    def schema_help(exc):
        # 스키마가 플러그인보다 오래됐을 때 traceback 대신 할 일을 알려준다.
        # 마이그레이션은 하지 않는다 — DB 는 커밋하지 않는 런타임 상태이고,
        # init 이 스키마를 다시 적용하는 것이 정해진 업그레이드 경로다.
        print(t("DB 스키마가 플러그인 버전과 맞지 않는다 (%s).\n"
              "`.claude/harness/bin/harness init` 을 다시 실행하라 — 파일은 "
              "덮어쓰지 않고 스키마만 갱신한다. 그래도 안 되면 %s 를 지우고 "
              "init 을 실행하라 (진행 중 상태만 사라지고 기록 파일은 남는다).")
              % (exc, DB_REL.replace(os.sep, "/")), file=sys.stderr)
        return 1

    try:
        try:
            lid = head_loop(con)
            sid = active_stage(con, lid) if lid else None
            if not lid or not sid:
                with con:
                    lid = create_loop(con, cfg, root, only_if_none=True)
                sid = active_stage(con, lid) or cfg["stages"][0]["id"]
            # 훅과 같은 이유·같은 자리 — `stage_known` 앞에서 결합한다.
            with swallow(t("경로 노드 결합")):
                graph_splice(con, cfg, lid)
        except sqlite3.OperationalError as exc:
            # 0바이트·타 SQLite 파일은 `connect` 는 통과하고 **첫 질의**에서
            # 죽는다. 훅이 안내하는 `status` 가 여기서 traceback 이면 복구
            # 안내가 막다른 길이 된다 — 5회차에 init 만 고치고 이 자리를
            # 남겼다(6회차 실측).
            return schema_help(exc)
        if not stage_known(cfg, sid):
            print(t("활성 단계 '%s' 가 `%s` 에 없다 — 상태와 설정이 어긋났다. "
                    "단계 id 를 되돌리거나 `harness loop new --reason \"...\"` 로 "
                    "새 구성에서 다시 시작하라.") % (sid, CONFIG_REL), file=sys.stderr)
            return 1
        fn = CLI.get(cmd)
        if not fn:
            print(t("알 수 없는 명령: %s\n사용 가능: %s\n전체 사용법은 `harness help`.")
                  % (cmd, ", ".join(sorted(CLI))), file=sys.stderr)
            return 2
        try:
            return fn(Ctx(con, cfg, root, lid, sid), argv[1:])
        except sqlite3.OperationalError as exc:
            return schema_help(exc)
    finally:
        con.close()


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
  advance --to <stage-id> --reason "..."
                               분기 노드(next 가 여럿)에서는 대상과 사유를 골라야 한다
  advance --done               (Compounding 에서만) 작업 종료 → Selection 으로
  advance --cycle              (Compounding 에서만) 다음 회차 → Scaffolding 으로, 같은 작업 유지
  skip <대상> --reason "..."    단계 건너뛰기 ✋
                               대상: <stage-id> | +N (N단계 전진) | until:<stage-id>
  approve-plan <file>          계획에 대한 사람의 승인 기록 ✋
  verify -- <검증 명령>        하네스가 직접 돌려 종료 코드로 판정한다. 통과해야
                               증거가 된다. 훅이 없는 도구에서도 이 길은 열려 있다

중간 그래프 (틀 셋 — 처음 둘과 마지막 — 사이는 회차마다 다를 수 있다)
  path                         이번 회차의 실행 그래프 — 방문 상태·분기·회차 한정 노드
  path add <id> --reason "..." [--label "..."] [--summary "..."]
           [--after <node>] [--write dev,tests]
                               앞쪽에 노드를 더한다 (회차 한정 — 회차가 닫히면 사라지고
                               이력은 event 로 남는다). 추가는 자유다 — 일이 늘 뿐
                               어떤 게이트도 사라지지 않는다
  path remove <node> --reason "..."
                               미방문 회차 한정 노드를 뺀다 ✋ — 미방문 노드 삭제는
                               스킵의 위장이므로 스킵과 같은 동의를 받는다

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
        print(t(USAGE))
        return 0
    try:
        return run_hook() if argv[0] == "hook" else run_cli(argv)
    except Refuse as ref:
        # **거절의 유일한 출구.** CLI 에서는 설명과 종료 코드가 그대로 나간다.
        #
        # 훅 경로에서는 stdout 이 JSON 채널이라 그리로 낼 수 없다. 그리고 훅이
        # 거절을 던지는 것은 애초에 프로그래밍 실수다 — 그때 traceback 으로
        # 죽으면 훅이 아무것도 막지 않는데 **아무도 그 사실을 모른다.** 이미
        # 있는 어휘로 "게이트가 꺼졌다"고 말한다.
        if argv[0] == "hook":
            return inactive(t("거절이 훅 경로로 새어 나왔다: %s")
                            % (ref.lines[0] if ref.lines else ref))
        for line in ref.lines:
            print(line)
        return ref.code


# ---------------------------------------------------------------- 게이트 적재
#
# 게이트 구현은 `gates/` 아래 파일로 갈라져 있다. **엔진을 주입한다** — 게이트가
# `import harness` 하면 직접 실행 시(`__main__`) 같은 파일이 두 번 로드되어 등록이
# 엉뚱한 모듈 객체로 간다. 목록은 적지 않고 `gates` 가 발견한다.
#
# 적재가 실패하면 그 게이트가 막던 것이 전부 통과한다 — 그래서 조용히 넘기지 않고
# 사실을 남긴다. `missing_gates()` 가 그것을 소리로 바꾼다.
# **원인을 버리지 않는다.** 예전에는 이 값을 채우기만 하고 읽는 곳이 없어서
# (`grep GATE_LOAD_ERROR` → 정의와 대입 둘뿐) `SyntaxError: invalid syntax
# (criteria.py, line 2)` 같은 진짜 이유가 사라졌다. 사용자는 "실리지 않았다" 만
# 보고 어느 파일이 왜 깨졌는지 알 수 없었다(4회차 D-M10).
GATE_LOAD_FAILS = []
try:
    import parts as _parts
    GATE_LOAD_FAILS += _parts.load(sys.modules[__name__])
except Exception as _exc:                      # noqa: BLE001 - 적재 실패도 사실이다
    GATE_LOAD_FAILS.append(("parts", "%s: %s" % (type(_exc).__name__, _exc)))
try:
    import gates as _gates
    GATE_LOAD_FAILS += _gates.register(sys.modules[__name__]) or []
except Exception as _exc:                      # noqa: BLE001 - 적재 실패도 사실이다
    GATE_LOAD_FAILS.append(("gates", "%s: %s" % (type(_exc).__name__, _exc)))


if __name__ == "__main__":
    sys.exit(main())
