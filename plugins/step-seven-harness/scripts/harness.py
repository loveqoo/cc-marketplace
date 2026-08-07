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
DB_REL = os.path.join(HARNESS_DIR, "harness.db")
CONFIG_REL = os.path.join(HARNESS_DIR, "stages.json")
WRAPPER_REL = os.path.join(HARNESS_DIR, "bin", "harness")
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


def sh_tokens(seg):
    """셸 토큰으로 쪼갠다. **따옴표는 토크나이저가 처리한다.**

    예전에는 `QUOTED_RE.sub("_", seg)` 로 따옴표 구간을 `_` 로 뭉갰다. 목적은
    `--reason "a b"` 의 `b` 가 위치 인자로 오인되지 않게 하는 것이었는데, 같은
    마스킹이 **따옴표로 감싼 실행 경로까지 지웠다.** 그래서
    `"...bin/harness" auto-skip on` 이 제어 명령으로 보이지 않아 **모든 동의
    게이트가 사라졌다** — 5차 리뷰가 찾았고 재현했다.

    근사를 고치지 않고 진짜 파서를 쓴다(`symtable` 때와 같은 판단). shlex 는
    따옴표를 소비하고 `--reason "a b"` 를 한 토큰으로 준다. 따옴표가 안 맞으면
    예외가 나므로, 그때는 옛 방식으로 떨어진다 — 판정을 아예 못 하는 것보다 낫다.
    """
    try:
        return shlex.split(seg)
    except ValueError:
        return QUOTED_RE.sub("_", seg).split()
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
    "cycle_adopt": "회차 포기 (재연결) — 측정에는 남고 회고 창은 그대로",
    "cycle_adopt_reason": "재연결 사유",
    "retro_keys": "회고 검색 키 확인",
    "plan_mode_exit": "plan mode 종료 관측",
    "stop_continue": "턴 이어붙임",
    "stop_stalled": "진전 없어 이어붙임 중단",
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

ADDED_COLUMNS = (("evidence", "digest", "TEXT"),
                 # 승격 시점의 **이벤트 id**. 재발 판정을 벽시계에서 여기로 옮긴다.
                 ("promotion", "after_id", "INTEGER"))


def migrate(con):
    """빠진 열을 채운다. 이미 있으면 아무것도 하지 않는다."""
    for table, col, typ in ADDED_COLUMNS:
        cols = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
        if cols and col not in cols:
            con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))


def connect(root, create=False):
    path = os.path.join(root, DB_REL)
    if not create and not os.path.isfile(path):
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=float(DB_WAIT_S))
    con.row_factory = sqlite3.Row
    # busy_timeout 이 **먼저**여야 한다. SQLite 는 journal mode 전환에 busy handler 를
    # 부르지 않으므로, 순서가 뒤면 동시 init 이 `database is locked` 로 죽는다.
    con.execute("PRAGMA busy_timeout=%d" % (DB_WAIT_S * 1000))
    con.execute("PRAGMA journal_mode=WAL")
    try:
        migrate(con)
        con.commit()
    except sqlite3.Error:
        pass  # 읽기 전용 파일시스템에서도 판정은 돌아야 한다
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
    if plugin_root_dir is None:
        plugin_root_dir = plugin_root()
    # **있는데 못 읽는 것과 없는 것은 다르다.** 손상된 문서를 템플릿으로 갈아치우면
    # 사용자가 덜어낸 규칙이 말없이 되살아나고, 그 사람은 이유 모를 차단만 본다.
    # 없으면(설치 전) 템플릿을 쓰고, 깨졌으면 그대로 알린다.
    if cfg is None and os.path.exists(path):
        return None
    tpl = jload(os.path.join(plugin_root_dir, "templates", "stages.json"))
    if tpl is None:
        # 프로젝트 사본으로 실행 중이다. 기본값은 엔진 옆에 함께 복사돼 있다.
        tpl = jload(os.path.join(root, DEFAULTS_REL))
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


def promote_as(cfg):
    """승격의 종류 → 설명. 어떻게 기계화하는지는 프로젝트마다 다르다.

    `test`(회귀 테스트로 승격)나 `lint`(린터 규칙으로) 같은 종류를 쓰는 팀이 있다.
    `declined` 는 `--as` 값이 아니라 보류 **결정**이므로 목록을 보여줄 때 제외한다.
    """
    m = cfg.obj("promotion.as_kinds")
    got = {k: v for k, v in m.items() if isinstance(v, str)} if m else {}
    return got or {k: t(v) for k, v in PROMOTE_AS_DEFAULT.items()}

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
            out.append((t(it["q"]), t(it.get("why") or "")))
    return out


def consent_map(cfg):
    """사람의 승인이 필요한 하네스 명령 → 무엇을 승인하는지의 설명.

    설정이라는 것은 **줄일 수 있다는 뜻**이다. `allow` 다이얼로그가 시끄러우면
    하네스를 끄는 대신 그 항목을 덜어낼 수 있다.
    """
    m = cfg.obj("consent")
    return {k: t(v) for k, v in m.items() if isinstance(v, str)} if m else {}


def bash_mutator_re(cfg):
    pat = cfg.at("bash.mutator_pattern")
    try:
        return re.compile(pat) if pat else BASH_MUTATORS
    except re.error:
        return BASH_MUTATORS       # 잘못된 정규식으로 게이트를 열지 않는다


def install_problems(root):
    """설치 형태에서 **하네스가 자신을 보호할 수 없는 경우**를 알린다.

    "훅은 프로젝트 밖의 플러그인 엔진을 실행하므로 프로젝트 사본은 게이트가 아니다"
    라고 문서에 적었는데, 그건 `git`/`github` 소스일 때만 참이다. 마켓플레이스를
    `directory` 소스로 등록하면(README 가 로컬 테스트에 권하는 방식) 플러그인 루트가
    **프로젝트 안**이 되고, 그러면 모델이 훅 엔진 자체를 고칠 수 있다.

    주장을 문서에 적어두면 설치 형태가 바뀔 때 거짓이 된다. **런타임에 재어 말한다.**
    개발 중에는 엔진을 고치는 것이 목적이므로 이것은 고장이 아니다 — 다만 그 상태에서
    자기 잠금을 신뢰해서는 안 된다는 사실은 보여야 한다.
    """
    try:
        pr = os.path.realpath(plugin_root())
        rp = os.path.realpath(root)
    except Exception:
        return []
    # **`normcase` 로는 부족하다.** POSIX 에서 그것은 항등 함수이고, `realpath` 도
    # 표기를 정규화하지 않는다(확인했다: `/PLUGINS` → `/PLUGINS`). 그래서 macOS 에서
    # `/Private/…` 로 엔진을 실행하고 root 가 `/private/…` 이면 — 같은 경로인데 —
    # 담김 판정이 실패해 경고가 나오지 않았다. 자기 잠금에서 쓴 것과 같은 방식으로,
    # 대소문자를 접어 비교한다. 구분하는 파일시스템에서 과잉 경고가 되는 것은
    # 받아들인다 — 경고를 놓치는 것보다 낫다.
    prn, rpn = os.path.normcase(pr).lower(), os.path.normcase(rp).lower()
    if prn == rpn or prn.startswith(rpn + os.sep):
        return [t("플러그인 엔진이 프로젝트 안에 있다 (%s) — 이 설치 형태에서는 "
                  "모델이 훅 엔진을 고칠 수 있으므로 자기 잠금을 신뢰할 수 없다. "
                  "개발 중이면 정상이다.") % os.path.relpath(pr, rp)]
    return []


def drift_problems(cfg, root):
    """**내장 조건의 판정 방식이 기본값에서 바뀐 것**을 알린다.

    조건의 이름과 판정 방식은 의미상 묶여 있다. `promotion_decided` 를
    `satisfied_by: file` 로 바꾸면 미결 승격이 남아 있어도 회차가 닫히고,
    `plan_approved` 를 `file` + `write_glob: ["**"]` 로 바꾸면 아무 파일이 사람의
    승인이 된다. 둘 다 적대적 리뷰에서 실증했고 아무 경고가 없었다.

    엔진에 이름을 다시 박아 금지하지는 않는다 — 그건 어휘화를 되돌리는 것이다.
    대신 **기본값과 대조해 달라진 것을 말한다.** 사용자가 정의한 조건은 기본값에
    없으므로 아무것도 말하지 않는다(오진 없음).
    """
    tpl = jload(os.path.join(plugin_root(), "templates", "stages.json")) \
        or jload(os.path.join(root, DEFAULTS_REL))
    if not isinstance(tpl, dict):
        return []
    base = tpl.get("criteria") or {}
    out = []
    for name, spec in sorted((cfg.obj("criteria") or {}).items()):
        want = base.get(name)
        if not isinstance(want, dict) or not isinstance(spec, dict):
            continue
        if spec.get("satisfied_by") != want.get("satisfied_by"):
            out.append(t("criteria.%s 의 판정 방식을 '%s' → '%s' 로 바꿨다 "
                         "— 이 조건의 이름이 뜻하는 것과 판정이 어긋난다. "
                         "의도한 것이면 그대로 두어라.")
                       % (name, want.get("satisfied_by"), spec.get("satisfied_by")))
        if want.get("human") and not spec.get("human"):
            out.append(t("criteria.%s 에서 human 표시를 뗐다 — 사람만 채울 수 있던 "
                         "조건이 모델도 채울 수 있게 된다") % name)
        # 글롭이 모든 경로로 넓어지면 '그 산출물' 이라는 뜻이 사라진다
        if "**" in cfg.seq("criteria.%s.write_glob" % name) \
                and "**" not in (want.get("write_glob") or []):
            out.append(t("criteria.%s.write_glob 이 '**' 로 넓어졌다 — 이 회차 "
                         "접두사가 붙은 **아무 파일이나** 이 조건을 채운다") % name)
    out += _rules_dropped(cfg, tpl)
    return out


def _rules_dropped(cfg, tpl):
    """기본값에 있는데 설정에서 **사라진** 쓰기 규칙.

    요약의 `쓰기 규칙 n/m` 은 분모가 **설정 자신**(`len(write_rules)`)이라
    규칙을 지우면 `7/7 → 6/6` 이 된다 — 삭제는 결코 결손으로 보이지 않았다
    (4회차 E-F12). 개수로는 잡을 수 없는 종류다. 분모를 템플릿으로 바꾸면
    사용자가 규칙을 **더할** 자유가 사라지므로, 개수 대신 **이름을 대조한다.**

    막지 않는다 — 규칙을 지우는 것은 사용자의 자유다. 지웠다는 **사실**만 말한다.
    단계 순서도 같은 종류다: 이름 집합은 같은데 순서가 바뀌면 우선순위가 바뀐다.
    """
    base = [r.get("id") for r in (tpl.get("write_rules") or []) if isinstance(r, dict)]
    have = {r.get("id") for r in write_rules(cfg) if isinstance(r, dict)}
    gone = [r for r in base if r and r not in have]
    out = []
    if gone:
        out.append(t("기본 쓰기 규칙 %s 가 설정에서 빠졌다 — 그 규칙이 막던 것이 "
                     "지금 아무것도 막지 않는다 (요약의 분모는 설정 자신이라 "
                     "삭제가 보이지 않는다). 의도한 것이면 그대로 두어라.")
                   % ", ".join(gone))
    b_st = [x.get("id") for x in (tpl.get("stages") or []) if isinstance(x, dict)]
    c_st = [x.get("id") for x in (cfg.get("stages") or []) if isinstance(x, dict)]
    if b_st and set(b_st) == set(c_st) and b_st != c_st:
        out.append(t("단계 순서를 바꿨다 (%s → %s) — 단계별 쓰기 허용과 종료 조건이 "
                     "따라 움직인다. 의도한 것이면 그대로 두어라.")
                   % (" → ".join(b_st), " → ".join(c_st)))
    return out


def language_problems(root):
    """번역 상태의 문제. config_problems 와 달리 root 가 필요해 따로 둔다."""
    lang, got, total = message_status(root)
    if not lang:
        return []
    if not got:
        return [t("language='%s' 인데 messages.%s.json 을 찾지 못했다 "
                  "— 모든 문장이 원문(한국어)으로 나온다") % (lang, lang)]
    if total and got < total:
        return [t("language='%s' 번역이 %d/%d (%d%%) — 나머지는 원문으로 나온다")
                % (lang, got, total, got * 100 // total)]
    return []


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
    # 게이트는 **자기 설정을 스스로 진단한다.** 여기서 게이트마다 적으면 게이트를
    # 더할 때 이 자리를 잊게 되고, 잊어도 조용하다 — 오늘 그것으로 세 번 당했다.
    out = gate_problems(cfg)
    # recall 대상 폴더. 여기 오타가 나면 그 폴더의 기록은 **영원히 안 나온다** —
    # 파일은 있고 키워드도 맞는데 조회에 안 걸린다. 조용한 결함으로 실제로 있었다.
    dev_dirs = set(cfg.seq("folder_rules.dev_subdirs"))
    for d in cfg.seq("recall.dirs", RECALL_DIRS_DEFAULT):
        if dev_dirs and d not in dev_dirs:
            out.append(t("recall.dirs 의 '%s' 가 folder_rules.dev_subdirs 에 없다 "
                       "— 그 폴더에는 쓸 수 없으므로 조회할 것도 없다") % d)
    if not cfg.seq("recall.dirs", RECALL_DIRS_DEFAULT):
        out.append(t("recall.dirs 가 비어 있다 — 과거 회고를 하나도 찾지 못한다"))
    if not retro_questions(cfg):
        out.append(t("retro_questions 가 비어 있다 — 회고에서 무엇을 물을지가 없다"))
    for i, it in enumerate(cfg.seq("retro_questions")):
        if not isinstance(it, dict) or not it.get("q"):
            out.append(t("retro_questions[%d] 에 q 가 없다 — 이 질문은 무시된다") % i)
    # **있는데 공허한 값**을 본다. 예전 진단은 '없는 참조'와 '모르는 값'만 봤고,
    # 그래서 `protected_paths: []`·`write_rules: []`·`mutator_pattern: "(?!)"` 가
    # 아무 말 없이 강제를 껐다. 적대적 리뷰에서 여섯 모양으로 확인했다.
    # 바닥값(SELF_LOCK)이 있으므로 하네스 자기 잠금은 이제 이것들로 풀리지 않지만,
    # 사용자가 지정한 보호 경로와 규칙은 여전히 조용히 사라질 수 있다.
    # 검증 증거 패턴을 **탐침한다.** 설정 텍스트를 읽어 "이게 너무 넓은가"를 판단하는
    # 것은 끝이 없다(`.*`, `.+`, `[\s\S]*`, `|`…). 대신 **증거가 되면 안 되는 명령**을
    # 넣어 보고 걸리는지 본다. 파싱이 아니라 실험이다.
    vpat = cfg.at("criteria.verification_evidence.bash_pattern")
    if vpat:
        try:
            vre = re.compile(vpat)
        except re.error:
            vre = None
        if vre is not None:
            hits = [c for c in ("ls", "echo hi", "cat README.md", "git status")
                    if vre.search(c)]
            if hits:
                out.append(t("criteria.verification_evidence.bash_pattern 이 검증이 "
                             "아닌 명령도 증거로 인정한다 (%s) — 성공한 아무 명령이나 "
                             "Verification 을 통과시킨다") % ", ".join(hits))

    kinds = promote_as(cfg)
    if not [k for k in kinds if k != "declined"]:
        out.append(t("promotion.as_kinds 에 승격 종류가 없다 — 보류밖에 할 수 없다"))
    for k in cfg.obj("promotion.verify_globs"):
        if k not in kinds:
            out.append(t("promotion.verify_globs 의 '%s' 가 as_kinds 에 없다 "
                       "— 아무도 그 종류로 승격할 수 없어 죽은 설정이다") % k)
    pat = cfg.at("bash.mutator_pattern")
    if pat:
        try:
            re.compile(pat)
        except re.error as e:
            out.append(t("bash.mutator_pattern 이 잘못된 정규식이다 (%s) "
                       "— 기본 패턴으로 되돌아간다") % e)

    known = set(cfg.obj("criteria"))
    for st in cfg.get("stages") or []:
        if not isinstance(st, dict):
            continue
        sid = st.get("id", "?")
        for field in ("exit_criteria", "stop_requires", "skip_requires"):
            for k in st.get(field) or []:
                if k not in known:
                    out.append(t("stages[%s].%s 의 '%s' 가 criteria 에 없다 "
                               "— 채울 방법이 없어 이 단계를 끝낼 수 없다")
                               % (sid, field, k))
        for p in st.get("panels") or []:
            if p not in PANELS:
                out.append(t("stages[%s].panels 의 '%s' 는 모르는 패널이다 (%s 중 하나) "
                           "— 조용히 무시된다")
                           % (sid, p, "/".join(sorted(PANELS))))
    return out


def stage_ids(cfg):
    return [s["id"] for s in cfg["stages"]]


def stage_index(cfg, sid):
    """설정에서의 자리. **모르는 id 는 0 이 아니다** — 호출 전에 걸러야 한다.

    예전에는 `else 0` 이었다. `stages.json` 에서 단계 id 하나만 바꾸면 DB 의 활성
    단계가 조용히 1단계로 읽혔고, 그 단계의 종료 조건(사람만 채울 수 있는
    `plan_approved` 포함)이 **아무 경고 없이 사라졌다.** 자기검사도 22/22 를 냈다.
    이제 그 상태는 `stage_known` 으로 미리 걸러 `inactive()` 가 소리 내어 말한다.
    """
    ids = stage_ids(cfg)
    return ids.index(sid) if sid in ids else 0


def stage_known(cfg, sid):
    return bool(sid) and sid in stage_ids(cfg)


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


def claim(con, sql, params):
    """조건부 UPDATE 로 **한 번만** 일어나는 일을 차지한다. 이겼으면 True.

    읽고-판단하고-쓰면 병렬 훅이 같은 자원을 여러 번 쓴다. `--uses 1` 예외를 넷이
    동시에 쓰고 `uses_left` 가 -3 이 되는 것을 재현했고, 같은 모양이 자동 승인 횟수와
    단계 전이에도 있었다. SQLite 의 조건부 UPDATE 는 원자적이므로 **rowcount 가
    승자를 정한다** — 판단을 WHERE 절 안으로 옮기는 것이 요점이다.
    """
    return con.execute(sql, params).rowcount > 0


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
    row = loop_row(con, lid)
    try:
        return int(row["cycle"]) if row and row["cycle"] else 1
    except (TypeError, ValueError, IndexError):
        return 1


def file_prefix(con, lid):
    """`.dev/` 산출물 파일명 접두사. 앞단 해시로 grep 하면 한 작업이 모인다."""
    return "%s-%d-" % (lid, cycle_of(con, lid))


def create_loop(con, cfg, root, intent=None, loop_id=None, only_if_none=False):
    """작업 하나와 그 단계들을 만든다. 만들어진(또는 이미 있던) 작업 id.

    `only_if_none` 은 **열린 작업이 없을 때만** 만든다. 읽고-판단하고-쓰면 병렬
    호출이 각자 하나씩 만든다 — 보기만 하는 `status` 넷을 동시에 돌렸더니 열린
    작업이 넷 생겼다. 판단을 INSERT 의 WHERE 안으로 옮겨 승자만 만들게 한다.
    """
    lid = loop_id or new_loop_id()
    sql, args = ("INSERT OR IGNORE INTO loop(id,intent,branch,created_at) "
                 "VALUES(?,?,?,?)", (lid, intent, git_branch(root), now()))
    if only_if_none:
        sql = ("INSERT INTO loop(id,intent,branch,created_at) SELECT ?,?,?,? "
               "WHERE NOT EXISTS (SELECT 1 FROM loop WHERE closed_at IS NULL)")
        if not claim(con, sql, args):
            row = con.execute("SELECT id FROM loop WHERE closed_at IS NULL "
                              "ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
            if row:
                return row["id"]
    else:
        con.execute(sql, args)
    for i, st in enumerate(cfg["stages"]):
        con.execute("INSERT OR IGNORE INTO stage(loop_id,stage,status,entered_at) "
                    "VALUES(?,?,?,?)",
                    (lid, st["id"], "active" if i == 0 else "pending",
                     now() if i == 0 else None))
    set_meta(con, "head", lid)
    return lid


def end_cycle(con, cfg, lid, kind):
    """회차를 끝내는 **공통 절차**: 집계를 남기고 진행 중 상태를 버린다.

    `rotate_loop` 과 `close_loop` 이 이것을 각자 갖고 있었다. 그래서 한쪽에만
    스냅샷을 넣었을 때 다른 쪽(`loop adopt`)이 옆문이 됐고, 마찰이 쌓인 회차를
    한 줄로 지울 수 있었다(4회차 C③). **"회차를 끝낸다" 의 뜻은 한 곳에 있다.**

    닫기 자체(누가 이겼나)는 호출자가 정한다 — `rotate_loop` 은 claim 으로,
    `close_loop` 은 무조건. 그것만이 둘의 진짜 차이다.
    """
    stages = cfg.get("stages") or [{}]
    with swallow(t("회차 스냅샷")):
        record_cycle_close(con, cfg, lid,
                           active_stage(con, lid) or stages[0].get("id") or "-", kind)
    for tbl in ("stage", "evidence", "wgrant"):
        con.execute("DELETE FROM %s WHERE loop_id=?" % tbl, (lid,))


def rotate_loop(con, cfg, root, lid, intent=None):
    """이 작업을 닫고 다음을 연다. **닫기를 차지한 쪽만 연다** (진 쪽에는 None).

    닫기와 열기가 두 걸음이면 병렬 호출이 각자 하나씩 연다 — `loop new` 넷을
    동시에 돌렸더니 열린 작업이 셋 남았다.
    """
    if not claim(con, "UPDATE loop SET closed_at=? WHERE id=? AND closed_at IS NULL",
                 (now(), lid)):
        return None
    end_cycle(con, cfg, lid, "cycle_close")
    return create_loop(con, cfg, root, intent)


def close_loop(con, cfg, lid, kind):
    """루프의 작업 상태를 버린다. 영구 기록은 폴더의 파일명이 갖고 있다.

    남기는 것: loop 인덱스(해시·의도·기간)와 event(관측 기록).
    event 를 버리면 복리의 원료가 사라지고, loop 인덱스가 없으면 event 가
    어느 작업의 것인지 알 수 없다. 버리는 것은 진행 중 상태뿐이다.

    **닫기 전에 스냅샷을 남긴다.** `rotate_loop` 에만 있었더니 `loop adopt` 가
    옆문이 됐다 — 진행 중 회차의 마찰이 어느 측정 창에도 속하지 못하고 사라져서,
    "차단이 쌓인 회차를 없애려면 `harness loop adopt <아무 id>` 한 줄" 이 됐다
    (4회차 C③ 실측). 스냅샷은 회차당 하나이므로(`record_cycle_close`) 이미
    남겼으면 여기서 두 번 남지 않는다.
    """
    end_cycle(con, cfg, lid, kind)
    con.execute("UPDATE loop SET closed_at=? WHERE id=?", (now(), lid))


# 단계 전이 SQL. 여섯 자리에 흩어져 있었고 셋은 `claim` 안, 셋은 무조건이었다.
# **문장은 한 곳에, 경쟁 판정은 호출자가.** 흩어져 있으면 열이 늘 때 하나가 빠진다.
STAGE_SET = {
    "enter":   ("UPDATE stage SET status='active', entered_at=? "
                "WHERE loop_id=? AND stage=?"),
    "done":    ("UPDATE stage SET status='done', left_at=? "
                "WHERE loop_id=? AND stage=? AND status='active'"),
    "skipped": ("UPDATE stage SET status='skipped', left_at=?, reason=?, "
                "authorized_by=? WHERE loop_id=? AND stage=? AND status='active'"),
    "skip_ahead": ("UPDATE stage SET status='skipped', left_at=?, reason=?, "
                   "authorized_by=? WHERE loop_id=? AND stage=?"),
    "reset":   ("UPDATE stage SET status='pending', entered_at=NULL, left_at=NULL, "
                "reason=NULL, authorized_by=NULL WHERE loop_id=? AND stage != ?"),
}


def stage_set(con, what, params):
    """단계 전이 하나. **차지했으면 True** — `claim` 과 같은 뜻이다."""
    return claim(con, STAGE_SET[what], params)


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
    """관측을 적는다. **적는 것이 판정을 막지 않는다.**

    이 INSERT 가 터지면 예외가 훅까지 올라가 `inactive()` 로 빠졌다 — 즉
    **판정을 이미 알고 있으면서 적을 수 없다는 이유로 그 답을 버렸다.**
    읽기 전용 파일시스템·디스크 꽉 참·권한 사고에서 게이트가 통째로 열렸다
    (4회차 C⑥ 실측: `chmod a-w` 뒤 `docs/b.md` 쓰기가 허용됐다).

    기록은 복리의 원료이지 강제의 조건이 아니다. 삼키되 `swallow` 로 삼켜
    사실이 `status` 에 남는다. **적지 못한 것과 막지 못한 것은 다른 일이다.**
    """
    with swallow(t("관측 기록(%s)") % kind):
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
                 from_epoch=None, to_epoch=None, after_id=None, upto_id=None):
    """이벤트를 고른다.

    ## 회차 경계는 시각이 아니라 **id** 로 나눈다

    시각은 초 단위라 같은 초에 일어난 두 사건의 순서를 말하지 못한다. 그래서 회차
    경계에 `+1초` 를 두었고, 그 1초 안에 일어난 다음 회차의 이벤트는 **어느 회차에도
    속하지 못하고 영영 사라졌다.** `id` 는 단조 증가하므로 그 문제가 없다 —
    순서를 시각으로 판정하지 말고 정체성으로 판정한다.

    `from_epoch`/`to_epoch` 는 승격 재발 판정처럼 event 가 아닌 것(promotion.at)을
    기준으로 삼는 자리에만 남는다.

    이 함수가 없을 때는 같은 관용구가 네 곳에 복붙돼 있었고, 그중 두 곳이
    SQL 문자열 비교를 쓰고 있었다. 오프셋 유무나 공백 구분 형식이 섞이면
    사전순과 실제 순서가 어긋나서 창 밖의 이벤트가 안으로 들어온다 —
    두 릴리스에 걸쳐 같은 버그를 두 번 냈다. 경계 판정은 여기 한 곳에만 있다.
    """
    sql = ["SELECT id, at, loop_id, stage, kind, rule, target, detail "
           "FROM event WHERE 1=1"]
    params = []
    if kinds:
        sql.append("AND kind IN (%s)" % ",".join("?" * len(kinds)))
        params += list(kinds)
    for col, val in (("loop_id", loop_id), ("rule", rule), ("target", target)):
        if val is not None:
            sql.append("AND IFNULL(%s,'-') = ?" % col)
            params.append(val)
    if after_id is not None:
        sql.append("AND id > ?")
        params.append(after_id)
    if upto_id is not None:
        sql.append("AND id <= ?")
        params.append(upto_id)
    sql.append("ORDER BY id")
    rows = con.execute(" ".join(sql), params).fetchall()
    if from_epoch is None and to_epoch is None:
        return rows
    out = []
    for r in rows:
        it = ts_epoch(r["at"])
        if from_epoch is not None and it < from_epoch:
            continue
        if to_epoch is not None and it >= to_epoch:
            continue
        out.append(r)
    return out


# 곁다리 작업이 실패한 사실. `swallow` 가 채우고 `status` 가 보여준다.
SWALLOWED = []


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
        SWALLOWED.append("%s: %s: %s" % (what, type(exc).__name__, exc))


def loop_row(con, lid):
    """작업 한 행. **컬럼을 골라 뽑던 일곱 자리를 여기로 모은다.**

    `SELECT cycle`, `SELECT intent`, `SELECT created_at`, `SELECT *` 가 제각각
    있었다. 열이 늘거나 뜻이 바뀌면 일곱 곳을 다 찾아야 하고, 실제로 그 종류의
    누락이 이 리포에서 반복됐다 — 회차 경계를 id 로 옮겼을 때 `recurrence`
    한 곳만 벽시계에 남은 것이 같은 모양이다(4회차 C④).
    """
    return con.execute("SELECT * FROM loop WHERE id=?", (lid,)).fetchone()


def promotion_rows(con, key=None, decision=None, maturity=None,
                   order="at", limit=None):
    """승격 결정. 없으면 빈 목록, `key` 를 주면 한 행 또는 None."""
    sql, params = ["SELECT * FROM promotion WHERE 1=1"], []
    for col, val in (("key", key), ("decision", decision), ("maturity", maturity)):
        if val is not None:
            sql.append("AND %s = ?" % col)
            params.append(val)
    sql.append("ORDER BY %s" % order)
    if limit:
        sql.append("LIMIT %d" % int(limit))
    rows = con.execute(" ".join(sql), params).fetchall()
    return (rows[0] if rows else None) if key is not None else rows


def live_grants(con, lid):
    """아직 쓸 수 있는 쓰기 예외."""
    return con.execute("SELECT * FROM wgrant WHERE loop_id=? AND uses_left>0",
                       (lid,)).fetchall()


def open_loops(con):
    """열린 작업의 id 들."""
    return {r["id"] for r in con.execute(
        "SELECT id FROM loop WHERE closed_at IS NULL")}


def loops_created_after(con, epoch):
    """그 시각 이후에 만들어진 작업 수. created_at 도 문자열로 비교하면 안 된다."""
    return sum(1 for r in con.execute("SELECT created_at FROM loop")
               if ts_epoch(r["created_at"]) > epoch)


def has_evidence(con, lid, kind):
    return con.execute("SELECT 1 FROM evidence WHERE loop_id=? AND kind=? LIMIT 1",
                       (lid, kind)).fetchone() is not None


def evidence_digest(root, item):
    """이 증거가 가리키는 **파일의 지문.** 파일이 아니면 None.

    ## 왜 필요한가 — 증거에는 유효기간이 있다

    증거는 "언제 무엇을 봤다"를 적는데, **본 것이 그 뒤에 변할 수 있다.**
    `plan_approved` 는 계획 **파일**을 가리킨다. 승인을 받은 뒤 그 파일을 고쳐도
    승인 기록은 그대로 살아 있었고, 그래서 **사람이 보지 않은 계획으로 진행**할 수
    있었다 — 5차 리뷰가 HIGH 로 찾았다.

    승인만의 문제가 아니다. 파일을 가리키는 증거는 전부 같은 성질을 갖는다. 그래서
    `plan_approved` 만 따로 손보지 않고 **증거라는 것 자체에 지문을 붙인다.** 지문이
    다르면 그 증거가 말하는 사실은 더 이상 참이 아니다.

    파일이 아닌 증거(완료 조건 문장, `agent:Task`, 명령 문자열)는 지문이 없고,
    지문이 없는 증거는 늘 유효하다 — 변할 근거가 없다.
    """
    # 예전에는 `"/" not in item` 으로 걸렀다 — 명령 문자열을 빼려던 것인데,
    # **리포 루트의 파일은 상대경로에 슬래시가 없다.** `README.md` 를 승인하면
    # 지문이 안 붙어 만료 기능이 통째로 꺼졌다. 조건을 지운다. 파일이 아닌 것은
    # 아래 `isfile` 이 이미 걸러낸다.
    if not item or root is None:
        return None
    rel = rel_to_root(root, item)
    if not rel:
        return None
    p = os.path.join(root, rel)
    if not os.path.isfile(p):
        return None
    dig = hashlib.sha256()
    try:
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                dig.update(chunk)
    except OSError:
        return None
    return dig.hexdigest()


def evidence_rows(con, root, lid, kind):
    """(item, 아직 유효한가). 지문이 없으면 유효하다."""
    out = []
    for r in con.execute("SELECT * FROM evidence WHERE loop_id=? AND kind=?",
                         (lid, kind)):
        keys = r.keys()
        dig = r["digest"] if "digest" in keys else None
        out.append((r["item"], dig is None or dig == evidence_digest(root, r["item"])))
    return out


def has_valid_evidence(con, root, lid, kind):
    return any(ok for _, ok in evidence_rows(con, root, lid, kind))


def stale_evidence(con, root, lid, kind):
    """근거가 바뀌어 만료된 증거들."""
    return [it for it, ok in evidence_rows(con, root, lid, kind) if not ok]


def record_evidence(con, lid, sid, kind, item, root=None):
    con.execute("INSERT OR IGNORE INTO evidence(loop_id,stage,kind,item,at) "
                "VALUES(?,?,?,?,?)", (lid, sid, kind, item, now()))
    dig = evidence_digest(root, item)
    if dig is not None:
        # **다시 적립할 때 지문이 갱신돼야 한다.** `INSERT OR IGNORE` 만 두면 계획을
        # 고치고 다시 승인해도 옛 지문이 남아 영영 만료 상태가 된다. `OR REPLACE` 를
        # 쓰지 않는 이유는 rowid 가 바뀌어 완료 조건의 **입력 순서**가 흐트러지기
        # 때문이다 (`acceptance_of` 가 rowid 순으로 읽는다).
        con.execute("UPDATE evidence SET digest=?, at=? "
                    "WHERE loop_id=? AND kind=? AND item=?",
                    (dig, now(), lid, kind, item))


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


def last_event_id(con):
    """지금까지 기록된 마지막 이벤트 id. 순서의 기준점이다 — 시계가 아니다."""
    row = con.execute("SELECT MAX(id) i FROM event").fetchone()
    return (row["i"] or 0) if row else 0


def recurrence(con, p):
    """승격 이후 같은 항목이 다시 걸렸는지 → (횟수, 작업 수).

    승격이 통했는지의 유일한 객관 증거다.

    **경계는 시각이 아니라 id 다.** 회차 경계는 전부 id 로 옮겼는데 여기 하나만
    벽시계에 남아 있었고, 그래서 두 가지가 함께 틀렸다(4회차 C④):
      · 시계가 앞섰다 되돌아오면(NTP 보정·VM 재개) 승격 **이후**의 재발이
        영원히 보이지 않는다 → `metrics` 가 "재발 0%" 라고 거짓 보고한다.
      · 시계가 정상이어도 승격과 **같은 초**에 일어난 재발은 `+1초` 경계에
        걸려 사라진다. `events_where` 가 바로 그 `+1초` 를 없애려고 만든 것인데
        여기만 남았다.
    id 는 단조 증가하므로 순서 판정에 시계가 필요 없다.

    `after_id` 가 없는 낡은 행은 시각으로 떨어진다 — 그 행들은 이 열이 생기기
    전에 쓰였고, 없는 것을 지어내지 않는다.
    """
    item = {"kind": p["kind"], "name": p["key"].split(":", 1)[1]}
    aid = p["after_id"] if "after_id" in p.keys() else None
    if aid is not None:
        hits = events_where(con, kinds=(item["kind"],), after_id=aid,
                            **promo_match(item))
    else:
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
    for p in promotion_rows(con):
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
    decided = {r["key"]: r for r in promotion_rows(con)}
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
    """이번 회차 창의 시작 — **직전 회차 종료 이벤트의 id** (배타적 하한).

    1회차에는 종료 기록이 없으므로 0 이다 (`loop_id` 로 이미 이 작업만 걸러진다).
    """
    row = con.execute(
        # `cycle_adopt` 도 경계다. 빠뜨렸더니 재연결 뒤 창이 옛 위치에 고정돼
        # **버려진 회차의 마찰이 다음 회차 기록으로 흡수**됐다 ("회차 2: 차단 5").
        "SELECT MAX(id) i FROM event WHERE loop_id=? "
        "AND kind IN ('cycle_close','cycle_adopt')",
        (lid,)).fetchone()
    return (row["i"] or 0) if row else 0


def cycle_seconds(con, lid, lo):
    """회차 창이 열린 뒤 흐른 초. 1회차는 경계 이벤트가 없으므로 작업 생성 시각을 쓴다."""
    if lo:
        row = con.execute("SELECT at FROM event WHERE id=?", (lo,)).fetchone()
        when = row["at"] if row else None
    else:
        row = loop_row(con, lid)
        when = row["created_at"] if row else None
    return max(0, int(time.time() - ts_epoch(when))) if when else 0


def retro_window_start(con, lid):
    """**회고가 덮어야 할 범위**의 시작. 측정 창과 다른 질문이다.

    측정 창은 `cycle_adopt` 에서 새로 열려야 한다 — 그러지 않으면 버려진 회차의
    마찰이 다음 회차 기록으로 흡수돼 "회차 2: 차단 5" 같은 거짓 문장이 나온다.
    반대로 회고는 **이어받은 작업의 앞선 사실까지 덮어야** 한다 — 재연결했다고 해서
    이미 적어 둔 회고가 '못 찾음' 이 되면 안 된다.

    한 창이 두 뜻을 갖고 있어서 둘 중 하나가 늘 틀렸다. 뜻이 둘이면 창도 둘이다.
    """
    row = con.execute(
        "SELECT MAX(id) i FROM event WHERE loop_id=? AND kind='cycle_close'",
        (lid,)).fetchone()
    return (row["i"] or 0) if row else 0


def cycle_counters(con, lid, lo):
    """이 회차의 마찰 수치. 회피 지표를 반드시 함께 담는다 — 차단만 보면 속는다.

    `lo` 는 cycle_window_start 가 준 event id 다 (배타적 하한).
    """
    rows = events_where(con, loop_id=lid, after_id=lo)
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

    # 반복 실패 = 이 회차의 실패 중, 같은 명령이 **앞서 이미 한 번 실패한** 것.
    # '앞서' 는 이전 회차·이전 작업뿐 아니라 **이 회차 안의 앞선 실패**도 포함한다.
    # 같은 명령을 두 번 깨뜨린 것은 회차 경계와 무관하게 반복이기 때문이다.
    # (설명이 "이전 회차에도" 로 읽혀 오해를 샀다 — 세는 방식이 아니라 말이 틀렸다.)
    # 첫 회차는 `lo == 0` 이라 `id <= 0` 이 늘 공집합이었다 — **이전 작업의 실패를
    # 하나도 세지 않았다.** 대부분의 작업이 1회차로 끝나므로 반복 실패 지표가 구조적으로
    # 낮게 나왔다. 창이 없으면 '이 작업의 첫 이벤트 이전' 을 경계로 쓴다.
    before = lo
    if not before:
        row = con.execute("SELECT MIN(id) i FROM event WHERE loop_id=?",
                          (lid,)).fetchone()
        before = (row["i"] - 1) if row and row["i"] else 0
    seen_before = {r["target"] for r in
                   events_where(con, kinds=("tool_fail",), upto_id=before)}
    refails, seen_now = 0, set()
    for r in rows:
        if r["kind"] != "tool_fail":
            continue
        if r["target"] in seen_before or r["target"] in seen_now:
            refails += 1
        seen_now.add(r["target"])

    return {
        "dur": cycle_seconds(con, lid, lo),
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


def record_cycle_close(con, cfg, lid, sid, kind="cycle_close"):
    """회차 경계에서 그 회차의 집계를 한 줄로 남긴다.

    stage 행은 작업이 닫힐 때 삭제되므로 나중에 회차별 비용을 되살릴 수 없다.
    경계에서 스냅샷을 남기면 event 는 작업이 닫혀도 살아남아 측정이 가능해진다.

    **회차 하나에 스냅샷 하나.** 이 불변식을 호출자들의 조율에 맡겼더니 깨졌다:
    `advance --done` 은 여기를 부르고 곧이어 `rotate_loop` 을 부르는데 그것도
    여기를 부른다. 두 번째 호출 시점에는 창 시작이 방금 쓴 행으로 옮겨져 있어
    **전부 0 인 유령 회차**가 하나 더 쌓였고, `metrics` 의 회차 추세가 정확히
    반토막 났다 (차단 4건 → 보고 2.0, 4회차 C① 실측). 더 나쁜 것은 `--cycle` 은
    1행, `--done` 은 2행이라 **두 종료 경로가 서로 다른 분모**를 만든 것이다 —
    작업을 자주 끝내는 것이 지표를 좋게 만드는 가장 싼 방법이 됐다.

    호출자를 세는 대신 여기서 못 박는다. 이미 있으면 그 행을 돌려준다.

    `kind` 는 **회차를 어떻게 끝냈나**다. 둘 다 집계를 남기지만 뜻이 다르다:
      · `cycle_close` — 끝냈다. 측정 창과 **회고 창** 둘 다 여기서 새로 연다.
      · `cycle_adopt` — 버렸다(재연결). 측정 창만 새로 연다 — 회고는 이어받은
        작업의 앞선 사실까지 덮어야 하므로(3회차) 창을 옮기면 안 된다.
    한 종류로 뭉치면 둘 중 하나가 늘 틀린다. 실제로 그랬다: 종류를 합쳤더니
    재연결 뒤 1회차 회고가 '못 찾음' 이 됐다(실측).
    """
    cyc = cycle_of(con, lid)
    tgt = "%s-%d" % (lid, cyc)
    row = con.execute("SELECT detail FROM event WHERE target=? "
                      "AND kind IN ('cycle_close','cycle_adopt')", (tgt,)).fetchone()
    if row:
        try:
            return json.loads(row["detail"])
        except ValueError:
            return None
    c = cycle_counters(con, lid, cycle_window_start(con, lid))
    c["cycle"] = cyc
    record_event(con, lid, sid, kind, str(cyc), tgt,
                 json.dumps(c, ensure_ascii=False))
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
                          after_id=cycle_window_start(con, lid)):
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
                          after_id=lo):
        k = r["target"] if r["kind"] == "tool_fail" else r["rule"]
        if k and k not in keys:
            keys.append(k)
    return keys[:limit]


def retro_files_of_loop(con, cfg, root, lid):
    """이 **작업**의 회고·학습 파일 (회차를 가리지 않는다).

    회차별로 좁히는 것은 `retro_files_of_cycle` 이다. 둘을 구분하는 이유는 질문이
    다르기 때문이다 — "이번 회차가 회고를 썼나" 와 "이 키가 나중에 찾아지나".
    """
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
            if n.startswith(lid + "-") and os.path.isfile(os.path.join(d, n)):
                out.append(os.path.join(d, n))
    return out


def retro_key_report(con, cfg, root, lid, lo):
    """(키, 찾은 키, 못 찾은 키). 검색과 **같은 범위**를 읽어 확인한다."""
    keys = cycle_search_keys(con, lid, lo)
    if not keys:
        return [], [], []
    # **검색 키의 창과 파일의 창을 맞춘다.** 키는 시간창(마지막 회차 종료 이후)에서
    # 오는데 파일은 접두사창(이번 회차)에서 왔다. `loop adopt` 가 회차만 올리고 측정
    # 창은 건드리지 않으므로 두 창이 갈라져, 이미 적어둔 키가 "못 찾음" 으로 나왔다 —
    # 4차 리뷰가 지적했다. 이 확인의 질문은 "나중에 찾아지나" 이므로 작업 전체가 맞다.
    hay = ""
    for path in retro_files_of_loop(con, cfg, root, lid):
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

    with swallow(t("할 일 후보")):
        for it in pending_promotions(con, cfg):
            add(t("승격 결정"), t("'%s' 가 작업 %d개에서 반복된다 — 훅·구조로 올릴지 결정")
                % (it["key"], it["loops"]),
                "harness promote %s --as hook --note \"...\"" % it["key"])
    with swallow(t("할 일 후보")):
        for r in promotion_rows(con, maturity="regressed"):
            add(t("재발한 승격"), t("'%s' 는 %s 로 승격했는데 다시 걸렸다 — 그 방법이 통하지 "
                "않았다") % (r["key"], r["decision"]),
                t("원인을 다시 보고 `harness promote %s` 로 다시 결정") % r["key"])
    with swallow(t("할 일 후보")):
        rep = tidy_report(con, cfg, root)
        for d, note in rep["dirs"]:
            add(t("기록 정리"), "%s %s" % (d, note), t("인덱스를 만들거나 갱신 (Scaffolding)"))
        if rep["groups"]:
            add(t("기록 정리"), t("한 작업이 여러 파일을 남긴 묶음 %d개 — 하나로 병합")
                % len(rep["groups"]), t("harness tidy 로 목록 확인 (Scaffolding)"))
        if rep["stale"]:
            add(t("기록 정리"), t("닫힌 작업의 오래된 파일 %d개 — 인덱스에 요약하고 정리")
                % len(rep["stale"]), t("harness tidy 로 목록 확인 (Scaffolding)"))
        if rep["learned"] and rep["learned"][0] >= rep["learned"][1]:
            add(t("예산"), t("LEARNED.md 가 %d/%d 줄로 찼다 — 한 줄을 비워야 새 규칙이 들어간다")
                % rep["learned"], t("harness promote <기존키> --decline --reason \"...\""))
    return out


def render_work_candidates(items, mode_note=True):
    if not items:
        return
    print(t("\n하네스가 아는 할 일 (%d개) — 새 작업이 없다면 여기서 고를 수 있다:")
          % len(items))
    for i, it in enumerate(items, 1):
        print("  %d. [%s] %s" % (i, it["kind"], it["what"]))
        print("     → %s" % it["how"])
    if mode_note:
        print(t("  고르면 `harness loop intent \"...\"` 와 `harness loop done-when \"...\"` 로 "
              "기록하고 진행하라. 이것들도 정말 필요 없으면 그렇다고 말하고 멈춰라."))


def promotion_summary(con, cfg):
    rows = con.execute("SELECT maturity, COUNT(*) c FROM promotion "
                       "GROUP BY maturity").fetchall()
    return {r["maturity"]: r["c"] for r in rows}


def learned_lines(con, cfg):
    """LEARNED.md 에 실릴 규칙 줄. rule 로 승격되고 살아 있는 것만.

    저장된 maturity 가 아니라 실시간 판정을 쓴다 — 그러지 않으면 재발한 규칙이
    항상 로드되는 문서에 계속 남는다(실제로 남았다).
    """
    return [r for r in promotion_rows(con, decision="rule")
            if not is_regressed(con, cfg, r)]


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
    body = [t(LEARNED_HEAD) % learned_budget(cfg)]
    if rows:
        for r in rows:
            body.append("- [%s] %s <!-- %s -->"
                        % (r["maturity"], (r["note"] or "").strip(), r["key"]))
    else:
        body.append(t("(아직 없다 — 반복된 실수가 승격되면 여기에 쌓인다.)"))
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
    # **증거가 있는 것과 그 증거가 아직 참인 것은 다르다.** 파일을 가리키는 증거는
    # 그 파일이 바뀌면 만료된다 — 자세한 근거는 `evidence_digest` 에 적었다.
    if has_valid_evidence(con, root, lid, kind):
        return True
    if how == "file":
        return fs_evidence(cfg, root, file_prefix(con, lid), kind) is not None
    return False


def criterion_help(cfg, kind):
    return t(cfg.at("criteria.%s.help" % kind, kind))


def criterion_why(con, cfg, root, lid, kind):
    """왜 아직 안 됐는지. **만료된 증거가 있으면 그 사실을 먼저 말한다.**

    "계획 승인이 필요하다" 만 보면 이미 승인한 사람은 왜 또냐고 생각하고, 하네스가
    고장 난 것으로 읽는다. 막을 때는 최적 행동을 함께 준다 — 그러려면 무엇이 달라졌는지
    부터 말해야 한다.
    """
    stale = stale_evidence(con, root, lid, kind)
    if stale:
        return t("'%s' 이(가) 그때 본 것과 달라졌다 — 승인·관측은 그 시점의 내용에 "
                 "대한 것이므로 만료됐다. %s") % (", ".join(stale),
                                            criterion_help(cfg, kind))
    return criterion_help(cfg, kind)


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


def floor_hit(root, path):
    """바닥값에 걸리나 — **경로가 가리키는 것**으로 본다.

    `self_lock_hit` 은 문자열 검사다. 이것은 symlink 별칭까지 본다. 바닥값을 판정하는
    곳은 전부 이것을 써야 한다. 이유는 `rel_aliases` 에 적었다.
    """
    for rel in rel_aliases(root, path):
        if self_lock_hit(rel):
            return rel
    return None


# 이 이름들은 **무해하다고 선언할 수 없다.** `bash.readers`(읽기니까 건너뛴다) 와
# `bash.interpreters`(다음 인자는 실행 대상이니까 건너뛴다) 는 둘 다 "이 이름은
# 안전하다"는 설정이고, 둘 다 변경 명령을 넣으면 **설정만으로 자기 잠금이 풀린다.**
#
# readers 쪽은 `readers: ["rm"]` 로 확인해 막았는데 interpreters 쪽은 열려 있었다 —
# `interpreters: ["rm"]` 이면 `rm <엔진>` 의 경로가 '실행 대상'으로 건너뛰어진다.
# 5차 리뷰가 찾았다. 같은 결함을 두 번 겪었으므로 **목록을 하나로 합친다.** 앞으로
# 세 번째 '무해 선언' 설정이 생겨도 같은 바닥을 공유한다.
NEVER_BENIGN = ("rm", "mv", "cp", "dd", "tee", "truncate", "shred", "install",
                "ln", "sed", "mkdir", "touch", "find", "chmod", "chown", "sqlite3")


def benign_head(cfg, key, head, default=()):
    """`head` 가 이 '무해 선언' 목록에 있고, 위장이 아닌가.

    설정은 잠금을 **푸는** 방향으로는 쓰이지 않는다.
    """
    return head in cfg.seq("bash." + key, default) and head not in NEVER_BENIGN


def protected_pats(cfg):
    """보호 경로 = 바닥값 ∪ 설정. 설정이 비어 있어도 바닥값은 남는다."""
    out = list(SELF_LOCK)
    for p in cfg.seq("folder_rules.protected_paths"):
        if isinstance(p, str) and p not in out:
            out.append(p)
    return out


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


BASH_SPLIT = re.compile(r"\|\||&&|[;&|\n]")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# 이 프로그램들에 넘긴 경로는 '실행 대상'이다. 하네스 래퍼를 python3 로 돌리는 것은
# 정상 동작이므로 변경 시도로 오인해서는 안 된다.
# 읽기만 하는 명령은 막지 않는다. 과잉 차단은 마찰이 되고, 마찰은 게이트를 끄게 만든다.
# find 는 없다 — `-delete`/`-exec` 로 파일을 지운다.


# 대상이 명령 문자열이 아니라 **실행 결과 안**에 있는 파괴. `find . -name harness.db
# -delete` 는 토큰에 보호 경로가 없어 어떤 문자열 검사도 지나간다.
#
# 특정하려 들지 않는다 — 그 방향은 셸 재구현이고, 근사는 이미 여러 번 틀렸다.
# **모른다는 사실 자체가 판정이다:** 막지도(정상 정리가 막힌다) 통과시키지도(구멍이다)
# 않고 사람에게 묻는다. 하네스에 이미 있는 어휘(`ask`)를 쓴다.
#
# 이것은 경계가 아니라 **가시성**이다. 경계는 바닥값과 래퍼 무결성이다.
BASH_OPAQUE = (
    (re.compile(r"\bfind\b(?=.*\s-(?:delete|exec|execdir)\b)"),
     "find 가 **찾아낸 것**을 지우거나 그것에 명령을 실행한다"),
    (re.compile(r"\bxargs\b(?=.*\b(?:rm|mv|tee|truncate|shred|sed|chmod)\b)"),
     "xargs 가 **넘겨받은 목록**을 대상으로 삼는다"),
)


# 바닥값 경로의 **문자열 형태**. 글롭을 떼고 접두사만 남긴다.
FLOOR_TEXT = tuple(sorted({p.rstrip("*").rstrip("/") for p in SELF_LOCK}, key=len))


FLOOR_RE = re.compile("|".join(re.escape(p) for p in FLOOR_TEXT))
# 경로 토큰의 **나머지**를 먼저 삼킨다. `FLOOR_TEXT` 는 짧은 것부터라
# `.claude/harness/bin` 이 먼저 맞고, 그 뒤 `/harness` 가 남는다.
WORD_RE = re.compile(r"""[^\s'"]*['"]?\s+([A-Za-z][\w-]*)(?:\s+([A-Za-z][\w-]*))?""")


def _invocation(cmd, at):
    """`at` 위치에서 이어지는 것이 **하네스 호출**인가.

    `<래퍼> status`, `python3 <엔진> advance`, `sh -c '<래퍼> skip …'` 은 전부
    실행이지 언급이 아니다. 토큰 위치로 가르려 했더니 `sh -c '<래퍼> advance'`
    가 과잉 차단됐다(실측). "무엇이 하네스 호출인가" 의 답은 `ctrl_known` 이
    이미 갖고 있다 — **답이 둘이면 갈린다.** 그 답을 다시 쓴다.
    """
    m = WORD_RE.match(cmd, at)
    if not m:
        return False
    one, two = m.group(1), m.group(2)
    return ctrl_known(one) or (bool(two) and ctrl_known("%s %s" % (one, two)))


def floor_named(cfg, cmd):
    """명령 **원문**이 바닥값을 이름으로 부르나. 그 이름 또는 None.

    왜 원문인가. 토큰 분석은 셸이 하는 일을 다시 구현해야 하고, 4회차 리뷰가
    그것을 세 방향에서 뚫었다 — 전부 실행까지 재현됐다:

        cp evil "$(pwd)/.claude/harness/bin/harness" && <래퍼> status
        cp evil $'.claude/harness/bin/harness'
        python3 -c "open('.claude/harness/harness.db','w')"

    셋 다 토큰으로는 경로가 아니다. 명령 치환·ANSI-C 인용·인터프리터 인라인
    코드를 `sh_expand` 가 펼치지 않기 때문이다. 그런데 **문자열에는 그대로
    들어 있다.** 확장을 하나씩 구현하는 길은 목록을 늘리는 길이고, 그 목록에는
    항상 다음 항목이 남는다.

    막지 않는다 — 하네스에 이미 있는 어휘로 **묻는다**. `bash_opaque` 와 같은
    정책이고 이유도 같다: 모르면 통과가 아니라 물음이다. 바닥값을 이름으로
    부르는 명령은 드물다. 드문 것을 사람이 한 번 보는 비용은 작다.

    **실행은 언급이 아니다.** 래퍼를 부르는 것(`<래퍼> status`)과 엔진을 돌리는
    것(`python3 <엔진> status`)은 정상 동작이라 세지 않는다.
    """
    for seg in BASH_SPLIT.split(cmd):
        # 읽기는 막지 않는다 — `cat <DB>` 까지 물으면 마찰이고, 마찰은 게이트를 끈다.
        # **엔진 기본값만** 면제한다. `readers` 설정으로는 이 면제를 넓힐 수 없다:
        # 그 길을 열면 `readers: ["rsync"]` 한 줄로 바닥값이 다시 열린다(4회차 B#1).
        # 리다이렉트가 붙으면 읽기가 아니다 (`cat x > <래퍼>`).
        toks = re.findall(r"\S+", seg)
        i = 0
        while i < len(toks) and ASSIGN_RE.match(toks[i]):
            i += 1
        if (i < len(toks) and ">" not in seg
                and os.path.basename(toks[i].strip("\"'")) in BASH_READERS_DEFAULT):
            continue
        for m in FLOOR_RE.finditer(seg):
            if not _invocation(seg, m.end()):
                return m.group(0)
    return None


def floor_verdict(cfg, root, cmd):
    """Bash 명령이 바닥값을 건드리나. `(판정, 근거)` 또는 `(None, None)`.

    **바닥값에 대한 답은 하나여야 한다.** 처음에는 원문 감시(`floor_named`)를
    경로 분석(`bash_protected_hit`) **옆에** 붙였다. 그러면 같은 질문에 판정기가
    둘이고, 둘 중 하나만 게이트의 `entry` 에 들어가 나머지는 자기증명 밖에 남는다
    — ①에서 고친 바로 그 모양을 내가 다시 만든 것이다.

    두 판정은 **확신의 차이**이지 다른 질문이 아니다:
      · 경로를 특정했다  → `deny`  (대상이 분명하다)
      · 원문에만 보인다  → `ask`   (셸 치환·인용·인라인 코드로 실행 시점에 정해진다)
    하나로 묶으면 호출자도 하나, 탐침도 하나다.
    """
    hit = bash_protected_hit(cfg, root, cmd)
    if hit:
        return "deny", hit
    named = floor_named(cfg, cmd)
    return ("ask", named) if named else (None, None)


def bash_opaque(cmd):
    """대상을 특정할 수 없는 파괴인가. 그 이유 또는 None."""
    for rex, why in BASH_OPAQUE:
        if rex.search(cmd):
            return t(why)
    return None


def bash_protected_hit(cfg, root, cmd):
    """Bash 명령이 보호 경로를 **대상으로** 삼는지. 실행하는 것은 대상이 아니다.

    check_write 는 Write/Edit 만 본다. Bash 는 `rm`, `sed -i`, 리다이렉트,
    `sqlite3 ... UPDATE` 로 같은 파일을 바꿀 수 있었고 그 경로는 검사되지 않았다.
    """
    # 바닥값 ∪ 설정. 설정을 비워도(`protected_paths: []`) 바닥값은 남는다 —
    # 예전에는 여기서 `if not pats: return None` 으로 통째로 꺼졌다.
    pats = protected_pats(cfg)
    floor = set(SELF_LOCK)

    mutating = bool(bash_mutator_re(cfg).search(cmd))
    # `>|경로` 의 `|` 를 BASH_SPLIT 이 파이프로 보고 쪼개면 경로가 다음 세그먼트의
    # **명령어 자리**로 밀려 '실행 대상' 으로 건너뛰어진다. 먼저 떼어놓는다.
    cmd = cmd.replace(">|", "> ")

    def candidates(tok):
        """`of=경로`, `>|경로` 처럼 붙어 오는 형태까지 경로로 본다.

        둘 다 실제로 통과했다: `dd if=/dev/null of=<db>`, `printf x >|<LEARNED>`.
        """
        out = []
        for raw in sh_expand(root, tok):      # 셸이 펼치는 것을 우리도 편다
            out.append(raw.lstrip("<>|&"))
            if "=" in raw:
                out.append(raw.split("=", 1)[1].lstrip("<>|&"))
        return [it for it in out if it]

    def protected(tok):
        use = pats
        for cand in candidates(tok):
            # 문자열 그대로와 symlink 를 푼 것 **둘 다** 본다 — 별칭 경로로 바닥값을
            # 건드리는 것을 막는다. 이유는 `rel_aliases` 에 적었다.
            for rel in rel_aliases(root, cand) or []:
                # 리포 루트(`.`)는 바닥값을 **담고 있다.** 예전에는 여기서 건너뛰었고,
                # 그래서 `find . -name harness.db -delete` 가 바닥값을 지나갔다.
                # 담고 있는 것에 대한 판정은 아래 containment 검사로 내려보낸다.
                if rel == ".":
                    continue
                if any(glob_match(rel, p) for p in use):
                    return rel
                # 바닥값은 대소문자를 무시하고도 본다 (macOS 에서 BIN == bin 이다).
                if self_lock_hit(rel):
                    return rel
                # 보호 경로를 **담고 있는** 디렉터리도 변경 명령의 대상이 될 수 없다.
                # `find .claude/harness -delete` 나 `rm -rf .claude` 가 그 경우다.
                # 바닥값에 대해서는 mutating 판정을 믿지 않는다 — `mutator_pattern` 을
                # `(?!)` 같은 '문법은 맞고 아무것도 안 맞는' 정규식으로 두면 이 검사가
                # 통째로 꺼졌다. 설정으로 잠금을 푸는 방향은 막는다.
                low = rel.lower()
                if any(p.lower().startswith(low + "/") for p in floor):
                    return rel
                if mutating and any(p.startswith(rel + "/") for p in use):
                    return rel
        return None

    for seg in BASH_SPLIT.split(cmd):
        toks = re.findall(r"\S+", seg)
        if not toks:
            continue
        # 앞의 `VAR=값` 은 명령이 아니라 대입이다. **값 안의 경로를 검사하고** 넘긴다.
        # 예전에는 버리기만 해서 `DB=<보호경로>; rm $DB` 가 그대로 지나갔다 — 주석은
        # "검사 대상으로 남긴다" 였는데 코드가 달랐다.
        while toks and ASSIGN_RE.match(toks[0]):
            hit = protected(toks[0])
            if hit:
                return hit
            toks = toks[1:]
        if not toks:
            continue
        head = os.path.basename(toks[0].strip("\"'"))
        # 리다이렉트가 있으면 읽기 명령도 쓰기가 된다 (`cat x > 엔진`).
        # `readers` 에 `rm` 을 넣어 잠금을 우회한 것을 확인했으므로, 읽기로 분류된
        # 세그먼트도 **바닥값만은** 검사한다.
        # `readers` 에 `rm` 을 넣어 잠금을 우회한 것을 확인했다. 그래서 **변경 명령
        # 이름은 읽기로 선언될 수 없다**(NEVER_READERS). 그 위장만 막으면 되고,
        # 읽기는 원래대로 통째로 건너뛴다 — `cat` 으로 DB를 읽는 것까지 막으면
        # 과잉 차단이고, 마찰은 게이트를 끄게 만든다.
        if benign_head(cfg, "readers", head, BASH_READERS_DEFAULT) and ">" not in seg:
            continue
        # 인터프리터도 같은 검사를 받는다. `interpreters: ["rm"]` 로 다음 인자를
        # '실행 대상'으로 건너뛰게 만들면 설정만으로 잠금이 풀렸다 — 5차 리뷰.
        skip = 2 if benign_head(cfg, "interpreters", head,
                                BASH_INTERPRETERS_DEFAULT) and len(toks) > 1 else 1
        for tok in toks[skip:]:
            hit = protected(tok)
            if hit:
                return hit
    return None


# ------------------------------------------------- Bash 가 무엇을 쓰는가

# ## 왜 필요한가
#
# 쓰기 판정은 `check_write` 하나인데 **Write·Edit 만 그 문을 지났다.** Bash 는 바닥값만
# 받았고, 단계별 쓰기 규칙 일곱 중 여섯은 Bash 에 없었다 — `sed -i` 한 줄로 "단계마다
# 쓸 수 있는 곳이 다르다"는 약속이 통째로 우회됐다.
#
# ## 구조: 판정은 하나, 수집만 둘
#
# 갈리는 것은 **무엇을 모으는가**여야 하고, **어떤 판정을 적용하는가**는 갈려서는 안 된다.
#
#   바닥값   — 걸리면 대가가 전부다. 토큰을 하나도 빼지 않는다 (`bash_protected_hit`).
#   단계 규칙 — 과잉 차단은 마찰이고 마찰은 게이트를 끈다. 변경 세그먼트만 본다 (여기).
#
# 명령별 표를 두지 않는다. 무엇이 변경인지는 이미 `bash.mutator_pattern` 이 알고,
# 무엇이 읽기인지는 이미 `bash.readers` 가 안다. 새 어휘를 만들면 그 표가 현실과
# 어긋나는 자리가 하나 더 생긴다.

REDIRECT_RE = re.compile(r"^\d*(>>?\|?|&>>?)$")
# 이 명령들은 **마지막 경로만** 바꾼다. `cp src/a.py /tmp/b` 가 src 를 쓴다고 보면
# 읽기만 하는 명령이 거부되고, 그 오판이 곧 마찰이다. `sed`·`perl` 도 여기 있다 —
# 앞 인자는 파일이 아니라 식(`s/x/y/`)이다. `mv` 는 없다: 원본도 사라진다.
BASH_TARGET_LAST = ("cp", "ln", "install", "sed", "perl")


def sh_expand(root, tok):
    """셸이 실행 시점에 펼칠 것을 **우리도 펼친다.** 결과들(없으면 원래 토큰).

    glob 문자가 있는 토큰을 "해석할 수 없다" 며 통과시켰다. 그 한 줄이 실패 개방이었다 —
    `cp evil .claude/harness/bi?/harness` 한 글자로 바닥값·격리·래퍼 무결성이 전부
    뚫렸고, 사전 승인된 래퍼가 남의 코드로 바뀌어 실행되는 것까지 재현됐다.

    모르면 통과가 아니다. 셸과 **같은 확장**을 해서 결과를 본다. 아무것도 안 맞으면
    셸도 리터럴을 그대로 넘기므로 원래 토큰을 돌려준다.
    """
    tok = tok.strip("\"'")
    if not tok:
        return []
    if not any(ch in tok for ch in "*?["):
        return [tok]
    base = tok if os.path.isabs(tok) else os.path.join(root, tok)
    try:
        hits = sorted(globlib.glob(base))
    except OSError:
        hits = []
    return hits or [tok]


def _target(root, tok, sure):
    """이 토큰이 가리키는 리포 안 경로. 아니면 None.

    `sure` 는 리다이렉트 피연산자처럼 **문법이 경로임을 증명한** 자리다.
    나머지는 추측이므로, 있는 파일이거나 `/` 를 포함한 자리만 경로로 본다 —
    `chmod 755 f` 의 `755`, `sed -i s/a/b/ f` 의 `s/a/b/` 를 거르기 위해서다.
    """
    for one in sh_expand(root, tok):
        for rel in rel_aliases(root, one):
            if rel == ".":
                continue
            if sure or os.path.lexists(os.path.join(root, rel)):
                return rel
            # 없는 파일은 **추측**이다. `/` 하나로 경로라고 보면 URL 과 도커 태그가
            # 쓰기 대상이 된다 — `curl … > /tmp/x.tgz` 가 "신규 최상위 폴더
            # 'https:/'" 로 거부됐고, `docker build -t myorg/app:1.0 . > log` 도
            # 같았다(4회차 B#13). 메시지가 엉뚱하면 사용자는 고장으로 보고 게이트를
            # 끈다. **콜론이 든 자리는 경로 문법이 아니다** — 이름 목록이 아니라
            # 문법이라 늘려야 할 다음 항목이 없다.
            if "/" in rel and not any(":" in part for part in rel.split("/")):
                return rel
    return None


def bash_writes(cfg, root, cmd):
    """이 명령의 **변경 세그먼트**가 대상으로 삼는 경로들 (순서 보존).

    확실하지 않은 것은 넣지 않는다. 여기서 빠진 것을 바닥값이 놓치지는 않는다.
    """
    out, mut = [], bash_mutator_re(cfg)

    def add(rel):
        if rel and rel not in out:
            out.append(rel)

    for seg in BASH_SPLIT.split(cmd.replace(">|", "> ")):
        toks = sh_tokens(seg)
        # **정규화해서 넘긴다.** `mutator_pattern` 은 명령 **전체**에 대해 정의됐고
        # `(^|[;&|]\s*)` 로 앵커돼 있다. BASH_SPLIT 은 구분자를 지우므로 세그먼트 앞에
        # 공백이 남고, 그러면 `^` 가 안 맞아 `a && touch x` 의 touch 가 통째로 샜다.
        # 어휘를 재사용하는 것은 옳았지만 **정의된 형태로 주어야** 한다.
        if not toks or not mut.search(seg.strip()):
            continue
        head = os.path.basename(toks[0].strip("\"'"))
        # 읽기 명령에 리다이렉트가 붙은 것(`cat a > b`)은 **b 만** 쓴다.
        reads_only = benign_head(cfg, "readers", head, BASH_READERS_DEFAULT)
        args, k = [], 1
        while k < len(toks):
            tok = toks[k]
            if REDIRECT_RE.match(tok) and k + 1 < len(toks):
                add(_target(root, toks[k + 1], True))   # 문법이 경로임을 증명한다
                k += 2
                continue
            if tok in ("<", "<<", "<<<"):
                k += 2                                  # 입력은 읽는다
                continue
            if tok.startswith("of="):                   # dd
                add(_target(root, tok[3:], True))
            elif "=" not in tok.split("/", 1)[0] \
                    and not tok.startswith(("-", "<", ">", "&")):
                args.append(tok)          # `k=v` 는 옵션이지 경로가 아니다
            k += 1
        if reads_only:
            args = []
        elif head in BASH_TARGET_LAST:
            args = args[-1:]
        for tok in args:
            add(_target(root, tok, False))
    return out


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


def auto_skip_state(con):
    """(활성, 만료사유) — 범위·횟수 만료까지 반영한 실제 상태."""
    if get_meta(con, "auto_skip") != "on":
        return False, None
    scope = get_meta(con, "auto_skip_loop")
    if scope and scope != head_loop(con):
        return False, t("작업 %s 범위였고 작업이 바뀌어 만료됐다") % scope
    uses = get_meta(con, "auto_skip_uses")
    if uses:
        try:
            if int(uses) <= 0:
                return False, t("사용 횟수를 모두 소진해 만료됐다")
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
    """자동 승인 1회 소진. (차지했나, 남은 횟수) — 무제한이면 (True, None).

    플래그는 사용자의 의도를 담고 실효 상태는 `auto_skip_state` 가 계산한다.
    여기서 'off' 로 뒤집으면 "왜 꺼졌는지"를 잃는다.
    """
    if auto_skip_uses_left(con) is None:
        return True, None            # 무제한 — 차지할 것이 없다
    won = claim(con, "UPDATE meta SET v = CAST(CAST(v AS INTEGER) - 1 AS TEXT) "
                     "WHERE k='auto_skip_uses' AND CAST(v AS INTEGER) > 0", ())
    return won, auto_skip_uses_left(con)


def auto_skip_scope_note(con):
    bits = []
    scope = get_meta(con, "auto_skip_loop")
    if scope:
        bits.append(t("작업 %s 범위") % scope)
    left = auto_skip_uses_left(con)
    if left is not None:
        bits.append(t("남은 %d회") % left)
    return ", ".join(bits) or t("무제한")


def skip_block_reason(cfg, sid, target, con=None, root=None, lid=None):
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
            return t("잘못된 형식: %s") % target
    elif target.startswith("until:"):
        want = target.split(":", 1)[1]
        if want not in ids:
            return t("알 수 없는 단계: %s") % want
        dest = ids.index(want) - 1
    elif target in ids:
        dest = ids.index(target)
    else:
        return t("알 수 없는 대상: %s") % target

    if dest < cur:
        return (t("뒤로 갈 수는 없다 — 이미 %s 단계이거나 그보다 뒤다. "
                "단계는 항상 앞으로만 간다.") % label_of(cfg, sid))

    locked = [ids[i] for i in range(cur, dest + 1)
              if cfg["stages"][i].get("skippable") is False]
    if not locked:
        # 여기까지 왔으면 이동 자체는 가능하다. 남은 것은 **CLI 가 실제로 요구하는
        # 기록**이다. 훅이 이것을 모르면 사용자가 승인한 **뒤에** CLI 가 거부하고,
        # 모델은 안내받은 명령을 다시 시도해 다이얼로그가 무한 반복된다 — 이 함수가
        # 존재하는 이유가 바로 그것인데 정작 이 축을 안 보고 있었다.
        if con is not None:
            for i in range(cur, dest + 1):
                for key in cfg["stages"][i].get("skip_requires") or []:
                    if not criterion_met(con, cfg, root, lid, key):
                        return (t("%s 를 건너뛰더라도 기록은 남겨야 한다: %s "
                                  "먼저 그 기록을 남긴 뒤 다시 시도하라 — 승인만 "
                                  "면제된다.")
                                % (cfg["stages"][i]["label"],
                                   criterion_why(con, cfg, root, lid, key)))
        return None

    names = ", ".join(stage_obj(cfg, x)["label"] for x in locked)
    if ids[cur] == ids[0]:
        # 여기서 예전에는 `skip until:selection` 을 안내했다. 그건 dest 가 -1 이 되어
        # **항상 실패하는 명령**이고, 모델이 그대로 반복해 승인 요청이 무한히 떴다.
        return (t("%s 단계는 건너뛸 수 없다 — 작업을 정하지 않고 넘어가면 이후 모든 단계가 "
                "기준 없이 돌아간다. 스킵이 아니라 **작업을 고르는 것**이 다음 행동이다: "
                "`harness status` 가 하네스가 아는 할 일을 후보로 보여준다(승격 결정, "
                "재발한 승격, 낡은 인덱스, 예산 소진). 고르면 "
                "`harness loop intent \"...\"` 와 `harness loop done-when \"...\"` 로 "
                "기록하고 진행하라. 그 후보들까지 정말 필요 없으면 그렇다고 말하고 "
                "멈춰라 — 그건 교착이 아니라 정상 종료다.") % names)
    if cur >= len(ids) - 1:
        # 마지막 단계에서 `until:<last>` 는 dest = index-1 이라 **항상 실패한다.**
        # Selection 쪽에서 고친 것과 같은 막다른 길이 반대편 끝에 남아 있었다.
        return (t("%s 단계는 건너뛸 수 없다 — 여기가 마지막이므로 건너뛸 곳이 없다. "
                  "이 회차를 닫는 것이 다음 행동이다: 중단 사유를 회고로 남기고 "
                  "`harness advance --cycle` (후속 회차) 또는 `harness advance --done` "
                  "(작업 종료) 을 실행하라.") % names)
    return (t("%s 단계는 건너뛸 수 없다. 이 회차를 중단하려면 "
            "`harness skip until:%s --reason \"...\"` 로 %s 까지 이동한 뒤, 중단 사유를 "
            "회고로 남기고 `harness advance --cycle` (또는 `--done`) 으로 닫아라.")
            % (names, last, stage_obj(cfg, last)["label"]))


PLAN_PREVIEW_LINES = 24
PLAN_PREVIEW_CHARS = 1400


def plan_preview(root, cmd):
    """승인 다이얼로그에 실을 계획 본문.

    계획 승인은 이 하네스에서 사람이 방해받는 유일하게 값있는 자리인데, 다이얼로그가
    **파일 이름만** 보여주고 있었다. 읽지 않고 찍는 도장은 마찰만 있고 정보가 없다 —
    그렇게 남은 `plan_approved` 기록은 가짜다. 무엇을 승인하는지 보여준다.
    """
    pos = [it for it in sh_tokens(cmd) if not it.startswith("--")]
    path = None
    for i, it in enumerate(pos):
        if it == "approve-plan" and i + 1 < len(pos):
            path = pos[i + 1].strip("\"'")
            break
    if not path:
        return t("계획 파일 경로가 없다. `approve-plan <파일>` 형식으로 지정하라.")
    full = path if os.path.isabs(path) else os.path.join(root, path)
    if not os.path.isfile(full):
        return t("⚠ 계획 파일이 없다: %s — 승인하기 전에 확인하라.") % path
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            body = fh.read(PLAN_PREVIEW_CHARS * 2)
    except OSError as exc:
        return t("⚠ 계획 파일을 읽을 수 없다 (%s): %s") % (path, exc)
    lines = body.splitlines()
    shown = lines[:PLAN_PREVIEW_LINES]
    text = "\n".join(shown)[:PLAN_PREVIEW_CHARS]
    more = []
    if len(lines) > len(shown):
        more.append(t("이하 %d줄 생략") % (len(lines) - len(shown)))
    if len(text) < len("\n".join(shown)):
        more.append(t("길이 잘림"))
    tail = t(" (%s — 전문은 %s)") % (", ".join(more), path) if more else ""
    return "%s%s\n%s" % (path, tail, text)


# 하네스 호출을 찾는다. 경로 앞머리에 `$`·`~`·`{`·`}` 도 온다 (`$PWD/…/harness`).
CTRL_CALL_RE = re.compile(r"(?:^|[\s=;&|(])(?:[\w./\\$~{}-]*[/\\])?"
                          r"(?:harness\.py|harness)(?=\s|$)([^;&|\n]*)")


def ctrl_requests(cmd):
    """제어 호출 전부 → `(sub, pos, direct, seg)`.

      sub    하위 명령. `argv_positional` 과 **같은 규칙**으로 뽑는다.
      pos    위치 인자 전체. 판정하는 쪽이 다시 파싱하지 않게 함께 준다.
      direct 세그먼트의 첫 토큰이 하네스인가. 아니면 무엇이 실행될지 확신할 수 없다.

    ## 세 번 틀린 자리다

      따옴표 제거   실행 경로가 사라져 게이트가 안 걸렸다
      shlex 토큰    `sh -c '… harness auto-skip on'` 이 한 토큰이라 안 걸렸다
      원문 정규식   `sk''ip`·`$(echo skip)` 을 못 읽고, 반대로 커밋 메시지의 `harness`
                    까지 제어 명령으로 봐서 그 뒤 검사가 통째로 꺼졌다

    셋의 공통점은 **모르면 통과**였다. 이제 모르면 묻는다 — 하네스를 부르는 것 같은데
    하위 명령을 못 읽으면 그 사실 자체가 판정이다(`sub` 이 아는 이름이 아니면 호출자가
    `ask` 를 낸다). 따옴표는 **지워서** 본다: 셸이 `sk''ip` 를 `skip` 으로 붙여 주므로
    우리도 붙여야 같은 것을 본다.
    """
    out = []
    for seg in BASH_SPLIT.split(cmd):
        bare = seg.replace('"', "").replace("'", "")
        head = (bare.split() or [""])[0]
        for tail in CTRL_CALL_RE.findall(bare):
            pos, skip = [], False
            for a in tail.split():
                if skip:
                    skip = False
                    continue
                if a.startswith("--"):
                    skip = "=" not in a       # `--flag value` 는 값도 건너뛴다
                    continue
                pos.append(a)
            if not pos:
                continue
            name = pos[0]
            if len(pos) > 1 and pos[1] in CTRL_SUB2.get(name, ()):
                name = "%s %s" % (name, pos[1])
            direct = os.path.basename(head) in CTRL_NAMES or head.endswith(
                tuple("/" + n for n in CTRL_NAMES))
            out.append((name, pos, direct, seg))
    return out


def ctrl_known(sub):
    return sub in set(CLI) | {"%s %s" % (k, v)
                              for k, vs in CTRL_SUB2.items() for v in vs}


def ctrl_decision(con, cfg, root, sub, pos, direct, cmd, mode, lid, sid):
    """제어 명령에 대한 판정을 **돌려준다**(emit 하지 않는다).

    한 Bash 명령에 제어 호출이 여러 개 있을 수 있어서, 호출자가 전부 순회하며
    동의가 필요한 것을 찾아야 한다. 이전에는 첫 매치만 보고 즉시 emit 해서
    `harness status; harness loop new` 가 통째로 통과했다.
    """
    if sub == "auto-skip":
        # off 는 게이트 복원이므로 동의 없이 허용한다. on 은 게이트를 무력화하므로
        # 반드시 사람의 동의를 받는다 — 그러지 않으면 모델이 스스로 켤 수 있다.
        # 원문을 다시 뒤지지 않는다. `auto-skip --reason x on` 과 `auto-skip "on"` 이
        # CLI 에서는 on 인데 이 정규식에서는 아니어서 게이트가 통째로 사라졌다.
        if "on" not in pos[1:]:
            return
    elif sub not in consent_map(cfg):
        return

    if sub == "skip":
        # 불가능한 스킵은 **묻지 않고** 거부한다. 승인을 받아봐야 거부되고,
        # 그러면 모델이 다시 시도해 다이얼로그만 반복된다.
        # 훅이 `--reason` 의 **값**을 위치 인자로 세는 바람에 CLI 는 실행하는 명령을
        # 훅이 "알 수 없는 대상: x" 로 막았다. 같은 pos 를 쓴다.
        tgt = pos[1] if len(pos) > 1 else None
        if tgt:
            why = skip_block_reason(cfg, sid, tgt, con, root, lid)
            if why:
                record_event(con, lid, sid, "block", "skip_impossible", tgt, why)
                return pre_decision("deny", why)

    reason = raw_flag(cmd, "reason")
    if not direct:
        # 하네스를 부르는 것 같지만 세그먼트의 머리가 아니다 — 무엇이 실행될지
        # 확신할 수 없다. **거부하지 않고 묻는다.** 여기서 거부하면 커밋 메시지에
        # `harness skip` 을 쓴 것만으로 빠져나갈 길이 없어진다(막다른 길).
        return pre_decision("ask", t("이 명령 안에 하네스 제어 호출(%s)이 보인다. "
                                     "실제로 실행되는지 하네스가 확신할 수 없어 사람에게 "
                                     "묻는다: `%s`") % (sub, cmd.strip()[:200]))
    if sub != "approve-plan" and not reason:
        record_event(con, lid, sid, "block", "no_reason", sub, cmd[:200])
        return pre_decision("deny",
            t("사유 없이 %s 할 수 없다. --reason \"...\" 로 사유를 명시하라.") % sub)

    if sub == "skip" and auto_skip_on(con):
        # 자동 승인이 켜져 있다. 다이얼로그는 생략하되 사실은 사용자에게 노출한다.
        out = pre_decision("defer", None)
        out["systemMessage"] = (t("harness: 단계 스킵을 자동 승인했다 (사유: %s · %s). "
                                "끄려면 `harness auto-skip off`.")
                                % (reason, auto_skip_scope_note(con)))
        return out

    detail = "%s: `%s`" % (consent_map(cfg).get(sub, sub + t(" 요청")), cmd.strip())
    if reason:
        detail += t("\n사유: %s") % reason
    if sub == "approve-plan":
        # 무엇을 승인하는지 보여준다. 이름만 보고 찍는 승인은 기록으로도 가짜다.
        detail += t("\n\n─── 계획 ───\n%s\n───────────") % plan_preview(root, cmd)
    detail += t("\n승인하면 하네스 상태에 기록된다.")
    if mode == "bypassPermissions" and sub == "auto-skip":
        # 하나의 예외. `auto-skip on` 의 효과는 **세션을 넘어 지속된다**(meta 에 저장되고
        # scope 를 project 로 두면 이후 세션에도 남는다). 세션 단위 사전 승인으로
        # 세션을 넘는 결정을 덮을 수는 없다. 이건 진짜 사람의 판단이 필요하다.
        record_event(con, lid, sid, "block", "bypass_mode", sub, cmd[:200])
        return pre_decision("deny", detail +
            t("\nbypassPermissions 는 이 세션의 사전 승인이지만 `auto-skip on` 은 효과가 "
            "세션을 넘어 남는다. 그래서 이것만은 거부한다 — 권한 모드를 낮추고 사람의 "
            "판단을 받아라. 이 세션의 스킵은 이미 사전 승인으로 통과한다."))

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
            t("harness: %s 을 bypassPermissions 사전 승인으로 통과시켰다%s. "
            "기록은 남는다 — `harness stats` 의 '게이트 우회'.")
            % (sub, t(" (사유: %s)") % reason if reason else ""))
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
        decision, hit = floor_verdict(cfg, root, cmd)
        if decision == "deny":
            with con:
                record_event(con, lid, sid, "block", "protected_bash", hit, cmd[:200])
            return emit(pre_decision("deny", (
                t("하네스 자신(%s)은 Bash 로도 변경할 수 없다. 규칙을 바꾸려면 "
                "`.claude/harness/stages.json` 을, 엔진을 바꾸려면 플러그인을 "
                "수정하라. 내용을 보려면 Read 도구를 쓰라. "
                "이 차단은 `allow` 로도 열리지 않는다.") % hit)))
        if decision == "ask":
            with con:
                record_event(con, lid, sid, "ask", "floor_named", hit, cmd[:200])
            return emit(pre_decision("ask", t(
                "이 명령의 원문에 하네스 바닥값(%s)이 보이는데, 무엇을 대상으로 "
                "삼는지 하네스가 특정하지 못했다. 셸 치환·인용·인터프리터 인라인 "
                "코드가 섞이면 실행 시점에야 정해진다. 바닥값은 엔진과 상태이고 "
                "사전 승인된 래퍼를 포함하므로 사람이 봐야 한다. 경로를 그대로 "
                "적으면 하네스가 대신 판정한다.") % hit))

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
        why = bash_opaque(cmd)
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
        if field:
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


def verification_hit(cfg, cmd):
    """이 명령이 **검증을 실행하나.** 문자열 어딘가에 이름이 있는 것과 다르다.

    `bash_pattern` 을 명령 전체에 `re.search` 했더니 아래가 전부 증거로 적립됐다.

        echo "npm test"        git commit -m "ran npm test"        cat tsc.log

    패턴이 아니라 **어디에 대고 맞추는가**가 문제였다. 세그먼트로 쪼개고, 따옴표 안은
    데이터이므로 지우고, 읽기 명령(`bash.readers`)은 아무것도 실행하지 않으므로 건너뛴다.
    `sh -c "npm test"` 도 이제 증거가 아니다 — 게이트가 잘못 열리는 것보다 `harness
    verify` 를 한 번 더 치는 편이 낫다.
    """
    pat = cfg.at("criteria.verification_evidence.bash_pattern")
    if not pat:
        return False
    try:
        vre = re.compile(pat)
    except re.error:
        return False
    readers = tuple(cfg.seq("bash.readers", BASH_READERS_DEFAULT)) + ("echo", "printf")
    for seg in BASH_SPLIT.split(cmd):
        bare = QUOTED_RE.sub(" ", seg).strip()
        if not bare:
            continue
        if os.path.basename(bare.split()[0].strip("\"'")) in readers:
            continue
        # **머리에서부터** 맞아야 한다. `search` 였을 때 `true npm test` 가 통과했다 —
        # 실행되는 프로그램은 `true` 인데 인정된 것은 `npm test` 였다. `# npm test`,
        # `false npm test`, `sleep 0 npm test` 도 같았다. 무해한 머리 이름을 목록으로
        # 모으는 것은 끝이 없다(`time`·`env`·`nice` 는 정상이다). 대신 패턴이
        # **프로그램 자리**를 가리키게 한다 — 그것이 패턴의 원래 뜻이다.
        if vre.match(bare):
            return True
    return False


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

    blocked, exhausted = [], []
    with con:
        for key, text in problems:
            # 세고-넣으면 동시에 뜬 Stop 훅 넷이 상한 1 을 넷 다 쓴다. 세는 것을
            # INSERT 의 WHERE 안으로 옮겨 rowcount 가 승자를 정하게 한다.
            if not claim(con, "INSERT INTO stop_block(prompt_id,key,at) "
                              "SELECT ?,?,? WHERE (SELECT COUNT(*) FROM stop_block "
                              "WHERE prompt_id=? AND key=?) < ?",
                         (prompt_id, key, now(), prompt_id, key,
                          int(limits.get(key, 1)))):
                exhausted.append(key)
                continue
            record_event(con, lid, sid, "stop_gate", key, stage["id"], text)
            blocked.append(text)
        for key in exhausted:
            record_event(con, lid, sid, "bypass", key, stage["id"],
                         t("차단 상한 소진으로 미충족 상태 종료"))

    if blocked:
        emit({"decision": "block", "reason": " / ".join(blocked)})
    elif exhausted:
        # 조용히 통과시키지 않는다 — 우회 사실을 사용자에게 노출한다
        emit({"systemMessage": t("harness: %s 단계를 미충족 상태로 종료했다 (%s). "
                               "차단 상한 소진.") % (stage["label"], ", ".join(exhausted))})


# 하네스가 **스스로** 남기는 기록은 진전이 아니다. 이걸 빼지 않으면 이어붙임
# 이벤트가 이벤트 수를 늘려 지문이 매번 바뀌고, 진전 감지가 자기 자신을 진전으로
# 세면서 영원히 발동하지 않는다 — 실제로 그렇게 만들어서 5회 헛돌았다.
FP_IGNORE_KINDS = ("stop_continue", "stop_stalled", "stop_gate", "bypass",
                   "cycle_close", "cycle_adopt")


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
                note = (t(" 다만 하네스가 아는 할 일이 %d개 있다 — `harness status` 로 "
                        "확인하고 고를 수 있다.") % n)
        return emit({"systemMessage":
                     t("harness: %s 단계에서 사람의 입력을 기다린다 (%s). 턴을 끝낸다.%s")
                     % (stage["label"], ", ".join(waiting), note)})
    limit = cfg.num("stop_continue.max_per_prompt", 6, low=1)
    no_prog = cfg.num("stop_continue.no_progress_limit", 2, low=1)
    fp = progress_fingerprint(con, lid, sid)
    stalled, used = stalled_rounds(con, lid, prompt_id, fp)

    if stalled >= no_prog:
        # 조용히 놓아주지 않는다. 헛돈 사실이 기록되고 사용자에게 보인다.
        with con:
            record_event(con, lid, sid, "stop_stalled", stage["id"], prompt_id,
                         t("지문 %s 가 %d회 연속 그대로 — 이어붙이기를 멈춘다") % (fp, stalled))
        return emit({"systemMessage": (
            t("harness: %d회 이어붙였으나 진전이 없어 멈춘다 (%s 단계). 같은 지시를 "
            "반복하는 대신 무엇이 막고 있는지 사람에게 물어라.") % (used, stage["label"]))})
    if used >= limit:
        with con:
            record_event(con, lid, sid, "bypass", "continue_limit", stage["id"],
                         t("이어붙임 상한 %d 소진") % limit)
        return emit({"systemMessage":
                     t("harness: 이어붙임 상한 %d회를 소진해 턴을 끝낸다 (%s 단계).")
                     % (limit, stage["label"])})

    with con:
        record_event(con, lid, sid, "stop_continue", str(used + 1), prompt_id, fp)
    missing = exit_blockers(con, cfg, root, lid, sid)
    todo = (t("이 단계의 남은 종료 조건: %s") % ", ".join(missing) if missing
            else t("이 단계의 종료 조건은 채웠다 — `harness advance` 로 넘어가라"))
    return emit({"decision": "block", "reason": (
        t("작업이 아직 끝나지 않았다 (현재 %s, 남은 단계 %d). 멈추지 말고 이어서 진행하라. "
        "%s. 작업이 정말 끝났으면 Compounding 에서 `harness advance --done` 으로 닫아라. "
        "(이어붙임 %d/%d)") % (stage["label"], left, todo, used + 1, limit))})


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
    return "python3 %s init" % os.path.abspath(__file__)


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


def _write_if_changed(path, body, mode=None):
    """True=썼다, False=이미 같다, **None=쓰지 못했다.**

    셋을 둘로 뭉개면 실패가 "바꿀 것이 없었다" 와 구분되지 않는다. 읽기 전용
    파일시스템에서 승격이 LEARNED.md 반영에 실패했는데도 성공으로 보고됐다.
    """
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
        return None


def engine_sources(scripts_dir):
    """엔진을 이루는 파이썬 파일 전부 (scripts_dir 기준 상대경로).

    목록을 손으로 적지 않는다 — 구현이 파일로 갈라질 때 새 파일이 사본에서 빠지면
    그 게이트가 조용히 사라진다.
    """
    out = []
    for dirpath, _dirs, names in os.walk(scripts_dir):
        for n in sorted(names):
            if n.endswith(".py"):
                out.append(os.path.relpath(os.path.join(dirpath, n), scripts_dir))
    return sorted(out)


def _copy_engine(src_dir, dst_dir):
    """엔진 파일 전부를 사본으로. **부분 복사를 남기지 않는다.**

    한 파일이라도 못 쓰면 **사본의 파이썬 파일을 전부 지운다.** 반쯤 복사되거나
    낡은 사본은 실행되면 게이트가 빠진 채로 돌고, 그게 곧 게이트 해제다. 없는 편이
    낫다 — 래퍼는 원본(플러그인)으로 떨어지고, 그것이 옳은 엔진이다.
    """
    changed = False
    for rel in engine_sources(src_dir):
        try:
            with open(os.path.join(src_dir, rel), encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            body = None
        r = (_write_if_changed(os.path.join(dst_dir, rel), body, 0o644)
             if body is not None else None)
        if r is None:                      # 못 읽었거나 못 썼다
            _purge_engine_copy(dst_dir)
            return None
        changed = changed or bool(r)
    # **사본은 원본과 같아야 한다.** 원본에 없는데 사본에 있는 것은 지운다.
    #
    # 없었을 때 두 구멍이 있었다(4회차 D-M9): ① `gates/zzz.pyc` 를 심으면
    # `pkgutil` 이 발견해 임포트하는데 복사·정리 어느 쪽에도 안 걸렸다 —
    # 사본 경로로 남의 게이트를 심을 수 있었다. ② 플러그인 업그레이드로 게이트
    # 파일이 없어져도 사본에는 영원히 남아 낡은 게이트가 계속 돌았다.
    keep = {os.path.normpath(r) for r in engine_sources(src_dir)}
    for rel in _importable(dst_dir):
        if os.path.normpath(rel) not in keep:
            with swallow(t("사본 정리(%s)") % rel):
                os.remove(os.path.join(dst_dir, rel))
            changed = True
    return changed


# 파이썬이 **임포트할 수 있는** 것. `.py` 만 보면 `.pyc` 가 남고, `pkgutil` 은
# 그것을 발견한다. 확장자 목록이지만 파이썬의 것이지 우리 어휘가 아니다.
IMPORTABLE = (".py", ".pyc", ".pyo", ".so")


def _importable(dst_dir):
    """사본 안에서 임포트될 수 있는 파일 (dst_dir 기준 상대경로)."""
    out = []
    for dirpath, _dirs, names in os.walk(dst_dir):
        for n in sorted(names):
            if n.endswith(IMPORTABLE):
                out.append(os.path.relpath(os.path.join(dirpath, n), dst_dir))
    return out


def _purge_engine_copy(dst_dir):
    """사본에서 **임포트될 수 있는 것을 전부** 없앤다. 낡은 엔진이 도는 것보다
    없는 것이 낫다 — 래퍼는 원본(플러그인)으로 떨어지고 그것이 옳은 엔진이다."""
    for rel in _importable(dst_dir):
        with swallow(t("사본 삭제(%s)") % rel):
            os.remove(os.path.join(dst_dir, rel))


def refresh_engine(root):
    """엔진 사본을 프로젝트 안에 둔다.

    모델이 실행하는 명령이 작업 디렉터리 밖의 파일을 가리키면 분류기·샌드박스가
    막는다. 사본은 gitignore 되고 세션 시작마다 갱신되므로 버전이 어긋나지 않는다.
    """
    src = os.path.abspath(__file__)
    dst = os.path.join(root, ENGINE_REL)
    if src == os.path.abspath(dst):
        return False  # 사본 자신이 실행 중이면 덮어쓰지 않는다
    changed = _copy_engine(os.path.dirname(src), os.path.dirname(dst))
    # **기본값도 사본과 함께 가야 한다.** 사본이 실행될 때 plugin_root() 는
    # `.claude/harness/` 를 가리키고 거기엔 templates/ 가 없다. 그래서 어휘 기본값을
    # 못 찾고, 훅(플러그인 엔진)과 CLI(사본)의 판정이 갈렸다 — 래퍼로 advance 하면
    # satisfied_by 를 몰라 디스크의 계획 파일을 인정하지 않았다. 재현해 확인했다.
    tdir = os.path.join(plugin_root(), "templates")
    for src_rel, dst_abs in [("stages.json", os.path.join(root, DEFAULTS_REL))] + [
            (n, os.path.join(root, HARNESS_DIR, "bin", n))
            for n in sorted(os.listdir(tdir)) if n.startswith("messages.")
    ] if os.path.isdir(tdir) else []:
        with swallow(t("엔진 사본 갱신")):
            with open(os.path.join(tdir, src_rel), encoding="utf-8") as fh:
                _write_if_changed(dst_abs, fh.read(), 0o644)
    return changed


def refresh_wrapper(root):
    """래퍼를 우리 것으로 맞춘다. 쓰지 못했으면 None (그것도 사실이다)."""
    refresh_engine(root)
    return _write_if_changed(os.path.join(root, WRAPPER_REL),
                             t(WRAPPER) % os.path.abspath(__file__), 0o755)


ENGINE_LINE_RE = re.compile(r'^P="[^"]*"$', re.M)


def wrapper_code(body):
    """래퍼에서 **실행되는 줄만** 남긴다.

    바이트로 비교하면 주석(한국어 설명)이 `language` 설정에 따라 달라져 정상 파일을
    변조로 오판한다. 오판은 마찰이고, 마찰은 게이트를 끄게 만든다. sh 에서 `#` 줄은
    실행되지 않으므로 빼고 본다. 첫 줄(shebang)은 **인터프리터를 정하므로** 남긴다.
    """
    lines = (body or "").splitlines()
    keep = lines[:1] + [ln for ln in lines[1:]
                        if ln.strip() and not ln.strip().startswith("#")]
    return "\n".join(keep)


def wrapper_shape(body):
    """엔진 경로를 지운 래퍼 코드. 플러그인이 업데이트되면 그 경로만 바뀐다."""
    return ENGINE_LINE_RE.sub('P=""', wrapper_code(body))


def wrapper_intact(root):
    """래퍼가 우리가 쓴 그것인가. 아니면 **복구하고** False.

    `SAFE_PERMS` 는 래퍼를 **경로로** 사전 승인한다. 그런데 그 파일은 프로젝트 안에
    있어 모델이 쓸 수 있는 자리다. 경로 검사를 우회하는 길이 하나만 남아도 그 즉시
    승인 없는 임의 코드 실행이 된다. 그래서 **신뢰를 경로가 아니라 내용에 건다** —
    우회가 성공해도 남의 코드는 실행되지 않는다.

    복구까지 하는 이유: 거부만 하면 `harness init` 조차 래퍼를 거쳐 사용자가 갇힌다.
    """
    path = os.path.join(root, WRAPPER_REL)
    want = t(WRAPPER) % os.path.abspath(__file__)
    try:
        with open(path, encoding="utf-8") as fh:
            have = fh.read()
    except Exception:
        have = None
    if have is not None and wrapper_code(have) == wrapper_code(want):
        return True                              # 온전하다
    # **엔진 경로만 다르면 변조가 아니라 낡은 것이다.** 플러그인을 업데이트하면 캐시
    # 디렉터리 이름이 바뀌어 그 줄이 달라진다 — 아무도 손대지 않았는데 보안 경고가
    # 뜨고 `wrapper_tampered` 가 통계를 오염시켰다. 조용히 맞춰 놓는다.
    # 파일이 아예 없는 것은 변조가 아니다 — 새 클론·워크트리에는 원래 없다(gitignore).
    stale = have is None or wrapper_shape(have) == wrapper_shape(want)
    ok = refresh_wrapper(root) is not None
    if not ok:
        return None                              # 복구도 못 했다
    return True if stale else False              # 갱신했다 / 변조를 되돌렸다


def status_report(ctx):
    """현재 상태. 출력하지 않는다 — 테스트가 값을 검사할 수 있어야 한다."""
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    row = loop_row(con, lid)
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
                    "reason": g["reason"]} for g in live_grants(con, lid)],
        "auto_skip": (auto_skip_scope_note(con) if auto_skip_on(con) else None),
        "auto_skip_reason": get_meta(con, "auto_skip_reason", "-"),
        "pending_promotions": [it["key"] for it in pending_promotions(con, cfg)],
        "promoted": promotion_summary(con, cfg),
        "tidy": tidy_headline(con, cfg, root),
        "enforcing": enforcing_summary(cfg),
        "selftest": [{"what": w, "ok": o, "got": g}
                     for w, o, g in selftest(ctx)],
        # 작업이 정해지지 않았을 때만. 정해졌으면 후보는 소음이다.
        "candidates": ([] if (row and row["intent"])
                       else work_candidates(con, cfg, root)),
    }


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


# 종료 조건 쪽 탐침. "이 조건이 자기 산출물이 아닌 것도 받아들이나" 를 본다.
# `write_glob: ["**"]` 로 넓히면 접두사만 맞는 아무 파일이 사람의 승인이 됐다 —
# 개수를 세는 요약으로는 안 보였다(Codex Claim C HIGH).
UNRELATED = ("src/a.py", "README.md", ".claude/settings.json", "Makefile")
# 검증 증거로 인정되면 안 되는 명령. 텍스트를 읽지 않고 넣어 본다.
# 검증이 **아닌** 명령. 5차 리뷰가 뚫은 모양(문자열 안에 이름만 있는 것)을 넣었다 —
# 탐침이 실제로 뚫린 자리를 담고 있어야 자기검사가 거짓말을 하지 않는다.
NOT_VERIFICATION = ("ls", "echo hi", "true", "cat README.md", "git status", "pwd",
                    'echo "npm test"', 'git commit -m "ran npm test"',
                    "cat tsc.log", "grep -rn pytest src/", "ls | grep vitest",
                    # **무해한 머리 + 진짜 테스트 이름.** 이 모양이 없어서 머리 앵커링을
                    # 없애는 뮤테이션이 조용히 통과했다 — 탐침이 막는 것을 증명해야 한다.
                    "true npm test", "sleep 0 pytest", "# npm test", "false npm test")
# 이 도구를 썼다는 사실만으로 검증이 됐다고 볼 수 없다. 읽기·탐색은 검증이 아니다.
# MCP 이름도 넣는다 — 브라우저 목록 조회가 검증으로 세어졌는데 탐침이 그 자리를 못 봤다.
INNOCUOUS_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit", "WebFetch", "TodoWrite",
                   "mcp__claude-in-chrome__list_connected_browsers",
                   "mcp__claude-in-chrome__tabs_close_mcp")
# **인정되어야 하는** 검증 명령. 과다만 재고 과소를 안 재면, 정직하게 테스트를 돌린
# 프로젝트일수록 게이트가 안 열리고 사용자는 스킵으로 빠진다 — 그것이 게이트를 끄는 길이다.
MUST_VERIFY = ("npm test", "pnpm -r test", "yarn workspace app test", "bun test",
               "python -m pytest", "uv run pytest", "poetry run pytest", "tox",
               "npx playwright test", "npx vitest run", "pytest -q", "vitest",
               "go test ./...", "cargo test", "cargo nextest run",
               "mvn -q test", "gradle check", "./gradlew test", "dotnet test",
               "deno test", "swift test", "ctest", "rspec", "bin/rails test",
               "make check", "make test", "npx tsc --noEmit", "ruff check .", "mypy .")


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
REQUIRED_GATES = ("write", "consent", "criteria", "stop", "promotion")
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
        self._orig = {}

    def _count(self, fn):
        def go(*a, **kw):
            self.hits += 1
            return fn(*a, **kw)
        return go

    def __enter__(self):
        for nm in self.names:
            self._orig[nm] = getattr(self.mod, nm)
            setattr(self.mod, nm, self._count(self._orig[nm]))
        del PROBE_EMITS[:]
        self._orig["emit"] = self.mod.emit
        self.mod.emit = PROBE_EMITS.append
        return self

    def __exit__(self, *exc):
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


def render_status(d, cfg):
    # 무시되는 설정이 있으면 **맨 위**에 말한다. 아래에 묻으면 안 읽는다.
    if d.get("config_problems"):
        print(t("⚠ stages.json 에서 무시되는 설정 %d건:") % len(d["config_problems"]))
        for p in d["config_problems"]:
            print("  - %s" % p)
        print()
    # 삼킨 실패도 사실이다. 곁다리 작업이라 판정은 안 막았지만, **아무도 모르는
    # 실패**를 남기지 않는 것이 이 플러그인의 전제다.
    if SWALLOWED:
        print(t("⚠ 곁다리 작업 %d건이 실패했다 (판정은 계속됐다):") % len(SWALLOWED))
        for w in SWALLOWED:
            print("  - %s" % w)
        print()
    st = d.get("selftest") or []
    fails = [x for x in st if not x.get("ok")]
    if fails:
        # **개수보다 이것이 먼저다.** 대표 조작이 기대와 다르게 판정된다는 것은
        # 설정이 어떻게 생겼든 강제가 깨졌다는 직접 증거다.
        print(t("⚠ 자기검사 %d/%d 실패 — 강제가 기대와 다르게 동작한다:")
              % (len(fails), len(st)))
        for x in fails:
            print("  - %s → %s" % (x["what"], x["got"]))
        print()
    e = d.get("enforcing") or {}
    if e:
        # 무엇이 실제로 강제되는지 **숫자로** 보여준다. 0 이 보이면 그게 신호다.
        if st:
            print(t("자기검사: %d/%d 통과 (대표 조작을 실제 판정에 넣어 확인)")
                  % (len(st) - len(fails), len(st)))
        print(t("강제 중: 보호 경로 %d%s · 언어 %s")
              % (e.get("protected_paths", 0),
                 "".join(" · %s %d/%d" % (n, on, tot)
                         for n, on, tot in e.get("gates", [])),
                 e.get("language", "ko")))
    print(t("작업 %s · 회차 %d · 단계 %s") % (d["loop"], d["cycle"], d["stage_label"]))
    if d["intent"]:
        print(t("  작업 내용: %s") % d["intent"])
    else:
        print(t("  작업 내용: (미정) — %s 단계에서 정하고 "
              "`harness loop intent \"...\"` 로 기록하라")
              % stage_obj(cfg, cfg["stages"][0]["id"])["label"])
    if d["acceptance"]:
        print(t("  완료 조건 (%d개):") % len(d["acceptance"]))
        for i, it in enumerate(d["acceptance"], 1):
            print("    %d. %s" % (i, it))
    else:
        print(t("  완료 조건: (미정) — `harness loop done-when \"<조건>\" ...` 으로 기록하라"))
    print(t("  요약: %s") % d["summary"])
    print(t("  쓰기 허용: %s") % (", ".join(d["write"]) or t("(없음)")))
    print(t("  .dev/ 산출물 파일명 접두사: %s") % d["prefix"])
    print(t("  단계: ") + " → ".join("%s(%s)" % (s["label"], s["status"])
                                  for s in d["stages"]))
    if d["exit_met"] or d["exit_missing"]:
        print(t("  종료 조건: 충족 %s / 미충족 %s")
              % (", ".join(d["exit_met"]) or "-", ", ".join(d["exit_missing"]) or "-"))
    if d["evidence"]:
        print(t("  증거: %s") % ", ".join("%s×%d" % kv for kv in d["evidence"].items()))
    for r in d["skips"]:
        print(t("  스킵: %s — %s (승인: %s)") % (r["stage"], r["reason"], r["authorized_by"]))
    for g in d["grants"]:
        print(t("  예외: %s (남은 %d회) — %s") % (g["glob"], g["uses_left"], g["reason"]))
    if d["auto_skip"]:
        print(t("  ⚠ 스킵 자동 승인 ON (%s) — 사유: %s. 끄려면 `harness auto-skip off`")
              % (d["auto_skip"], d["auto_skip_reason"]))
    if d["pending_promotions"]:
        print(t("  승격 결정 대기 %d개 (Compounding 의 종료 조건): %s")
              % (len(d["pending_promotions"]), ", ".join(d["pending_promotions"])))
    if d["promoted"]:
        print(t("  승격됨: %s")
              % ", ".join("%s %d" % kv for kv in sorted(d["promoted"].items())))
    if d["tidy"]:
        print("  %s" % d["tidy"])
    if d.get("candidates"):
        render_work_candidates(d["candidates"])


def cli_status(ctx, argv):
    data = status_report(ctx)
    probs = (config_problems(ctx.cfg) + drift_problems(ctx.cfg, ctx.root)
             + install_problems(ctx.root)
             + language_problems(ctx.root))
    if probs:
        data["config_problems"] = probs
    dump_json(data) if "--json" in argv else render_status(data, ctx.cfg)
    return 0


def _enter(ctx, dest_idx):
    """dest_idx 단계를 active 로. 범위를 넘으면 루프를 닫고 새 루프를 만든다."""
    con, cfg, root, lid = ctx.con, ctx.cfg, ctx.root, ctx.lid
    if dest_idx >= len(cfg["stages"]):
        close_loop(con, cfg, lid)
        return create_loop(con, cfg, root), cfg["stages"][0]["id"], True
    sid = cfg["stages"][dest_idx]["id"]
    # **들어가는 쪽도 차지해야 한다.** 떠나는 쪽에만 claim 을 붙였더니, 대상 단계 행이
    # 없을 때(설정에서 id 가 바뀐 경우) 0행 갱신인데도 "→ 단계 N" 이라고 말하고
    # 활성 단계가 0개인 루프가 남았다. 다음 명령이 그 작업을 말없이 버렸다.
    if not stage_set(con, "enter", (now(), lid, sid)):
        return lid, None, False
    return lid, sid, False


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
    print(t("\n회고에 답할 것:"))
    for i, (q, why) in enumerate(retro_questions(cfg), 1):
        print("  %d. **%s** — %s" % (i, q, why))

    keys = cycle_search_keys(con, lid, retro_window_start(con, lid))
    if keys:
        print(t("\n이 회차의 검색 키 — 회고 **앞부분**에 이 문자열을 그대로 넣어라:"))
        print("  " + "  ".join("`%s`" % k for k in keys))
        print(t("  나중에 이 키로 찾는다. 없으면 그 회고는 다시 찾아지지 않는다 "
              "(내용이 같아도 그렇다)."))

    sk = skips_of(con, lid)
    if sk:
        print(t("\n이 루프에서 건너뛴 단계 — 회고에 사유와 함께 기록하라:"))
        for r in sk:
            print(t("  - %s: %s (승인: %s)") % (r["stage"], r["reason"], r["authorized_by"]))
    rows = con.execute(
        "SELECT kind, rule, target, COUNT(*) c FROM event "
        "WHERE loop_id=? AND kind IN ('block','tool_fail','bypass') "
        "GROUP BY kind, rule, target HAVING c > 0 ORDER BY c DESC LIMIT 8",
        (lid,)).fetchall()
    if rows:
        print(t("\n이 루프에서 관측된 것 — 회고 대상:"))
        for r in rows:
            print("  - %s/%s %s ×%d" % (r["kind"], r["rule"] or "-", r["target"], r["c"]))
    churn = con.execute(
        "SELECT target, COUNT(*) c FROM event WHERE loop_id=? AND kind='edit' "
        "GROUP BY target HAVING c >= 4 ORDER BY c DESC LIMIT 5", (lid,)).fetchall()
    if churn:
        print(t("\n재편집이 많은 파일 — 구조 문제일 수 있다:"))
        for r in churn:
            print("  - %s ×%d" % (r["target"], r["c"]))

    # 여러 작업에서 반복된 것은 이 단계의 종료 조건이다. 산문으로 적고 끝내면
    # 다음 작업에서 같은 실수가 또 나오고, 그건 복리가 아니다.
    pend = pending_promotions(con, cfg)
    if pend:
        print(t("\n여러 작업에서 반복된 항목 — 이 단계를 끝내려면 결정해야 한다 "
              "(종료 조건 promotion_decided):"))
        for it in pend:
            mark = t("  ← %s 로 승격했는데 다시 걸렸다") % it["regressed"] \
                if it.get("regressed") else ""
            print(t("  - %s ×%d (작업 %d개)%s")
                  % (it["key"], it["count"], it["loops"], mark))
        print(t("  `harness promote` 로 목록과 결정 방법을 본다. "
              "승격하지 않기로 하는 것도 결정이다."))


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
    stage_set(con, "reset", (lid, ids[0]))
    con.execute("UPDATE loop SET cycle=cycle+1 WHERE id=?", (lid,))
    stage_set(con, "enter", (now(), lid, ids[1]))
    return ids[1]


def cli_advance(ctx, argv):
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    last = cfg["stages"][-1]["id"]
    want_done = "--done" in argv
    want_cycle = "--cycle" in argv

    if sid != last and (want_done or want_cycle):
        raise Refuse(t("--done / --cycle 은 마지막 단계(%s)에서만 쓴다.")
                     % stage_obj(cfg, last)["label"], code=2)
    if sid == last:
        if want_done and want_cycle:
            raise Refuse(t("--done 과 --cycle 은 함께 쓸 수 없다."), code=2)
        if not (want_done or want_cycle):
            raise Refuse(t("%s 단계에서는 두 갈래 중 하나를 골라야 한다. 스스로 판단하라:")
                         % stage_obj(cfg, last)["label"],
                         t("  harness advance --done    작업이 끝났다 → %s (새 작업 선정)")
                         % stage_obj(cfg, cfg["stages"][0]["id"])["label"],
                         t("  harness advance --cycle   후속 회차가 남았다 → %s (같은 작업 유지)")
                         % stage_obj(cfg, cfg["stages"][1]["id"])["label"], code=1)

    missing = exit_blockers(con, cfg, root, lid, sid)
    if missing:
        print(t("advance 거부 — %s 단계의 종료 조건이 남았다:") % stage_obj(cfg, sid)["label"])
        for k in missing:
            print("  - %s: %s" % (k, criterion_why(con, cfg, root, lid, k)))
        if stage_obj(cfg, sid).get("skippable") is False:
            print(t("이 단계는 건너뛸 수 없다. 조건을 채워야 한다."))
        else:
            print(t("정당한 사유가 있으면 `harness skip %s --reason \"...\"` 로 "
                  "사람의 승인을 받아라.") % sid)
        return 1

    # 회고가 나중에 찾아지는지 확인한다. 통찰의 질은 채점하지 않지만 찾아지는지는
    # 기계적 사실이라 확인할 수 있다. 막지는 않는다 — 무엇을 쓸지는 판단이다.
    # **읽기만 한다.** 기록은 claim 을 이긴 뒤에 (아래 트랜잭션 안에서) 남긴다.
    # 예전에는 여기서 바로 커밋해서, 병렬 advance 넷 중 진 셋도 `retro_keys` 를
    # 남겼다 — 회차 전이는 한 번인데 이벤트가 넷이었다(4회차 C⑨ 실측). 그 종류는
    # `FP_IGNORE_KINDS` 에 없어 진전 지문까지 바꿔, 아무 진전 없이 "진전 있음" 으로
    # 보이게 만들었다. **판정 전에 커밋하지 않는다.**
    retro_note = None
    if sid == last:
        with swallow(t("회고 키 확인")):
            keys, found, missing = retro_key_report(
                con, cfg, root, lid, retro_window_start(con, lid))
            if keys:
                retro_note = (keys, found, missing)

    snap = None
    with con:
        # **이 단계를 끝내는 것은 한 번뿐이다.** 무조건 UPDATE 였을 때는 병렬 advance
        # 둘이 모두 성공해 열린 작업이 둘 생기거나 가짜 회차 스냅샷이 남았다.
        # 판단을 WHERE 절 안으로 옮겨 rowcount 가 승자를 정하게 한다.
        if not stage_set(con, "done", (now(), lid, sid)):
            raise Refuse(t("이미 %s 단계를 벗어났다 — 다른 호출이 먼저 진행했다. "
                           "`harness status` 로 현재 단계를 확인하라.")
                         % stage_obj(cfg, sid)["label"], code=1)
        if retro_note:
            keys, found, missing = retro_note
            record_event(con, lid, sid, "retro_keys", str(len(found)),
                         "%s-%d" % (lid, cycle_of(con, lid)),
                         "found=%d/%d missing=%s"
                         % (len(found), len(keys), ",".join(missing) or "-"))
        # 회차 경계에서만 스냅샷을 남긴다. close_loop 이 stage 를 지우기 전에.
        if sid == last:
            snap = record_cycle_close(con, cfg, lid, sid)
        if sid == last and want_done:
            nlid = rotate_loop(con, cfg, root, lid)
            if nlid is None:
                raise RuntimeError(t("다른 호출이 먼저 이 작업을 닫았다"))
            nsid = cfg["stages"][0]["id"]
            done_task = True
        elif sid == last:
            nlid, nsid, done_task = lid, next_cycle(ctx), False
        else:
            nlid, nsid, _ = _enter(ctx, stage_index(cfg, sid) + 1)
            done_task = False
            if nsid is None:
                raise RuntimeError(t("다음 단계 행이 없다 — 상태와 설정이 어긋났다"))

    if retro_note:
        keys, found, missing = retro_note
        if missing:
            print(t("회고 확인: 검색 키 %d개 중 %d개가 빠졌다 — %s")
                  % (len(keys), len(missing), ", ".join("`%s`" % k for k in missing)))
            print(t("   그 문자열이 회고에 없으면 다음에 같은 일이 생겨도 찾아지지 않는다. "
                  "다음 회차 회고에는 넣어라."))
        else:
            print(t("회고 확인: 검색 키 %d개 전부 들어 있다 — 다시 찾아진다.") % len(keys))
    if snap:
        print(t("회차 %d 기록: 차단 %d · 실패 %d(반복 %d) · 재편집 최대 %d · "
              "우회 %d · 스킵 %d")
              % (snap["cycle"], snap["blocks"], snap["fails"], snap["refails"],
                 snap["churn"], snap["bypass"], snap["skips"]))
    if done_task:
        print(t("작업 %s 종료 — 기록은 각 폴더의 파일에 남아 있다.") % lid)
        print(t("새 작업 %s → 단계 %s") % (nlid, label_of(cfg, nsid)))
    else:
        if sid == last:
            print(t("작업 %s 회차 %d 시작 (같은 작업 유지)")
                  % (nlid, cycle_of(con, nlid)))
            print(t("   파일명 접두사: %s") % file_prefix(con, nlid))
        print(t("→ 단계 %s") % label_of(cfg, nsid))
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
        raise Refuse(t("사용법: harness skip <stage-id|+N|until:<stage-id>> --reason \"...\""), code=2)
    # 훅과 **같은 함수**로 판정한다. 훅이 이미 막았으므로 보통 여기 오지 않지만,
    # 셸 간접 호출로 훅을 우회해 들어온 경우에도 같은 답을 내야 한다.
    why = skip_block_reason(cfg, sid, target, con, root, lid)
    if why:
        raise Refuse(why, code=1)
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
                raise Refuse(t("%s 를 건너뛰더라도 기록은 남겨야 한다: %s")
                             % (st["label"], criterion_why(con, cfg, root, lid, key)),
                             t("먼저 그 기록을 남긴 뒤 다시 시도하라. 승인만 면제된다."), code=1)

    # 자동 승인으로 통과한 스킵은 사람이 승인한 것과 구분해 기록한다
    by = "auto" if auto_skip_on(con) else "user"
    left = None
    skipped = []
    with con:
        # 현재 단계를 벗어나는 것은 **한 번뿐이다.** 이 claim 이 병렬 스킵의 단일
        # 소비점이다 — 자동 승인 `--uses 1` 을 둘이 동시에 써도 하나만 통과한다.
        # 종료 조건을 충족했다면 done, 아니면 skipped 로 정직하게 기록한다.
        if dest == cur or exit_blockers(con, cfg, root, lid, sid):
            won = stage_set(con, "skipped", (now(), reason, by, lid, sid))
            skipped.append(sid)
        else:
            won = stage_set(con, "done", (now(), lid, sid))
        if not won:
            raise Refuse(t("이미 %s 단계를 벗어났다 — 다른 호출이 먼저 진행했다. "
                           "`harness status` 로 현재 단계를 확인하라.")
                         % stage_obj(cfg, sid)["label"], code=1)
        for i in range(cur + 1, dest + 1):
            stage_set(con, "skip_ahead", (now(), reason, by, lid, ids[i]))
            skipped.append(ids[i])
        for s in skipped:
            record_event(con, lid, s, "skip", s, by, reason)
        if by == "auto":
            _, left = consume_auto_skip(con)
        nlid, nsid, cycled = _enter(ctx, dest + 1)
        if nsid is None:
            raise RuntimeError(t("다음 단계 행이 없다 — 상태와 설정이 어긋났다"))
    print(t("스킵(%s): %s") % (t("자동 승인") if by == "auto" else t("사용자 승인"),
                            ", ".join(skipped) or t("(없음)")))
    print(t("사유: %s") % reason)
    if by == "auto" and left is not None:
        print(t("자동 승인 남은 횟수: %d%s") % (left, t(" — 소진되어 OFF 로 돌아갔다") if left == 0 else ""))
    if cycled:
        print(t("작업 %s 종료 → 새 작업 %s, 단계 %s") % (lid, nlid, label_of(cfg, nsid)))
    else:
        print(t("→ 단계 %s") % label_of(cfg, nsid))
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
        raise Refuse(t("사용법: harness verify -- <검증 명령>"),
                     t("  예: harness verify -- pytest tests/"), code=2)
    stages = evidence_stages(cfg)
    if sid not in stages:
        raise Refuse(t("verify 는 %s 단계에서 쓴다 (현재 %s).")
                     % (", ".join(stages), label_of(cfg, sid)), code=2)
    if set(cmd) & SHELL_META:
        raise Refuse(t("셸 메타문자가 있는 명령은 거부한다 — 검증 명령 하나만 넘겨라."), code=2)
    # 관측 경로와 **같은 판정**을 쓴다. 두 곳이 갈리면 `verify` 로는 되는데 그냥
    # 돌리면 안 되는(또는 그 반대) 일이 생긴다.
    # 패턴이 없으면 검사를 건너뛰었다 — 그 순간 이 명령은 게이트를 지나는 **셸**이
    # 된다. 없으면 거부한다. 무엇이 검증인지 모르면 아무것도 돌리지 않는 것이 맞다.
    if not cfg.at("criteria.verification_evidence.bash_pattern"):
        raise Refuse(t("`criteria.verification_evidence.bash_pattern` 이 없다 — 무엇이 검증인지 "
                       "정해지지 않았으므로 아무것도 실행하지 않는다."), code=2)
    if not verification_hit(cfg, cmd):
        raise Refuse(t("검증 명령으로 보이지 않는다: %s") % cmd,
                     t("이 자리는 검증을 돌리는 곳이다. 임의의 명령을 돌리는 곳이 아니다."), code=2)
    try:
        rc = subprocess.call(shlex.split(cmd), cwd=root)
    except OSError as e:
        raise Refuse(t("실행할 수 없다: %s") % e, code=2)
    if rc != 0:
        # 실패도 사실이므로 적립한다. 증거로는 세지 않는다.
        with con:
            record_event(con, lid, sid, "tool_fail", "verify", norm_cmd(cmd),
                         "exit %d" % rc)
        raise Refuse(t("\n검증 실패 (exit %d) — 증거로 기록하지 않았다. 고치고 다시 돌려라.") % rc, code=1)
    with con:
        record_evidence(con, lid, sid, "verification_evidence",
                        ("verify: " + cmd)[:120])
    print(t("\n검증 통과 — 증거로 기록했다: %s") % cmd)
    return 0


def cli_allow(ctx, argv):
    con, cfg, lid = ctx.con, ctx.cfg, ctx.lid
    pos = argv_positional(argv)
    glob = pos[0] if pos else None
    reason = argv_value(argv, "reason")
    uses = argv_value(argv, "uses")
    if not glob or not reason:
        raise Refuse(t("사용법: harness allow <glob> --reason \"...\" [--uses N]"), code=2)
    with con:
        con.execute("INSERT INTO wgrant(loop_id,glob,reason,uses_left,at) "
                    "VALUES(?,?,?,?,?)",
                    (lid, glob, reason, int(uses) if uses else 3, now()))
    # 예전에는 무조건 "사용자 승인" 이라고 적었다. `consent.allow` 를 빼면 아무도
    # 승인하지 않았는데 그렇게 출력됐다 — 기록이 거짓말을 하면 감사가 무의미하다.
    print(t("예외 등록%s: %s — %s")
          % (t(" (사용자 승인)") if "allow" in consent_map(cfg) else "", glob, reason))
    return 0


def cli_approve_plan(ctx, argv):
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    pos = argv_positional(argv)
    path = pos[0] if pos else None
    if not path:
        raise Refuse(t("사용법: harness approve-plan <plan-file>"), code=2)
    rel = rel_to_root(root, path)
    if not rel or not os.path.isfile(os.path.join(root, rel)):
        raise Refuse(t("계획 파일을 찾을 수 없다: %s") % path, code=2)
    # 승인 대상은 **이 회차의 계획 파일**이어야 한다. 예전에는 아무 파일이나 받아서
    # `README.md` 를 승인하면 `plan_file` 게이트까지 함께 열렸고, 지난 회차의 계획서로도
    # 열렸다. 판정은 `fs_evidence` 가 이미 하고 있으므로 그 규칙을 그대로 쓴다.
    want = cfg.seq("criteria.plan_file.write_glob")
    pre = file_prefix(con, lid)
    if want and not (any(glob_match(rel, g) for g in want)
                     and os.path.basename(rel).startswith(pre)):
        raise Refuse(t("계획 파일이 아니다: %s") % rel,
                     t("이 회차의 계획은 %s 아래에 `%s` 로 시작하는 이름이어야 한다.")
                     % (", ".join(want), pre), code=2)
    if evidence_digest(root, rel) is None:
        # 지문을 못 구하면 그 승인은 **만료될 수 없다.** `chmod 000` 한 번으로 승인 후
        # 계획을 통째로 갈아치울 수 있었다. 읽을 수 없으면 승인하지 않는다.
        raise Refuse(t("계획 파일을 읽을 수 없어 승인할 수 없다: %s") % rel,
                     t("읽을 수 있게 한 뒤 다시 시도하라 — 승인은 그 시점의 내용에 대한 것이라 "
                       "내용을 확인할 수 없으면 기록이 거짓이 된다."), code=2)
    with con:
        record_evidence(con, lid, sid, "plan_file", rel, root)
        record_evidence(con, lid, sid, "plan_approved", rel, root)
    print(t("계획 승인 기록: %s") % rel)
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
            with swallow(t("파일 읽기")):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    hay += "\n" + fh.read(recall_read_bytes(cfg)).lower()
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
    live = open_loops(con)

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
            note = t("파일 %d개인데 INDEX.md 가 없다") % len(files)
        elif has_idx and mtime(idx) < newest:
            note = t("INDEX.md 가 최신 파일보다 낡았다 (파일 %d개)") % len(files)
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
            if mt < cutoff and lid_of not in live:
                out["stale"].append((".dev/%s/%s" % (sub, n),
                                     int((time.time() - mt) // 86400)))
        for k, v in groups.items():
            if len(v) >= group_min and k not in live:
                out["groups"].append((k, sorted(v)))

    budget = learned_budget(cfg)
    used = len(learned_lines(con, cfg))
    if used:
        out["learned"] = (used, budget)
    out["regressed"] = promotion_rows(con, maturity="regressed")
    out["stale"].sort(key=lambda it: -it[1])
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

    print(t("정리 후보 (Scaffolding 단계의 일이다. 삭제·병합 여부는 자율)"))
    if rep["dirs"]:
        print(t("\n인덱스가 필요하거나 낡은 폴더"))
        for d, note in rep["dirs"]:
            print("  %-26s %s" % (d, note))
    if rep["groups"]:
        print(t("\n한 작업이 여러 파일을 남겼다 — 하나로 병합할 후보"))
        for k, files in rep["groups"][:limit]:
            print(t("  작업 %s — %d개") % (k, len(files)))
            for f in files[:4]:
                print("      %s" % f)
            if len(files) > 4:
                print(t("      ... +%d개") % (len(files) - 4))
    if rep["stale"]:
        print(t("\n닫힌 작업의 오래된 파일 — 인덱스에 요약하고 지울 후보"))
        for f, days in rep["stale"][:limit]:
            print(t("  %-52s %d일") % (f[:52], days))
        if len(rep["stale"]) > limit:
            print(t("  ... +%d개") % (len(rep["stale"]) - limit))
    if rep["regressed"]:
        print(t("\n승격했는데 다시 걸린 항목 — 승격이 통하지 않았다"))
        for r in rep["regressed"]:
            print("  %-30s %s: %s" % (r["key"][:30], r["decision"], r["note"] or "-"))
        print(t("  Compounding 에서 다시 결정하게 된다 (`harness promote`)."))
    if rep["learned"]:
        used, budget = rep["learned"]
        print(t("\nLEARNED.md: %d/%d줄%s")
              % (used, budget, t(" — 예산 소진. 새 규칙을 올리려면 먼저 비워라")
                 if used >= budget else ""))
        print(t("  내리기: `harness promote <key> --decline --reason \"...\"`"))
    if not any((rep["dirs"], rep["groups"], rep["stale"], rep["regressed"],
                rep["learned"])):
        print(t("  (없음 — 정리할 것이 없다)"))
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
    for p in promotion_rows(con):
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
    # **버린 회차도 표본이다.** `cycle_close` 만 셌더니 `loop adopt` 한 줄로
    # 마찰이 쌓인 회차를 표본에서 지울 수 있었다 (4회차 C③).
    for r in con.execute("SELECT at, detail FROM event "
                         "WHERE kind IN ('cycle_close','cycle_adopt') ORDER BY id"):
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
    print(t("복리 측정 — 작업 %d개, 기록된 회차 %d개%s")
          % (d["loops"], d["cycles"],
             " (%s ~ %s)" % tuple(d["span"]) if d["span"] else ""))

    print(t("\n① 승격 생존율 — 무엇이 실제로 막았나"))
    agg = d["survival"]
    if not agg:
        print(t("  (아직 승격이 없다. 여러 작업에서 반복된 항목이 생기면 쌓인다)"))
    else:
        for k in sorted(agg, key=lambda x: -agg[x]["n"]):
            v = agg[k]
            seen = (t("변경관측 %d/%d") % (v["vy"], v["vn"])) if v["vn"] else ""
            print(t("  %-10s %2d건 중 %2d건 재발 (%s)  %s")
                  % (k, v["n"], v["re"], _pct(v["re"], v["n"]).strip(), seen))
        total = sum(v["n"] for v in agg.values())
        if total < 20:
            print(t("  ⚠ 표본 %d건. 비율을 믿지 마라 — 20~30건은 있어야 한다.") % total)
        print(t("  재발 = 승격 이후 같은 항목이 다시 걸린 것. 변경관측 = 그 회차에 "
              "주장에 맞는 파일 변경이 있었나."))

    print(t("\n② 회차 추세 — 마찰과 회피를 나란히 본다"))
    if not d["buckets"]:
        print(t(NO_CYCLES))
    else:
        print("  %-12s %s" % (t("회차구간"), " ".join("%8s" % t(it) for _, it in TREND_KEYS)))
        for b in d["buckets"]:
            print("  %-12s %s" % (t("%d-%d회") % (b["from"], b["to"]),
                                  " ".join("%8.1f" % b["avg"][k] for k, _ in TREND_KEYS)))
        if d["verdict"] in VERDICT_TEXT:
            print("  " + t(VERDICT_TEXT[d["verdict"]]))

    print(t("\n③ 반복 실패 비율 — 실패 주입이 겨냥한 것"))
    # 누적 비율(전체 실패 중 첫 실패가 아닌 것)은 쓰지 않는다. 명령 종류는 적고
    # 실행은 많으니 시간이 지나면 무조건 100% 에 수렴한다 — 창이 없는 비율은
    # 아무것도 말해주지 않는다. 회차 구간별로만 읽는다.
    if not d["buckets"]:
        print(t(NO_CYCLES))
    else:
        for b in d["buckets"]:
            print(t("  %-12s 실패 %3d건 중 이전에도 실패한 것 %3d건 (%s)")
                  % (t("%d-%d회") % (b["from"], b["to"]), b["fails"], b["refails"],
                     _pct(b["refails"], b["fails"]).strip()))
        print(t("  '이전에도 실패한 것' = 그 회차 시작 전에 이미 같은 명령이 실패한 적 있음."))

    print(t("\n측정하지 못하는 것: 결과물의 품질, 그 회차가 필요했는지, 사람이 아낀 시간."))
    print(t("점수를 만들지 않는 이유: 하나로 합치면 그 하나를 최적화하게 된다."))


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
                print(t("성숙도 갱신: %s → %s") % (key, mat))
        else:
            agg = {}
            for _, mat in changed:
                agg[mat] = agg.get(mat, 0) + 1
            print(t("성숙도 갱신 %d건: %s")
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
        raise Refuse(t("반복 항목이 아니다: %s") % key,
                     t("`harness promote` 로 목록을 확인하라 (키는 'block:<규칙>' 또는 "
                     "'tool_fail:<명령>' 형식이다)."), code=2)
    if decline:
        as_kind = "declined"
    if not as_kind:
        raise Refuse(t("무엇으로 승격할지 골라야 한다: --as %s, 또는 --decline.")
                     % "|".join(k for k in promote_as(cfg) if k != "declined"), code=2)
    if as_kind not in promote_as(cfg):
        raise Refuse(t("알 수 없는 승격 종류: %s (가능: %s)")
                     % (as_kind, ", ".join(promote_as(cfg))), code=2)
    if not note:
        raise Refuse(t("사유/내용이 필요하다: %s")
                     % (t("--reason \"왜 승격하지 않는가\"") if as_kind == "declined"
                        else t("--note \"무엇을 어떻게 바꿨는가\"")), code=2)

    if as_kind == "rule":
        used = len(learned_lines(con, cfg))
        existing = promotion_rows(con, key=key)
        grows = not (existing and existing["decision"] == "rule")
        if grows and used >= learned_budget(cfg):
            raise Refuse(t("LEARNED.md 예산이 찼다 (%d/%d줄). 항상 로드되는 문서라 상한이 있다.")
                         % (used, learned_budget(cfg)),
                         t("먼저 한 줄을 비워라: `harness promote <기존키> --decline "
                         "--reason \"...\"` (`harness tidy` 로 목록 확인)"), code=1)

    kind = key.split(":", 1)[0]
    # 보류의 성숙도는 'declined' 다. established 로 두면 "확립된 규칙"과 구분되지 않는다.
    maturity = "declined" if as_kind == "declined" else "established"
    seen = promote_change_seen(con, cfg, lid, as_kind)
    with con:
        con.execute(
            "INSERT INTO promotion(key,kind,decision,maturity,note,loop_id,at,"
            "recheck_at,after_id) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "decision=excluded.decision, maturity=excluded.maturity, "
            "note=excluded.note, loop_id=excluded.loop_id, at=excluded.at, "
            "recheck_at=excluded.recheck_at, after_id=excluded.after_id",
            (key, kind, as_kind, maturity, note, lid, now(), now(),
             last_event_id(con)))
        record_event(con, lid, sid,
                     "promote_declined" if as_kind == "declined" else "promote",
                     as_kind, key, note)
        if seen is not None:
            # 주장과 사실을 따로 남긴다. metrics 가 나란히 보여준다.
            record_event(con, lid, sid, "promote_verify", as_kind, key,
                         "change_seen=%s" % ("yes" if seen else "no"))
    wrote = refresh_learned(con, cfg, root)

    print("%s: %s → %s" % (t("보류 기록") if as_kind == "declined" else t("승격 기록"),
                           key, promote_as(cfg)[as_kind]))
    print("  %s" % note)
    if wrote is None:
        # 승격은 DB 에 남았지만 **복리가 실제로 도는 곳**은 LEARNED.md 다. 그 반영이
        # 실패했는데 성공처럼 보고하면, 다음 세션은 배운 것 없이 시작한다.
        print(t("  ⚠ %s 에 반영하지 못했다 (쓰기 실패 — 권한이나 파일시스템을 확인하라). "
                "결정은 기록됐지만 다음 세션에 실리지 않는다. 고친 뒤 `%s promote` 를 "
                "다시 실행하거나 `%s status` 로 확인하라.")
              % (LEARNED_REL.replace(os.sep, "/"), WRAPPER_CMD, WRAPPER_CMD))
    elif wrote:
        print(t("  %s 갱신 (%d/%d줄)")
              % (LEARNED_REL.replace(os.sep, "/"), len(learned_lines(con, cfg)),
                 learned_budget(cfg)))
    if seen is False:
        print(t("  ⚠ 이 회차에 그에 맞는 파일 변경이 관측되지 않았다 (%s). 막지는 않지만 "
              "기록된다 — `harness metrics` 가 주장과 사실을 나란히 보여준다.")
              % ", ".join(verify_globs(cfg, as_kind) or []))
    elif seen:
        print(t("  설정/구조 변경이 이 회차에 관측됐다 — 주장이 사실로 뒷받침된다."))
    if as_kind == "declined":
        print(t("  보류도 결정이다. 이 항목이 앞으로 %s개 작업에서 다시 걸리면 "
              "결정이 무효화되어 다시 올라온다.")
              % cfg.num("promotion.reopen_after_loops", 2, low=1))
    else:
        print(t("  성숙도 established. 재발 없이 작업 %s개가 지나면 proven 이 된다.")
              % cfg.num("promotion.proven_after_loops", 3, low=1))
    left = pending_promotions(con, cfg)
    print(t("남은 결정: %d개") % len(left))
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
        row = loop_row(con, lid)
        intent = (row["intent"] if row else None) or ""
        picked = [it for it in re.split(r"[\s,·/]+", intent)
                  if len(it) >= 2 and it.lower() not in STOPWORDS][:6]
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
        head = t("이 루프의 작업에서 추출: %s") % " ".join(keywords)
    elif keywords:
        head = t("키워드: %s") % " ".join(keywords)
    else:
        head = t("전체 — 작업이 정해졌으면 `harness loop intent \"...\"` 로 기록하라")
    print(t("과거 관측 기록 (%s)") % head)
    if not rows:
        print(t("  (없음)"))
    for r in rows:
        mark = t("  ← 여러 작업에서 반복") if r["loops"] > 1 else ""
        print(t("  %-10s %-16s %-34s ×%d (작업 %d)%s")
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
            print(t("\n재편집이 많은 파일"))
            for r in matched:
                print(t("  %-40s ×%d (작업 %d)") % (r["target"][:40], r["c"], r["loops"]))

    indexes, files = _recall_files(cfg, root, keywords)
    if indexes:
        print(t("\n인덱스 — 쌓인 기록의 진입점 (먼저 읽어라)"))
        for f in indexes:
            print("  %s" % f)
    print(t("\n관련 회고·학습 파일 (필요하면 읽어라)"))
    if not files:
        print(t("  (없음)"))
    for f in files:
        print("  %s" % f)
    return 0


def cli_stats(ctx, argv):
    """누적 수치. --loop 를 주면 현재 작업만."""
    con, cfg, root, lid = ctx.con, ctx.cfg, ctx.root, ctx.lid
    only = "--loop" in argv
    cond, params = ("WHERE loop_id = ?", [lid]) if only else ("", [])
    print(t("범위: %s") % (t("현재 작업 %s") % lid if only else t("전체 누적")))

    lc = con.execute("SELECT COUNT(*) c, SUM(closed_at IS NOT NULL) closed "
                     "FROM loop").fetchone()
    print(t("작업: %d개 (완료 %d)") % (lc["c"], lc["closed"] or 0))

    rows = con.execute("SELECT kind, COUNT(*) c FROM event %s GROUP BY kind "
                       "ORDER BY c DESC" % cond, params).fetchall()
    print(t("이벤트: ") + (", ".join("%s %d" % (EVENT_KINDS.get(r["kind"], r["kind"]), r["c"])
                                  for r in rows) or t("(없음)")))

    # 반복 신호는 규칙 단위로 봐야 드러난다. (규칙, 대상) 으로 묶으면
    # 같은 규칙에 다른 파일로 계속 걸리는 패턴이 흩어져 보이지 않는다.
    # tool_fail 만 예외 — 정규화된 명령 자체가 의미 있는 키다.
    for kind, title, key in (("block", t("차단된 규칙"), "rule"),
                             ("tool_fail", t("실패한 도구"), "target"),
                             ("skip", t("건너뛴 단계"), "rule"),
                             ("stop_gate", t("미충족 종료 조건"), "rule"),
                             ("bypass", t("우회한 게이트 (사전승인 포함)"), "rule")):
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
                bits += t(", 대상 %d종") % r["targets"]
            if r["loops"] > 1:
                bits += t(" ← %d개 작업에서 반복") % r["loops"]
            print("  %-24s %s" % (r["k"][:24], bits))
    with con:
        sync_promotions(con, cfg)
    refresh_learned(con, cfg, root)
    rows = promotion_rows(con, order="maturity, at")
    if rows:
        print(t("\n승격 이력 — 반복을 기계화한 기록"))
        for r in rows:
            print("  %-28s %-10s %-12s %s"
                  % (r["key"][:28], r["decision"], r["maturity"],
                     (r["note"] or "-")[:36]))
    pend = pending_promotions(con, cfg)
    if pend:
        print(t("\n승격 결정 대기 %d개 — `harness promote`") % len(pend))
    print(t("\n상세 조회: `harness recall <키워드|경로>`"))
    return 0


@auto_skip_sub("off")
def _as_off(ctx, argv, pos):
    con = ctx.con
    with con:
        set_meta(con, "auto_skip", "off")
        set_meta(con, "auto_skip_uses", "")
        set_meta(con, "auto_skip_loop", "")
        set_meta(con, "auto_skip_off_at", now())
    print(t("스킵 자동 승인 OFF — 이제 모든 스킵이 사용자 동의를 요구한다."))
    return 0


@auto_skip_sub("on")
def _as_on(ctx, argv, pos):
    con, cfg, lid = ctx.con, ctx.cfg, ctx.lid
    reason = argv_value(argv, "reason")
    uses = argv_value(argv, "uses")
    scope = argv_value(argv, "scope") or "project"
    if not reason:
        raise Refuse(t("사용법: harness auto-skip on --reason \"...\" "
                     "[--uses N] [--scope loop|project]"), code=2)
    if scope not in ("loop", "project"):
        raise Refuse(t("--scope 는 loop 또는 project 여야 한다."), code=2)
    if uses is not None:
        try:
            if int(uses) < 1:
                raise ValueError
        except ValueError:
            raise Refuse(t("--uses 는 1 이상의 정수여야 한다."), code=2)
    with con:
        set_meta(con, "auto_skip", "on")
        set_meta(con, "auto_skip_reason", reason)
        set_meta(con, "auto_skip_at", now())
        set_meta(con, "auto_skip_uses", str(int(uses)) if uses else "")
        set_meta(con, "auto_skip_loop", lid if scope == "loop" else "")
    print(t("스킵 자동 승인 ON%s — 사유: %s")
          % (t(" (사용자 승인)") if "auto-skip" in consent_map(cfg) else "", reason))
    print(t("범위: %s") % auto_skip_scope_note(con))
    print(t("사유는 계속 필수이고 기록에는 authorized_by=auto 로 남는다. "
          "끄려면 `harness auto-skip off`."))


@auto_skip_sub("status")
def _as_status(ctx, argv, pos):
    con = ctx.con
    active, expired = auto_skip_state(con)
    if active:
        print(t("스킵 자동 승인: ON (since %s) — 사유: %s")
              % (get_meta(con, "auto_skip_at", "-"),
                 get_meta(con, "auto_skip_reason", "-")))
        print(t("  범위: %s") % auto_skip_scope_note(con))
    else:
        print(t("스킵 자동 승인: OFF — 모든 스킵이 사용자 동의를 요구한다.")
              + (" (%s)" % expired if expired else ""))
    return 0


def cli_auto_skip(ctx, argv):
    """스킵 자동 승인 토글. on 은 PreToolUse 가 사람의 동의를 받은 뒤에만 도달한다."""
    pos = argv_positional(argv)
    return dispatch(AUTO_SKIP_SUBS, "auto-skip", pos[0] if pos else "status")(ctx, argv, pos)


@loop_sub("new")
def _loop_new(ctx, argv, pos):
    con, cfg, root, lid = ctx.con, ctx.cfg, ctx.root, ctx.lid
    intent = argv_value(argv, "intent")
    with con:
        nlid = rotate_loop(con, cfg, root, lid, intent)
        if nlid and intent:
            record_evidence(con, nlid, cfg["stages"][0]["id"], "intent_set", intent)
    if not nlid:
        raise Refuse(t("이미 %s 는 닫혔다 — 다른 호출이 먼저 새 작업을 시작했다. "
                       "`harness status` 로 확인하라.") % lid, code=1)
    print(t("작업 %s 종료 → 새 작업 %s, 단계 %s")
          % (lid, nlid, label_of(cfg, cfg["stages"][0]["id"])))
    return 0


@loop_sub("intent")
def _loop_intent(ctx, argv, pos):
    con, lid, sid = ctx.con, ctx.lid, ctx.sid
    text = " ".join(pos[1:]).strip() or (argv_value(argv, "intent") or "").strip()
    if not text:
        raise Refuse(t("사용법: harness loop intent \"<이번 루프에서 할 작업>\""), code=2)
    with con:
        con.execute("UPDATE loop SET intent=? WHERE id=?", (text, lid))
        # Scaffolding 의 종료 조건. 작업을 기록하지 않으면 단계를 넘어갈 수 없다.
        record_evidence(con, lid, sid, "intent_set", text)
    print(t("작업 %s 의 내용: %s") % (lid, text))
    print(t("Context 단계의 `harness recall` 이 이 작업을 기준으로 과거 기록을 찾는다."))
    return 0


@loop_sub("done-when")
def _loop_done_when(ctx, argv, pos):
    con, lid, sid = ctx.con, ctx.lid, ctx.sid
    items = [it for it in pos[1:] if it.strip()]
    if "--clear" in argv:
        with con:
            con.execute("DELETE FROM evidence WHERE loop_id=? AND kind='acceptance'",
                        (lid,))
        print(t("완료 조건을 비웠다. 다시 기록하라."))
        return 0
    if items:
        with con:
            for it in items:
                record_evidence(con, lid, sid, "acceptance", it.strip())
    rows = acceptance_of(con, lid)
    if not rows:
        raise Refuse(t("사용법: harness loop done-when \"<완료 조건>\" [\"<조건2>\" ...] [--clear]"),
                     t("무엇이 '끝'인지 기록한다. Verification 이 이것을 대조하고,"),
                     t("Compounding 이 작업 종료 판단의 근거로 쓴다. 회차가 바뀌어도 유지된다."), code=2)
    print(t("작업 %s 의 완료 조건 (%d개):") % (lid, len(rows)))
    for i, it in enumerate(rows, 1):
        print("  %d. %s" % (i, it))
    return 0


@loop_sub("adopt")
def _loop_adopt(ctx, argv, pos):
    con, cfg, root, lid = ctx.con, ctx.cfg, ctx.root, ctx.lid
    want = pos[1] if len(pos) > 1 else None
    if not want:
        raise Refuse(t("사용법: harness loop adopt <loop-id> --reason \"...\""), code=2)
    # **재연결은 상태 전이다.** 예전에는 `create_loop`(INSERT OR IGNORE)에
    # 회차 +1 을 얹었고, 그래서 세 가지가 어긋났다 — 적대적 리뷰가 전부 찾았다.
    #   ① 없는 ID 를 adopt 하면 cycle=1 행을 만든 뒤 올려 **첫 상태가 회차 2**
    #   ② 연속 adopt 하면 `cycle_close` 없이 회차만 올라 **측정 창에 이전
    #      회차의 이벤트가 섞인다**
    #   ③ 닫힌 작업을 adopt 해도 `closed_at` 이 남아 tidy 가 닫힌 작업으로 본다
    #
    # 뿌리는 하나다. `INSERT OR IGNORE` 가 "만들기"와 "이어받기"를 뭉갰고,
    # `cycle` 이 **접두사 구분자**와 **측정 창 번호** 두 일을 겸했다.
    # 그래서 둘을 갈라 명시적으로 처리한다.
    existed = loop_row(con, want)
    with con:
        # **버리는 것도 끝내는 것이다 — 집계는 남는다.** 3회차에는 재연결이
        # `cycle_close` 를 쓰면 회고 창까지 옮겨 가는 것이 문제라 아예 집계를
        # 안 남겼는데, 그러면 마찰이 쌓인 회차를 `loop adopt` 한 줄로 표본에서
        # 지울 수 있었다(4회차 C③). 두 리뷰가 각자 옳았다 — 뿌리는 `cycle_close`
        # 가 **측정 창**과 **회고 창** 두 경계를 겸한 것이다. 종류를 갈라
        # `cycle_adopt` 에 집계를 담는다: 측정 창은 옮기고 회고 창은 두고.
        close_loop(con, cfg, lid, "cycle_adopt")
        if existed:
            # 이어받는다. 회차를 올리고 다시 열린 작업이므로 closed_at 을 지운다.
            new_cycle = (existed["cycle"] or 1) + 1
            # 사유는 **집계와 다른 행**에 남긴다. 같은 행에 넣었더니 JSON 처럼
            # 생긴 사유가 가짜 회차로 집계됐다(3회차).
            record_event(con, want, cfg["stages"][0]["id"], "cycle_adopt_reason",
                         str(existed["cycle"] or 1),
                         "%s-%s" % (want, existed["cycle"] or 1),
                         argv_value(argv, "reason") or "")
            con.execute("UPDATE loop SET cycle=?, closed_at=NULL WHERE id=?",
                        (new_cycle, want))
            create_loop(con, cfg, root, argv_value(argv, "reason"), loop_id=want)
        else:
            # 없던 ID 다. 새로 만드는 것이므로 1회차에서 시작한다.
            new_cycle = 1
            create_loop(con, cfg, root, argv_value(argv, "reason"), loop_id=want)
    if existed:
        print(t("작업 %s 재연결(사용자 승인). 단계는 1단계부터, 회차 %d 로 다시 "
                "추적한다 — 지난 회차의 산출물은 이번 조건을 채우지 않는다.")
              % (want, new_cycle))
    else:
        print(t("작업 %s 는 기록에 없어 **새로 만들었다**(사용자 승인). "
                "회차 1 · 단계 1부터 시작한다.") % want)
    return 0


@loop_sub("show")
def _loop_show(ctx, argv, pos):
    con, lid = ctx.con, ctx.lid
    row = loop_row(con, lid)
    print("loop %s · branch %s · created %s"
          % (lid, row["branch"] if row else "-", row["created_at"] if row else "-"))
    if row and row["intent"]:
        print("  intent: %s" % row["intent"])
    return 0


def cli_loop(ctx, argv):
    """서브명령을 **표에서** 찾는다. if 체인이었을 때 오타가 조용히 `show` 로
    떨어져 `harness loop inetnt "작업 내용"` 이 rc=0 을 냈다 — 사용자는
    기록됐다고 믿었다."""
    pos = argv_positional(argv)
    return dispatch(LOOP_SUBS, "loop", pos[0] if pos else "show")(ctx, argv, pos)


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
    cfg = load_config(root, plugin_root())
    load_messages(root, cfg.at("language") if isinstance(cfg, dict) else None)
    if con is None or not isinstance(cfg, dict) or not cfg.get("stages"):
        print(t("DB 또는 설정이 손상되었다: %s\n복구: `%s`")
              % (os.path.join(root, HARNESS_DIR), init_hint(root)), file=sys.stderr)
        return 1
    try:
        lid = head_loop(con)
        sid = active_stage(con, lid) if lid else None
        if not lid or not sid:
            with con:
                lid = create_loop(con, cfg, root, only_if_none=True)
            sid = active_stage(con, lid) or cfg["stages"][0]["id"]
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
            # 스키마가 플러그인보다 오래됐을 때 traceback 대신 할 일을 알려준다.
            # 마이그레이션은 하지 않는다 — DB 는 커밋하지 않는 런타임 상태이고,
            # init 이 스키마를 다시 적용하는 것이 정해진 업그레이드 경로다.
            print(t("DB 스키마가 플러그인 버전과 맞지 않는다 (%s).\n"
                  "`.claude/harness/bin/harness init` 을 다시 실행하라 — 파일은 "
                  "덮어쓰지 않고 스키마만 갱신한다. 그래도 안 되면 %s 를 지우고 "
                  "init 을 실행하라 (진행 중 상태만 사라지고 기록 파일은 남는다).")
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


# 우리 표 이름. **스키마에서 뽑는다** — 손으로 적으면 표가 늘 때 갈린다.
SCHEMA_TABLES = tuple(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))


def quarantine_db(root):
    """**하네스 DB 로 쓸 수 없으면** 옆으로 치우고 그 경로를 돌려준다. 멀쩡하면 None.

    예전에는 "열리나" 만 물었다(`PRAGMA schema_version`). 0바이트 파일은 **유효한
    빈 SQLite** 라 그 탐침을 통과했고, 그래서 `python3 -c "open(<DB>,'w')"` 한 줄로
    상태를 날려도 격리되지 않고 `.corrupt-N` 백업 없이 조용히 재초기화됐다 —
    기록이 전소되는데 아무 흔적이 없다(4회차 C②).

    질문을 바꾼다: **이게 우리 DB 인가.** 하네스 DB 라면 우리 표가 하나는 있다.
    (전부를 요구하지 않는다 — `CREATE TABLE IF NOT EXISTS` 가 업그레이드 경로라
    낡은 DB 는 새 표가 없는 것이 정상이다.)
    """
    path = os.path.join(root, DB_REL)
    if not os.path.isfile(path):
        return None
    try:
        probe = sqlite3.connect(path)
        try:
            names = {r[0] for r in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            probe.close()
        if names & set(SCHEMA_TABLES):
            return None
    except sqlite3.Error:
        pass
    for n in range(1, 100):
        dst = "%s.corrupt-%d" % (path, n)
        if not os.path.exists(dst):
            try:
                os.replace(path, dst)
            except OSError:
                return None
            for suffix in ("-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass
            return dst
    return None


def install_db(root, cfg):
    """스키마를 적용하고 활성 작업을 보장한다. 업그레이드 경로가 이 함수다 —
    `CREATE TABLE IF NOT EXISTS` 라서 재실행이 곧 스키마 갱신이다."""
    made = []
    fresh = not os.path.isfile(os.path.join(root, DB_REL))
    # **`init` 은 복구 경로다.** 훅이 "복구: harness init" 이라고 안내하는데 정작
    # 손상된 DB 앞에서 traceback 으로 죽으면 게이트가 영구히 꺼진다. 읽을 수 없으면
    # 지우지 않고 **옆으로 치운다** — 사람이 나중에 열어볼 수 있어야 한다.
    moved = quarantine_db(root)
    if moved:
        made.append(t("%s (읽을 수 없어 %s 로 옮겼다)") % (DB_REL, os.path.basename(moved)))
        fresh = True
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
    # 격리한 손상 DB 사본도 런타임 상태다 — 커밋 대상이 아니다.
    want = [".claude/harness/harness.db", ".claude/harness/harness.db-wal",
            ".claude/harness/harness.db.corrupt-*",
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
        fh.write(t("\n# step-seven-harness (런타임 상태 — 커밋하지 않는다)\n"))
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
        fh.write(("\n" if body else "") + t(AGENTS_BLOCK))
    return ["AGENTS.md (%s)" % (t("절차 안내 추가") if body else t("새로 만듦"))]


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
    # 읽고-쓰는 사이에 다른 `init` 이 넣을 수 있다. 동시 넷이 앵커를 세 번 겹쳐
    # 넣었다 — 이 파일은 세션마다 로드되므로 중복은 그대로 컨텍스트 낭비다.
    with open(cm, "a+", encoding="utf-8") as fh:
        fh.seek(0)
        now_lines = {ln.strip() for ln in fh.read().splitlines()}
        add = [a for a in add if a not in now_lines]
        if not add:
            return []
        fh.seek(0, os.SEEK_END)
        if fh.tell() and not body.endswith("\n"):
            fh.write("\n")
        fh.write("\n" + "\n".join(add) + "\n")
    return [t("CLAUDE.md (앵커 %d줄)") % len(add)]


def cli_init(argv):
    root = os.path.abspath(argv[0] if argv else os.getcwd())
    pr = plugin_root()
    created = install_templates(root, pr)
    db_made, lid = install_db(root, load_config(root, pr))
    created += db_made
    refresh_wrapper(root)

    nperm = ensure_permissions(root)
    if nperm > 0:
        created.append(t(".claude/settings.json (조회 명령 %d개 허용)") % nperm)
    elif nperm == -2:
        print(t("주의: .claude/settings.json 을 다른 쪽이 동시에 쓰고 있어 권한 허용을 "
              "건너뛰었다. 남의 변경을 덮지 않으려고 포기한 것이다 — `harness init` 을 "
              "다시 실행하면 된다."), file=sys.stderr)
    elif nperm < 0:
        print(t("주의: .claude/settings.json 을 읽을 수 없어 권한 허용을 건너뛰었다."),
              file=sys.stderr)

    created += install_gitignore(root)
    created += install_anchors(root)
    created += install_agents_md(root)
    label = None
    with swallow(t("설치 후 점검")):
        con2 = connect(root)
        if con2 is not None:
            try:
                sid2 = active_stage(con2, lid)
                if sid2:
                    label = label_of(load_config(root, pr), sid2)
            finally:
                con2.close()
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
        "options": [{"as": k, "why": v} for k, v in promote_as(ctx.cfg).items()],
        "decided": [dict(r) for r in
                    promotion_rows(ctx.con, order="at DESC", limit=8)],
    }


def render_promote(d):
    pend = d["pending"]
    print(t("승격 결정이 필요한 항목 (%d개)") % len(pend))
    if not pend:
        print(t("  (없음 — 여러 작업에서 반복된 항목이 아직 없다)"))
    for it in pend:
        mark = (t("  ← %s 로 승격했는데 다시 걸렸다") % it["regressed"]
                if it.get("regressed") else "")
        print(t("  %-34s ×%d, 작업 %d개%s")
              % (it["key"][:34], it["count"], it["loops"], mark))
    if pend:
        print(t("\n결정 방법 (하나 고른다):"))
        for o in d["options"]:
            flag = ("--decline --reason \"...\"" if o["as"] == "declined"
                    else "--as %s --note \"...\"" % o["as"])
            print("  harness promote <key> %-32s %s" % (flag, o["why"]))
    if d["decided"]:
        print(t("\n이미 결정된 항목"))
        for r in d["decided"]:
            print("  %-30s %-10s %-12s %s"
                  % (r["key"][:30], r["decision"], r["maturity"],
                     (r["note"] or "-")[:40]))


def render_init(root, created, lid, stage_label=None):
    print(t("하네스 설치 완료: %s") % root)
    for c in created:
        print("  + %s" % c)
    if not created:
        print(t("  (변경 없음 — 이미 설치되어 있다)"))
    # 단계를 **여기서** 말한다. 문서에 적어두면 단계 구성이 바뀔 때 뒤처지고,
    # 모델은 그 낡은 문장을 정확히 따라 틀린 말을 한다 — 실제로 그렇게 됐다
    # (0.10.0 에서 Selection 을 신설한 뒤에도 스킬 문서가 `1/6 Scaffolding` 이었다).
    print(t("활성 작업: %s%s") % (lid, t(" · 단계 %s") % stage_label if stage_label else ""))
    print(t("커밋 대상: .claude/harness/{POLICY.md,LEARNED.md,stages.json,rationale.md}, "
          "CLAUDE.md, AGENTS.md"))


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
    import gates as _gates
    GATE_LOAD_FAILS = _gates.register(sys.modules[__name__]) or []
except Exception as _exc:                      # noqa: BLE001 - 적재 실패도 사실이다
    GATE_LOAD_FAILS = [("gates", "%s: %s" % (type(_exc).__name__, _exc))]


if __name__ == "__main__":
    sys.exit(main())
