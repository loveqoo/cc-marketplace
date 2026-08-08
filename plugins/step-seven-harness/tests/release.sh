#!/bin/sh
# **출시 인수 시나리오.** 이 여덟이 통과하면 출시한다.
#   usage: sh plugins/step-seven-harness/tests/release.sh [repo-root]
#
# ## 왜 따로 있나 — smoke 와 무엇이 다른가
#
# `smoke.sh` 는 함수 하나하나가 옳은지 본다(766개). 그것이 전부 초록이어도 **이
# 플러그인을 한 번도 써 본 적은 없다.** 다섯 번의 적대적 리뷰가 찾은 것들도
# 대부분 "부분은 맞는데 이어 붙이면 안 되는" 종류였다. 그래서 여기서는 부분을
# 보지 않고 **한 사람이 이 도구로 일을 한 바퀴 하는 동안 일어나는 일**을 본다.
#
# 두 가지를 smoke 와 다르게 한다:
#
#   ① **선언된 문으로 들어간다.** smoke 는 `python3 harness.py hook` 을 직접
#      부른다 — `hooks/hooks.json` 이 깨져도 초록이다. 여기서는 `_door.py` 가
#      hooks.json 을 읽어 거기 적힌 명령으로 보낸다. matcher 에서 Bash 가 빠지면
#      ②·③이 무너진다.
#   ② **설치된 래퍼로 명령한다.** `.claude/harness/bin/harness` 다. 사용자와
#      모델이 실제로 치는 것이 그것이고, 그 안의 엔진 탐색 순서가 곧 신뢰 경계다.
#
# ## 순서는 시나리오 번호가 아니라 **작업의 순서**다
#
# 바닥·동의 시나리오를 한 바퀴 **안에서** 돌린다. 그래야 ⑤에서 "그때 겪은 마찰"과
# "지표가 말하는 마찰"을 같은 회차에 대고 비교할 수 있다. 밖에서 돌리면 회차
# 스냅샷에 안 잡혀 ⑤가 0 과 0 을 비교하는 공허한 검사가 된다.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_SRC="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 리포는 건드리지 않는다. 플러그인도 사본으로 쓴다 — ⑦이 플러그인을 고친다.
PLUG="$WORK/plugin"
P="$WORK/project"
cp -R "$PLUGIN_SRC" "$PLUG" || { echo "플러그인 사본을 만들지 못했다" >&2; exit 1; }
mkdir -p "$P"

PASS=0
FAIL=0
DENIES=0        # 문에서 실제로 거절당한 횟수. ⑤가 이 수를 쓴다.
SCENARIOS=0

ok()  { PASS=$((PASS + 1)); printf '    ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '    FAIL %s\n         %s\n' "$1" "$2"; }

eq() { # eq <라벨> <기대> <실제>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "기대 [$2] / 실제 [$3]"; fi
}

has() { # has <라벨> <있어야 할 것> <실제>
  case "$3" in
    *"$2"*) ok "$1" ;;
    *) bad "$1" "'$2' 가 없다 — 실제: $(printf '%s' "$3" | tr '\n' ' ' | cut -c1-200)" ;;
  esac
}

exists() { # exists <라벨> <경로>
  if [ -e "$2" ]; then ok "$1"; else bad "$1" "$2 가 없다"; fi
}

absent() { # absent <라벨> <경로>
  if [ -e "$2" ]; then bad "$1" "$2 가 남아 있다"; else ok "$1"; fi
}

# 판정 장치가 살아 있나. `eq` 를 무조건 통과로 바꿔도 일곱 시나리오가 전부 초록이
# 된다 — smoke 와 all.sh 가 같은 구멍으로 당했다(5회차 변이 테스트).
_canary() {
  p0=$PASS; f0=$FAIL
  eq "카나리아: 같다" a a
  eq "카나리아: 다르다" a b >/dev/null
  has "카나리아: 있다" b abc
  has "카나리아: 없다" z abc >/dev/null
  if [ "$PASS" -ne $((p0 + 2)) ] || [ "$FAIL" -ne $((f0 + 2)) ]; then
    printf '카나리아 실패 — 이 파일의 판정이 고장났다 (PASS %d→%d, FAIL %d→%d)\n' \
      "$p0" "$PASS" "$f0" "$FAIL" >&2
    exit 1
  fi
  PASS=$p0; FAIL=$f0
}

# ---------------------------------------------------------------------- 두 개의 문

door() { # door <이벤트> <matcher 대상> <입력 JSON>  → 훅 출력
  printf '%s' "$3" | python3 "$HERE/_door.py" "$PLUG" "$P" "$1" "$2"
}

# **문은 한 번만 두드린다.** 사유를 보려고 같은 입력을 다시 보냈더니 차단이 두 번
# 기록됐고, ⑤가 "겪은 마찰 8, 보고된 차단 10" 으로 어긋났다. 검사가 측정 대상을
# 바꾼 것이다 — 훅은 판정하면서 **기록도 하기 때문에** 두 번 부르면 안 된다.
LAST_OUT=""
LAST_RC=0

knock() { # knock <이벤트> <대상> <JSON>
  LAST_OUT="$(door "$1" "$2" "$3" 2>/dev/null)"
  LAST_RC=$?
}

field() { # field <decision|reason> — 마지막 두드림의 결과에서 꺼낸다
  printf '%s' "$LAST_OUT" | python3 -c '
import json, sys
want = sys.argv[1]
key = "permissionDecision" if want == "decision" else "permissionDecisionReason"
raw = sys.stdin.read().strip()
out = "-" if want == "decision" else ""
for cand in [raw] + raw.splitlines():
    cand = cand.strip()
    if not cand.startswith("{"):
        continue
    try:
        o = json.loads(cand)
    except ValueError:
        continue
    v = (o.get("hookSpecificOutput") or {}).get(key)
    if v:
        out = v.replace("\n", " ")
        break
print(out)' "$1"
}

# 판정과 종료코드를 함께 본다. 하네스는 통과시킬 때 아무 말도 하지 않는다
# (`permissionDecision` 자체가 없다). 그 침묵과 "훅이 죽어서 아무 말도 못 한 것" 은
# 출력이 똑같다 — 종료코드로만 갈린다.
verdict() { # verdict <라벨> <deny|ask|pass> <이벤트> <대상> <JSON>
  knock "$3" "$4" "$5"
  got="$LAST_RC:$(field decision)"
  case "$2" in
    pass) # 침묵이 통과다. 단 훅이 정상 종료했어야 한다.
      case "$got" in
        0:-|0:allow) ok "$1" ;;
        *) bad "$1" "통과여야 하는데 [$got] 다" ;;
      esac ;;
    *)
      eq "$1" "0:$2" "$got"
      case "$got" in *:deny) DENIES=$((DENIES + 1)) ;; esac ;;
  esac
  return 0
}

h() { # 설치된 래퍼로 명령한다
  (cd "$P" && "$P/.claude/harness/bin/harness" "$@")
}

jget() { # jget <점.경로>  ; JSON 은 stdin
  python3 -c '
import json, sys
d = json.load(sys.stdin)
for p in sys.argv[1].split("."):
    d = d[int(p)] if isinstance(d, list) else d[p]
print(d if isinstance(d, str) else json.dumps(d, ensure_ascii=False, sort_keys=True))' "$1"
}

st() { # 현재 status 의 한 필드
  h status --json 2>/dev/null | jget "$1"
}

pre_write() { # pre_write <상대경로>  → PreToolUse Write JSON
  python3 -c '
import json, os, sys
print(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write",
                  "tool_input": {"file_path": os.path.join(sys.argv[1], sys.argv[2]),
                                 "content": "내용\n"}}, ensure_ascii=False))' "$P" "$1"
}

post_write() { # post_write <상대경로>
  python3 -c '
import json, os, sys
print(json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Write",
                  "tool_input": {"file_path": os.path.join(sys.argv[1], sys.argv[2]),
                                 "content": "내용\n"},
                  "tool_response": {"success": True}}, ensure_ascii=False))' "$P" "$1"
}

pre_bash() { # pre_bash <명령>
  python3 -c '
import json, sys
print(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                  "tool_input": {"command": sys.argv[1]}}, ensure_ascii=False))' "$1"
}

post_bash() { # post_bash <명령>
  python3 -c '
import json, sys
print(json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                  "tool_input": {"command": sys.argv[1]},
                  "tool_response": {"stdout": "ok", "interrupted": False}},
                 ensure_ascii=False))' "$1"
}

# 문 자신이 살아 있나. 아무도 안 받는 이벤트를 보내면 **rc=9** 여야 한다.
# 여기서 조용히 빈 출력을 내면 판정 없음 = 허용으로 읽혀, hooks.json 을 통째로
# 비워도 이 파일의 `allow` 단정이 전부 통과한다.
_door_canary() {
  printf '{}' | python3 "$HERE/_door.py" "$PLUG" "$P" ZzzNoSuchEvent Zzz >/dev/null 2>&1
  if [ "$?" -ne 9 ]; then
    echo "문 카나리아 실패 — 아무도 안 받는 이벤트가 rc=9 가 아니다" >&2
    exit 1
  fi
}

scenario() { SCENARIOS=$((SCENARIOS + 1)); printf '\n%s\n' "$1"; }

# ====================================================================== ① 설치

s_install() {
  scenario "① 설치 — 빈 프로젝트에서 init 하면 게이트가 살아난다"

  out="$( (cd "$P" && python3 "$PLUG/scripts/harness.py" init) 2>&1 )"
  eq "init 이 rc=0" 0 "$?"
  has "무엇을 만들었는지 말한다" "harness.db" "$out"

  exists "래퍼가 있다" "$P/.claude/harness/bin/harness"
  if [ -x "$P/.claude/harness/bin/harness" ]; then ok "래퍼가 실행 가능하다"
  else bad "래퍼가 실행 가능하다" "+x 가 아니다"; fi
  exists "DB 가 있다" "$P/.claude/harness/harness.db"
  exists "기본 어휘 사본이 있다" "$P/.claude/harness/bin/defaults.json"
  exists "고칠 수 있는 규칙 파일이 있다" "$P/.claude/harness/stages.json"
  exists "모델이 읽을 안내를 남겼다" "$P/AGENTS.md"
  has "런타임 상태를 커밋 대상에서 뺐다" ".claude/harness/bin/" "$(cat "$P/.gitignore" 2>&1)"

  # 엔진 사본이 **원본과 같은 파일 집합**인가. 새 파일이 사본에서 빠지면 그 게이트가
  # 조용히 사라진다 — 이번 회차에 실제로 `parts/` 를 만들며 겪은 일이다.
  src="$(cd "$PLUG/scripts" && find . -name '*.py' | sort)"
  dst="$(cd "$P/.claude/harness/bin" && find . -name '*.py' | sort)"
  eq "엔진 사본이 원본과 같은 파일 집합" "$src" "$dst"

  # 조회 명령이 사전 승인됐나. 안 되면 모델이 매번 권한 프롬프트를 만나고,
  # 그러면 조회를 포기하고 파일을 직접 읽는 우회로 간다.
  perms="$(cat "$P/.claude/settings.json" 2>/dev/null)"
  has "settings.json 이 래퍼 조회를 허용한다" ".claude/harness/bin/harness status" "$perms"

  # **여기서부터가 진짜 질문이다** — 설치했으니 막히나.
  verdict "설치 직후 하네스 자신은 못 쓴다" deny \
    PreToolUse Write "$(pre_write .claude/harness/bin/harness)"

  # 자기검사는 전부 통과해야 한다. 개수를 정규식에 박지 않고 값을 센다.
  h status --json > "$WORK/st.json" 2>/dev/null
  bads="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
s = d.get("selftest") or []
print("%d/%d" % (sum(1 for x in s if x["ok"]), len(s)))' "$WORK/st.json")"
  total="${bads#*/}"
  eq "자기검사가 전부 통과" "$total/$total" "$bads"
  if [ "${total:-0}" -ge 30 ]; then ok "탐침이 $total 개 있다"
  else bad "탐침이 충분하다" "$total 개뿐이다 — 표에서 사라졌다"; fi

  eq "설치 직후 단계는 Selection" selection "$(st stage)"
}

# ================================================================== ② 바닥

s_floor() {
  scenario "② 바닥 — 엔진·DB·래퍼는 allow 로도 열리지 않는다"

  verdict "래퍼를 못 쓴다" deny PreToolUse Write "$(pre_write .claude/harness/bin/harness)"
  verdict "엔진 사본을 못 쓴다" deny PreToolUse Write "$(pre_write .claude/harness/bin/harness.py)"
  verdict "DB 를 못 쓴다" deny PreToolUse Write "$(pre_write .claude/harness/harness.db)"

  # 도구를 바꿔도 같아야 한다. Write 만 막고 Bash 를 열어두면 바닥이 아니다.
  verdict "셸 리다이렉트로도 못 쓴다" deny \
    PreToolUse Bash "$(pre_bash 'echo x > .claude/harness/bin/harness')"

  # **여기가 이 시나리오의 핵심이다.** 예외 승인을 받아도 바닥은 그대로여야 한다.
  h allow ".claude/harness/bin/**" --reason "출시 시나리오" >/dev/null 2>&1
  verdict "allow 를 받아도 래퍼는 못 쓴다" deny \
    PreToolUse Write "$(pre_write .claude/harness/bin/harness)"
  has "열리지 않는 이유를 말한다" "allow" "$(field reason)"

  # 구분력. 전부 막으면 게이트가 아니라 벽이다.
  verdict "작업 산출물은 막지 않는다" pass \
    PreToolUse Write "$(pre_write ".dev/plan/$(st prefix)메모.md")"
  return 0
}

# ================================================================== ③ 동의

s_consent() {
  scenario "③ 동의 — 결과가 무거운 명령은 사람에게 묻는다"

  W=".claude/harness/bin/harness"
  verdict "auto-skip on 은 묻는다" ask PreToolUse Bash "$(pre_bash "$W auto-skip on --reason \"바쁘다\"")"
  verdict "loop new 는 묻는다" ask PreToolUse Bash "$(pre_bash "$W loop new --reason \"딴 일\"")"
  verdict "allow 는 묻는다" ask PreToolUse Bash "$(pre_bash "$W allow \"docs/**\" --reason x")"
  verdict "approve-plan 은 묻는다" ask PreToolUse Bash "$(pre_bash "$W approve-plan p.md")"

  # **스킵은 여기서 묻지 않는다 — 거절한다.** Selection 은 건너뛸 수 없는 단계라
  # 동의를 구할 일 자체가 없다. "물어야 한다" 로 단정하면 이 구분이 사라진다.
  # (스킵할 수 있는 단계에서 정말 묻는지는 ④ 가 Scaffolding 에서 확인한다.)
  verdict "건너뛸 수 없는 단계의 skip 은 거절한다" deny \
    PreToolUse Bash "$(pre_bash "$W skip selection --reason \"바쁘다\"")"
  has "왜 못 건너뛰는지 말한다" "건너뛸 수 없다" "$(field reason)"

  # 껍데기를 씌워도 같아야 한다. 문자열 모양으로 판정하면 여기서 뚫린다.
  verdict "sh -c 로 감싸도 묻는다" ask \
    PreToolUse Bash "$(pre_bash "sh -c '$W loop new --reason x'")"
  # 모르면 통과가 아니라 물음이다.
  verdict "모르는 하위 명령도 묻는다" ask PreToolUse Bash "$(pre_bash "$W frobnicate")"
  # 구분력 — 조회는 묻지 않는다.
  verdict "status 는 묻지 않는다" pass PreToolUse Bash "$(pre_bash "$W status")"
  return 0
}

# ============================================================== ④ 한 바퀴

advance_to() { # advance_to <다음 단계 id> <라벨>
  h advance >"$WORK/adv.out" 2>&1
  rc=$?
  now="$(st stage)"
  if [ "$now" = "$1" ]; then
    ok "$2"
  else
    bad "$2" "rc=$rc, 단계=[$now] (기대 $1)  $(tr '\n' ' ' < "$WORK/adv.out" | cut -c1-200)"
  fi
}

s_cycle() {
  scenario "④ 한 바퀴 — Selection 에서 Compounding 까지 정직하게 완주한다"

  L0="$(st loop)"
  PFX="$(st prefix)"
  if [ -n "$PFX" ]; then ok "산출물 접두사를 알려준다 ($PFX)"
  else bad "산출물 접두사를 알려준다" "비어 있다"; fi

  # -- Selection: 무엇을 하는지, 무엇이 끝인지
  h advance >"$WORK/adv.out" 2>&1
  has "의도 없이는 진행을 거부한다" "intent_set" "$(cat "$WORK/adv.out")"
  h loop intent "출시 인수 시나리오를 통과시킨다" >/dev/null 2>&1
  h loop done-when "일곱 시나리오가 전부 통과한다" >/dev/null 2>&1
  eq "남은 종료 조건이 없다" "[]" "$(st exit_missing)"
  advance_to scaffolding "Selection → Scaffolding"

  # 여기는 건너뛸 수 있는 단계다. **그래서 묻는다** — ③에서 Selection 이 거절한
  # 것과 같은 명령이다. 묻기만 하고 실제로 건너뛰지는 않는다.
  verdict "건너뛸 수 있는 단계의 skip 은 묻는다" ask \
    PreToolUse Bash "$(pre_bash ".claude/harness/bin/harness skip scaffolding --reason \"할 게 없다\"")"

  advance_to context "Scaffolding → Context"
  advance_to planning "Context → Planning"

  # -- Planning: 계획 파일 + 사람의 승인
  PLAN=".dev/plan/${PFX}계획.md"
  verdict "계획 파일 쓰기는 허용된다" pass PreToolUse Write "$(pre_write "$PLAN")"
  mkdir -p "$P/.dev/plan" && printf '# 계획\n일곱 시나리오를 만든다.\n' > "$P/$PLAN"
  door PostToolUse Write "$(post_write "$PLAN")" >/dev/null 2>&1
  h advance >"$WORK/adv.out" 2>&1
  has "승인 없는 계획으로는 못 넘어간다" "plan_approved" "$(cat "$WORK/adv.out")"
  h approve-plan "$PLAN" >/dev/null 2>&1
  advance_to execution "Planning → Execution"

  # -- Execution: 검증 증거를 관측으로 쌓는다
  door PostToolUse Bash "$(post_bash 'python3 -m pytest -q')" >/dev/null 2>&1
  ev="$(st evidence)"
  has "테스트 실행이 증거로 잡혔다" "verification_evidence" "$ev"
  advance_to verification "Execution → Verification"

  eq "Verification 에 남은 조건이 없다" "[]" "$(st exit_missing)"
  advance_to compounding "Verification → Compounding"

  # -- Compounding: 회고 + 승격 결정
  RETRO=".dev/retrospect/${PFX}회고.md"
  verdict "회고 파일 쓰기는 허용된다" pass PreToolUse Write "$(pre_write "$RETRO")"
  mkdir -p "$P/.dev/retrospect" && printf '# 회고\n문으로 들어가니 보였다.\n' > "$P/$RETRO"
  door PostToolUse Write "$(post_write "$RETRO")" >/dev/null 2>&1
  eq "Compounding 에 남은 조건이 없다" "[]" "$(st exit_missing)"

  # 마지막 단계에서는 **스스로 갈래를 골라야 한다** — 그냥 advance 는 거부다.
  h advance >"$WORK/adv.out" 2>&1
  eq "마지막 단계의 맨 advance 는 거부" 1 "$?"
  has "두 갈래를 제시한다" "--done" "$(cat "$WORK/adv.out")"

  h advance --done >"$WORK/adv.out" 2>&1
  eq "advance --done 이 rc=0" 0 "$?"
  eq "새 작업의 Selection 으로 돌아왔다" selection "$(st stage)"
  L1="$(st loop)"
  if [ -n "$L1" ] && [ "$L1" != "$L0" ]; then ok "작업 해시가 바뀌었다 ($L0 → $L1)"
  else bad "작업 해시가 바뀌었다" "$L0 그대로다"; fi
  return 0
}

# ============================================================== ⑤ 측정

s_metrics() {
  scenario "⑤ 측정 — 지표가 방금 겪은 마찰과 같은 수를 말한다"

  h metrics --json > "$WORK/m.json" 2>/dev/null
  eq "metrics 가 rc=0" 0 "$?"
  eq "닫힌 회차가 1개" 1 "$(jget cycles < "$WORK/m.json")"
  if [ "$(jget loops < "$WORK/m.json")" -ge 2 ]; then ok "작업이 2개 이상 기록됐다"
  else bad "작업이 2개 이상 기록됐다" "$(jget loops < "$WORK/m.json")개"; fi

  # **이 줄이 이 시나리오다.** 문에서 실제로 거절당한 횟수와 보고서의 차단 수가
  # 같아야 한다. 대리 지표를 쓰지 않는다 — 우리가 겪은 것을 센다.
  blocks="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
b = d["buckets"]
print(int(round(sum(x["avg"]["blocks"] for x in b))) if b else -1)' "$WORK/m.json")"
  eq "차단 수가 실제 거절 횟수와 같다" "$DENIES" "$blocks"
  if [ "$DENIES" -ge 4 ]; then ok "비교할 마찰이 실제로 있었다 ($DENIES 건)"
  else bad "비교할 마찰이 실제로 있었다" "$DENIES 건뿐 — 0 과 0 을 비교할 뻔했다"; fi
  return 0
}

# ============================================================== ⑥ 복구

s_recover() {
  scenario "⑥ 복구 — DB 가 깨져도 init 으로 돌아온다"

  DB="$P/.claude/harness/harness.db"
  printf 'SQLite format 3\000 이 뒤로는 쓰레기다' > "$DB"

  out="$(h status 2>&1)"
  case "$out" in
    *Traceback*) bad "깨진 DB 에서 죽지 않는다" "역추적이 나왔다" ;;
    *) ok "깨진 DB 에서 죽지 않는다" ;;
  esac

  out="$( (cd "$P" && python3 "$PLUG/scripts/harness.py" init) 2>&1 )"
  eq "init 이 rc=0" 0 "$?"
  exists "깨진 DB 를 격리했다" "$DB.corrupt-1"
  exists "DB 를 다시 만들었다" "$DB"
  eq "다시 Selection 에서 시작한다" selection "$(st stage)"

  # 복구가 곧 게이트 복구여야 한다. 파일만 생기고 판정이 죽어 있으면 최악이다.
  verdict "복구 후에도 바닥이 막는다" deny \
    PreToolUse Write "$(pre_write .claude/harness/harness.db)"
  return 0
}

# ============================================================== ⑦ 갱신

s_upgrade() {
  scenario "⑦ 갱신 — 플러그인이 바뀌면 사본도 바뀐다 (없어진 것은 사라진다)"

  MARK="$PLUG/scripts/parts/zz_mark.py"
  COPY="$P/.claude/harness/bin/parts/zz_mark.py"

  printf 'ZZ_MARK = "v2"\n' > "$MARK"
  (cd "$P" && python3 "$PLUG/scripts/harness.py" init) >/dev/null 2>&1
  exists "새 엔진 파일이 사본에 들어왔다" "$COPY"

  rm -f "$MARK"
  (cd "$P" && python3 "$PLUG/scripts/harness.py" init) >/dev/null 2>&1
  # 이것이 없으면 **삭제된 게이트가 사본에서 영원히 돈다** (4회차 D-M9).
  absent "없어진 엔진 파일은 사본에서도 사라진다" "$COPY"

  eq "갱신 후에도 자기검사가 산다" selection "$(st stage)"
  verdict "갱신 후에도 바닥이 막는다" deny \
    PreToolUse Write "$(pre_write .claude/harness/bin/harness)"
  return 0
}

# ============================================================== ⑧ 중간 그래프

s_graph() {
  scenario "⑧ 중간 그래프 — 가감한 예시 그래프로 한 바퀴, 회차 중 자란 노드를 지나간다"

  # 설계 합의(.dev/plan/dynamic-middle-graph.md)의 끝 조건 ②와 ④가 이 시나리오다:
  # 중간 노드를 **가감한** 설정이 한 바퀴를 완주하고, 회차 진행 중 `path add` 로
  # 자란 노드를 실제로 지나간다.
  P2="$WORK/graph"
  mkdir -p "$P2"
  (cd "$P2" && python3 "$PLUG/scripts/harness.py" init) >/dev/null 2>&1
  python3 - "$P2/.claude/harness/stages.json" <<'PY'
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
sts = [s for s in cfg["stages"] if s["id"] != "context"]        # 하나 뺀다
idx = [i for i, s in enumerate(sts) if s["id"] == "planning"][0]
sts.insert(idx, {"id": "research", "label": "Research", "summary": "조사",
                 "write": ["dev"], "exit_criteria": [], "stop_requires": []})
cfg["stages"] = sts                                             # 하나 더한다
json.dump(cfg, open(p, "w"), ensure_ascii=False, indent=2)
PY
  h2() { (cd "$P2" && "$P2/.claude/harness/bin/harness" "$@"); }
  st2() { h2 status --json 2>/dev/null | jget "$1"; }

  eq "가감한 그래프에서 시작한다" selection "$(st2 stage)"
  h2 loop intent "중간 그래프 한 바퀴" >/dev/null 2>&1
  h2 loop done-when "여덟 번째 시나리오가 통과한다" >/dev/null 2>&1
  h2 advance >/dev/null 2>&1                       # Scaffolding

  # 회차 진행 중 그래프가 앞쪽으로 자란다 — 추가는 동의 없이 기록만 남는다.
  has "회차 중 노드를 더한다" "probe-step" \
    "$(h2 path add probe-step --after research --reason '검증 앞 실험')"

  h2 advance >/dev/null 2>&1
  eq "더한 설정 노드에 들어선다 (뺀 노드는 지나가지 않는다)" research "$(st2 stage)"
  h2 advance >/dev/null 2>&1
  eq "회차 중 자란 노드를 지나간다" probe-step "$(st2 stage)"
  h2 advance >/dev/null 2>&1                       # Planning

  PFX2="$(st2 prefix)"
  mkdir -p "$P2/.dev/plan" "$P2/.dev/retrospect"
  printf '# 계획\n중간 그래프 검증.\n' > "$P2/.dev/plan/${PFX2}p.md"
  h2 approve-plan ".dev/plan/${PFX2}p.md" >/dev/null 2>&1
  h2 advance >/dev/null 2>&1                       # Execution
  printf 'check:\n\t@true\n' > "$P2/Makefile"
  has "검증은 하네스가 직접 돌린다" "검증 통과" "$(h2 verify -- make check 2>&1)"
  h2 advance >/dev/null 2>&1                       # Verification
  h2 advance >/dev/null 2>&1                       # Compounding
  eq "가감한 그래프로 Compounding 까지 왔다" compounding "$(st2 stage)"

  printf '# 회고\n그래프가 자라고 닫혔다.\n' > "$P2/.dev/retrospect/${PFX2}r.md"
  h2 advance --cycle >/dev/null 2>&1
  eq "advance --cycle 로 다음 회차가 열린다" scaffolding "$(st2 stage)"
  case "$(h2 path 2>/dev/null)" in
    *probe-step*) bad "회차가 닫히면 자란 노드가 사라진다" "probe-step 이 남아 있다" ;;
    *) ok "회차가 닫히면 자란 노드가 사라진다" ;;
  esac
  has "자란 이력은 기록으로 남는다" "probe-step" "$(python3 -c "
import sqlite3, sys
rows = sqlite3.connect(sys.argv[1]).execute(
  \"SELECT rule FROM event WHERE kind='path_add'\").fetchall()
print(','.join(r[0] for r in rows))" "$P2/.claude/harness/harness.db")"
  return 0
}

# ====================================================================== 실행

echo "== 출시 인수 시나리오"
_canary
_door_canary

s_install
s_floor
s_consent
s_cycle
s_metrics
s_recover
s_upgrade
s_graph

echo
if [ "$SCENARIOS" -ne 8 ]; then
  printf '시나리오를 %d개밖에 돌리지 않았다 (8개여야 한다)\n' "$SCENARIOS" >&2
  exit 1
fi
printf '시나리오 %d개, 단정 %d개 통과, %d개 실패\n' "$SCENARIOS" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
echo "출시 조건 충족."
