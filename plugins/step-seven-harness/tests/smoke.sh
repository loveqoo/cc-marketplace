#!/bin/sh
# step-seven-harness 스모크 테스트.
#   usage: sh plugins/step-seven-harness/tests/smoke.sh
# 임시 프로젝트를 만들고 훅 이벤트 JSON 을 엔진에 먹여 판정을 검증한다.
set -e

ENGINE="$(cd "$(dirname "$0")/.." && pwd)/scripts/harness.py"
export ENGINE_PATH="$ENGINE"
# 리포 루트. **여기서 정의한다** — 예전에는 첫 사용(ctx_check)보다 170줄 뒤에
# 정의돼 있어서 그때까지는 빈 문자열이었다. 빈 인자를 받은 검사기는 cwd 를 리포
# 루트로 삼았으므로, **리포 루트에서 실행할 때만 우연히 통했다.** 다른 디렉터리에서
# 돌리면 검사기가 통째로 죽고 `set -e` 가 스위트를 끊었다 — 5차 리뷰 도중 발견했다.
MC="$(cd "$(dirname "$0")/../../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PASS=0
FAIL=0

hook() { printf '%s' "$1" | CLAUDE_PROJECT_DIR="$WORK" python3 "$ENGINE" hook; }
cli() { (cd "$WORK" && python3 "$ENGINE" "$@"); }

check() { # check <label> <expect-substring> <actual>
  if printf '%s' "$3" | grep -q "$2"; then
    PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"
  else
    FAIL=$((FAIL + 1)); printf '  FAIL %s\n     기대: %s\n     실제: %s\n' "$1" "$2" "$3"
  fi
}

check_empty() { # check_empty <label> <actual>
  if [ -z "$2" ]; then
    PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"
  else
    FAIL=$((FAIL + 1)); printf '  FAIL %s\n     기대: (출력 없음)\n     실제: %s\n' "$1" "$2"
  fi
}

# **판정 장치가 살아 있나.** 5회차 변이 테스트: `check()` 를 무조건 PASS 로
# 바꿔도, 마지막 `[ "$FAIL" -eq 0 ]` 을 지워도 `766 passed, 0 failed` + 상위
# `all.sh` 는 `전부 통과` 였다. 766개 단정이 전부 죽어도 초록이다.
#
# 검사를 검사하는 것은 검사밖에 없다. 반드시 실패해야 하는 것과 반드시 통과해야
# 하는 것을 하나씩 넣고, 그 결과가 기대와 다르면 **여기서 멈춘다.**
_canary() {
  local p0=$PASS f0=$FAIL
  check "카나리아: 통과해야 한다" 'abc' 'xxabcxx'
  check "카나리아: 실패해야 한다" 'NEVER-MATCHES-THIS' 'zzz' >/dev/null
  check_empty "카나리아: 빈 것" ""
  check_empty "카나리아: 안 빈 것" "x" >/dev/null
  if [ "$PASS" -ne $((p0 + 2)) ] || [ "$FAIL" -ne $((f0 + 2)) ]; then
    printf '카나리아 실패 — check()/check_empty() 의 판정이 고장났다 (PASS %d→%d, FAIL %d→%d)\n' \
      "$p0" "$PASS" "$f0" "$FAIL" >&2
    exit 1
  fi
  PASS=$p0; FAIL=$f0        # 카나리아는 집계에 넣지 않는다
}
_canary

# JSON 경로의 값을 정확히 비교한다. 정규식이 아니므로 "무엇이든 통과"가 불가능하다.
jq1() { python3 -c "
import json,sys
d=json.load(sys.stdin)
for p in sys.argv[1].split('.'):
    d = d[int(p)] if isinstance(d, list) else d[p]
print(json.dumps(d, ensure_ascii=False, sort_keys=True))
" "$1"; }

jcheck() { # jcheck <label> <json-path> <expected-json> <json-text>
  actual="$(printf '%s' "$4" | jq1 "$2" 2>/dev/null)"
  if [ "$actual" = "$3" ]; then
    PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"
  else
    FAIL=$((FAIL + 1)); printf '  FAIL %s\n     경로: %s\n     기대: %s\n     실제: %s\n' \
      "$1" "$2" "$3" "$actual"
  fi
}

check_absent() { # check_absent <label> <must-not-appear> <actual>
  if printf '%s' "$3" | grep -q "$2"; then
    FAIL=$((FAIL + 1)); printf '  FAIL %s\n     나오면 안 되는 것: %s\n     실제: %s\n' "$1" "$2" "$3"
  else
    PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"
  fi
}

# 자기검사 결과를 **값으로** 확인한다. 개수를 정규식에 박으면 안 된다.
#
# 예전에는 `자기검사: 1[0-9]*/1[0-9]* 통과` 였고, 그 정규식은 `18/19`(=1건 실패)에도
# 맞았다 — 라벨은 "전부 통과한다" 인데 **부분 통과를 통과로 셌다.** 탐침을 19개에서
# 22개로 늘리자 드러났다. 대리 지표(정규식)를 버리고 값을 꺼내 비교한다.
# 탐침이 몇 개여야 하는지 **엔진에서 세어 온다.** 파일에 박으면 탐침이 늘 때마다
# 사람이 기대값을 고치게 되고, 그 습관이 진짜 회귀도 함께 고친다.
#
# 예전에는 이 주석만 있고 `SF_MIN=0` 이었다 — `total < 0` 은 영원히 거짓이라
# **바닥이 죽어 있었다.** 탐침을 42개에서 10개로 줄여도 다섯 자리가 전부
# 통과했다(4회차 E-F8). 주석이 코드보다 앞서 있으면 그 주석은 거짓말이다.
# 게이트 수 × 2 는 자기증명 규칙의 하한이다(게이트마다 막는 탐침 + 통과 탐침).
SF_MIN="$(python3 -c '
import os, sys
sys.path.insert(0, os.path.dirname(os.environ["ENGINE_PATH"]))
import harness as h
print(len(h.GATES) * 2)' 2>/dev/null || echo 0)"
[ "${SF_MIN:-0}" -ge 2 ] || { echo "탐침 하한을 엔진에서 세지 못했다"; exit 1; }

# 기본 쓰기 규칙 개수도 **템플릿에서 세어 온다.** 파일에 박으면 규칙이 늘 때마다
# 사람이 기대값을 고치게 되고, 그 습관이 진짜 회귀도 함께 고친다 — SF_MIN 과 같은
# 이유다(규칙 하나를 더하자 네 자리가 한꺼번에 빨개졌다).
NRULES="$(python3 -c '
import json, os, sys
p = os.path.join(os.path.dirname(os.environ["ENGINE_PATH"]), "..", "templates", "stages.json")
print(len(json.load(open(p, encoding="utf-8"))["write_rules"]))' 2>/dev/null || echo 0)"
[ "${NRULES:-0}" -ge 1 ] || { echo "쓰기 규칙 개수를 템플릿에서 세지 못했다"; exit 1; }

check_selftest() { # <label> <기대-실패수> <status 출력>
  local OUT
  OUT="$(printf '%s' "$3" | SF_WANT="$2" SF_MIN="$SF_MIN" python3 -c '
import os, re, sys
want = int(os.environ["SF_WANT"])
m = re.search(r"자기검사: (\d+)/(\d+) 통과", sys.stdin.read())
if not m:
    print("자기검사 줄이 없다")
else:
    ok, total = int(m.group(1)), int(m.group(2))
    floor = int(os.environ.get("SF_MIN", "0"))
    if total < floor:
        print("탐침이 %d개뿐이다 (최소 %d) — 표에서 사라졌다"
              % (total, floor))
    elif total - ok != want:
        print("실패 %d개, 기대 %d개 (%d/%d)" % (total - ok, want, ok, total))
    else:
        print("OK")')"
  if [ "$OUT" = "OK" ]; then
    PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"
  else
    FAIL=$((FAIL + 1)); printf '  FAIL %s\n     %s\n' "$1" "$OUT"
  fi
}

check_no_prefix_complaint() { # <label> <hook-output>
  if printf '%s' "$2" | grep -q '말머리는'; then
    FAIL=$((FAIL + 1)); printf '  FAIL %s\n     말머리 불만이 나왔다: %s\n' "$1" "$2"
  else
    PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"
  fi
}

sql() { python3 - "$WORK/.claude/harness/harness.db" "$1" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
q = sys.argv[2]
if q.count(";") > 1:
    con.executescript(q)   # execute 는 여러 구문을 거부한다
else:
    for row in con.execute(q):
        print("|".join("" if v is None else str(v) for v in row))
con.commit()
PY
}
loopid() { sql "SELECT v FROM meta WHERE k='head'"; }
setstage() {
  sql "UPDATE stage SET status='pending' WHERE status='active'" >/dev/null
  sql "UPDATE stage SET status='active' WHERE stage='$1'" >/dev/null
}

W() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s"}}' "$WORK" "$1"; }
B() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","permission_mode":"%s","tool_name":"Bash","tool_input":{"command":"%s"}}' "$WORK" "$2" "$1"; }
STOP() { printf '{"hook_event_name":"Stop","cwd":"%s","prompt_id":"%s","last_assistant_message":"%s"}' "$WORK" "$1" "$2"; }

echo "== 미설치 프로젝트에서는 조용해야 한다"
check_empty "DB 없으면 무출력" "$(hook "$(W docs/x.md)")"

echo "== init"
cli init >/dev/null
LID="$(loopid)"
check "CLAUDE.md 앵커 1줄" '^@\.claude/harness/POLICY\.md$' "$(cat "$WORK/CLAUDE.md")"
check "루프 해시 형식 YYMMDD-xxxxxx" '^[0-9]\{6\}-[0-9a-f]\{6\}$' "$LID"
check "gitignore 에 db·wal·shm" 'harness\.db-wal' "$(cat "$WORK/.gitignore")"
check "래퍼 생성" '^ok$' \
  "$([ -x "$WORK/.claude/harness/bin/harness" ] && echo ok || echo no)"
check "7단계 모두 등록" '^7$' "$(sql 'SELECT COUNT(*) FROM stage')"

echo "== 사용법 안내"
check "인자 없이 실행하면 명령 목록" 'auto-skip on' "$(python3 "$ENGINE")"
check "help 도 같은 목록" 'approve-plan' "$(python3 "$ENGINE" help)"
check "승인 필요 명령을 표시" '사용자 승인 다이얼로그가 뜬다' "$(python3 "$ENGINE" --help)"
check "외울 필요 없다고 안내" '외울 필요는 없다' "$(python3 "$ENGINE" help)"
check "오타는 help 로 안내" 'harness help' "$(cli statuss 2>&1 || true)"
check "하네스 밖에서도 help 동작" 'step-seven-harness' "$(cd / && python3 "$ENGINE" help)"

echo "== 작업 선정 (Selection)"
check "작업 미정을 알린다" '작업 내용: (미정)' "$(cli status)"
check "작업을 기록하지 않으면 Selection 을 끝낼 수 없다" 'intent_set' \
  "$(cli advance || true)"
check "거부 시 방법을 알려준다" 'harness loop intent' "$(cli advance || true)"
check "작업을 기록한다" 'src/auth.ts 토큰 갱신' \
  "$(cli loop intent 'src/auth.ts 토큰 갱신 수정')"
check "status 에 작업이 보인다" '작업 내용: src/auth.ts' "$(cli status)"
check "사유 없는 intent 는 사용법 안내" '사용법' "$(cli loop intent || true)"
check "조회 명령이 미리 허용된다" 'harness recall' \
  "$(python3 -c "import json;print(json.load(open('$WORK/.claude/settings.json'))['permissions']['allow'])")"
# 예전에는 `harness skip` 과 `auto-skip on` **둘만** 셌다 — 동의 명령은 여섯 종류인데
# 넷은 세지 않았으므로, 그 넷이 사전 승인 목록에 새어 들어가도 통과했다(5차 리뷰).
# 목록을 이 파일에 박지 않고 **설정에서 읽어** 전수로 센다. 종류가 늘면 자동으로 센다.
CONSENT_LEAK="$(python3 - "$WORK" "$(dirname "$ENGINE")" <<'PYC'
import json, os, sys
sys.path.insert(0, sys.argv[2])
import harness as h
root = sys.argv[1]
cfg = h.load_config(root, os.path.dirname(sys.argv[2]))

# 동의 명령의 **위험한 실제 호출**. 부분 문자열로 세면 `auto-skip status`(읽기 전용,
# 의도적으로 허용) 가 `auto-skip` 에 걸려 오탐한다. 그래서 무엇이 허용되면 안 되는지를
# 호출 형태로 적는다. consent 종류가 늘면 여기 없다는 것이 아래에서 드러난다.
DANGER = {
    "skip": "skip --reason 아무거나",
    "allow": "allow docs/** --reason 아무거나",
    "approve-plan": "approve-plan .dev/plan/p.md",
    "loop new": "loop new",
    "loop adopt": "loop adopt deadbeef",
    "auto-skip": "auto-skip on",
}
subs = sorted(h.consent_map(cfg))
missing = [su for su in subs if su not in DANGER]

allow = json.load(open(os.path.join(root, ".claude", "settings.json"),
                       encoding="utf-8"))["permissions"]["allow"]


def grants(entry, cmd):
    """`allow` 항목이 이 명령을 승인하나.

    Claude Code 의 규칙 두 가지만 본다: `Bash(x)` 는 **정확히** x, `Bash(x:*)` 는
    x 로 시작하는 것 전부. 그 이상은 모델링하지 않는다 — 남의 매처를 재구현하기
    시작하면 근사가 쌓인다.
    """
    if not (entry.startswith("Bash(") and entry.endswith(")")):
        return False
    body = entry[5:-1]
    full = h.WRAPPER_CMD + " " + cmd
    if body.endswith(":*"):
        pre = body[:-2]
        return full == pre or full.startswith(pre + " ")
    return full == body


leaked = [su for su in subs if su in DANGER
          and any(grants(e, DANGER[su]) for e in allow)]
print("종류 %d개 검사; 표 누락 %d개; 새어나간 것 %d개: %s"
      % (len(subs), len(missing), len(leaked), ", ".join(leaked + missing) or "없음"))
PYC
)"
check "동의 명령을 전수로 센다" '종류 [1-9][0-9]* *개 검사' "$CONSENT_LEAK"
check "위험 호출 표에 빠진 종류가 없다" '표 누락 0개' "$CONSENT_LEAK"
check "동의 필요 명령은 하나도 사전 승인되지 않는다" '새어나간 것 0개' "$CONSENT_LEAK"
check "작업만 기록해서는 Selection 을 끝낼 수 없다" 'acceptance' "$(cli advance || true)"
check "완료 조건 미정을 알린다" '완료 조건: (미정)' "$(cli status)"
check "완료 조건을 기록한다" '테스트 전부 통과' \
  "$(cli loop done-when '테스트 전부 통과' '응답 200ms 이하')"
check "입력 순서를 보존한다" '1\. 테스트 전부 통과' "$(cli loop done-when)"
check "status 에 완료 조건이 보인다" '완료 조건 (2개)' "$(cli status)"
check "둘 다 기록하면 종료 조건이 충족된다" '충족 intent_set, acceptance' "$(cli status)"

echo "== 엔진 사본이 프로젝트 안에 있다"
check "엔진 사본 생성" 'harness\.py' "$(ls "$WORK/.claude/harness/bin/")"
# 라벨이 거꾸로였다. 래퍼는 **플러그인 원본을 먼저** 쓴다 — 사본이 먼저면 사본을
# 덮는 것이 곧 사전 승인된 임의 코드 실행이다. 사본은 마지막 폴백일 뿐이다.
check "래퍼가 사본을 마지막 폴백으로만 쓴다" \
  'if \[ ! -f "\$P" \]; then P="\$D/harness\.py"; fi' \
  "$(cat "$WORK/.claude/harness/bin/harness")"
check "래퍼가 플러그인 원본을 먼저 가리킨다" 'P="[^$][^"]*/scripts/harness\.py"' \
  "$(cat "$WORK/.claude/harness/bin/harness")"
check "사본으로 실행된다" '단계 1/7 Selection' \
  "$(cd "$WORK" && ./.claude/harness/bin/harness status | grep '단계')"

echo "== 하네스 자신은 수정할 수 없다"
check "엔진 사본 쓰기 차단" '하네스 자신은 수정할 수 없다' \
  "$(hook "$(W .claude/harness/bin/harness.py)")"
check "래퍼 쓰기 차단" '하네스 자신은 수정할 수 없다' \
  "$(hook "$(W .claude/harness/bin/harness)")"
check "DB 쓰기 차단 (손상시키면 게이트가 꺼진다)" '하네스 자신은 수정할 수 없다' \
  "$(hook "$(W .claude/harness/harness.db)")"
# 열지 못하는 예외는 **등록 자체가 거절된다.** 예전에는 성공을 출력하고도 다음
# 쓰기가 그대로 막혔고, 소모되지 않은 예외만 DB 에 남았다 (현장 보고 §1).
check "바닥값에는 예외를 등록조차 못 한다" '이 예외는 그 차단을 열지 못한다' \
  "$(cli allow '.claude/harness/bin/**' --reason '엔진 수정 시도' 2>&1 || true)"
check "거절 사유로 그 차단의 메시지를 그대로 보여준다" '하네스 자신은 수정할 수 없다' \
  "$(cli allow '.claude/harness/bin/**' --reason '엔진 수정 시도' 2>&1 || true)"
check "예외 등록으로도 열리지 않는다" '하네스 자신은 수정할 수 없다' \
  "$(hook "$(W .claude/harness/bin/harness.py)")"

echo "== Selection 은 .dev 만 쓸 수 있다"
check "Selection 에서 소스 쓰기 차단" 'Selection' "$(hook "$(W src/a.py)")"
check_empty "Selection 에서 .dev 쓰기 허용" "$(hook "$(W ".dev/plan/$LID-1-cands.md")")"

echo "== 폴더 가드"
setstage scaffolding
check "docs/ 쓰기 차단" '"permissionDecision": "deny"' "$(hook "$(W docs/spec/001-a.md)")"
check_empty "Scaffolding 에서 신규 최상위 폴더 허용" "$(hook "$(W src/a.py)")"
check ".dev 하위 폴더 규칙 위반 차단" '규칙 위반' "$(hook "$(W .dev/nope/a.md)")"
check ".dev 깊은 경로도 폴더 규칙 적용" '규칙 위반' "$(hook "$(W .dev/nope/deep/a.md)")"
check_empty ".dev 직속 누적 문서는 허용" "$(hook "$(W .dev/INDEX.md)")"
check_empty "누적 폴더 안 INDEX.md 는 접두사 면제" "$(hook "$(W .dev/retrospect/INDEX.md)")"
check_empty "README.md 도 접두사 면제" "$(hook "$(W .dev/learning/README.md)")"
check "면제 대상이 아닌 파일은 여전히 접두사 필요" '회차>-. 로 시작' \
  "$(hook "$(W .dev/retrospect/362-foo.md)")"

echo "== 루프 해시 파일명 강제"
check "해시 접두사 없으면 차단" '회차>-. 로 시작' "$(hook "$(W .dev/plan/my-plan.md)")"
check "차단 시 올바른 이름 제시" "$LID-1-my-plan.md" "$(hook "$(W .dev/plan/my-plan.md)")"
check_empty "해시 접두사 있으면 허용" "$(hook "$(W ".dev/plan/$LID-1-my-plan.md")")"
check_empty "scratch 는 해시 강제 안 함" "$(hook "$(W .dev/scratch/tmp.txt)")"

echo "== 단계 게이트"
setstage planning
check "Planning 에서 소스 쓰기 차단" 'Planning' "$(hook "$(W src/a.py)")"
check_empty "Planning 에서 .dev 쓰기 허용" "$(hook "$(W ".dev/plan/$LID-1-b.md")")"
setstage execution
mkdir -p "$WORK/src"
check_empty "Execution 에서 기존 소스 폴더 허용" "$(hook "$(W src/a.py)")"
check "Execution 에서 신규 최상위 폴더 차단" '신규 최상위 폴더' "$(hook "$(W newdir/a.py)")"

echo "== 스킵 동의 게이트"
check "사유 없는 skip 거부" '"permissionDecision": "deny"' \
  "$(hook "$(B '.claude/harness/bin/harness skip verification' default)")"
check "사유 있는 skip 은 ask" '"permissionDecision": "ask"' \
  "$(hook "$(B '.claude/harness/bin/harness skip verification --reason \"문서 작업이라 불필요\"' default)")"
check "ask 에 사유 노출" '문서 작업이라 불필요' \
  "$(hook "$(B '.claude/harness/bin/harness skip verification --reason \"문서 작업이라 불필요\"' default)")"
check "bypassPermissions 는 사전 승인으로 통과" '"permissionDecision": "defer"' \
  "$(hook "$(B '.claude/harness/bin/harness skip verification --reason \"x\"' bypassPermissions)")"
check_empty "status 는 통과" "$(hook "$(B '.claude/harness/bin/harness status' default)")"

echo "== 결함 A 회귀: 완료한 단계를 스킵으로 기록하지 않는다"
SKIPOUT="$(cli skip verification --reason '문서 작업이라 검증 불필요')"
check "Execution 은 done" '^done$' "$(sql "SELECT status FROM stage WHERE stage='execution'")"
check "Verification 은 skipped" '^skipped$' "$(sql "SELECT status FROM stage WHERE stage='verification'")"
check "스킵 사유가 잘리지 않는다" '문서 작업이라 검증 불필요' "$(cli status)"
check "Compounding 진입 시 스킵을 회고용으로 노출" '회고에 사유와 함께 기록' "$SKIPOUT"

echo "== Compounding 은 건너뛸 수 없다"
setstage planning
check "skip compounding 거부" '건너뛸 수 없다' \
  "$(cli skip compounding --reason '회고 생략' || true)"
check "거부 시 대안을 제시한다" 'skip until:compounding' \
  "$(cli skip compounding --reason '회고 생략' || true)"
check "compounding 을 지나치는 +N 도 거부" '건너뛸 수 없다' \
  "$(cli skip +6 --reason '전부 생략' || true)"
check "단계가 유지된다" 'Planning' "$(cli status | grep '단계')"

echo "== 스킵은 승인을 면제하지만 기록을 면제하지 않는다"
check "계획 없이 Planning 스킵 거부" '기록은 남겨야 한다' \
  "$(cli skip planning --reason '무인 실행 중' || true)"
check "무엇을 남겨야 하는지 알려준다" '\.dev/plan/' \
  "$(cli skip planning --reason '무인 실행 중' || true)"
PLID="$(loopid)"
mkdir -p "$WORK/.dev/plan"
hook "$(printf '{"hook_event_name":"PostToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":".dev/plan/%s-1-p.md"}}' "$WORK" "$PLID")" >/dev/null
check "기록을 남기면 스킵 통과" 'planning' "$(cli skip planning --reason '승인만 면제')"
setstage planning

echo "== 루프 중단 패턴: skip until:compounding → 회고 → 새 루프"
ABORT="$(cli skip until:compounding --reason 'Planning에서 구조 선행 필요를 발견')"
check "Compounding 까지 이동" '7/7 Compounding' "$ABORT"
check "Compounding 자신은 스킵되지 않는다" '^pending$\|^active$' \
  "$(sql "SELECT status FROM stage WHERE stage='compounding'")"
check "Compounding 은 맨손 advance 를 거부하고 두 갈래를 제시" 'advance --done' \
  "$(cli advance || true)"
check "회고 없이는 작업을 닫을 수 없다" 'retro_file' "$(cli advance --done || true)"
ALID="$(loopid)"
mkdir -p "$WORK/.dev/retrospect"
hook "$(printf '{"hook_event_name":"PostToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":".dev/retrospect/%s-1-abort.md"}}' "$WORK" "$ALID")" >/dev/null
NEWOUT="$(cli advance --done)"
check "회고 후 작업이 닫히고 새 작업이 시작된다" '새 작업' "$NEWOUT"
check "새 작업은 Selection 부터" '1/7 Selection' "$NEWOUT"
check "중단한 작업의 스킵 사유가 event 에 남는다" '구조 선행 필요' \
  "$(sql "SELECT detail FROM event WHERE kind='skip' AND loop_id='$ALID' LIMIT 1")"

echo "== 완료 조건 제시"
cli loop done-when '유지되는지 확인할 조건' >/dev/null
setstage execution
check "Verification 진입 시 완료 조건을 밀어준다" '이 작업의 완료 조건' "$(cli advance)"

echo "== 회차 반복: advance --cycle"
cli loop intent '회차 테스트' >/dev/null
setstage selection; cli advance >/dev/null   # selection 을 done 으로 만든다
setstage compounding
CLID="$(loopid)"
hook "$(printf '{"hook_event_name":"PostToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":".dev/retrospect/%s-1-r.md"}}' "$WORK" "$CLID")" >/dev/null
CYC="$(cli advance --cycle)"
check "같은 작업을 유지한다" "$CLID" "$CYC"
check "회차가 올라간다" '회차 2' "$CYC"
check "Scaffolding 으로 돌아간다" '2/7 Scaffolding' "$CYC"
check "파일명 접두사에 회차가 반영된다" "$CLID-2-" "$(cli status)"
check "회차가 바뀌면 계획 증거가 초기화된다" '^0$' \
  "$(sql "SELECT COUNT(*) FROM evidence WHERE loop_id='$CLID' AND kind='plan_file'")"
check "작업 선정 기록은 유지된다" '^1$' \
  "$(sql "SELECT COUNT(*) FROM evidence WHERE loop_id='$CLID' AND kind='intent_set'")"
check "완료 조건도 회차를 넘어 유지된다" '유지되는지 확인할 조건' "$(cli loop done-when)"
check "Selection 은 done 으로 남는다" '^done$' \
  "$(sql "SELECT status FROM stage WHERE loop_id='$CLID' AND stage='selection'")"
check "--done/--cycle 은 마지막 단계에서만" '마지막 단계' "$(cli advance --done || true)"

echo "== 결함 B 회귀: 턴 중 단계 전이 시 이전 말머리 허용"
setstage scaffolding
hook '{"hook_event_name":"PreToolUse","cwd":"'"$WORK"'","prompt_id":"pz","tool_name":"Read","tool_input":{}}' >/dev/null
setstage execution
check_no_prefix_complaint "그 턴에 관여한 단계의 말머리는 통과" "$(hook "$(STOP pz '[Scaffolding] 완료')")"
check "무관한 단계 말머리는 차단" '"decision": "block"' "$(hook "$(STOP pz2 '[Context] 완료')")"

echo "== 말머리는 번호가 아니라 단계 이름으로 검증한다"
check_no_prefix_complaint "이름만 써도 통과" "$(hook "$(STOP pn1 '[Execution] 완료')")"
check_no_prefix_complaint "대소문자 무시" "$(hook "$(STOP pn2 '[execution] 완료')")"
check_no_prefix_complaint "앞뒤 공백 허용" "$(hook "$(STOP pn3 '[ Execution ] 완료')")"
check "번호를 병기하면 차단 (중복 정보)" '"decision": "block"' \
  "$(hook "$(STOP pn4 '[4/6 Execution] 완료')")"
check "이름 없이 번호만 쓰면 차단" '"decision": "block"' "$(hook "$(STOP pn5 '[4/6] 완료')")"
check "닫는 대괄호가 없으면 차단" '"decision": "block"' "$(hook "$(STOP pn6 '[Execution 완료')")"
check "다른 단계 이름은 차단" '"decision": "block"' "$(hook "$(STOP pn7 '[Planning] 완료')")"
check "이름이 다른 이름을 포함해도 정확 일치만" '"decision": "block"' \
  "$(hook "$(STOP pn8 '[Execution 단계] 완료')")"
check "차단 메시지가 형식을 제시한다" '\[Execution\] 여야 한다' \
  "$(hook "$(STOP pn9 '말머리 없음')")"

echo "== 턴 종료 게이트와 상한 소진 노출"
setstage verification
check "1회차 차단" '검증 증거가 없다' "$(hook "$(STOP pv '[Verification] 끝')")"
check "2회차 차단 (limit 2)" '"decision": "block"' "$(hook "$(STOP pv '[Verification] 끝')")"
check "3회차는 우회를 사용자에게 노출" 'systemMessage' "$(hook "$(STOP pv '[Verification] 끝')")"
hook '{"hook_event_name":"PostToolUse","cwd":"'"$WORK"'","tool_name":"Bash","tool_input":{"command":"npm test"}}' >/dev/null
# 이 자리는 '통과' 를 주장했지만 헬퍼는 **말머리 불만만** 봤다. 검증 증거가 없어
# 막혀도 통과로 세어졌다 — 적대적 리뷰가 지적했다. 무엇이 없어야 하는지 적는다.
check_no_prefix_complaint "말머리 불만이 없다" "$(hook "$(STOP pv2 '[Verification] 끝')")"
check_absent "검증 증거로 막히지 않는다" '검증 증거가 없다' \
  "$(hook "$(STOP pv2 '[Verification] 끝')")"

echo "== docs 예외"
setstage scaffolding
cli allow 'docs/spec/**' --reason '사용자가 스펙 작성을 지시' >/dev/null
check_empty "예외 등록 후 docs 쓰기 허용" "$(hook "$(W docs/spec/001-a.md)")"
check "예외에도 명명 규칙은 강제" 'NNN-name.md' "$(hook "$(W docs/spec/bad_name.md)")"

echo "== 스킵 자동 승인 토글"
setstage context
SKON='.claude/harness/bin/harness auto-skip on'
check "기본값은 OFF" 'OFF' "$(cli auto-skip status)"
check "auto-skip on 은 사유 없이 거부" '"permissionDecision": "deny"' \
  "$(hook "$(B "$SKON" default)")"
check "auto-skip on 은 사용자 승인 필요" '"permissionDecision": "ask"' \
  "$(hook "$(B "$SKON --reason \\\"급한 핫픽스 기간\\\"" default)")"
check "켜는 행위의 위험을 다이얼로그에 노출" '다이얼로그 없이 통과' \
  "$(hook "$(B "$SKON --reason \\\"급한 핫픽스 기간\\\"" default)")"
check "auto-skip on 은 bypass 에서도 거부 (세션을 넘는 결정)" '"permissionDecision": "deny"' \
  "$(hook "$(B "$SKON --reason \\\"x\\\"" bypassPermissions)")"
check_empty "auto-skip off 는 승인 없이 통과" \
  "$(hook "$(B '.claude/harness/bin/harness auto-skip off' default)")"

cli auto-skip on --reason "급한 핫픽스 기간" >/dev/null
check "ON 상태 표시" 'ON' "$(cli auto-skip status)"
SKIPCMD='.claude/harness/bin/harness skip context --reason \"자동 승인 테스트\"'
check "ON 이면 스킵이 ask 가 아니라 defer" '"permissionDecision": "defer"' \
  "$(hook "$(B "$SKIPCMD" default)")"
check "자동 승인 사실을 사용자에게 노출" 'systemMessage' "$(hook "$(B "$SKIPCMD" default)")"
check "ON 이어도 사유는 여전히 필수" '"permissionDecision": "deny"' \
  "$(hook "$(B '.claude/harness/bin/harness skip context' default)")"
check "ON 이어도 allow 는 동의를 요구" '"permissionDecision": "ask"' \
  "$(hook "$(B '.claude/harness/bin/harness allow \"docs/x.md\" --reason \"y\"' default)")"
check "SessionStart 가 ON 을 경고" '⚠ 스킵 자동 승인 ON' \
  "$(hook '{"hook_event_name":"SessionStart","cwd":"'"$WORK"'","source":"startup"}')"
cli skip context --reason "자동 승인으로 건너뜀" >/dev/null
check "기록은 authorized_by=auto 로 구분" '^auto$' \
  "$(sql "SELECT authorized_by FROM stage WHERE stage='context'")"
check "status 가 ON 을 경고" '자동 승인 ON' "$(cli status)"
cli auto-skip off >/dev/null
check "OFF 로 복원" 'OFF' "$(cli auto-skip status)"
setstage context
check "복원 후 다시 동의를 요구" '"permissionDecision": "ask"' \
  "$(hook "$(B "$SKIPCMD" default)")"

echo "== 자동 승인 횟수 만료 (--uses N)"
cli auto-skip on --reason "핫픽스 2회만" --uses 2 >/dev/null
check "횟수 범위 표시" '남은 2회' "$(cli auto-skip status)"
setstage context; cli skip context --reason "1회차" >/dev/null
check "1회 소진" '남은 1회' "$(cli auto-skip status)"
setstage context; LASTOUT="$(cli skip context --reason '2회차')"
check "소진 시 안내" '소진되어 OFF' "$LASTOUT"
check "소진 후 OFF + 사유" '소진해 만료' "$(cli auto-skip status)"
setstage context
check "만료 후 다시 동의를 요구" '"permissionDecision": "ask"' \
  "$(hook "$(B "$SKIPCMD" default)")"

echo "== 자동 승인 루프 범위 만료 (--scope loop)"
cli auto-skip on --reason "이번 루프만" --scope loop >/dev/null
check "작업 범위 표시" '작업 .* 범위' "$(cli auto-skip status)"
check "같은 작업에서는 활성" '"permissionDecision": "defer"' \
  "$(hook "$(B "$SKIPCMD" default)")"
cli loop new >/dev/null
check "작업이 바뀌면 만료" '작업이 바뀌어' "$(cli auto-skip status)"
setstage context
check "만료 후 다시 동의를 요구" '"permissionDecision": "ask"' \
  "$(hook "$(B "$SKIPCMD" default)")"
cli auto-skip off >/dev/null
check "--uses 0 거부" '1 이상' "$(cli auto-skip on --reason x --uses 0 || true)"
check "--scope 오타 거부" 'loop 또는 project' \
  "$(cli auto-skip on --reason x --scope bogus || true)"
check "거부되면 켜지지 않는다" 'OFF' "$(cli auto-skip status)"

echo "== 루프 종료 시 행을 버린다"
LID="$(loopid)"
cli advance >/dev/null; setstage compounding
mkdir -p "$WORK/.dev/retrospect"
hook "$(printf '{"hook_event_name":"PostToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":".dev/retrospect/%s-r.md"}}' "$WORK" "$LID")" >/dev/null
OUT="$(cli advance --done)"
check "작업 종료 후 새 작업 시작" '새 작업' "$OUT"
check "이전 작업 stage 행 삭제" '^0$' "$(sql "SELECT COUNT(*) FROM stage WHERE loop_id='$LID'")"
check "이전 작업 evidence 행 삭제" '^0$' "$(sql "SELECT COUNT(*) FROM evidence WHERE loop_id='$LID'")"
if [ "$(loopid)" != "$LID" ]; then
  PASS=$((PASS + 1)); echo "  ok   새 해시가 발급된다"
else
  FAIL=$((FAIL + 1)); echo "  FAIL 새 해시가 발급된다"
fi

echo "== 관측 기록(event) 적립"
check "차단이 규칙명과 함께 적립된다" 'docs_readonly' \
  "$(sql "SELECT rule FROM event WHERE kind='block' AND rule='docs_readonly' LIMIT 1")"
check "단계 스킵이 적립된다" '^skip$' \
  "$(sql "SELECT kind FROM event WHERE kind='skip' LIMIT 1")"
check "파일 편집이 적립된다" '^edit$' \
  "$(sql "SELECT kind FROM event WHERE kind='edit' LIMIT 1")"
check "종료 게이트 미충족이 적립된다" '^verification_evidence$' \
  "$(sql "SELECT rule FROM event WHERE kind='stop_gate' AND rule='verification_evidence' LIMIT 1")"
check "게이트 우회가 적립된다" '^bypass$' \
  "$(sql "SELECT kind FROM event WHERE kind='bypass' LIMIT 1")"
check "이전 루프 event 는 살아남는다" '^0$' \
  "$(sql "SELECT CASE WHEN COUNT(*)>0 THEN 0 ELSE 1 END FROM event WHERE loop_id='$LID'")"
check "닫힌 루프 인덱스는 남는다" '^1$' \
  "$(sql "SELECT COUNT(*) FROM loop WHERE id='$LID' AND closed_at IS NOT NULL")"

echo "== 도구 실패 적립 (PostToolUseFailure)"
PF() { printf '{"hook_event_name":"PostToolUseFailure","cwd":"%s","tool_name":"Bash","tool_input":{"command":"%s"},"tool_error":"%s"}' "$WORK" "$1" "$2"; }
hook "$(PF 'npm test -- --watch=false' 'FAIL src/api.test.ts')" >/dev/null
hook "$(PF 'npm test' 'FAIL src/api.test.ts again')" >/dev/null
check "명령이 정규화되어 같은 키로 집계" '^npm test|2$' \
  "$(sql "SELECT target, COUNT(*) FROM event WHERE kind='tool_fail' GROUP BY target")"
check "오류 내용이 detail 에 남는다" 'src/api.test.ts' \
  "$(sql "SELECT detail FROM event WHERE kind='tool_fail' LIMIT 1")"

echo "== 루프를 넘는 반복 감지"
# 새 루프에서 같은 경로를 같은 규칙으로 다시 차단시킨다
setstage scaffolding
hook "$(W docs/spec/001-a.md)" >/dev/null
check "같은 규칙이 2개 루프에 걸쳐 기록된다" '^2$' \
  "$(sql "SELECT COUNT(DISTINCT loop_id) FROM event WHERE rule='docs_readonly'")"

echo "== recall (pull 방식 조회)"
mkdir -p "$WORK/.dev/retrospect" "$WORK/.dev/learning"
NEW="$(loopid)"
printf 'api 리팩터링 회고\n비동기 처리에서 실수했다\n' > "$WORK/.dev/retrospect/$NEW-api.md"
printf '무관한 학습\n' > "$WORK/.dev/learning/$NEW-unrelated.md"
check "키워드로 회고 파일을 찾는다" 'api\.md' "$(cli recall 비동기)"
printf '# 회고 인덱스\n전체 목록\n' > "$WORK/.dev/retrospect/INDEX.md"
check "인덱스를 진입점으로 먼저 제시한다" '인덱스 — 쌓인 기록의 진입점' "$(cli recall 비동기)"
check "키워드와 무관해도 인덱스는 나온다" 'retrospect/INDEX\.md' "$(cli recall zzz없는키워드)"
check "경로 키워드가 조각으로 넓혀져 산문에 걸린다" 'api\.md' "$(cli recall src/api.ts)"
check "무관한 파일은 걸리지 않는다" '^0$' \
  "$(cli recall 비동기 | grep -c unrelated || true)"
check "명령 키워드로 실패 기록을 찾는다" 'npm test' "$(cli recall npm)"
check "무관한 기록은 걸러진다" '^0$' \
  "$(cli recall npm | sed -n '/과거 관측/,/^$/p' | grep -c docs_readonly || true)"
check "키워드가 아무것도 안 맞으면 비어 있다" '(없음)' "$(cli recall zzz존재하지않음)"
check "여러 루프 반복을 표시한다" '여러 작업에서 반복' "$(cli recall docs)"
check "--kind 로 종류를 좁힌다" 'tool_fail' "$(cli recall --kind tool_fail)"
check "작업 미정이면 전체 + 기록 안내" '작업이 정해졌으면' "$(cli recall)"
cli loop intent 'npm test 실패 조사' >/dev/null
check "작업이 정해지면 그것을 기본 키워드로 쓴다" '작업에서 추출' "$(cli recall)"
check "작업 키워드로 관련 기록을 찾는다" 'npm test' "$(cli recall)"
cli loop intent 'src/auth.ts 토큰 갱신 로직 수정' >/dev/null
check "저정보 단어(src·로직·수정)는 키워드에서 제외" '추출: auth.ts 토큰 갱신)' "$(cli recall | head -1)"

echo "== stats (누적 수치)"
check "작업 수를 센다" '작업: ' "$(cli stats)"
check "이벤트 종류별 집계" '규칙 차단' "$(cli stats)"
check "차단된 규칙 상위 표시" 'docs_readonly' "$(cli stats)"
check "규칙 단위로 묶어 대상 종수를 센다" '대상 ' "$(cli stats)"
check "실패한 도구 상위 표시" 'npm test' "$(cli stats)"
check "반복을 명시한다" '작업에서 반복' "$(cli stats)"
check "--loop 는 현재 루프만" "현재 작업 $NEW" "$(cli stats --loop)"
check "recall 로 안내한다" 'harness recall' "$(cli stats)"

echo "== 단계별 안내 (hint)"
setstage context
check "Context 는 조회 방법만 알려준다 (push 아님)" 'harness recall' \
  "$(hook '{"hook_event_name":"SessionStart","cwd":"'"$WORK"'","source":"startup"}')"
check "Context hint 가 무관한 것을 읽지 말라고 한다" '무관한 과거 실수까지 읽지 마라' \
  "$(hook '{"hook_event_name":"SessionStart","cwd":"'"$WORK"'","source":"startup"}')"
setstage verification
hook '{"hook_event_name":"PostToolUse","cwd":"'"$WORK"'","tool_name":"Bash","tool_input":{"command":"npm test"}}' >/dev/null
OUT3="$(cli advance)"
check "Compounding 진입 시 이 루프 관측을 밀어준다" '이 루프에서 관측된 것' "$OUT3"
check "승격 논의를 유도한다" '복리가 아니라 일기' "$OUT3"

echo "== 실패 지점 주입 (PostToolUseFailure)"
PTF() { printf '{"hook_event_name":"PostToolUseFailure","cwd":"%s","tool_name":"Bash","tool_input":{"command":"%s"},"error":"%s"}' "$WORK" "$1" "$2"; }
check_empty "첫 실패는 조용하다 (배울 것이 없다)" "$(hook "$(PTF 'flakycmd run' 'boom one')")"
OUTF="$(hook "$(PTF 'flakycmd run' 'boom two')")"
check "두 번째 실패는 횟수를 알려준다" '2번째' "$OUTF"
check "이전 오류를 함께 준다" 'boom one' "$OUTF"
check "직접 다룬 기록이 없으면 인덱스로 유도한다" '인덱스에서 찾아보라' "$OUTF"
check "임계 미달이면 사용자에게 알리지 않는다" '^0$' "$(printf '%s' "$OUTF" | grep -c systemMessage)"
mkdir -p "$WORK/.dev/troubleshooting"
echo 'flakycmd 는 먼저 setup 이 필요하다' > "$WORK/.dev/troubleshooting/flakycmd-fix.md"
OUTF2="$(hook "$(PTF 'flakycmd run' 'boom three')")"
check "관련 기록을 찾아 제시한다" 'flakycmd-fix.md' "$OUTF2"
check "적립 전에 세므로 3번째로 표시된다" '3번째' "$OUTF2"

echo "== 승격 (promote)"
check "반복 항목이 없으면 빈 목록" '(없음' "$(cli promote)"
sql "INSERT INTO loop(id,created_at,closed_at) VALUES
 ('250101-p00001','2025-01-01T10:00:00+0900','2025-01-01T18:00:00+0900'),
 ('250102-p00002','2025-01-02T10:00:00+0900','2025-01-02T18:00:00+0900'),
 ('250103-p00003','2025-01-03T10:00:00+0900','2025-01-03T18:00:00+0900');
 INSERT INTO event(at,loop_id,stage,kind,rule,target) VALUES
 ('2025-01-01T11:00:00+0900','250101-p00001','execution','block','fakerule','a.txt'),
 ('2025-01-02T11:00:00+0900','250102-p00002','execution','block','fakerule','b.txt'),
 ('2025-01-03T11:00:00+0900','250103-p00003','execution','block','fakerule','c.txt');" >/dev/null
check "3개 작업에서 반복되면 목록에 뜬다" 'block:fakerule' "$(cli promote)"
check "결정 방법을 함께 제시한다" 'decline --reason' "$(cli promote)"
check "status 가 승격 대기를 노출한다" '승격 결정 대기' "$(cli status)"
check "SessionStart 가 승격 대기를 알린다" '승격 결정 대기' \
  "$(hook '{"hook_event_name":"SessionStart","cwd":"'"$WORK"'","source":"startup"}')"
check "모르는 키는 거부한다" '반복 항목이 아니다' "$(cli promote block:nosuchrule --as hook --note x || true)"
check "종류 없이는 거부한다" '골라야 한다' "$(cli promote block:fakerule || true)"
check "사유 없이는 거부한다" '필요하다' "$(cli promote block:fakerule --as hook || true)"

echo "== Compounding 종료 조건 promotion_decided"
setstage compounding
sql "INSERT OR IGNORE INTO evidence(loop_id,stage,kind,item,at) VALUES('$(loopid)','compounding','retro_file','r','x')" >/dev/null
OUTP="$(cli advance --done || true)"
check "승격 결정 없이는 작업을 닫을 수 없다" 'promotion_decided' "$OUTP"
check "회고는 있으므로 회고를 요구하지 않는다" '^0$' "$(printf '%s' "$OUTP" | grep -c 'retro_file:')"
check "Stop 훅도 막는다" '승격 결정이 남았다' \
  "$(hook "$(STOP prm '[Compounding] 끝')")"

echo "== rule 승격과 LEARNED.md"
OUTR="$(cli promote block:fakerule --as rule --note '가짜 규칙은 이렇게 피한다')"
check "승격이 기록된다" '승격 기록' "$OUTR"
check "established 로 시작한다" 'established' "$OUTR"
check "LEARNED.md 를 갱신한다" 'LEARNED.md' "$OUTR"
check "LEARNED.md 에 줄이 들어간다" '가짜 규칙은 이렇게 피한다' "$(cat "$WORK/.claude/harness/LEARNED.md")"
check "성숙도를 표시한다" 'established' "$(cat "$WORK/.claude/harness/LEARNED.md")"
check "직접 편집하지 말라고 한다" '직접 편집하지 마라' "$(cat "$WORK/.claude/harness/LEARNED.md")"
check "LEARNED.md 는 쓰기 금지 경로다" 'deny' "$(hook "$(W .claude/harness/LEARNED.md)")"
check "예외로도 열리지 않는다고 알린다" 'allow` 로도 열리지 않는다' "$(hook "$(W .claude/harness/LEARNED.md)")"
check "결정 후에는 작업을 닫을 수 있다" '단계 1/7' "$(cli advance --done)"

echo "== 보류(decline)도 결정이다"
sql "INSERT INTO event(at,loop_id,stage,kind,rule,target) VALUES
 ('2025-01-01T12:00:00+0900','250101-p00001','execution','tool_fail','Bash','fakecmd x'),
 ('2025-01-02T12:00:00+0900','250102-p00002','execution','tool_fail','Bash','fakecmd x'),
 ('2025-01-03T12:00:00+0900','250103-p00003','execution','tool_fail','Bash','fakecmd x');" >/dev/null
OUTD="$(cli promote 'tool_fail:fakecmd x' --decline --reason '원인이 외부 환경이다')"
check "보류는 보류로 표시된다" '보류 기록' "$OUTD"
check "되돌아온다는 것을 알린다" '무효화' "$OUTD"
check "보류 성숙도는 declined" 'declined' "$(sql "SELECT maturity FROM promotion WHERE key='tool_fail:fakecmd x'")"
check "보류는 event 로 남는다" 'promote_declined' "$(sql "SELECT kind FROM event WHERE kind='promote_declined' LIMIT 1")"
check "stats 가 보류를 노출한다" '승격 보류' "$(cli stats)"
check "stats 가 승격 이력을 보여준다" '승격 이력' "$(cli stats)"
check "보류 후에는 대기 목록에서 빠진다" '(없음' "$(cli promote)"

echo "== 재발하면 결정이 무효화된다"
sql "UPDATE promotion SET at='2025-01-01T00:00:00+0900', recheck_at='2025-01-01T00:00:00+0900'
     WHERE key='block:fakerule';
     INSERT INTO loop(id,created_at,closed_at) VALUES
     ('250201-p00004','2025-02-01T10:00:00+0900','2025-02-01T18:00:00+0900'),
     ('250202-p00005','2025-02-02T10:00:00+0900','2025-02-02T18:00:00+0900');
     INSERT INTO event(at,loop_id,stage,kind,rule,target) VALUES
     ('2025-02-01T11:00:00+0900','250201-p00004','execution','block','fakerule','d.txt'),
     ('2025-02-02T11:00:00+0900','250202-p00005','execution','block','fakerule','e.txt');" >/dev/null
OUTG="$(cli promote)"
check "재발하면 다시 목록에 오른다" 'block:fakerule' "$OUTG"
check "무엇으로 승격했었는지 알려준다" '다시 걸렸다' "$OUTG"
check "성숙도가 regressed 다" 'regressed' "$(sql "SELECT maturity FROM promotion WHERE key='block:fakerule'")"
check "재발은 LEARNED.md 에서 내려간다" '^0$' \
  "$(grep -c '가짜 규칙' "$WORK/.claude/harness/LEARNED.md" || true)"
check "tidy 가 재발을 짚는다" '다시 걸린 항목' "$(cli tidy)"
OUTG2="$(cli promote block:fakerule --as structure --note '구조를 바꿔 원인을 없앴다')"
check "다시 결정하면 established 로 돌아온다" 'established' "$OUTG2"
check "재결정 후에는 목록에서 빠진다" '(없음' "$(cli promote)"

echo "== LEARNED.md 예산"
python3 - "$WORK/.claude/harness/harness.db" <<'PYEOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
for n in range(20):
    con.execute("INSERT OR REPLACE INTO promotion(key,kind,decision,maturity,note,loop_id,at,recheck_at) VALUES(?,?,?,?,?,?,?,?)",
                ("filler:%d" % n, "block", "rule", "established",
                 "채움 %d" % n, "x", "2025-01-01T00:00:00+0900",
                 "2025-01-01T00:00:00+0900"))
con.commit()
PYEOF
sql "INSERT INTO event(at,loop_id,stage,kind,rule,target) VALUES
 ('2025-01-01T13:00:00+0900','250101-p00001','execution','block','budgetrule','a'),
 ('2025-01-02T13:00:00+0900','250102-p00002','execution','block','budgetrule','b'),
 ('2025-01-03T13:00:00+0900','250103-p00003','execution','block','budgetrule','c');" >/dev/null
OUTB="$(cli promote block:budgetrule --as rule --note '예산을 넘기려는 규칙' || true)"
check "예산이 차면 rule 승격을 거부한다" '예산이 찼다' "$OUTB"
check "무엇을 먼저 하라고 알려준다" '먼저 한 줄을 비워라' "$OUTB"
check "예산이 차도 hook 승격은 가능하다" '승격 기록' \
  "$(cli promote block:budgetrule --as hook --note '훅으로 막았다')"
check "tidy 가 예산 소진을 알린다" '예산 소진' "$(cli tidy)"

echo "== tidy (정리 후보)"
mkdir -p "$WORK/.dev/retrospect"
rm -f "$WORK/.dev/retrospect/INDEX.md"
i=1
while [ "$i" -le 13 ]; do
  echo "회고 $i" > "$WORK/.dev/retrospect/250101-abc123-$i-retro.md"
  i=$((i + 1))
done
find "$WORK/.dev/retrospect" -name '*.md' -exec touch -t 202401011200 {} \;
OUTT="$(cli tidy)"
check "인덱스 없는 폴더를 짚는다" 'INDEX.md 가 없다' "$OUTT"
check "오래된 파일을 짚는다" '오래된 파일' "$OUTT"
check "며칠 지났는지 알려준다" '일$' "$OUTT"
check "한 작업이 흘린 파일을 병합 후보로 묶는다" '병합할 후보' "$OUTT"
check "삭제 여부는 자율이라고 명시한다" '자율' "$OUTT"
setstage scaffolding
check "Scaffolding 에서 한 줄로 요약한다" '정리 후보:' "$(cli status)"
check "요약이 tidy 를 가리킨다" 'harness tidy' "$(cli status)"
# INDEX.md 가 최신 파일보다 낡아야 '낡음' 이다 (같은 mtime 은 낡지 않음)
echo "인덱스" > "$WORK/.dev/retrospect/INDEX.md"
touch -t 202001011200 "$WORK/.dev/retrospect/INDEX.md"
check "인덱스가 낡으면 짚는다" '낡았다' "$(cli tidy)"
touch "$WORK/.dev/retrospect/INDEX.md"
check "인덱스가 최신이면 짚지 않는다" '^0$' \
  "$(cli tidy | grep -c '낡았다' || true)"

echo "== init 이 만드는 것"
check "LEARNED.md 앵커를 넣는다" '@.claude/harness/LEARNED.md' "$(cat "$WORK/CLAUDE.md")"
check "POLICY.md 앵커도 유지한다" '@.claude/harness/POLICY.md' "$(cat "$WORK/CLAUDE.md")"
check "재실행은 앵커를 중복하지 않는다" '^1$' \
  "$(grep -c '@.claude/harness/LEARNED.md' "$WORK/CLAUDE.md")"
check "promote 를 권한 허용에 넣는다" 'promote' "$(cat "$WORK/.claude/settings.json")"
check "tidy 를 권한 허용에 넣는다" 'tidy' "$(cat "$WORK/.claude/settings.json")"

echo "== help"
check "promote 를 사용법에 적는다" 'promote <key>' "$(cli help)"
check "tidy 를 사용법에 적는다" 'tidy  ' "$(cli help)"

echo "== Bash 로 하네스 자신을 건드릴 수 없다"
check "rm 으로 DB 삭제 차단" 'Bash 로도 변경할 수 없다' \
  "$(hook "$(B 'rm .claude/harness/harness.db' default)")"
check "sed -i 로 엔진 변경 차단" 'Bash 로도 변경할 수 없다' \
  "$(hook "$(B 'sed -i s/a/b/ .claude/harness/bin/harness.py' default)")"
check "리다이렉트로 LEARNED.md 변경 차단" 'Bash 로도 변경할 수 없다' \
  "$(hook "$(B 'echo x > .claude/harness/LEARNED.md' default)")"
check "sqlite3 로 DB 변경 차단" 'Bash 로도 변경할 수 없다' \
  "$(hook "$(B 'sqlite3 .claude/harness/harness.db \"UPDATE meta SET v=1\"' default)")"
check "제어 명령을 뒤에 붙여 회피할 수 없다" 'Bash 로도 변경할 수 없다' \
  "$(hook "$(B 'rm .claude/harness/bin/harness && echo ok' default)")"
check "allow 로도 열리지 않는다고 알린다" 'allow` 로도 열리지 않는다' \
  "$(hook "$(B 'rm .claude/harness/harness.db' default)")"
check_empty "래퍼 실행은 막지 않는다" \
  "$(hook "$(B '.claude/harness/bin/harness status' default)")"
check_empty "python3 로 엔진 실행은 막지 않는다" \
  "$(hook "$(B 'python3 .claude/harness/bin/harness.py status' default)")"
check_empty "읽기 명령은 막지 않는다" \
  "$(hook "$(B 'cat .claude/harness/LEARNED.md' default)")"
check_empty "무관한 쓰기는 막지 않는다" "$(hook "$(B 'rm src/tmp.txt' default)")"

echo "== loop new / loop adopt 는 사용자 동의를 받는다"
check "loop new 는 ask" '"permissionDecision": "ask"' \
  "$(hook "$(B '.claude/harness/bin/harness loop new --reason \"작업 전환\"' default)")"
check "loop new 의 결과를 사유와 함께 설명한다" '승격 결정을 건너뛰게 된다' \
  "$(hook "$(B '.claude/harness/bin/harness loop new --reason \"작업 전환\"' default)")"
check "loop adopt 는 ask" '"permissionDecision": "ask"' \
  "$(hook "$(B '.claude/harness/bin/harness loop adopt 260101-abcdef --reason x' default)")"
check "사유 없는 loop new 는 거부" '사유 없이 loop new' \
  "$(hook "$(B '.claude/harness/bin/harness loop new' default)")"
check "bypassPermissions 에서 loop new 는 사전 승인" '"permissionDecision": "defer"' \
  "$(hook "$(B '.claude/harness/bin/harness loop new --reason x' bypassPermissions)")"
check_empty "loop 조회는 동의 없이" "$(hook "$(B '.claude/harness/bin/harness loop' default)")"
check_empty "loop intent 는 동의 없이" \
  "$(hook "$(B '.claude/harness/bin/harness loop intent \"작업\"' default)")"

echo "== 재발 판정은 저장된 maturity 를 믿지 않는다"
sql "INSERT INTO loop(id,created_at,closed_at) VALUES
 ('250301-s00001','2025-03-01T10:00:00+0900','x'),
 ('250302-s00002','2025-03-02T10:00:00+0900','x'),
 ('250303-s00003','2025-03-03T10:00:00+0900','x'),
 ('250401-s00004','2025-04-01T10:00:00+0900','x'),
 ('250402-s00005','2025-04-02T10:00:00+0900','x');
 INSERT INTO event(at,loop_id,stage,kind,rule,target) VALUES
 ('2025-03-01T11:00:00+0900','250301-s00001','execution','block','syncrule','a'),
 ('2025-03-02T11:00:00+0900','250302-s00002','execution','block','syncrule','b'),
 ('2025-03-03T11:00:00+0900','250303-s00003','execution','block','syncrule','c'),
 ('2025-04-01T11:00:00+0900','250401-s00004','execution','block','syncrule','d'),
 ('2025-04-02T11:00:00+0900','250402-s00005','execution','block','syncrule','e');
 INSERT INTO promotion(key,kind,decision,maturity,note,loop_id,at,recheck_at) VALUES('block:syncrule','block','rule','established',
   '동기화 확인용','x','2025-03-15T00:00:00+0900','2025-03-15T00:00:00+0900');" >/dev/null
check "저장값이 established 인 것을 확인" 'established' \
  "$(sql "SELECT maturity FROM promotion WHERE key='block:syncrule'")"
check "sync 없이도 재발이 대기 목록에 뜬다" 'block:syncrule' "$(cli promote)"
setstage compounding
sql "INSERT OR IGNORE INTO evidence(loop_id,stage,kind,item,at) VALUES('$(loopid)','compounding','retro_file','r','x')" >/dev/null
check "sync 없이도 게이트가 막는다" 'promotion_decided' "$(cli advance --done || true)"
check "Stop 훅도 sync 없이 막는다" '승격 결정이 남았다' \
  "$(hook "$(STOP syncp '[Compounding] 끝')")"
cli promote block:syncrule --as structure --note '원인 제거' >/dev/null
check "결정하면 통과한다" '단계 1/7' "$(cli advance --done || true)"

echo "== 절대 시각 비교 (타임존·DST)"
TSOUT="$(python3 - "$(dirname "$ENGINE")" <<'PYTS'
import sys
sys.path.insert(0, sys.argv[1])
import harness as h
a, b = h.ts_epoch("2026-08-04T12:00:00+0900"), h.ts_epoch("2026-08-04T03:00:00+0000")
assert a == b, ("같은 순간이 다르게 계산됨", a, b)
early, late = h.ts_epoch("2026-11-01T01:45:00-0400"), h.ts_epoch("2026-11-01T01:30:00-0500")
assert late > early, ("DST 순서 뒤집힘", early, late)
assert "2026-11-01T01:30:00-0500" < "2026-11-01T01:45:00-0400", "문자열 비교 전제"
assert h.ts_epoch("2026-08-04T12:00:00") > 0, "오프셋 없는 값"
assert h.ts_epoch("") == 0.0 and h.ts_epoch(None) == 0.0, "빈 값"
assert h.ts_epoch("쓰레기") == 0.0, "파싱 불가"
print("ok")
PYTS
)"
check "오프셋이 달라도 같은 순간으로 계산한다" 'ok' "$TSOUT"

echo "== 우회 시도는 승격 대상이 아니다"
check "no_reason 은 후보에서 빠진다" '^0$' "$(cli promote | grep -c 'block:no_reason')"
check "bypass_mode 도 빠진다" '^0$' "$(cli promote | grep -c 'block:bypass_mode')"
check "protected_bash 도 빠진다" '^0$' "$(cli promote | grep -c 'protected_bash')"
check "그래도 stats 에는 남는다" 'no_reason' "$(cli stats)"

echo "== 실패 회수의 작업 수 계산"
PTF2() { printf '{"hook_event_name":"PostToolUseFailure","cwd":"%s","tool_name":"Bash","tool_input":{"command":"%s"},"error":"%s"}' "$WORK" "$1" "$2"; }
hook "$(PTF2 'onelooponly x' 'e1')" >/dev/null
OUTL="$(hook "$(PTF2 'onelooponly x' 'e2')")"
check "한 작업 안의 반복은 작업 수를 늘리지 않는다" '^0$' \
  "$(printf '%s' "$OUTL" | grep -c '작업 2개')"
check "그래도 횟수는 센다" '2번째' "$OUTL"

echo "== init 이 기존 파일을 망가뜨리지 않는다"
IW="$(mktemp -d)"
(cd "$IW" && git init -q . && mkdir -p .claude \
  && printf '{"permissions":[]}\n' > .claude/settings.json \
  && printf '# 내 프로젝트\n\n예시: `@.claude/harness/LEARNED.md` 는 하네스가 만든다\n' > CLAUDE.md \
  && printf '# step-seven-harness (런타임 상태)\nnode_modules\n' > .gitignore)
IRC=0; IOUT="$( (cd "$IW" && python3 "$ENGINE" init) 2>&1 )" || IRC=$?
check "permissions 가 리스트여도 init 이 완주한다" '^0$' "$IRC"
check "설치 완료를 보고한다" '하네스 설치 완료' "$IOUT"
check "설명문 속 문자열은 앵커로 세지 않는다" '^2$' \
  "$(grep -c '^@.claude/harness/' "$IW/CLAUDE.md")"
check "기존 CLAUDE.md 내용을 보존한다" '내 프로젝트' "$(cat "$IW/CLAUDE.md")"
check "주석 언급을 ignore 규칙으로 세지 않는다" 'harness.db' "$(cat "$IW/.gitignore")"
check "손상된 settings 는 건드리지 않는다" '권한 허용을 건너뛰었다' "$IOUT"
check "재실행은 앵커를 늘리지 않는다" '^2$' \
  "$( (cd "$IW" && python3 "$ENGINE" init >/dev/null 2>&1); grep -c '^@.claude/harness/' "$IW/CLAUDE.md")"
rm -rf "$IW"

echo "== 회차 종료 스냅샷"
MW="$(mktemp -d)"
(cd "$MW" && git init -q . && python3 "$ENGINE" init >/dev/null)
check "데이터가 없으면 그렇다고 말한다" '회차 종료 기록이 없다' \
  "$( (cd "$MW" && python3 "$ENGINE" metrics) )"
check "승격이 없으면 그렇다고 말한다" '아직 승격이 없다' \
  "$( (cd "$MW" && python3 "$ENGINE" metrics) )"
mcli() { (cd "$MW" && python3 "$ENGINE" "$@"); }
msql() { python3 - "$MW/.claude/harness/harness.db" "$1" <<'PYM'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
q = sys.argv[2]
if q.count(";") > 1:
    con.executescript(q)
else:
    for row in con.execute(q):
        print("|".join("" if v is None else str(v) for v in row))
con.commit()
PYM
}
MLID="$(msql "SELECT v FROM meta WHERE k='head'")"
mcli loop intent "측정 확인" >/dev/null
mcli loop done-when "cycle_close 가 남는다" >/dev/null
# 마찰을 만든다: 차단 2건, 실패 2건(같은 명령 반복), 재편집 3회
msql "INSERT INTO event(at,loop_id,stage,kind,rule,target) VALUES
 (datetime('now'),'$MLID','execution','block','stage_write','a.py'),
 (datetime('now'),'$MLID','execution','block','stage_write','b.py'),
 (datetime('now'),'$MLID','execution','tool_fail','Bash','npm test'),
 (datetime('now'),'$MLID','execution','tool_fail','Bash','npm test'),
 (datetime('now'),'$MLID','execution','edit',NULL,'src/x.py'),
 (datetime('now'),'$MLID','execution','edit',NULL,'src/x.py'),
 (datetime('now'),'$MLID','execution','edit',NULL,'src/x.py');" >/dev/null
msql "UPDATE stage SET status='pending' WHERE status='active';
 UPDATE stage SET status='active' WHERE stage='compounding';
 INSERT OR IGNORE INTO evidence(loop_id,stage,kind,item,at) VALUES('$MLID','compounding','retro_file','r',datetime('now'));" >/dev/null
MOUT="$(mcli advance --cycle)"
check "회차 경계에서 집계를 보고한다" '회차 1 기록' "$MOUT"
check "차단 수를 센다" '차단 2' "$MOUT"
check "반복 실패를 따로 센다" '반복 1' "$MOUT"
MSNAP="$(msql "SELECT detail FROM event WHERE kind='cycle_close'")"
jcheck "스냅샷의 차단 수가 정확히 2" blocks 2 "$MSNAP"
jcheck "스냅샷의 실패 수가 정확히 2" fails 2 "$MSNAP"
jcheck "스냅샷의 반복 실패가 정확히 1" refails 1 "$MSNAP"
jcheck "스냅샷의 재편집 최대가 정확히 3" churn 3 "$MSNAP"
jcheck "우회는 0" bypass 0 "$MSNAP"
check "cycle_close 이벤트가 남는다" '^1$' \
  "$(msql "SELECT COUNT(*) FROM event WHERE kind='cycle_close'")"
check "집계가 JSON 으로 저장된다" 'churn' \
  "$(msql "SELECT detail FROM event WHERE kind='cycle_close'")"
check "재편집 최대치를 담는다" '"churn": 3' \
  "$(msql "SELECT detail FROM event WHERE kind='cycle_close'")"
check "작업이 닫혀도 스냅샷은 남는다" '^1$' \
  "$(msql "UPDATE loop SET closed_at=datetime('now') WHERE id='$MLID'" >/dev/null; \
     msql "SELECT COUNT(*) FROM event WHERE kind='cycle_close'")"

echo "== metrics 출력"
MM="$(mcli metrics)"
check "승격 절이 있다" '승격 생존율' "$MM"
check "회차 추세 절이 있다" '회차 추세' "$MM"
check "반복 실패 절이 있다" '반복 실패 비율' "$MM"
check "측정 못 하는 것을 명시한다" '측정하지 못하는 것' "$MM"
check "점수를 만들지 않는 이유를 적는다" '점수를 만들지 않는' "$MM"
check "표본이 작으면 경고한다" '비율을 믿지 마라' \
  "$(msql "INSERT OR REPLACE INTO promotion(key,kind,decision,maturity,note,loop_id,at,recheck_at) VALUES('block:m1','block','hook','established','n','x',datetime('now'),datetime('now'))" >/dev/null; mcli metrics)"

echo "== Goodhart 가드"
# 마찰은 줄지만 우회가 느는 이력을 심는다 -> 경고가 떠야 한다
python3 - "$MW/.claude/harness/harness.db" <<'PYG'
import json, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("DELETE FROM event WHERE kind='cycle_close'")
for i in range(12):
    snap = dict(cycle=1, dur=100, blocks=8 - i // 2, fails=4, refails=max(0, 3 - i // 4),
                churn=3, edits=10, gates=0,
                bypass=0 if i < 6 else 3, skips=0 if i < 6 else 2,
                declines=0 if i < 6 else 2, promotes=0)
    con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target,detail) "
                "VALUES(?,?,?,?,?,?,?)",
                ("2026-0%d-01T10:00:00+0900" % (1 + i % 9), "x", "compounding",
                 "cycle_close", "1", "x-%d" % i, json.dumps(snap)))
con.commit()
PYG
check "마찰↓ 우회↑ 는 회피로 경고한다" '회피일 수 있다' "$(mcli metrics)"
jcheck "판정 필드가 evasion" verdict '"evasion"' "$(mcli metrics --json)"
python3 - "$MW/.claude/harness/harness.db" <<'PYG2'
import json, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("DELETE FROM event WHERE kind='cycle_close'")
for i in range(12):
    snap = dict(cycle=1, dur=100, blocks=8 - i // 2, fails=4, refails=max(0, 3 - i // 4),
                churn=3, edits=10, gates=0, bypass=0, skips=0, declines=0, promotes=0)
    con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target,detail) "
                "VALUES(?,?,?,?,?,?,?)",
                ("2026-0%d-01T10:00:00+0900" % (1 + i % 9), "x", "compounding",
                 "cycle_close", "1", "x-%d" % i, json.dumps(snap)))
con.commit()
PYG2
check "마찰↓ 우회→ 는 개선 신호로 읽는다" '개선 신호' "$(mcli metrics)"
jcheck "판정 필드가 improving" verdict '"improving"' "$(mcli metrics --json)"
check "그래도 난이도는 통제 못 한다고 적는다" '난이도 차이는 통제하지 못한다' "$(mcli metrics)"

echo "== 승격 주장 대비 실제 변경 관측"
msql "INSERT INTO loop(id,created_at) VALUES('260701-v00001','2026-07-01T10:00:00+0900');
 UPDATE meta SET v='260701-v00001' WHERE k='head';
 INSERT INTO stage(loop_id,stage,status,entered_at) VALUES('260701-v00001','compounding','active','2026-07-01T10:00:00+0900');
 INSERT INTO event(at,loop_id,stage,kind,rule,target) VALUES
 ('2026-07-01T11:00:00+0900','v1','execution','block','vrule','a'),
 ('2026-07-01T11:00:00+0900','v2','execution','block','vrule','b'),
 ('2026-07-01T11:00:00+0900','v3','execution','block','vrule','c');" >/dev/null
VOUT="$(mcli promote block:vrule --as hook --note '고쳤다고 주장만 한다')"
check "변경이 없으면 경고한다" '변경이 관측되지 않았다' "$VOUT"
check "막지는 않는다" '승격 기록' "$VOUT"
check "관측 결과가 event 로 남는다" 'change_seen=no' \
  "$(msql "SELECT detail FROM event WHERE kind='promote_verify' AND target='block:vrule'")"
msql "INSERT INTO event(at,loop_id,stage,kind,rule,target) VALUES
 (datetime('now'),'260701-v00001','compounding','edit',NULL,'.claude/harness/stages.json');" >/dev/null
VOUT2="$(mcli promote block:vrule --as hook --note '이번엔 정말 고쳤다')"
check "변경이 있으면 뒷받침한다고 말한다" '사실로 뒷받침된다' "$VOUT2"
check "metrics 가 주장과 사실을 나란히 보여준다" '변경관측' "$(mcli metrics)"
# .dev/ 만 고친 회차는 hook 승격의 근거가 아니다 (verify_exclude)
msql "INSERT INTO loop(id,created_at) VALUES('260702-v00002','2026-07-02T10:00:00+0900');
 UPDATE meta SET v='260702-v00002' WHERE k='head';
 INSERT INTO stage(loop_id,stage,status,entered_at) VALUES('260702-v00002','compounding','active','2026-07-02T10:00:00+0900');
 INSERT INTO event(at,loop_id,stage,kind,rule,target) VALUES
 ('2026-07-02T11:00:00+0900','w1','execution','block','wrule','a'),
 ('2026-07-02T11:00:00+0900','w2','execution','block','wrule','b'),
 ('2026-07-02T11:00:00+0900','w3','execution','block','wrule','c'),
 (datetime('now'),'260702-v00002','compounding','edit',NULL,'.dev/plan/x.md');" >/dev/null
check ".dev/ 편집만으로는 근거가 되지 않는다" '변경이 관측되지 않았다' \
  "$(mcli promote block:wrule --as hook --note '계획만 썼다')"
check "그 사실이 event 로 남는다" 'change_seen=no' \
  "$(msql "SELECT detail FROM event WHERE kind='promote_verify' AND target='block:wrule'")"
# **실제로 rule 승격을 해야** 이 단정이 뜻을 갖는다. 예전에는 픽스처가 `--as hook`
# 만 써서 개수가 늘 0이었고, rule 승격이 잘못 검증되든 제대로 제외되든 통과했다 —
# 적대적 리뷰가 지적했다.
msql "INSERT INTO event(at,loop_id,stage,kind,rule,target) VALUES
  ('2026-07-02T11:00:00+0900','r1','execution','block','rrule','a'),
  ('2026-07-02T11:00:00+0900','r2','execution','block','rrule','b'),
  ('2026-07-02T11:00:00+0900','r3','execution','block','rrule','c');" >/dev/null
check "rule 승격 자체는 성공한다" '승격 기록' \
  "$(mcli promote block:rrule --as rule --note 'LEARNED.md 한 줄로 올린다')"
check "hook 승격은 검증 기록을 남긴다" '^1$' \
  "$(msql "SELECT COUNT(*) FROM event WHERE kind='promote_verify' AND target='block:wrule'")"
check "rule 승격은 검증 대상이 아니다" '^0$' \
  "$(msql "SELECT COUNT(*) FROM event WHERE kind='promote_verify' AND target='block:rrule'")"
rm -rf "$MW"

echo "== --json 은 산문이 아니라 값을 낸다"
# 상태를 먼저 고정한다. 현재 상태에 기대를 맞추면 앞의 테스트가 바뀔 때마다 깨진다.
setstage context
SJ="$(cli status --json)"
jcheck "현재 단계" stage '"context"' "$SJ"
jcheck "단계 라벨" stage_label '"3/7 Context"' "$SJ"
jcheck "Context 의 쓰기 허용 클래스" write '["context", "dev"]' "$SJ"
jcheck "Context 는 종료 조건이 없다" exit_missing '[]' "$SJ"
jcheck "단계 개수는 7" stages.6.id '"compounding"' "$SJ"
setstage selection
jcheck "Selection 은 종료 조건이 둘" exit_missing \
  '["intent_set", "acceptance"]' "$(cli status --json)"
check "접두사가 해시-회차 형태다" '^"[0-9]\{6\}-[0-9a-f]\{6\}-1-"$' \
  "$(printf '%s' "$SJ" | jq1 prefix)"
TJ="$(cli tidy --json)"
jcheck "정리 후보 구조가 있다" dirs '[]' "$TJ"
check "metrics --json 이 파싱된다" '^[0-9]' \
  "$(printf '%s' "$(cli metrics --json)" | jq1 loops)"

echo "== 결정된 항목에 대한 실패 주입 (부분 열 회귀)"
# is_regressed 는 kind/key/recheck_at 을 쓴다. 일부 열만 SELECT 하면 sqlite3.Row 가
# IndexError 를 내고, 훅이 fail-open 이라 조용히 죽는다 — 0.18.0 에 있던 버그다.
RW="$(mktemp -d)"
(cd "$RW" && git init -q . && python3 "$ENGINE" init >/dev/null)
python3 - "$RW/.claude/harness/harness.db" <<'PYR'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("INSERT INTO promotion(key,kind,decision,maturity,note,loop_id,at,recheck_at) VALUES(?,?,?,?,?,?,?,?)",
            ("tool_fail:cargo test", "tool_fail", "declined", "declined",
             "환경 문제", "x", "2026-01-01T00:00:00+0900", "2026-01-01T00:00:00+0900"))
for i, l in enumerate(("r1", "r2", "r3")):
    con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target,detail) "
                "VALUES(?,?,?,?,?,?,?)",
                ("2026-01-0%dT10:00:00+0900" % (i + 1), l, "execution",
                 "tool_fail", "Bash", "cargo test", "err%d" % i))
con.commit()
PYR
RJ="$(printf '{"hook_event_name":"PostToolUseFailure","cwd":"%s","tool_name":"Bash","tool_input":{"command":"cargo test"},"error":"again"}' "$RW" \
      | CLAUDE_PROJECT_DIR="$RW" python3 "$ENGINE" hook 2>&1)"
check "결정된 항목에도 주입이 동작한다" 'additionalContext' "$RJ"
check "보류였음을 알려준다" '승격을 보류한 적이 있다' "$RJ"
check "행 조회 오류가 나지 않는다" '^0$' \
  "$(printf '%s' "$RJ" | grep -c 'No item with that key')"
check "임계에 닿으면 사용자에게도 알린다" 'systemMessage' "$RJ"
rm -rf "$RW"

echo "== 턴 이어붙임과 진전 감지"
# 이 경로에 테스트가 없어서 두 버그를 놓쳤다: root 미정의(즉시 예외), 그리고
# 이어붙임 이벤트가 이벤트 수를 늘려 진전 감지가 자기 자신을 진전으로 센 것.
CW="$(mktemp -d)"
(cd "$CW" && git init -q . && python3 "$ENGINE" init >/dev/null)
ccli() { (cd "$CW" && python3 "$ENGINE" "$@"); }
csql() { python3 - "$CW/.claude/harness/harness.db" "$1" <<'PYC'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
q = sys.argv[2]
if q.count(";") > 1:
    con.executescript(q)
else:
    for row in con.execute(q):
        print("|".join("" if v is None else str(v) for v in row))
con.commit()
PYC
}
cstop() { printf '{"hook_event_name":"Stop","cwd":"%s","prompt_id":"%s","last_assistant_message":"[Scaffolding] 했다"}' "$CW" "$1" \
  | CLAUDE_PROJECT_DIR="$CW" python3 "$ENGINE" hook 2>&1; }
cwork() { csql "INSERT INTO event(at,loop_id,stage,kind,rule,target) VALUES (strftime('%Y-%m-%dT%H:%M:%S','now')||'+0900','$CLID','scaffolding','edit',NULL,'$1');" >/dev/null; }

ccli loop intent "이어붙임" >/dev/null
ccli loop done-when "진전 없으면 멈춘다" >/dev/null
ccli advance >/dev/null
CLID="$(csql "SELECT v FROM meta WHERE k='head'")"

check "예외 없이 동작한다" '^0$' "$(printf '%s' "$(cstop e0)" | grep -c 'is not defined')"
check "1회는 이어붙인다" '"decision": "block"' "$(cstop p1)"
check "이어붙임 횟수를 알려준다" '이어붙임 2/6' "$(cstop p1)"
check "진전이 없으면 3회째에 멈춘다" '진전이 없어 멈춘다' "$(cstop p1)"
check "무엇을 하라고 알려준다" '사람에게 물어라' "$(cstop p1)"
check "멈춘 사실이 기록된다" '^1$' \
  "$(csql "SELECT COUNT(DISTINCT target) FROM event WHERE kind='stop_stalled'")"

echo "  -- 진전이 있으면 상한까지 밀어준다"
i=1
while [ "$i" -le 6 ]; do cwork "f$i.py"; OUT="$(cstop p2)"; i=$((i + 1)); done
check "6회까지는 계속 이어붙인다" '이어붙임 6/6' "$OUT"
cwork "f7.py"
check "상한을 넘으면 턴을 끝낸다" '상한 6회를 소진' "$(cstop p2)"
check "상한 소진도 기록된다" 'continue_limit' \
  "$(csql "SELECT rule FROM event WHERE kind='bypass' AND rule='continue_limit' LIMIT 1")"

echo "  -- 진전이 중간에 끊기면 거기서 멈춘다"
cwork g1.py; cstop p3 >/dev/null
cwork g2.py; cstop p3 >/dev/null
cstop p3 >/dev/null
check "끊긴 뒤 두 번째에 멈춘다" '진전이 없어 멈춘다' "$(cstop p3)"

echo "  -- 종료 조건이 남아 있으면 이어붙이지 않고 그것을 요구한다"
csql "UPDATE stage SET status='pending' WHERE status='active';
      UPDATE stage SET status='active' WHERE stage='verification';" >/dev/null
check "미충족 조건이 이어붙임보다 앞선다" '검증 증거가 없다' \
  "$(printf '{\"hook_event_name\":\"Stop\",\"cwd\":\"%s\",\"prompt_id\":\"pv\",\"last_assistant_message\":\"[Verification] 끝\"}' "$CW" \
     | CLAUDE_PROJECT_DIR="$CW" python3 "$ENGINE" hook)"

echo "  -- 마지막 단계에서는 이어붙이지 않는다"
csql "UPDATE stage SET status='done' WHERE stage != 'compounding';
      UPDATE stage SET status='active' WHERE stage='compounding';
      INSERT OR IGNORE INTO evidence(loop_id,stage,kind,item,at) VALUES('$CLID','compounding','retro_file','r',strftime('%Y-%m-%dT%H:%M:%S','now')||'+0900');" >/dev/null
check "마지막 단계에서는 작업을 닫으라고 안내한다" 'advance --done' \
  "$(printf '{\"hook_event_name\":\"Stop\",\"cwd\":\"%s\",\"prompt_id\":\"pz\",\"last_assistant_message\":\"[Compounding] 끝\"}' "$CW" \
     | CLAUDE_PROJECT_DIR="$CW" python3 "$ENGINE" hook)"

check "하네스 자신의 기록은 진전으로 세지 않는다" 'ok' "$(python3 - "$CW" "$(dirname "$ENGINE")" <<'PYFP'
import sys
sys.path.insert(0, sys.argv[2])
import harness as h
root = sys.argv[1]
con = h.connect(root)
lid = h.head_loop(con)
before = h.progress_fingerprint(con, lid, "scaffolding")
# 하네스 자신의 기록만 늘린다 -> 지문이 그대로여야 한다
with con:
    for kind in h.FP_IGNORE_KINDS:
        h.record_event(con, lid, "scaffolding", kind, "r", "t", "d")
after = h.progress_fingerprint(con, lid, "scaffolding")
assert before == after, ("자기 기록이 진전으로 셌다", before, after)
# 모델 활동은 지문을 바꿔야 한다
with con:
    h.record_event(con, lid, "scaffolding", "edit", None, "z.py")
assert h.progress_fingerprint(con, lid, "scaffolding") != after, "편집이 진전으로 안 셌다"
print("ok")
PYFP
)"
rm -rf "$CW"

echo "== 체인 뒤의 변경 명령도 같은 판정을 받는다"
# `bash_writes` 는 세그먼트를 `mut.search(seg.strip())` 으로 본다. `.strip()` 을
# 빼면 `BASH_SPLIT` 이 남긴 앞 공백 때문에 `^` 앵커가 안 맞아 **`&&`/`;` 뒤의
# 변경 명령이 통째로 샌다.** 코드에는 그 사고가 주석으로 적혀 있는데 검사가
# 없었다 — 변이를 심으니 41종 검사가 전부 초록이었다(4회차 E-F2).
# 첫 세그먼트만 보는 검사는 이 회귀를 절대 못 잡는다. 뒤 세그먼트를 본다.
CHW="$(mktemp -d)"
(cd "$CHW" && git init -q . && python3 "$ENGINE" init >/dev/null)
chb() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Bash","tool_input":{"command":"%s"}}' "$CHW" "$1" \
  | CLAUDE_PROJECT_DIR="$CHW" python3 "$ENGINE" hook; }
chseen() { # 이 저장소에 지금까지 기록된 추측 대상 전부
  python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1] + '/.claude/harness/harness.db')
print(' '.join(r[0] for r in c.execute(
    \"select target from event where kind='bash_write_seen'\")))" "$CHW"; }
# 이제 막지 않으므로 **수집됐는지**를 단정한다. 원래 이 절의 목적이 판정이 아니라
# `BASH_SPLIT` 회귀(뒤 세그먼트가 통째로 새는 것)였다 — 목적은 그대로다.
for C in "true && touch src/a.py" \
         "echo a; touch src/a.py" \
         "cd . && mkdir -p src/newdir" \
         "false || sed -i s/a/b/ .claude/settings.json"; do
  chb "$C" >/dev/null
  check "체인 뒤도 수집된다: $C" '[a-z]' "$(chseen)"
done
# 첫 세그먼트 형태도 계속 막혀야 한다 (한쪽만 고치는 것을 막는다)
chb "touch src/a.py" >/dev/null
check "첫 세그먼트도 여전히 수집된다" 'src/a.py' "$(chseen)"
# 그리고 **차단으로 세지 않는다.** 추측을 `block` 으로 세면 마찰 추세가 거짓이
# 된다 — 이 저장소에는 Bash 만 들어갔으므로 block 은 0이어야 한다.
check "추측은 차단 통계에 섞이지 않는다" '^0$' \
  "$(python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1] + '/.claude/harness/harness.db')
print(len(list(c.execute(\"select 1 from event where kind='block'\"))))" "$CHW")"
# 과잉 차단 반대편: 체인이어도 읽기는 지나간다
check_empty "체인이어도 읽기는 지나간다" "$(chb "cd . && cat README.md")"
rm -rf "$CHW"

echo "== 동의 게이트: 훅과 CLI 가 같은 방식으로 subcommand 를 읽는다"
GW="$(mktemp -d)"
(cd "$GW" && git init -q . && python3 "$ENGINE" init >/dev/null)
gb() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","permission_mode":"%s","tool_name":"Bash","tool_input":{"command":"%s"}}' "$GW" "${2:-default}" "$1" \
  | CLAUDE_PROJECT_DIR="$GW" python3 "$ENGINE" hook; }
W_="$GW/.claude/harness/bin/harness"
check "옵션을 앞에 둬도 게이트가 걸린다" '"permissionDecision": "ask"' \
  "$(gb "$W_ loop --reason=x new")"
check "앞선 무해한 명령으로 우회할 수 없다" '"permissionDecision": "ask"' \
  "$(gb "$W_ status; $W_ loop new --reason x")"
# JSON 안에서 이스케이프가 필요 없는 홑따옴표를 쓴다. 겹따옴표를 쓰면 셸이 먼저
# 벗겨서 JSON 이 깨지고, 훅이 조용히 0 을 반환해 검사가 무의미해진다.
check "따옴표 안 공백이 위치 인자를 흐리지 않는다" '"permissionDecision": "ask"' \
  "$(gb "$W_ loop new --reason 'a b'")"
check "홑따옴표 값도 사유로 인식된다" '사유: a b' \
  "$(gb "$W_ loop new --reason 'a b'")"
check "&& 로 이어도 걸린다" '"permissionDecision": "ask"' \
  "$(gb "$W_ recall x && $W_ allow docs/x.md --reason y")"
check "bypassPermissions 에서 옵션 배치도 사전 승인" '"permissionDecision": "defer"' \
  "$(gb "$W_ loop --reason=x new" bypassPermissions)"
check_empty "동의 불필요 명령은 조용하다 (status)" "$(gb "$W_ status")"
check_empty "동의 불필요 명령은 조용하다 (loop)" "$(gb "$W_ loop")"
check_empty "동의 불필요 명령은 조용하다 (metrics)" "$(gb "$W_ metrics")"
check_empty "동의 불필요 명령은 조용하다 (recall)" "$(gb "$W_ recall npm test")"

echo "== Bash 보호 경로: 리다이렉트·옵션값·find 액션"
for BAD in "find .claude/harness -name harness.db -delete" \
           "dd if=/dev/null of=.claude/harness/harness.db" \
           "printf x >|.claude/harness/LEARNED.md" \
           "find .claude/harness -name '*.py' -exec rm {} +" \
           "rm -rf .claude" \
           "ln -sf /dev/null .claude/harness/harness.db"; do
  check "차단: $BAD" 'Bash 로도 변경할 수 없다' "$(gb "$BAD")"
done
# `mkdir -p .claude/hooks` 는 여기서 빠졌다. 이 절은 **바닥값**을 검사하는데 그 명령은
# 바닥값이 아니라 **단계 규칙** 질문이고, 이제 Bash 도 그 규칙을 받는다. 아래 절에서
# Write 와 판정이 같은지로 검사한다 — 예전에는 이 목록이 "Bash 는 단계 규칙을 안
# 받는다"는 비대칭을 기대값으로 굳혀 놓고 있었다.
# `rm src/tmp.txt` 도 여기서 빠졌다. Write 로 같은 경로를 쓰면 거부되므로, Bash 만
# 허용하는 것은 **비대칭(=버그)을 기대값으로 굳히는 것**이다. 아래 대칭 검사가 본다.
for OK in "cat .claude/harness/LEARNED.md" \
          "find . -name '*.py'" "grep -r foo src/"; do
  check_empty "허용: $OK" "$(gb "$OK")"
done

echo "== Bash 와 Write 의 판정은 **의도적으로 갈린다** (경계와 가시성)"
# 예전 불변식은 "두 문이 같은 판정" 이었다. 그 불변식은 **은퇴했다.**
#
# 같으려면 Bash 명령에서 정확한 경로를 알아내야 하는데 그건 정적으로 결정
# 불가능하다 — 19번 고쳤고 매번 다음이 있었다
# (`.dev/shell-write-detection-is-undecidable.md`). 그래서 층을 갈랐다:
#
#   Write/Edit : 도구가 경로를 그대로 준다 → **추측 없음 → 막는다**
#   Bash       : 추측이다              → **막지 않고 기록한다**
#   바닥값      : 둘 다 막는다 (경계는 그대로다)
#
# 새 불변식도 경로마다 두 문에 같은 질문을 넣어 확인한다. 갈리는 것 자체가
# 계약이므로, **갈리는 방식**을 단정한다.
SYMW="$(mktemp -d)"
(cd "$SYMW" && git init -q . && python3 "$ENGINE" init >/dev/null && mkdir -p src docs/01-a .dev/plan .claude/hooks && touch src/a.py docs/01-a/01-n.md)
sym_decision() { # <tool-json> -> deny|ask|allow|-
  printf '%s' "$1" | CLAUDE_PROJECT_DIR="$SYMW" python3 -c '
import json, sys
o = json.loads(sys.stdin.read() or "{}")
print((o.get("hookSpecificOutput") or {}).get("permissionDecision", "-"))'
}
sym_pair() { # <rel> -> "write=X bash=Y"
  local RJ BJ
  RJ="$(printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s","content":"x"}}' "$SYMW" "$1" \
    | CLAUDE_PROJECT_DIR="$SYMW" python3 "$ENGINE" hook)"
  BJ="$(printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Bash","tool_input":{"command":"printf x > %s"}}' "$SYMW" "$1" \
    | CLAUDE_PROJECT_DIR="$SYMW" python3 "$ENGINE" hook)"
  printf 'write=%s bash=%s' "$(sym_decision "$RJ")" "$(sym_decision "$BJ")"
}
for REL in "src/a.py" "docs/01-a/01-n.md" "docs/새.md" ".claude/hooks/x.json" \
           ".dev/plan/p.md" ".claude/harness/stages.json" "README.md"; do
  SP="$(sym_pair "$REL")"
  case "$SP" in
    # Write 가 막는 곳은 Bash 가 통과시킨다. 둘 다 통과하는 곳은 그대로 둘 다 통과.
    "write=deny bash=-"|"write=- bash=-")
      PASS=$((PASS + 1)); printf '  ok   %s: 계약대로 (%s)\n' "$REL" "$SP" ;;
    *) FAIL=$((FAIL + 1)); printf '  FAIL %s: 계약과 다르다\n     %s\n' "$REL" "$SP" ;;
  esac
done
# 둘 다 통과여도 위 검사가 초록이 된다 — **Write 가 실제로 막는 경로가 하나는
# 있어야** 구분력이 생긴다. 그것이 곧 "경계는 살아 있다" 의 단정이다.
check "Write 는 실제로 막는다" 'write=deny' "$(sym_pair ".claude/hooks/x.json")"
# 그리고 Bash 는 통과시키되 **기록을 남긴다.** 통과만 확인하면 기능이 통째로
# 죽어도 초록이다.
printf '%s' "$(printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Bash","tool_input":{"command":"sed -i s/a/b/ .claude/hooks/x.json"}}' "$SYMW")" \
  | CLAUDE_PROJECT_DIR="$SYMW" python3 "$ENGINE" hook >/dev/null
check "sed -i 는 막히지 않는다" '^-$' \
  "$(sym_decision "$(printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Bash","tool_input":{"command":"sed -i s/a/b/ .claude/hooks/x.json"}}' "$SYMW" | CLAUDE_PROJECT_DIR="$SYMW" python3 "$ENGINE" hook)")"
check "그 대신 기록이 남는다" 'x.json' \
  "$(python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1] + '/.claude/harness/harness.db')
print(' '.join(r[0] for r in c.execute(
    \"select target from event where kind='bash_write_seen'\")))" "$SYMW")"
check "기록은 recall 로 다시 찾아진다" 'bash_write_seen' \
  "$( (cd "$SYMW" && python3 "$ENGINE" recall 2>&1) )"
# 바닥값은 Bash 로도 그대로 막힌다 — 경계는 이 변경에서 하나도 약해지지 않는다.
check "바닥값은 Bash 로도 막힌다" '"permissionDecision": "deny"' \
  "$(printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Bash","tool_input":{"command":"sed -i s/a/b/ .claude/harness/bin/harness"}}' "$SYMW" \
     | CLAUDE_PROJECT_DIR="$SYMW" python3 "$ENGINE" hook)"
# 훅에서 실제로 `ask` 가 나오는지. 판정 함수만 검사하면 훅이 그 값을 안 쓸 수 있다.
OPQ="$(printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Bash","tool_input":{"command":"ls | xargs rm"}}' "$SYMW" \
  | CLAUDE_PROJECT_DIR="$SYMW" python3 "$ENGINE" hook)"
check "대상을 모르면 훅이 사람에게 묻는다" '"permissionDecision": "ask"' "$OPQ"
check "무엇을 모르는지 말한다" '무엇을 바꿀지 하네스가 알 수 없다' "$OPQ"
check "무엇을 하면 되는지 알려준다" '대상이 분명한 형태로' "$OPQ"
check_absent "정상 명령에는 묻지 않는다" '"ask"' \
  "$(printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Bash","tool_input":{"command":"ls | xargs cat"}}' "$SYMW" \
     | CLAUDE_PROJECT_DIR="$SYMW" python3 "$ENGINE" hook)"
rm -rf "$SYMW"

echo "== 일회성 쓰기 예외는 병렬 훅에서도 한 번만 쓰인다"
python3 - "$GW/.claude/harness/harness.db" <<'PYG'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
lid = con.execute("SELECT v FROM meta WHERE k='head'").fetchone()[0]
con.execute("INSERT INTO wgrant(loop_id,glob,reason,uses_left,at) "
            "VALUES(?,?,?,?,datetime('now'))", (lid, "docs/**", "t", 1))
con.commit()
PYG
for i in 1 2 3 4; do
  printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s/docs/spec/00%d-a.md"}}' \
    "$GW" "$GW" "$i" | CLAUDE_PROJECT_DIR="$GW" python3 "$ENGINE" hook > "$GW/g$i" 2>&1 &
done
wait
check "통과한 쓰기는 하나뿐" '^1$' \
  "$(n=0; for i in 1 2 3 4; do [ -s "$GW/g$i" ] || n=$((n+1)); done; echo $n)"
check "나머지는 차단된다" '^3$' \
  "$(n=0; for i in 1 2 3 4; do grep -q deny "$GW/g$i" && n=$((n+1)); done; echo $n)"
check "uses_left 가 음수로 내려가지 않는다" '^0$' \
  "$(python3 -c "
import sqlite3,sys
print(sqlite3.connect(sys.argv[1]).execute('SELECT uses_left FROM wgrant').fetchone()[0])
" "$GW/.claude/harness/harness.db")"

echo "== prompt_id 가 없으면 세션별로 예산을 가둔다"
GL="$(python3 -c "
import sqlite3,sys
print(sqlite3.connect(sys.argv[1]).execute(\"SELECT v FROM meta WHERE k='head'\").fetchone()[0])
" "$GW/.claude/harness/harness.db")"
python3 - "$GW/.claude/harness/harness.db" "$GL" <<'PYS'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("UPDATE stage SET status='pending' WHERE status='active'")
con.execute("UPDATE stage SET status='active' WHERE stage='scaffolding' AND loop_id=?",
            (sys.argv[2],))
con.commit()
PYS
gs() { printf '{"hook_event_name":"Stop","cwd":"%s","session_id":"%s","last_assistant_message":"[Scaffolding] x"}' "$GW" "$1" \
  | CLAUDE_PROJECT_DIR="$GW" python3 "$ENGINE" hook; }
gs sA >/dev/null; gs sA >/dev/null
check "같은 세션은 3회째에 멈춘다" '진전이 없어 멈춘다' "$(gs sA)"
check "다른 세션은 예산이 새로 시작된다" '"decision": "block"' "$(gs sB)"
check "세션별로 따로 집계된다" '^2$' \
  "$(python3 -c "
import sqlite3,sys
print(sqlite3.connect(sys.argv[1]).execute(
  \"SELECT COUNT(DISTINCT target) FROM event WHERE kind='stop_continue'\").fetchone()[0])
" "$GW/.claude/harness/harness.db")"
rm -rf "$GW"

echo "== 회고의 질문과 형식"
RW2="$(mktemp -d)"
(cd "$RW2" && git init -q . && python3 "$ENGINE" init >/dev/null)
r2() { (cd "$RW2" && python3 "$ENGINE" "$@"); }
rpy() { python3 - "$RW2" "$(dirname "$ENGINE")" "$@"; }
r2 loop intent "회고 확인" >/dev/null
r2 loop done-when "키가 확인된다" >/dev/null
# 엔진의 now() 로 이벤트를 심는다. SQL strftime 은 UTC 라 창 밖으로 밀린다.
rpy <<'PYR' >/dev/null
import sys; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1]); lid = h.head_loop(con)
with con:
    h.record_event(con, lid, "execution", "tool_fail", "Bash", "npm test")
    h.record_event(con, lid, "execution", "block", "docs_readonly", "docs/a.md")
    con.execute("UPDATE stage SET status='pending' WHERE status='active'")
    con.execute("UPDATE stage SET status='active' WHERE stage='verification'")
    h.record_evidence(con, lid, "verification", "verification_evidence", "t")
PYR
ROUT="$(r2 advance)"
check "회고 질문을 제시한다" '회고에 답할 것' "$ROUT"
check "성공 쪽을 묻는다 (ExpeL 의 짝)" '무엇이 통했나' "$ROUT"
check "잘못된 가정을 묻는다" '무엇을 잘못 가정했나' "$ROUT"
check "회차를 싸게 만들 것을 묻는다" '싸게 만들었을 것' "$ROUT"
check "검색 키를 그대로 알려준다" '검색 키' "$ROUT"
check "관측된 명령이 키에 들어간다" 'npm test' "$ROUT"
check "관측된 규칙도 키에 들어간다" 'docs_readonly' "$ROUT"
check "키가 없으면 안 찾아진다고 경고한다" '찾아지지 않는다' "$ROUT"
check "질문이 관측 목록보다 앞에 온다" '^1$' \
  "$(printf '%s' "$ROUT" | awk '/회고에 답할 것/{q=NR} /관측된 것/{o=NR} END{print (q && o && q<o) ? 1 : 0}')"

# 키가 빠진 회고 -> 알리되 막지 않는다
mkdir -p "$RW2/.dev/retrospect"
RPRE="$(rpy <<'PYP'
import sys; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1]); lid = h.head_loop(con)
with con: h.record_evidence(con, lid, "compounding", "retro_file", "r")
print(h.file_prefix(con, lid))
PYP
)"
printf '# 회고\n- 테스트가 위치 때문에 실패했다.\n' > "$RW2/.dev/retrospect/${RPRE}retro.md"
COUT="$(r2 advance --cycle)"
check "키가 빠지면 알려준다" '검색 키 2개 중 2개가 빠졌다' "$COUT"
check "막지 않고 회차는 닫힌다" '회차 1 기록' "$COUT"
check "확인 결과가 event 로 남는다" 'found=0/2' \
  "$(rpy <<'PYQ'
import sys; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1])
r = con.execute("SELECT detail FROM event WHERE kind='retro_keys' ORDER BY id").fetchone()
print(r["detail"] if r else "")
PYQ
)"

# 키를 넣은 회고 -> 전부 들어 있다고 확인
# 회차 경계는 **배타적**이다(종료 시각 +1초). 테스트는 한 초 안에 다 끝나므로
# 새 회차의 이벤트는 시각을 명시해 심는다. 실사용에서는 회차 간격이 분 단위다.
rpy <<'PYS' >/dev/null
import sys, time; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1]); lid = h.head_loop(con)
later = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() + 5))
with con:
    con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target) "
                "VALUES(?,?,?,?,?,?)",
                (later, lid, "execution", "tool_fail", "Bash", "npm test"))
    con.execute("UPDATE stage SET status='pending' WHERE status='active'")
    con.execute("UPDATE stage SET status='active' WHERE stage='compounding'")
    h.record_evidence(con, lid, "compounding", "retro_file", "r")
PYS
RPRE2="$(rpy <<'PYT'
import sys; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1])
print(h.file_prefix(con, h.head_loop(con)))
PYT
)"
printf '# 회고 — `npm test`\n- `npm test` 는 루트에서 돌려야 한다.\n' \
  > "$RW2/.dev/retrospect/${RPRE2}retro.md"
check "키가 다 들어가면 그렇다고 알린다" '전부 들어 있다' "$(r2 advance --done)"

# 형식이 실제로 찾아짐을 결정한다 (이 기능의 존재 이유)
printf '# 회고\n테스트 실행이 파일을 못 찾아 실패했다.\n' \
  > "$RW2/.dev/retrospect/${RPRE}paraphrase.md"
check "키가 없는 회고는 recall 에 안 걸린다" '^0$' \
  "$(r2 recall 'npm test' | grep -c 'paraphrase')"
check "키가 있는 회고는 recall 에 걸린다" '^1$' \
  "$(r2 recall 'npm test' | grep -c "${RPRE2}retro.md")"

# 회차 경계는 **시각이 아니라 id** 로 나뉜다. 예전에는 종료 시각 +1초를 배타 경계로
# 뒀고, 그래서 종료와 같은 초에 일어난 **다음 회차의** 이벤트가 어느 회차에도 속하지
# 못하고 영영 사라졌다. 아래는 세 이벤트를 실제 순서대로 넣고 경계가 그 순서를 따르는지
# 본다 — 세 개 모두 같은 초다.
check "회차 경계는 시각이 아니라 순서를 따른다" 'ok' \
  "$(rpy <<'PYB'
import sys; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1])
lid = "260101-bbbbbb"
same = "2026-01-01T12:00:00+0900"       # 셋 다 **같은 초**
with con:
    con.execute("INSERT OR IGNORE INTO loop(id,created_at) VALUES(?,?)",
                (lid, "2026-01-01T10:00:00+0900"))
    con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target) "
                "VALUES(?,?,?,?,?,?)",
                (same, lid, "execution", "tool_fail", "Bash", "old cmd"))
    con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target,detail) "
                "VALUES(?,?,?,?,?,?,?)",
                (same, lid, "compounding", "cycle_close", "1", lid + "-1", "{}"))
    con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target) "
                "VALUES(?,?,?,?,?,?)",
                (same, lid, "execution", "tool_fail", "Bash", "new cmd"))
keys = h.cycle_search_keys(con, lid, h.cycle_window_start(con, lid))
assert keys == ["new cmd"], ("경계가 순서를 따르지 않는다", keys)
print("ok")
PYB
)"
rm -rf "$RW2"

echo "== 불가능한 스킵은 묻지 않고 거부한다 (다이얼로그 무한 반복 방지)"
# 도그푸딩에서 나온 버그: Selection 에 작업이 없을 때 모델이 skip 을 시도하면
# ask 가 뜨고, 승인 뒤에 거부되고, 안내 메시지가 `skip until:selection` 이라는
# **항상 실패하는 명령**을 알려줘서 모델이 그것을 반복했다. 승인 요청이 무한히 떴다.
SW="$(mktemp -d)"
(cd "$SW" && git init -q . && python3 "$ENGINE" init >/dev/null)
sw() { (cd "$SW" && python3 "$ENGINE" "$@"); }
# JSON 안에서는 홑따옴표를 쓴다. 겹따옴표는 셸이 먼저 벗겨 JSON 이 깨진다.
sb() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","permission_mode":"%s","tool_name":"Bash","tool_input":{"command":"%s"}}' "$SW" "${2:-default}" "$1" \
  | CLAUDE_PROJECT_DIR="$SW" python3 "$ENGINE" hook; }
SH_="$SW/.claude/harness/bin/harness"

check "Selection 스킵은 묻지 않고 거부" '"permissionDecision": "deny"' \
  "$(sb "$SH_ skip selection --reason x")"
check "스킵이 아니라 작업을 고르는 것이 다음 행동이라고 말한다" '작업을 고르는 것' \
  "$(sb "$SH_ skip selection --reason x")"
check "후보를 어디서 보는지 알려준다" 'harness status' \
  "$(sb "$SH_ skip selection --reason x")"
check "정말 없으면 멈추는 것이 정상 종료라고 말한다" '정상 종료' \
  "$(sb "$SH_ skip selection --reason x")"
check "until:selection 도 거부 (항상 실패하는 명령이었다)" '뒤로 갈 수는 없다' \
  "$(sb "$SH_ skip until:selection --reason x")"
check "+1 도 Selection 을 포함하면 거부" '"permissionDecision": "deny"' \
  "$(sb "$SH_ skip +1 --reason x")"
check "불가능한 스킵도 기록에 남는다" 'skip_impossible' \
  "$(python3 -c "
import sqlite3,sys
print(sqlite3.connect(sys.argv[1]).execute(
  \"SELECT rule FROM event WHERE kind='block' AND rule='skip_impossible' LIMIT 1\"
).fetchone() or '')" "$SW/.claude/harness/harness.db")"
for M in acceptEdits plan bypassPermissions; do
  check "권한 모드 $M 에서도 같은 거부" '"permissionDecision": "deny"' \
    "$(sb "$SH_ skip selection --reason x" "$M")"
done

echo "  -- 안내받은 명령이 실제로 동작해야 한다"
sw loop intent "작업" >/dev/null
sw loop done-when "끝" >/dev/null
sw advance >/dev/null
# Selection 을 지난 뒤에는 '회차 중단' 안내가 맞다. Selection 에 있을 때와 다르다.
check "Compounding 스킵 거부는 until:compounding 을 안내한다" 'until:compounding' \
  "$(sb "$SH_ skip compounding --reason x")"
check "정상 스킵은 여전히 승인을 받는다" '"permissionDecision": "ask"' \
  "$(sb "$SH_ skip context --reason '불필요'")"
check "기록이 남지 않았으면 훅도 미리 거부한다 (승인 뒤 거부 금지)" \
  '"permissionDecision": "deny"' \
  "$(sb "$SH_ skip until:compounding --reason '구조 선행'")"
check "안내받은 명령이 실제로 실행된다 (거부 이유가 다르다)" '기록은 남겨야 한다' \
  "$(sw skip until:compounding --reason '구조 선행')"

echo "== 사람만 채울 수 있는 조건이 남으면 턴을 밀지 않는다"
SW2="$(mktemp -d)"
(cd "$SW2" && git init -q . && python3 "$ENGINE" init >/dev/null)
s2() { (cd "$SW2" && python3 "$ENGINE" "$@"); }
sstop() { printf '{"hook_event_name":"Stop","cwd":"%s","session_id":"%s","last_assistant_message":"[%s] 했습니다"}' "$SW2" "$1" "$2" \
  | CLAUDE_PROJECT_DIR="$SW2" python3 "$ENGINE" hook; }
check "Selection 에 작업이 없으면 턴을 끝낸다" '사람의 입력을 기다린다' \
  "$(sstop sA Selection)"
check "무엇을 기다리는지 말한다" 'intent_set' "$(sstop sA Selection)"
check "이어붙이지 않는다" '^0$' \
  "$(printf '%s' "$(sstop sA Selection)" | grep -c '이어서 진행하라')"
# Planning 에서 계획은 썼고 승인만 남은 상태
s2 loop intent "작업" >/dev/null
s2 loop done-when "끝" >/dev/null
s2 advance >/dev/null; s2 advance >/dev/null; s2 advance >/dev/null
python3 - "$SW2" "$(dirname "$ENGINE")" <<'PYP' >/dev/null
import os, sys; sys.path.insert(0, sys.argv[2])
import harness as h
root = sys.argv[1]; con = h.connect(root); lid = h.head_loop(con)
with con:
    h.record_evidence(con, lid, "planning", "plan_file", "p")
PYP
check "Planning 에서 승인 대기 중이면 밀지 않는다" 'plan_approved' "$(sstop sB Planning)"
rm -rf "$SW" "$SW2"

echo "== Selection: 새 작업이 없을 때 하네스가 할 일을 내놓는다"
# "새 작업이 없다" 가 "할 일이 없다" 는 뜻이 아니다. 승격 결정, 재발한 승격,
# 낡은 인덱스는 전부 복리를 유지하는 일이고 사람이 주지 않아도 존재한다.
# 이걸 내놓지 않으면 무인 실행이 Selection 에서 멈춘다.
CW3="$(mktemp -d)"
(cd "$CW3" && git init -q . && python3 "$ENGINE" init >/dev/null)
c3() { (cd "$CW3" && python3 "$ENGINE" "$@"); }
python3 - "$CW3" "$(dirname "$ENGINE")" <<'PYW' >/dev/null
import sys; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1])
with con:
    for i, l in enumerate(("250601-a", "250602-b", "250603-c")):
        con.execute("INSERT OR IGNORE INTO loop(id,created_at,closed_at) VALUES(?,?,?)",
                    (l, "2025-06-0%dT10:00:00+0900" % (i + 1), "x"))
        con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target) "
                    "VALUES(?,?,?,?,?,?)",
                    ("2025-06-0%dT11:00:00+0900" % (i + 1), l, "execution",
                     "block", "docs_readonly", "docs/a.md"))
    con.execute("INSERT OR REPLACE INTO promotion(key,kind,decision,maturity,note,loop_id,at,recheck_at) VALUES(?,?,?,?,?,?,?,?)",
                ("block:loop_prefix", "block", "hook", "regressed", "n", "x",
                 "2025-05-01T00:00:00+0900", "2025-05-01T00:00:00+0900"))
PYW
SJ3="$(c3 status)"
check "status 가 할 일 후보를 내놓는다" '하네스가 아는 할 일' "$SJ3"
check "승격 결정을 후보로 준다" '승격 결정' "$SJ3"
check "재발한 승격도 후보다" '재발한 승격' "$SJ3"
check "실행할 명령을 함께 준다" 'harness promote' "$SJ3"
check "정말 없으면 멈추라고 말한다" '그렇다고 말하고 멈춰라' "$SJ3"
check "--json 에도 실린다" 'candidates' "$(c3 status --json)"

echo "  -- 스킵 거부가 '물어라' 가 아니라 '고르라' 로 안내한다"
sb3() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","permission_mode":"bypassPermissions","tool_name":"Bash","tool_input":{"command":"%s"}}' "$CW3" "$1" \
  | CLAUDE_PROJECT_DIR="$CW3" python3 "$ENGINE" hook; }
SR3="$(sb3 "$CW3/.claude/harness/bin/harness skip selection --reason x")"
check "작업을 고르는 것이 다음 행동" '작업을 고르는 것' "$SR3"
check "후보를 어디서 보는지 알려준다" 'harness status' "$SR3"
check "멈추는 것이 정상 종료라고 말한다" '정상 종료' "$SR3"

echo "  -- 턴 종료 메시지가 후보 개수를 알려준다"
check "할 일이 있으면 개수를 말한다" '아는 할 일이' \
  "$(printf '{\"hook_event_name\":\"Stop\",\"cwd\":\"%s\",\"session_id\":\"sX\",\"last_assistant_message\":\"[Selection] 없습니다\"}' "$CW3" \
     | CLAUDE_PROJECT_DIR="$CW3" python3 "$ENGINE" hook)"

echo "  -- 작업이 정해지면 후보는 소음이므로 내놓지 않는다"
c3 loop intent "정해진 작업" >/dev/null
check "intent 가 있으면 후보를 숨긴다" '^0$' \
  "$(c3 status | grep -c '하네스가 아는 할 일')"
rm -rf "$CW3"

echo "== Ctx 언팩 누락 (AST 전수)"
CRC2=0; COUT="$(python3 "$(dirname "$0")/ctx_check.py" "$MC" 2>&1)" || CRC2=$?
check "ctx 를 풀지 않고 쓰는 함수가 없다" '^0$' "$CRC2"
# `[0-9]*` 는 `함수 0개` 에도 맞았다 — 0개를 훑고도 '훑었다'가 통과했다.
check "검사가 실제로 함수를 훑었다" '스코프 [1-9][0-9]* *개' "$COUT"

echo "== 계획 승인 다이얼로그가 계획을 보여준다"
# auto-mode(acceptEdits)에서도 계획 승인은 물어야 한다 — "편집 자동 수락" 이지
# "계획 검토 생략" 이 아니다. 다만 다이얼로그가 파일 이름만 보여주면 읽지 않고
# 찍는 도장이 되고, 그렇게 남은 plan_approved 기록은 가짜다.
PW="$(mktemp -d)"
(cd "$PW" && git init -q . && python3 "$ENGINE" init >/dev/null)
pw() { (cd "$PW" && python3 "$ENGINE" "$@"); }
pb() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","permission_mode":"%s","tool_name":"Bash","tool_input":{"command":"%s"}}' "$PW" "$2" "$1" \
  | CLAUDE_PROJECT_DIR="$PW" python3 "$ENGINE" hook; }
pw loop intent "결제 리팩터" >/dev/null
pw loop done-when "테스트 통과" >/dev/null
pw advance >/dev/null; pw advance >/dev/null; pw advance >/dev/null
PPRE="$(python3 - "$PW" "$(dirname "$ENGINE")" <<'PYP'
import sys; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1])
print(h.file_prefix(con, h.head_loop(con)))
PYP
)"
mkdir -p "$PW/.dev/plan"
printf '# 결제 리팩터\n\n## 목표\n전략 패턴으로 분리한다.\n\n## 위험\n결제는 되돌릴 수 없다.\n' \
  > "$PW/.dev/plan/${PPRE}plan.md"
PCMD="$PW/.claude/harness/bin/harness approve-plan .dev/plan/${PPRE}plan.md"

for M in default acceptEdits plan; do
  check "$M 에서 계획 승인은 물어본다" '"permissionDecision": "ask"' "$(pb "$PCMD" "$M")"
done
check "bypassPermissions 는 사전 승인으로 통과" '"permissionDecision": "defer"' \
  "$(pb "$PCMD" bypassPermissions)"
POUT2="$(pb "$PCMD" acceptEdits)"
check "다이얼로그가 계획 본문을 보여준다" '전략 패턴으로 분리한다' "$POUT2"
check "위험 절도 보인다" '되돌릴 수 없다' "$POUT2"
check "계획 구역을 표시한다" '─── 계획 ───' "$POUT2"
check "파일 경로도 알려준다" "${PPRE}plan.md" "$POUT2"
check "계획 파일이 없으면 경고한다" '계획 파일이 없다' \
  "$(pb "$PW/.claude/harness/bin/harness approve-plan .dev/plan/nope.md" default)"
check "긴 계획은 잘라서 알려준다" '이하 .*줄 생략' \
  "$(python3 -c "
import sys
open(sys.argv[1], 'w').write('# 계획\n' + '내용 줄\n' * 200)" "$PW/.dev/plan/${PPRE}long.md"; \
     pb "$PW/.claude/harness/bin/harness approve-plan .dev/plan/${PPRE}long.md" default)"
rm -rf "$PW"

echo "== plan mode: 확정 뒤에 파일을 쓴다"
# plan mode 는 파일 쓰기를 막으므로 그 안에서 plan_file 을 채울 수 없다.
# 그래서 순서를 뒤집는다 — 계획을 대화로 확정하고, 나온 뒤에 확정본만 파일로.
MW2="$(mktemp -d)"
(cd "$MW2" && git init -q . && python3 "$ENGINE" init >/dev/null)
m2() { (cd "$MW2" && python3 "$ENGINE" "$@"); }
check "Planning 안내가 파일을 먼저 쓰지 말라고 한다" '파일을 먼저 쓰지 마라' \
  "$(python3 -c "
import json,sys
c=json.load(open(sys.argv[1]))
print([s for s in c['stages'] if s['id']=='planning'][0]['hint'])" \
  "$MW2/.claude/harness/stages.json")"
check "확정본을 그대로 쓰라고 한다" '확정본을 그대로' \
  "$(python3 -c "
import json,sys
c=json.load(open(sys.argv[1]))
print([s for s in c['stages'] if s['id']=='planning'][0]['hint'])" \
  "$MW2/.claude/harness/stages.json")"

m2 loop intent "계획" >/dev/null
m2 loop done-when "끝" >/dev/null
m2 advance >/dev/null; m2 advance >/dev/null; m2 advance >/dev/null
printf '{"hook_event_name":"PostToolUse","cwd":"%s","session_id":"s1","tool_name":"ExitPlanMode","tool_input":{"plan":"# 계획"},"tool_response":{"approved":true}}' "$MW2" \
  | CLAUDE_PROJECT_DIR="$MW2" python3 "$ENGINE" hook >/dev/null
check "ExitPlanMode 를 관측해 기록한다" 'ExitPlanMode' \
  "$(python3 -c "
import sqlite3,sys
r=sqlite3.connect(sys.argv[1]).execute(
  \"SELECT detail FROM event WHERE kind='plan_mode_exit'\").fetchone()
print(r[0] if r else '')" "$MW2/.claude/harness/harness.db")"
# **아직 증거로 쓰지 않는다.** 거절 시에도 훅이 뜨는지 모르기 때문이다.
check "그것만으로 plan_approved 가 서지 않는다" 'plan_approved' \
  "$(python3 - "$MW2" "$(dirname "$ENGINE")" <<'PYE'
import sys; sys.path.insert(0, sys.argv[2])
import harness as h
root = sys.argv[1]; con = h.connect(root)
cfg = h.load_config(root, None)
print(",".join(h.exit_blockers(con, cfg, root, h.head_loop(con), "planning")))
PYE
)"
check "관측만으로 게이트가 열리지 않는다" '^0$' \
  "$(python3 - "$MW2" "$(dirname "$ENGINE")" <<'PYF'
import sys; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1])
print(1 if h.has_evidence(con, h.head_loop(con), "plan_approved") else 0)
PYF
)"
rm -rf "$MW2"

echo "== 모드 사전승인은 회피로 세지 않는다"
# bypassPermissions 로 돌리면 모든 동의 명령이 bypass 로 적립된다. 그걸 회피로
# 세면 무인 실행이 곧바로 "게이트가 연극이 되고 있다" 로 판정된다 — 사람이
# 그렇게 하라고 지시한 것인데. preauth 열로 갈라 판정에서 뺀다.
AW="$(mktemp -d)"
(cd "$AW" && git init -q . && python3 "$ENGINE" init >/dev/null)
aw() { (cd "$AW" && python3 "$ENGINE" "$@"); }
aw loop intent "무인" >/dev/null
aw loop done-when "끝" >/dev/null
aw advance >/dev/null
for i in 1 2 3; do
  printf '{"hook_event_name":"PreToolUse","cwd":"%s","permission_mode":"bypassPermissions","tool_name":"Bash","tool_input":{"command":".claude/harness/bin/harness allow docs/x%d.md --reason 스펙"}}' "$AW" "$i" \
    | CLAUDE_PROJECT_DIR="$AW" python3 "$ENGINE" hook >/dev/null
done
python3 - "$AW" "$(dirname "$ENGINE")" <<'PYA' >/dev/null
import sys; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1]); lid = h.head_loop(con)
with con:
    h.record_event(con, lid, "verification", "bypass", "prefix", "verification", "상한 소진")
PYA
ACNT="$(python3 - "$AW" "$(dirname "$ENGINE")" <<'PYB'
import sys; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1]); lid = h.head_loop(con)
c = h.cycle_counters(con, lid, h.cycle_window_start(con, lid))
print("preauth=%d bypass=%d" % (c["preauth"], c["bypass"]))
PYB
)"
check "사전승인 3건을 따로 센다" 'preauth=3' "$ACNT"
check "실제 우회 1건만 우회로 센다" 'bypass=1' "$ACNT"

echo "  -- 판정이 사전승인에 흔들리지 않는다"
AV="$(python3 - "$(dirname "$ENGINE")" <<'PYV'
import sys; sys.path.insert(0, sys.argv[1])
import harness as h
mk = lambda b, r, by, pre: {"blocks": b, "refails": r, "bypass": by, "skips": 0,
                            "declines": 0, "churn": 0, "preauth": pre}
out = []
out.append("무인=%s" % h.trend_verdict([mk(5, 3, 0, 0), mk(2, 1, 0, 40)]))
out.append("실제회피=%s" % h.trend_verdict([mk(5, 3, 0, 0), mk(2, 1, 3, 0)]))
out.append("둘다=%s" % h.trend_verdict([mk(5, 3, 0, 0), mk(2, 1, 3, 40)]))
print(" ".join(out))
PYV
)"
check "사전승인만 늘면 개선 신호" '무인=improving' "$AV"
check "실제 우회가 늘면 회피" '실제회피=evasion' "$AV"
check "실제 우회가 있으면 사전승인과 무관하게 회피" '둘다=evasion' "$AV"
# 추세 표는 회차 스냅샷이 있어야 나오고, 판정은 구간이 둘 이상이어야 나온다
# (_bucket 은 6개 미만이면 한 구간으로 묶는다). 6개를 심는다.
python3 - "$AW" "$(dirname "$ENGINE")" <<'PYC' >/dev/null
import json, sys; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1])
with con:
    for i in range(6):
        snap = dict(cycle=1, dur=100, blocks=6 - i, fails=3, refails=max(0, 2 - i // 2),
                    churn=2, edits=5, gates=0, bypass=0, skips=0, declines=0,
                    preauth=0 if i < 3 else 30, promotes=0)
        con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target,detail) "
                    "VALUES(?,?,?,?,?,?,?)",
                    ("2026-0%d-01T10:00:00+0900" % (i + 1), "z", "compounding",
                     "cycle_close", "1", "z-%d" % i, json.dumps(snap)))
PYC
AM="$(aw metrics)"
check "metrics 표에 사전승인 열이 있다" '사전승인' "$AM"
check "사전승인만 올라도 개선 신호로 읽는다" '개선 신호' "$AM"
rm -rf "$AW"

echo "== 한 번뿐인 일은 조건부 UPDATE 로 차지한다 (병렬)"
# 읽고-판단하고-쓰면 병렬 훅이 같은 자원을 여러 번 쓴다. 판단을 WHERE 절 안으로
# 옮기고 rowcount 로 승자를 정한다 — 쓰기 예외가 이미 쓰던 방법을 전이에도 쓴다.
PLW="$(mktemp -d)"
(cd "$PLW" && git init -q . && python3 "$ENGINE" init >/dev/null)
pl() { (cd "$PLW" && python3 "$ENGINE" "$@"); }
pl loop intent "병렬 검사" >/dev/null
pl loop done-when "끝" >/dev/null
for i in 1 2 3 4; do (cd "$PLW" && python3 "$ENGINE" advance > "$PLW/ad$i" 2>&1) & done
wait
# **경합의 결과를 기대값으로 박지 않는다.** 넷이 실제로 겹치는지는 스케줄러가 정하고,
# 안 겹치면 넷 다 정상적으로 한 단계씩 나아간다(그것도 옳다). 검사할 것은 불변식이다:
#   성공 + 거절 = 4  (아무도 조용히 사라지지 않는다)
#   활성 단계 = 1     (전이가 겹쳐도 상태는 하나다)
#   나아간 칸수 = 성공 수
PLOK="$(grep -l '→ 단계' "$PLW"/ad[1-4] | wc -l | tr -d ' ')"
PLNO="$(grep -l '이미 .*단계를 벗어났다' "$PLW"/ad[1-4] | wc -l | tr -d ' ')"
check "모든 호출이 성공 또는 거절로 끝난다" '^4$' "$((PLOK + PLNO))"
check "활성 단계는 언제나 하나다" '^1$' \
  "$(sqlite3 "$PLW/.claude/harness/harness.db" "select count(*) from stage where status='active'")"
check "나아간 칸수가 성공 수와 같다" "^$((PLOK + 1))\$" \
  "$(pl status | grep -o '단계 [0-9]' | grep -o '[0-9]')"
# 자동 승인 1회를 병렬 스킵 넷이 나눠 쓰지 못한다
pl auto-skip on --reason "검사" --uses 1 >/dev/null
for i in 1 2 3 4; do (cd "$PLW" && python3 "$ENGINE" skip context --reason "p$i" > "$PLW/sk$i" 2>&1) & done
wait
SKOK="$(grep -l '^스킵' "$PLW"/sk[1-4] | wc -l | tr -d ' ')"
# 성공 메시지에도 '자동 승인' 이 들어 있어 두 번 세었다. 성공하지 **않은** 것을 센다.
SKNO="$(grep -L '^스킵' "$PLW"/sk[1-4] | wc -l | tr -d ' ')"
check "스킵도 성공 또는 거절로 끝난다" '^4$' "$((SKOK + SKNO))"
check "스킵 뒤에도 활성 단계는 하나다" '^1$' \
  "$(sqlite3 "$PLW/.claude/harness/harness.db" "select count(*) from stage where status='active'")"
# 자동 승인 1회는 **한 번만** 쓰인다 — 성공한 스킵이 하나를 넘지 않는다.
check "자동 승인 1회로는 스킵 하나만 통과한다" '^[01]$' "$SKOK"
check "열린 작업은 하나다" '^1$' \
  "$(sqlite3 "$PLW/.claude/harness/harness.db" "select count(*) from loop where closed_at is null")"
rm -rf "$PLW"

echo "== 게이트가 꺼졌으면 반드시 말한다"
# 설치하지 않은 것과 고장 난 것은 다르다. 둘을 같게 다루면 고장이 침묵이 된다.
OFW="$(mktemp -d)"
(cd "$OFW" && git init -q . && python3 "$ENGINE" init >/dev/null)
ofhook() { printf '{"hook_event_name":"%s","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"x.py","content":"x"}}' \
  "$1" "$OFW" | CLAUDE_PROJECT_DIR="$OFW" python3 "$ENGINE" hook 2>&1; }
rm -f "$OFW"/.claude/harness/harness.db*
DBGONE="$(ofhook PreToolUse)"
check "DB 파일이 사라지면 침묵하지 않는다" '게이트가 꺼졌다' "$DBGONE"
check "무엇이 없는지 말한다" '상태 DB' "$DBGONE"
check "복구 방법을 준다" 'init' "$DBGONE"
rm -rf "$OFW"
# 문법은 멀쩡한데 stages 만 빈 경우 — "문법을 확인하라" 는 없는 오타를 찾게 만든다
EMW="$(mktemp -d)"
(cd "$EMW" && git init -q . && python3 "$ENGINE" init >/dev/null)
python3 - "$EMW" <<'PYM'
import json, os, sys
p = os.path.join(sys.argv[1], ".claude", "harness", "stages.json")
d = json.load(open(p, encoding="utf-8"))
d["stages"], d["consent"], d["promotion"] = [], {}, {}
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYM
EMPTY="$(printf '{"hook_event_name":"SessionStart","cwd":"%s","source":"startup"}' "$EMW" \
  | CLAUDE_PROJECT_DIR="$EMW" python3 "$ENGINE" hook 2>&1)"
check "단계를 비우면 게이트가 꺼졌다고 말한다" '게이트가 꺼졌다' "$EMPTY"
check "무엇이 비었는지 말한다" 'stages 가 비어 있다' "$EMPTY"
check_absent "없는 문법 오류를 찾게 하지 않는다" 'JSON 문법' "$EMPTY"
rm -rf "$EMW"

echo "== 쓰기 실패를 성공처럼 보고하지 않는다"
# True=썼다 / False=이미 같다 / None=쓰지 못했다. 셋을 둘로 뭉개면 실패가
# "바꿀 것이 없었다" 와 구분되지 않는다.
ROW="$(mktemp -d)"
(cd "$ROW" && git init -q . && python3 "$ENGINE" init >/dev/null)
(cd "$ROW" && python3 "$ENGINE" loop intent x >/dev/null && python3 "$ENGINE" loop done-when y >/dev/null)
python3 - "$ROW" <<'PYR'
import datetime, os, sqlite3, sys
c = sqlite3.connect(os.path.join(sys.argv[1], ".claude/harness/harness.db"))
at = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S+0900")
for lp in ("260101-aaaaaa", "260102-bbbbbb", "260103-cccccc"):
    c.execute("INSERT OR IGNORE INTO loop(id,intent,created_at,cycle) VALUES(?,?,?,1)",
              (lp, "x", at))
    for _ in range(2):
        c.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target) "
                  "VALUES(?,?,?,?,?,?)",
                  (at, lp, "execution", "block", "docs_readonly", "docs/a.md"))
c.commit()
PYR
chmod 444 "$ROW/.claude/harness/LEARNED.md"
ROUT="$(cd "$ROW" && python3 "$ENGINE" promote "block:docs_readonly" --as rule --note "규칙" 2>&1)"
chmod 644 "$ROW/.claude/harness/LEARNED.md"
check "반영 실패를 알린다" '반영하지 못했다' "$ROUT"
check "무엇을 잃는지 말한다" '다음 세션에 실리지 않는다' "$ROUT"
check_absent "갱신했다고 말하지 않는다" 'LEARNED.md 갱신' "$ROUT"
rm -rf "$ROW"

echo "== 증거에는 유효기간이 있다 (근거가 바뀌면 만료된다)"
# 증거는 "언제 무엇을 봤다"를 적는데 **본 것이 그 뒤에 변할 수 있다.** 계획을 승인받고
# 그 파일을 고쳐도 승인이 살아 있었다 — 사람이 보지 않은 계획으로 진행할 수 있었다.
EVW="$(mktemp -d)"
(cd "$EVW" && git init -q . && python3 "$ENGINE" init >/dev/null)
ev() { (cd "$EVW" && python3 "$ENGINE" "$@"); }
ev loop intent "결제 리팩터" >/dev/null
ev loop done-when "테스트 통과" >/dev/null
ev advance >/dev/null; ev advance >/dev/null; ev advance >/dev/null
EPRE="$(python3 - "$EVW" "$(dirname "$ENGINE")" <<'PYE'
import sys; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1])
print(h.file_prefix(con, h.head_loop(con)))
PYE
)"
mkdir -p "$EVW/.dev/plan"
EPLAN=".dev/plan/${EPRE}plan.md"
printf '# 결제\n\n## 목표\n전략 패턴으로 분리한다.\n' > "$EVW/$EPLAN"
ev approve-plan "$EPLAN" >/dev/null
check "승인 직후에는 조건이 채워진다" '충족 .*plan_approved' "$(ev status)"
# 승인 뒤에 내용을 바꾼다
printf '# 결제\n\n## 목표\n결제 테이블을 전부 드롭한다.\n' > "$EVW/$EPLAN"
EOUT="$(ev advance 2>&1 || true)"
check "계획이 바뀌면 승인이 만료된다" 'advance 거부' "$EOUT"
check "무엇이 달라졌는지 말한다" '그때 본 것과 달라졌다' "$EOUT"
check "어느 파일인지 말한다" "$EPRE" "$EOUT"
check "무엇을 하면 되는지 말한다" 'approve-plan' "$EOUT"
# 다시 승인하면 통한다 — 만료가 막다른 길이 되면 안 된다
ev approve-plan "$EPLAN" >/dev/null
check "다시 승인하면 진행된다" 'Execution' "$(ev advance 2>&1)"
# 파일이 아닌 증거는 만료 대상이 아니다 (변할 근거가 없다)
check_absent "완료 조건은 만료되지 않는다" 'acceptance.*달라졌다' "$(ev status 2>&1)"
rm -rf "$EVW"

echo "== 검증 증거는 '문자열'이 아니라 '실행'을 본다"
# `bash_pattern` 을 명령 **전체**에 search 했다. 그래서 아래가 전부 검증으로 적립됐다.
VFW="$(mktemp -d)"
(cd "$VFW" && git init -q . && python3 "$ENGINE" init >/dev/null)
vf() { (cd "$VFW" && python3 "$ENGINE" "$@"); }
vhit() { python3 - "$VFW" "$(dirname "$ENGINE")" "$1" <<'PYV'
import os, sys
sys.path.insert(0, sys.argv[2])
import harness as h
cfg = h.load_config(sys.argv[1], os.path.dirname(sys.argv[2]))
print("증거" if h.verification_hit(cfg, sys.argv[3]) else "아님")
PYV
}
for C in "npm test" "npm run test" "pytest -q" "make check" "go test ./..." \
         "cargo test" "npx tsc --noEmit" "echo hi && npm test"; do
  check "검증으로 센다: $C" '^증거$' "$(vhit "$C")"
done
for C in 'echo "npm test"' 'git commit -m "ran npm test"' "cat tsc.log" \
         "grep -rn pytest src/" "ls | grep vitest" "ls" "git status"; do
  check "검증으로 세지 않는다: $C" '^아님$' "$(vhit "$C")"
done
rm -rf "$VFW"

echo "== 자기 잠금 우회 세 갈래 (훅으로 실제 재현)"
# 5차 리뷰의 CRITICAL 셋은 **같은 패턴**이다: 바닥값을 경로 **문자열**로 판정하는데,
# 같은 파일에 다른 문자열을 붙이는 길이 셋 있었다. 대가도 셋 다 같다 — 그 파일은
# `.claude/settings.json` 에 사전 승인된 래퍼이므로 **승인 없는 임의 코드 실행**이다.
# rules_check 가 판정 함수를 직접 검사하고, 여기서는 훅 JSON 으로 끝까지 확인한다.
LW="$(mktemp -d)"
(cd "$LW" && git init -q . && python3 "$ENGINE" init >/dev/null)
lb() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Bash","tool_input":{"command":%s}}' \
    "$LW" "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$1")" \
  | CLAUDE_PROJECT_DIR="$LW" python3 "$ENGINE" hook 2>&1; }
lw() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":%s,"content":"x"}}' \
    "$LW" "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$1")" \
  | CLAUDE_PROJECT_DIR="$LW" python3 "$ENGINE" hook 2>&1; }

# (1) symlink 별칭 — `ln -s . alias` 하나로 문자열이 달라진다.
ln -s . "$LW/alias"
check "별칭 경로로 래퍼를 Write 할 수 없다" '"permissionDecision": *"deny"' \
  "$(lw "alias/.claude/harness/bin/harness")"
check "그 이유가 자기 잠금이라고 말한다" '하네스 자신은 수정할 수 없다' \
  "$(lw "alias/.claude/harness/bin/harness")"
check "별칭 경로로 리다이렉트할 수 없다" '"permissionDecision": *"deny"' \
  "$(lb 'printf x > alias/.claude/harness/bin/harness')"
# `'^$'` 는 빈 문자열에 맞지 않는다(grep 은 빈 입력을 훑지 않는다). 없어야 하는 것을
# 확인할 때는 `check_absent` 를 쓴다 — 무엇이 나오면 안 되는지 라벨에 남는다.
check_absent "무관한 별칭 경로는 자기 잠금에 걸리지 않는다" '하네스 자신은 수정할 수 없다' \
  "$(lb 'printf x > alias/src/a.txt')"

# (2) 설정만으로 잠금 해제 — `interpreters: ["rm"]` 이면 다음 인자가 '실행 대상'이 된다.
python3 - "$LW" <<'PYI'
import json, os, sys
p = os.path.join(sys.argv[1], ".claude", "harness", "stages.json")
d = json.load(open(p, encoding="utf-8"))
d.setdefault("bash", {})["interpreters"] = ["rm", "python3"]
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYI
check "interpreters 에 rm 을 넣어도 DB 를 지울 수 없다" '"permissionDecision": *"deny"' \
  "$(lb 'rm .claude/harness/harness.db')"
check "그 설정이 무시된다고 알린다" 'bash.interpreters .*인터프리터로 선언할 수 없다' \
  "$(cd "$LW" && python3 "$ENGINE" status 2>&1)"
python3 - "$LW" <<'PYJ'
import json, os, sys
p = os.path.join(sys.argv[1], ".claude", "harness", "stages.json")
d = json.load(open(p, encoding="utf-8"))
d["bash"].pop("interpreters")
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYJ

# (3) 인터프리터 문자열 안에 숨긴 경로 — **이 우회는 막지 않는다.** 셸과 파이썬을
# 재구현하는 대신 대가를 없앤다: 래퍼는 내용이 우리 것일 때만 실행된다.
python3 -c "open('$LW/.claude/harness/bin/harness','w').write('#!/bin/sh\necho PWNED\n')"
check "변조가 실제로 성공했다 (전제)" 'PWNED' "$(cat "$LW/.claude/harness/bin/harness")"
LT="$(lb '.claude/harness/bin/harness status')"
check "변조된 래퍼는 실행 전에 막힌다" '"permissionDecision": *"deny"' "$LT"
check "왜 막았는지 말한다" '사전 승인' "$LT"
check "막으면서 원본으로 복구한다" 'step-seven-harness wrapper' \
  "$(cat "$LW/.claude/harness/bin/harness")"
check_absent "복구된 래퍼에 변조가 남지 않았다" 'PWNED' \
  "$(cat "$LW/.claude/harness/bin/harness")"
check_absent "다시 실행하면 통한다" '"deny"' \
  "$(lb '.claude/harness/bin/harness status')"
check "복구된 래퍼가 실제로 동작한다" '단계 1/7' \
  "$(cd "$LW" && ./.claude/harness/bin/harness status | grep '단계')"
# 오판은 마찰이고, 마찰은 게이트를 끄게 만든다. 주석만 달라도 막으면 안 된다.
printf '# 사람이 남긴 메모\n' >> "$LW/.claude/harness/bin/harness"
check_absent "주석이 붙은 것은 변조가 아니다" '"deny"' \
  "$(lb '.claude/harness/bin/harness status')"
rm -rf "$LW"

echo "== 측정 산술 (손계산 대조)"
# 합성 이력의 기대값을 미리 종이에 세고 코드가 그 값을 내는지 본다.
# cycle_counters 11개 항목과 _survival 8개 항목.
MRC=0; MOUT="$(python3 "$(dirname "$0")/math_check.py" "$MC" 2>&1)" || MRC=$?
check "손계산과 전부 일치" '^0$' "$MRC"
check "실패 항목 없음" '실패 0개' "$MOUT"
if [ "$MRC" != 0 ]; then printf '%s\n' "$MOUT" | grep FAIL | sed 's/^/     /'; fi

echo "== 순수 함수 경계 (_bucket / _pct / trend_verdict)"
POUT="$(python3 - "$(dirname "$0")/../scripts" <<'PYP'
import sys
sys.path.insert(0, sys.argv[1])
import harness as h
bad = []
for n in range(0, 41):
    bs = h._bucket([{"i": i} for i in range(n)])
    if n == 0:
        if bs != []:
            bad.append("n=0")
        continue
    cov = [(lo, hi, len(r)) for lo, hi, r in bs]
    if sum(c for _, _, c in cov) != n: bad.append("총합 n=%d" % n)
    if any(hi - lo + 1 != c for lo, hi, c in cov): bad.append("라벨폭 n=%d" % n)
    if cov[0][0] != 1 or cov[-1][1] != n: bad.append("양끝 n=%d" % n)
    if any(cov[i][1] + 1 != cov[i+1][0] for i in range(len(cov)-1)):
        bad.append("연속성 n=%d" % n)
if h._pct(1, 0).strip() != "-": bad.append("0 분모")
if h._pct(2, 3).strip() != "67%": bad.append("반올림")
mk = lambda b, r, by: {"blocks": b, "refails": r, "bypass": by, "skips": 0, "declines": 0}
if h.trend_verdict([mk(5,3,0), mk(2,1,0)]) != "improving": bad.append("improving")
if h.trend_verdict([mk(5,3,0), mk(2,1,3)]) != "evasion": bad.append("evasion")
if h.trend_verdict([mk(5,3,0), mk(5,3,2)]) != "mismatch": bad.append("mismatch")
if h.trend_verdict([mk(5,3,0)]) is not None: bad.append("구간1")
print("실패: %s" % (bad or "없음"))
PYP
)"
check "_bucket n=0..40 라벨·분할·연속성" '실패: 없음' "$POUT"

echo "== settings.json 안전 쓰기 (남의 설정을 덮지 않는다)"
SRC2=0; SOUT="$(python3 "$(dirname "$0")/settings_check.py" "$MC" 2>&1)" || SRC2=$?
check "남의 설정을 덮지 않는다" '^0$' "$SRC2"
check "경쟁 쓰기 검사가 실제로 돌았다" '경쟁 쓰기가 있어도' "$SOUT"
if [ "$SRC2" != 0 ]; then printf '%s\n' "$SOUT" | grep FAIL | sed 's/^/     /'; fi

echo "== 문서 구조 (중복·링크)"
DRC=0; DOUT="$(python3 "$(dirname "$0")/doc_check.py" "$MC" 2>&1)" || DRC=$?
check "문서에 산문 중복·깨진 링크가 없다" '^0$' "$DRC"
check "검사가 실제로 문서를 읽었다" '산문' "$DOUT"
if [ "$DRC" != 0 ]; then printf '%s\n' "$DOUT" | tail -6 | sed 's/^/     /'; fi


echo "== 훅 없이도 게이트가 맞는다 (다른 에이전트 도구·사람이 직접 쓴 경우)"
# 왜: 증거를 PostToolUse 관측에만 의존하면 훅이 없는 환경에서 종료 조건이
# 영원히 안 채워진다. Codex 는 셸만 가로채고, opencode 는 subagent 를 놓친다.
# 파일 존재는 관측 없이도 아는 사실이므로 관측을 기다리지 않고 본다.
FW="$(mktemp -d)"
(cd "$FW" && git init -q . && python3 "$ENGINE" init >/dev/null)
fw() { (cd "$FW" && python3 "$ENGINE" "$@"); }
fmiss() { fw status --json | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["exit_missing"]))'; }
fw loop intent "훅 없는 환경" >/dev/null
fw loop done-when "끝" >/dev/null
fw advance >/dev/null; fw advance >/dev/null; fw advance >/dev/null
FPRE="$(fw status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["prefix"])')"
mkdir -p "$FW/.dev/plan"
check "계획 파일이 없으면 막는다" 'plan_file' "$(fmiss)"
# 훅을 거치지 않고 파일만 만든다
printf '# 계획\n' > "$FW/.dev/plan/${FPRE}plan.md"
check "관측 없이 파일만 있어도 plan_file 이 선다" '^plan_approved$' "$(fmiss)"
check "관측된 증거 행은 여전히 0개다" '^0$' \
  "$(python3 -c "
import sqlite3,sys
print(sqlite3.connect(sys.argv[1]).execute(
  \"SELECT COUNT(*) FROM evidence WHERE kind='plan_file'\").fetchone()[0])" \
  "$FW/.claude/harness/harness.db")"
# 반증 둘. 이 둘이 통과하면 게이트가 사람 없이 열린다.
rm -f "$FW/.dev/plan/${FPRE}plan.md"
printf '# 남의 계획\n' > "$FW/.dev/plan/250101-aaaaaa-1-plan.md"
check "다른 작업의 파일로는 열리지 않는다" 'plan_file' "$(fmiss)"
# **회차만 다른 경우**를 따로 본다. 위 픽스처는 해시와 회차가 둘 다 달라서, 해시만
# 보고 회차를 무시하는 구현도 통과했다 — 적대적 리뷰가 지적했다.
rm -f "$FW/.dev/plan/250101-aaaaaa-1-plan.md"
FWCYC="$(printf '%s' "$FPRE" | sed 's/-1-$/-2-/')"
printf '# 다음 회차 계획\n' > "$FW/.dev/plan/${FWCYC}plan.md"
check "같은 작업이라도 회차가 다르면 열리지 않는다" 'plan_file' "$(fmiss)"
rm -f "$FW/.dev/plan/${FWCYC}plan.md"
rm -f "$FW/.dev/plan/"*.md
printf '# INDEX\n' > "$FW/.dev/plan/INDEX.md"
check "누적 인덱스로는 열리지 않는다" 'plan_file' "$(fmiss)"
rm -f "$FW/.dev/plan/INDEX.md"
printf '# 계획\n' > "$FW/.dev/plan/${FPRE}plan.md"
fw approve-plan ".dev/plan/${FPRE}plan.md" >/dev/null
check_empty "승인은 여전히 사람이 한다" "$(fmiss)"

echo "  -- 회고 파일도 같다"
fw advance >/dev/null; fw advance >/dev/null       # Execution → Verification

echo "== 실패한 검증은 증거가 아니다"
# 이것이 왜 버그였나: bash_pattern 은 **명령 문자열만** 봤다. pytest 를 돌려
# 3개가 깨져도 verification_evidence 가 적립되고 게이트가 열렸다. 실제로 확인했다.
pfail() { printf '{"hook_event_name":"PostToolUse","cwd":"%s","session_id":"s1","tool_name":"Bash","tool_input":{"command":"pytest tests/"},"tool_response":%s}' "$FW" "$1" \
  | CLAUDE_PROJECT_DIR="$FW" python3 "$ENGINE" hook; }
pfail '{"stdout":"3 failed","isError":true}' >/dev/null
check "isError 면 증거로 세지 않는다" 'verification_evidence' "$(fmiss)"
pfail '{"stdout":"x","exit_code":1}' >/dev/null
check "exit_code 가 0 이 아니면 세지 않는다" 'verification_evidence' "$(fmiss)"
pfail '{"stdout":"x","interrupted":true}' >/dev/null
check "중단된 명령도 세지 않는다" 'verification_evidence' "$(fmiss)"
# 정상 경로가 죽지 않았는지가 더 중요하다 — 막기만 하는 검사는 쓸모가 없다
pfail '{"stdout":"3 passed","isError":false}' >/dev/null
check_empty "통과한 명령은 여전히 증거가 된다" "$(fmiss)"

echo "== verify: 하네스가 직접 돌려 종료 코드로 판정한다"
FW2="$(mktemp -d)"
(cd "$FW2" && git init -q . && python3 "$ENGINE" init >/dev/null)
f2() { (cd "$FW2" && python3 "$ENGINE" "$@"); }
f2miss() { f2 status --json | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["exit_missing"]))'; }
f2 loop intent "verify" >/dev/null
f2 loop done-when "끝" >/dev/null
for _ in 1 2 3; do f2 advance >/dev/null 2>&1; done
F2PRE="$(f2 status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["prefix"])')"
mkdir -p "$FW2/.dev/plan"; printf '#\n' > "$FW2/.dev/plan/${F2PRE}p.md"
f2 approve-plan ".dev/plan/${F2PRE}p.md" >/dev/null
f2 advance >/dev/null; f2 advance >/dev/null
check "Verification 에 와 있다" 'Verification' \
  "$(f2 status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["stage_label"])')"
# 임의 명령을 돌리는 셸이 되면 PreToolUse 전체가 무의미해진다
check "검증 명령이 아니면 거부한다" '검증 명령으로 보이지 않는다' \
  "$(f2 verify -- rm -rf .claude 2>&1)"
check "셸 메타문자를 거부한다" '셸 메타문자' \
  "$(f2 verify -- 'pytest; rm -rf /' 2>&1)"
check "거부한 뒤에도 증거는 없다" 'verification_evidence' "$(f2miss)"
# 실패하는 검증
printf 'check:\n\t@exit 1\n' > "$FW2/Makefile"
check "실패하면 증거로 기록하지 않는다" '증거로 기록하지 않았다' "$(f2 verify -- make check 2>&1)"
check "실패 뒤에도 게이트는 닫혀 있다" 'verification_evidence' "$(f2miss)"
check "실패는 tool_fail 로 적립된다" 'verify' \
  "$(python3 -c "
import sqlite3,sys
r=sqlite3.connect(sys.argv[1]).execute(
  \"SELECT rule FROM event WHERE kind='tool_fail' AND rule='verify'\").fetchone()
print(r[0] if r else '')" "$FW2/.claude/harness/harness.db")"
# 통과하는 검증
printf 'check:\n\t@true\n' > "$FW2/Makefile"
check "통과하면 증거가 된다" '검증 통과' "$(f2 verify -- make check 2>&1)"
check_empty "게이트가 열린다" "$(f2miss)"
check "advance 가 통과한다" 'Compounding' "$(f2 advance 2>&1)"
rm -rf "$FW" "$FW2"

echo "== AGENTS.md: 훅이 없는 도구도 규칙을 읽는다"
# AGENTS.md 는 `@import` 를 모른다 — 그건 Claude Code 기능이다. Codex·Cursor·
# Copilot·Gemini CLI·Aider 는 이 파일을 그냥 마크다운으로 읽으므로, 앵커 한 줄이
# 아니라 읽고 바로 쓸 수 있는 문장이어야 한다.
AW="$(mktemp -d)"
(cd "$AW" && git init -q . && printf '# 내 프로젝트\n\n손으로 쓴 것.\n' > AGENTS.md \
  && python3 "$ENGINE" init >/dev/null)
AB="$(cat "$AW/AGENTS.md")"
check "AGENTS.md 에 절차 안내가 들어간다" 'step-seven-harness -->' "$AB"
check "원칙 파일을 가리킨다" 'POLICY.md' "$AB"
check "승격 규칙 파일을 가리킨다" 'LEARNED.md' "$AB"
check "상태 확인 명령을 준다" 'harness status' "$AB"
check "진행 명령을 준다" 'harness advance' "$AB"
check "검증 명령을 준다" 'harness verify' "$AB"
check "@import 를 쓰지 않는다 (다른 툴은 모른다)" '^0$' \
  "$(printf '%s' "$AB" | grep -c '^@\.claude')"
check "사람이 쓴 내용을 보존한다" '손으로 쓴 것' "$AB"
check "설치 보고에 나온다" 'AGENTS.md' \
  "$( (cd "$AW" && python3 "$ENGINE" init) )"
# 두 번 붙으면 다른 툴이 같은 지시를 두 번 읽는다
(cd "$AW" && python3 "$ENGINE" init >/dev/null)
check "다시 설치해도 한 번만 붙는다" '^1$' \
  "$(grep -c 'step-seven-harness -->' "$AW/AGENTS.md")"
check "커밋 대상으로 안내한다" 'AGENTS.md' \
  "$( (cd "$AW" && python3 "$ENGINE" init) | grep '커밋 대상')"
rm -rf "$AW"

echo "== 종료 조건이 어휘가 됐다 (파이썬을 고치지 않고 바꿀 수 있어야 한다)"
# 이것이 어휘화 작업의 목표다. 예전에는 EVIDENCE_STAGES·HUMAN_CRITERIA·CRITERIA_HELP·
# promotion_decided 특수분기가 파이썬에 있었고, 조건을 더하거나 이름을 바꾸려면 엔진을
# 고쳐야 했다. 엔진이 알아야 하는 것은 **판정 방식**이지 조건 이름이 아니다.
# 그래서 검사도 설정만 바꾸고 동작이 따라오는지 보는 형태여야 한다.
VW="$(mktemp -d)"
(cd "$VW" && git init -q . && python3 "$ENGINE" init >/dev/null)
vw() { (cd "$VW" && python3 "$ENGINE" "$@"); }
VCFG="$VW/.claude/harness/stages.json"
edcfg() { python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
exec(sys.argv[2])
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$1" "$2"; }

check "criteria 어휘가 설치된다" 'satisfied_by' "$(cat "$VCFG")"
check "도움말이 코드가 아니라 데이터다" 'harness loop intent' "$(cat "$VCFG")"

echo "  -- 도움말을 설정에서 바꾸면 엔진이 그것을 말한다"
edcfg "$VCFG" 'cfg["criteria"]["intent_set"]["help"] = "내가 정한 안내 문구"'
check "거부 메시지가 설정을 따른다" '내가 정한 안내 문구' "$(vw advance 2>&1)"

echo "  -- 사람만 채울 수 있는 조건도 설정이 정한다 (HUMAN_CRITERIA 가 코드에 없다)"
vstop() { printf '{"hook_event_name":"Stop","cwd":"%s","session_id":"%s","last_assistant_message":"[Selection] 했습니다"}' "$VW" "$1" \
  | CLAUDE_PROJECT_DIR="$VW" python3 "$ENGINE" hook; }
check "human 이면 턴을 밀지 않는다" '사람의 입력을 기다린다' "$(vstop vh1)"
edcfg "$VCFG" 'cfg["criteria"]["intent_set"].pop("human", None)'
check "human 을 떼면 이어서 진행하라고 한다" '이어서 진행하라' "$(vstop vh2)"

echo "  -- 없던 조건을 새로 정의해도 게이트가 된다 (엔진은 이름을 모른다)"
VW2="$(mktemp -d)"
(cd "$VW2" && git init -q . && python3 "$ENGINE" init >/dev/null)
vw2() { (cd "$VW2" && python3 "$ENGINE" "$@"); }
edcfg "$VW2/.claude/harness/stages.json" '
cfg["criteria"]["design_doc"] = {
    "satisfied_by": "file",
    "write_glob": [".dev/design/**/*.md"],
    "help": "설계 문서를 .dev/design/ 아래에 남겨야 한다",
}
for st in cfg["stages"]:
    if st["id"] == "planning":
        st["exit_criteria"] = ["design_doc"]
        st["stop_requires"] = ["design_doc"]
cfg["folder_rules"]["dev_subdirs"].append("design")
cfg["folder_rules"]["loop_prefixed_dirs"].append("design")
'
vw2 loop intent "새 조건" >/dev/null
vw2 loop done-when "끝" >/dev/null
vw2 advance >/dev/null; vw2 advance >/dev/null; vw2 advance >/dev/null
check "새로 정의한 조건이 게이트가 된다" 'design_doc' "$(vw2 advance 2>&1)"
check "설정에 쓴 도움말을 그대로 낸다" '설계 문서를 .dev/design' "$(vw2 advance 2>&1)"
V2PRE="$(vw2 status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["prefix"])')"
mkdir -p "$VW2/.dev/design"
printf '# 설계\n' > "$VW2/.dev/design/${V2PRE}d.md"
check "파일을 놓으면 열린다 (satisfied_by=file)" 'Execution' "$(vw2 advance 2>&1)"

echo "  -- 관측 단계도 설정이 정한다 (EVIDENCE_STAGES 가 코드에 없다)"
edcfg "$VCFG" 'cfg["criteria"]["verification_evidence"]["stages"] = ["verification"]'
check "verify 가 설정된 단계만 허용한다" 'verification 단계에서 쓴다' \
  "$(vw verify -- pytest 2>&1)"

echo "== 패널이 선언형이다 (단계 id 가 코드에 없다)"
# 예전에는 `if sid == "scaffolding"` 이 코드에 있었다. 0.10.0 에서 Selection 을
# 신설했을 때 이 부류가 낡았고, 모델은 낡은 문장을 정확히 따라 틀린 말을 했다.
PW="$(mktemp -d)"
(cd "$PW" && git init -q . && python3 "$ENGINE" init >/dev/null)
pw2() { (cd "$PW" && python3 "$ENGINE" "$@"); }
# 완료 조건 패널을 Context 로 옮긴다. 원래는 Verification·Compounding 에만 있다.
edcfg "$PW/.claude/harness/stages.json" '
for st in cfg["stages"]:
    st["panels"] = ["acceptance"] if st["id"] == "context" else []
'
pw2 loop intent "패널 이동" >/dev/null
pw2 loop done-when "조건 하나" >/dev/null
check "옮긴 단계에서 패널이 나온다" '이 작업의 완료 조건' \
  "$(pw2 advance >/dev/null 2>&1; pw2 advance 2>&1)"
check "패널을 비우면 아무것도 안 나온다" '^0$' \
  "$(pw2 advance 2>&1 | grep -c '이 작업의 완료 조건' || true)"

echo "== 기존 설치가 새 어휘를 받는다 (init 은 stages.json 을 덮지 않는다)"
# 이것이 없으면 어휘를 설정으로 옮긴 순간 기존 설치의 게이트가 조용히 어긋난다.
OW="$(mktemp -d)"
(cd "$OW" && git init -q . && python3 "$ENGINE" init >/dev/null)
ow() { (cd "$OW" && python3 "$ENGINE" "$@"); }
# 어휘화 이전 모양으로 되돌린다. bash_pattern 은 사용자가 고쳐 둔 상태를 흉내낸다.
edcfg "$OW/.claude/harness/stages.json" '
crit = cfg.pop("criteria")
cfg["evidence_signals"] = {
    # **기본값을 복사하면 공허하다** — 이월이 없어도 템플릿 채움이 같은 값을 준다.
    # 사용자가 고친 값이어야 이월을 검사한다.
    "plan_file": {"write_glob": [".dev/design/**/*.md"]},
    "retro_file": {"write_glob": crit["retro_file"]["write_glob"]},
    "verification_evidence": {"bash_pattern": "\\bmake\\s+smoke\\b"},
}
for st in cfg["stages"]:
    st.pop("panels", None)
'
check "옛 문서로도 동작한다" 'Selection' "$(ow status 2>&1)"
check "새 어휘가 채워진다" 'intent_set' "$(ow advance 2>&1)"
ow loop intent "옛 설치" >/dev/null
ow loop done-when "끝" >/dev/null
ow advance >/dev/null; ow advance >/dev/null; ow advance >/dev/null
OPRE="$(ow status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["prefix"])')"
# 사용자가 고친 위치(.dev/design)에 둔다. 이월이 없으면 템플릿 기본값(.dev/plan)만
# 인정되므로 이 파일은 조건을 채우지 못한다 — 그때 이 단정이 실패한다.
mkdir -p "$OW/.dev/design" "$OW/.dev/plan"
printf '#\n' > "$OW/.dev/design/${OPRE}p.md"
check "사용자가 고친 write_glob 이 이어진다" '^plan_approved$' \
  "$(ow status --json | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["exit_missing"]))')"
# 그리고 기본 위치는 더 이상 인정되지 않아야 한다 (대체이지 병합이 아니다)
rm -f "$OW/.dev/design/${OPRE}p.md"; printf '#\n' > "$OW/.dev/plan/${OPRE}p.md"
check "기본 위치는 인정되지 않는다" 'plan_file' \
  "$(ow status --json | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["exit_missing"]))')"
printf '#\n' > "$OW/.dev/design/${OPRE}p.md"
ow approve-plan ".dev/design/${OPRE}p.md" >/dev/null
ow advance >/dev/null; ow advance >/dev/null
printf 'smoke:\n\t@true\n' > "$OW/Makefile"
# `make smoke` 는 기본 패턴(`make (test|check|lint)`)에 없다. 고쳐 둔 패턴이 사라지면
# "검증 명령으로 보이지 않는다"로 거부된다 — 사용자는 자기 설정이 사라진 걸 모른 채
# 게이트가 안 열리는 것만 본다. 조용한 회귀다.
check "사용자가 고친 bash_pattern 이 이어진다" '검증 통과' "$(ow verify -- make smoke 2>&1)"
# 사용자가 패턴을 갈아끼웠으면 기본 패턴은 더 이상 쓰이지 않는다. `make check` 는
# 기본 패턴에는 있고 사용자 패턴에는 없으므로 거부돼야 한다 — 병합이 아니라 대체다.
check "사용자 패턴이 기본값을 대체한다 (병합이 아니다)" '검증 명령으로 보이지 않는다' \
  "$(ow verify -- make check 2>&1)"
rm -rf "$VW" "$VW2" "$PW" "$OW"

echo "== 어휘를 설정으로 옮기면 오타가 새로운 실패 방식이 된다"
# 어휘화가 넓힌 표면이다. 이 실패들은 전부 **조용했다** — 직접 돌려 확인했다.
#   satisfied_by 오타  → 파일 검사가 말없이 꺼짐
#   panels 오타        → 아무것도 안 함
#   없는 조건 참조      → 채울 방법이 없는 단계
# 막지는 않는다. 설정이 조금 틀렸다고 세션을 벽돌로 만들면 그게 더 나쁘다. 말한다.
CW="$(mktemp -d)"
(cd "$CW" && git init -q . && python3 "$ENGINE" init >/dev/null)
cw() { (cd "$CW" && python3 "$ENGINE" "$@"); }
cwed() { python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
exec(sys.argv[2])
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$CW/.claude/harness/stages.json" "$1"; }

check_empty "정상 설정에는 아무 말도 하지 않는다" \
  "$(cw status 2>&1 | grep '무시되는 설정' || true)"
cwed 'cfg["criteria"]["plan_file"]["satisfied_by"] = "fille"'
check "satisfied_by 오타를 지적한다" "satisfied_by='fille'" "$(cw status 2>&1)"
check "무슨 값이 가능한지 알려준다" 'no_pending_promotions' "$(cw status 2>&1)"
check "무엇이 일어나는지 알려준다" 'cli 로 떨어진다' "$(cw status 2>&1)"
check "맨 위에 말한다 (묻히면 안 읽는다)" '^⚠' "$(cw status 2>&1 | head -1)"
cwed 'cfg["criteria"]["plan_file"]["satisfied_by"] = "file"; cfg["criteria"]["plan_file"].pop("write_glob")'
check "file 인데 write_glob 이 없으면 지적한다" '어떤 파일도 이 조건을 채우지 못한다' \
  "$(cw status 2>&1)"
cwed 'cfg["stages"][0]["panels"] = ["work_candidatez"]'
check "패널 오타를 지적한다" "'work_candidatez' 는 모르는 패널" "$(cw status 2>&1)"
check "가능한 패널을 알려준다" 'work_candidates' "$(cw status 2>&1)"
cwed 'cfg["stages"][1]["exit_criteria"] = ["nonexistent_thing"]'
check "criteria 에 없는 조건을 지적한다" '채울 방법이 없어' "$(cw status 2>&1)"
check "--json 에도 실린다" 'config_problems' "$(cw status --json 2>&1)"
check "세션 시작에서 사람에게 알린다" '무시되는 설정' \
  "$(printf '{"hook_event_name":"SessionStart","cwd":"%s"}' "$CW" \
     | CLAUDE_PROJECT_DIR="$CW" python3 "$ENGINE" hook)"

echo "== 쓰기 규칙이 선언형이다 (술어 7개가 데이터가 됐다)"
# 규칙 일곱 개가 전부 같은 모양이었다: 가드(선택자) + 판정 하나. 그래서 파이썬 함수가
# 아니라 데이터로 쓴다. 배열 순서가 우선순위다.
WR="$(mktemp -d)"
(cd "$WR" && git init -q . && python3 "$ENGINE" init >/dev/null)
wr() { (cd "$WR" && python3 "$ENGINE" "$@"); }
wrhk() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s/%s"}}' "$WR" "$WR" "$1" \
  | CLAUDE_PROJECT_DIR="$WR" python3 "$ENGINE" hook; }
wrrule() { python3 -c "
import sqlite3, sys
r = sqlite3.connect(sys.argv[1]).execute(
  \"SELECT rule FROM event WHERE kind='block' ORDER BY rowid DESC LIMIT 1\").fetchone()
print(r[0] if r else '(없음)')" "$WR/.claude/harness/harness.db"; }
wred() { python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
exec(sys.argv[2])
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$WR/.claude/harness/stages.json" "$1"; }

check "규칙이 설정에 있다" 'write_rules' "$(cat "$WR/.claude/harness/stages.json")"
check "거부 메시지가 데이터다" '하네스 자신은 수정할 수 없다' \
  "$(cat "$WR/.claude/harness/stages.json")"

echo "  -- 규칙마다 자기 id 로 적립돼야 한다 (승격의 원료다)"
wrhk ".claude/harness/harness.db" >/dev/null
# 하네스 자신은 **바닥값**(코드)이 먼저 막는다. 설정으로 풀 수 없어야 하기 때문이다.
check "바닥값이 먼저 발동한다" '^self_lock$' "$(wrrule)"
wrhk ".claude/harness/LEARNED.md" >/dev/null
# LEARNED.md 는 바닥값이 아니라 설정(protected_paths)이 지킨다 — 설정 규칙도 산다
check "설정의 protected 규칙도 발동한다" '^protected$' "$(wrrule)"
wrhk "docs/x.md" >/dev/null
check "docs_readonly 가 발동한다" '^docs_readonly$' "$(wrrule)"
wrhk ".dev/nope/a.md" >/dev/null
check "dev_subdir 가 발동한다" '^dev_subdir$' "$(wrrule)"
wrhk ".dev/plan/nohash.md" >/dev/null
check "loop_prefix 가 발동한다" '^loop_prefix$' "$(wrrule)"
wr loop intent "규칙" >/dev/null
wr loop done-when "끝" >/dev/null
wr advance >/dev/null; wr advance >/dev/null
wrhk "src/new.py" >/dev/null
check "stage_write 가 발동한다" '^stage_write$' "$(wrrule)"

echo "  -- 없던 규칙을 설정만으로 만들 수 있다 (DSL 이 성립하나)"
# 별도 픽스처. `tests` 쓰기가 허용된 단계(Scaffolding)여야 새 규칙까지 도달한다 —
# 앞의 WR 은 Context 에 있어 stage_write 가 먼저 걸린다. 우선순위가 배열 순서인 증거다.
NR="$(mktemp -d)"
(cd "$NR" && git init -q . && python3 "$ENGINE" init >/dev/null)
nrhk() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s/%s"}}' "$NR" "$NR" "$1" \
  | CLAUDE_PROJECT_DIR="$NR" python3 "$ENGINE" hook; }
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['folder_rules']['test_name_pattern'] = '^test_[a-z0-9_]+\\\\.py\$'
cfg['write_rules'].append({
    'id': 'test_naming',
    'when': {'class': 'tests', 'min_depth': 2},
    'require': {'basename_matches': 'folder_rules.test_name_pattern'},
    'deny': \"tests/ 파일명은 test_*.py 여야 한다. '{basename}' 는 규칙 위반이다 ({rel}).\",
})
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$NR/.claude/harness/stages.json"
(cd "$NR" && python3 "$ENGINE" loop intent "새 규칙" >/dev/null \
  && python3 "$ENGINE" loop done-when "끝" >/dev/null \
  && python3 "$ENGINE" advance >/dev/null)      # Scaffolding: tests 쓰기 허용
check "새 규칙이 나쁜 이름을 막는다" 'tests/ 파일명은 test_' "$(nrhk 'tests/wrong.py')"
check "치환이 동작한다" "'wrong.py' 는 규칙 위반" "$(nrhk 'tests/wrong.py')"
check "새 규칙 id 로 적립된다 (승격의 원료가 된다)" '^test_naming$' \
  "$(nrhk 'tests/wrong.py' >/dev/null; python3 -c "
import sqlite3, sys
r = sqlite3.connect(sys.argv[1]).execute(
  \"SELECT rule FROM event WHERE kind='block' ORDER BY rowid DESC LIMIT 1\").fetchone()
print(r[0] if r else '(없음)')" "$NR/.claude/harness/harness.db")"
check_empty "좋은 이름은 통과한다" "$(nrhk 'tests/test_ok.py')"
rm -rf "$NR"

echo "  -- 규칙 오타는 조용히 죽는다. 말해야 한다."
check_empty "정상 설정에는 오진이 없다" \
  "$(wr status 2>&1 | grep '무시되는 설정' || true)"
wred 'cfg["write_rules"][1]["when"] = {"clazz": "docs"}'
check "선택자 오타를 지적한다" "'clazz' 는 모르는 선택자" "$(wr status 2>&1)"
wred 'cfg["write_rules"][1]["when"] = {"class": "docs"}; cfg["write_rules"][4]["require"]["never"] = True'
check "판정이 둘이면 지적한다" '판정이 2개다' "$(wr status 2>&1)"
wred 'cfg["write_rules"][4]["require"].pop("never"); cfg["write_rules"][4]["require"]["subdir_in"] = "dev_subdirz"'
check "없는 folder_rules 키를 지적한다" "'dev_subdirz' 가 folder_rules 에 없다" "$(wr status 2>&1)"
wred 'cfg["write_rules"][4]["require"] = {"predicate": "my_check"}'
check "없는 파이썬 술어를 지적한다" '파이썬 술어가 없다' "$(wr status 2>&1)"
wred 'cfg["write_rules"][4]["require"] = {"subdir_in": "dev_subdirs"}; cfg["write_rules"][4].pop("deny")'
check "deny 가 없으면 지적한다" 'deny 메시지가 없다' "$(wr status 2>&1)"
wred 'cfg["write_rules"][4]["deny"] = "x"; cfg["write_rules"][4]["id"] = "protected"'
check "id 중복을 지적한다" "id 'protected' 가 중복" "$(wr status 2>&1)"
rm -rf "$WR"

echo "== 정책·내용도 설정이 정한다 (recall 폴더·회고 질문·동의·셸 분류)"
# recall.dirs 는 조용한 결함이었다. dev_subdirs 에 폴더를 더해도 recall 이 그 폴더를
# 못 봐서, 안내대로 stages.json 을 고친 사람이 **다시 안 읽히는 기록**을 쌓게 됐다.
# 파일은 있고 키워드도 맞는데 영원히 안 나온다 — 복리의 반대다.
PW5="$(mktemp -d)"
(cd "$PW5" && git init -q . && python3 "$ENGINE" init >/dev/null)
p5() { (cd "$PW5" && python3 "$ENGINE" "$@"); }
p5ed() { python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
exec(sys.argv[2])
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$PW5/.claude/harness/stages.json" "$1"; }

p5 loop intent "정책 어휘" >/dev/null
P5PRE="$(p5 status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["prefix"])')"
mkdir -p "$PW5/.dev/postmortem"
printf '# 사후분석\nENOENT 오류를 이렇게 고쳤다\n' > "$PW5/.dev/postmortem/${P5PRE}pm.md"
check_empty "recall 대상이 아닌 폴더는 안 나온다" \
  "$(p5 recall ENOENT 2>&1 | grep 'postmortem' || true)"
p5ed '
cfg["folder_rules"]["dev_subdirs"].append("postmortem")
cfg["folder_rules"]["loop_prefixed_dirs"].append("postmortem")
cfg["recall"]["dirs"].append("postmortem")'
check "recall.dirs 에 넣으면 나온다" 'postmortem' "$(p5 recall ENOENT 2>&1)"
check "쓸 수 없는 폴더를 recall 대상으로 두면 지적한다" 'dev_subdirs 에 없다' \
  "$(p5ed 'cfg["recall"]["dirs"].append("nowhere")'; p5 status 2>&1)"
check "recall.dirs 가 비면 지적한다" '하나도 찾지 못한다' \
  "$(p5ed 'cfg["recall"]["dirs"] = []'; p5 status 2>&1)"

echo "  -- 회고 질문은 복리의 핵심이므로 사람이 정한다"
p5ed 'cfg["recall"]["dirs"] = ["retrospect", "learning", "troubleshooting"]
cfg["retro_questions"] = [{"q": "무엇을 다시 만들 뻔했나", "why": "중복 구현이 가장 비싸다"}]'
p5 loop done-when "끝" >/dev/null
p5 advance >/dev/null; p5 advance >/dev/null; p5 advance >/dev/null
P5B="$(p5 status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["prefix"])')"
mkdir -p "$PW5/.dev/plan"; printf '#\n' > "$PW5/.dev/plan/${P5B}p.md"
p5 approve-plan ".dev/plan/${P5B}p.md" >/dev/null
p5 advance >/dev/null
printf 'check:\n\t@true\n' > "$PW5/Makefile"
p5 verify -- make check >/dev/null
p5 advance >/dev/null
# 회고 패널은 **Compounding 에 진입할 때** 한 번 나온다. 두 단정이 각각 advance 를
# 부르면 두 번째는 이미 Compounding 이어서 패널이 아예 안 나오고, 그러면 "기본 질문이
# 없다"가 무조건 통과한다 — 적대적 리뷰가 지적했다. 진입 출력을 한 번 담아 둘 다 본다.
P5ENTER="$(p5 advance 2>&1 || true)"
check "설정한 회고 질문이 제시된다" '무엇을 다시 만들 뻔했나' "$P5ENTER"
check "진입 출력에 회고 패널이 있다" '회고에 답할 것' "$P5ENTER"
check_empty "기본 질문은 더 이상 나오지 않는다" \
  "$(printf '%s' "$P5ENTER" | grep '무엇이 통했나' || true)"
check "질문이 비면 지적한다" '무엇을 물을지가 없다' \
  "$(p5ed 'cfg["retro_questions"] = []'; p5 status 2>&1)"

echo "  -- 동의가 필요한 명령도 설정이다 (마찰이 크면 덜어낼 수 있다)"
CS="$(mktemp -d)"
(cd "$CS" && git init -q . && python3 "$ENGINE" init >/dev/null)
csb() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Bash","tool_input":{"command":"%s"}}' "$CS" "$1" \
  | CLAUDE_PROJECT_DIR="$CS" python3 "$ENGINE" hook; }
CSH="$CS/.claude/harness/bin/harness"
check "기본은 allow 에 승인을 요구한다" '"permissionDecision": "ask"' \
  "$(csb "$CSH allow 'docs/x.md' --reason '필요'")"
check "무엇을 승인하는지 설정 문구가 나온다" '쓰기 금지 경로에 대한 예외 요청' \
  "$(csb "$CSH allow 'docs/x.md' --reason '필요'")"
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['consent'].pop('allow')
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$CS/.claude/harness/stages.json"
check_empty "consent 에서 덜어내면 묻지 않는다" \
  "$(csb "$CSH allow 'docs/x.md' --reason '필요'")"
# 단 auto-skip on 은 별도 분기라 설정과 무관하게 계속 묻는다 (세션을 넘는 결정이다)
check "auto-skip on 은 덜어내도 계속 묻는다" '"permissionDecision": "ask"' \
  "$(python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['consent'] = {}
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$CS/.claude/harness/stages.json"; csb "$CSH auto-skip on --reason '무인 실행'")"

echo "  -- 셸 명령 분류도 설정이다 (툴체인이 프로젝트마다 다르다)"
BS="$(mktemp -d)"
(cd "$BS" && git init -q . && python3 "$ENGINE" init >/dev/null)
bsb() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Bash","tool_input":{"command":"%s"}}' "$BS" "$1" \
  | CLAUDE_PROJECT_DIR="$BS" python3 "$ENGINE" hook; }
# 보호 경로를 직접 지정하면 패턴과 무관하게 막힌다 — fail-safe 다.
check "보호 경로 직접 지정은 패턴과 무관하게 막힌다" '하네스 자신' \
  "$(bsb "myrm .claude/harness/harness.db")"
# mutator_pattern 의 효과는 **바닥값이 아닌** 보호 경로의 부모에서만 관찰된다.
# `.claude` 는 바닥값의 부모라 패턴과 무관하게 항상 막힌다(그게 요점이다).
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['folder_rules']['protected_paths'].append('vault/**')
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$BS/.claude/harness/stages.json"
check_empty "모르는 명령의 부모 지정은 통과 (기본 패턴)" "$(bsb "myrm -rf vault")"
check "바닥값의 부모는 패턴과 무관하게 막힌다" '하네스 자신' "$(bsb "myrm -rf .claude")"
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['bash']['mutator_pattern'] = r'(^|[;&|]\s*)(myrm|rm)\b'
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$BS/.claude/harness/stages.json"
check "패턴에 넣으면 그 부모도 막는다" '하네스 자신' "$(bsb "myrm -rf vault")"

echo "  -- readers 도 설정이다 (읽기 명령은 막지 않는다)"
check_empty "cat 은 보호 경로를 읽어도 통과" "$(bsb "cat .claude/harness/harness.db")"
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['bash']['readers'] = [r for r in cfg['bash']['readers'] if r != 'cat']
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$BS/.claude/harness/stages.json"
check "readers 에서 빼면 cat 도 막힌다" '하네스 자신' \
  "$(bsb "cat .claude/harness/harness.db")"
check "잘못된 정규식은 지적하고 기본값으로 돈다" '잘못된 정규식' \
  "$(python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['bash']['mutator_pattern'] = '([unclosed'
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$BS/.claude/harness/stages.json"; (cd "$BS" && python3 "$ENGINE" status 2>&1))"
check "그래도 기본 보호는 살아 있다" '하네스 자신' "$(bsb "rm .claude/harness/harness.db")"
rm -rf "$PW5" "$CS" "$BS"

echo "== 출력 문자열이 카탈로그로 번역된다 (원문이 키다)"
# 영어 버전을 분기로 만들려면 2,860줄 번역 + 413개 단정을 함께 고쳐야 했다. 출력
# 문자열이 카탈로그로 나오면 그것이 **파일 하나**가 된다. 원문을 키로 쓰므로 키를
# 발명할 필요가 없고, 번역이 없으면 원문으로 떨어져 동작이 바뀌지 않는다.
MW="$(mktemp -d)"
(cd "$MW" && git init -q . && python3 "$ENGINE" init >/dev/null)
mw() { (cd "$MW" && python3 "$ENGINE" "$@"); }
check "카탈로그가 엔진 사본 옆에 복사된다" 'messages.ko.json' "$(ls "$MW/.claude/harness/bin")"
check "카탈로그는 커밋 대상이 아니다" '^0$' \
  "$(cd "$MW" && git status --porcelain | grep -c 'messages' || true)"
# 원문 언어에서는 조회조차 하지 않는다
check_empty "기본(한국어)에서는 번역 경고가 없다" \
  "$(mw status 2>&1 | grep 'language=' || true)"

# 시험용 영어 카탈로그를 만든다. 세 문장만 번역하고 나머지는 원문으로 떨어져야 한다.
python3 -c "
import json, sys
bin_dir = sys.argv[1]
ko = json.load(open(bin_dir + '/messages.ko.json', encoding='utf-8'))
pick = [k for k in ko if k.startswith('advance 거부 —')] \
     + [k for k in ko if k.startswith('하네스 자신은 수정할 수 없다')]
en = {}
for k in pick:
    en[k] = 'EN|' + k
json.dump(en, open(bin_dir + '/messages.en.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=2)
print(len(en))
" "$MW/.claude/harness/bin" > /dev/null
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['language'] = 'en'
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$MW/.claude/harness/stages.json"

check "설정한 언어로 번역된다" 'EN|advance 거부' "$(mw advance 2>&1)"
check "번역 없는 문장은 원문으로 떨어진다" 'intent_set: 이번에 할 작업을' "$(mw advance 2>&1)"
MWH="$(printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s/.claude/harness/harness.db"}}' "$MW" "$MW" | CLAUDE_PROJECT_DIR="$MW" python3 "$ENGINE" hook)"
check "훅 경로에서도 번역된다" 'EN|하네스 자신은' "$MWH"
check "부분 번역이면 그 사실을 말한다" '번역이 [0-9]*/[0-9]*' "$(mw status 2>&1)"
# 환경변수가 설정을 이겨야 한다 — 파일을 고치지 않고 시험하는 용도이기 때문이다
check "환경변수가 설정을 이긴다" 'EN|advance 거부' \
  "$(python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['language'] = 'ko'
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$MW/.claude/harness/stages.json"; (cd "$MW" && HARNESS_LANG=en python3 "$ENGINE" advance 2>&1) || true)"
check "환경변수가 없으면 설정을 따른다" 'advance 거부 —' \
  "$( (cd "$MW" && python3 "$ENGINE" advance 2>&1) || true)"
# 카탈로그가 없는 언어를 켜면 전부 원문이 된다 — 켠 사람은 '설정이 안 먹었다'로 읽는다
check "카탈로그가 없으면 그 사실을 말한다" '찾지 못했다' \
  "$(cd "$MW" && HARNESS_LANG=fr python3 "$ENGINE" status 2>&1)"
# 카탈로그가 깨져도 게이트가 멈추면 안 된다
printf 'not json {' > "$MW/.claude/harness/bin/messages.en.json"
check "깨진 카탈로그에도 원문으로 동작한다" 'advance 거부' \
  "$(cd "$MW" && HARNESS_LANG=en python3 "$ENGINE" advance 2>&1 || true)"
rm -rf "$MW"

echo "== 승격의 종류도 설정이다 (기계화하는 방법이 프로젝트마다 다르다)"
AK="$(mktemp -d)"
(cd "$AK" && git init -q . && python3 "$ENGINE" init >/dev/null)
ak() { (cd "$AK" && python3 "$ENGINE" "$@"); }
# 반복 항목을 먼저 만든다 — 키 검사가 --as 검사보다 앞이라 유효한 키가 필요하다
python3 -c "
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1])
n = time.strftime('%Y-%m-%dT%H:%M:%S+0900')
for i in range(3):
    c.execute('INSERT INTO loop(id,intent,created_at) VALUES(?,?,?)', ('ak%d' % i, 't', n))
    c.execute('INSERT INTO event(loop_id,stage,kind,rule,target,at) VALUES(?,?,?,?,?,?)',
              ('ak%d' % i, 'execution', 'block', 'fakerule', 'f.py', n))
c.commit()" "$AK/.claude/harness/harness.db"
check "기본 종류가 안내된다" 'hook, rule, skill, structure' \
  "$(ak promote 'block:fakerule' --as bogus 2>&1)"
check_empty "없는 종류로는 승격되지 않는다" \
  "$(ak promote 'block:fakerule' --as bogus 2>&1 | grep '승격 기록' || true)"
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['promotion']['as_kinds']['test'] = '회귀 테스트로 승격 (다시 깨지면 CI 가 잡는다)'
cfg['promotion']['verify_globs']['test'] = ['tests/**']
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$AK/.claude/harness/stages.json"
check "새 종류가 안내에 들어간다" 'test' "$(ak promote 'block:fakerule' --as bogus 2>&1)"
check "설정한 종류로 승격된다" '회귀 테스트로 승격' \
  "$(ak promote 'block:fakerule' --as test --note '테스트로 막았다' 2>&1)"
check "as_kinds 에 없는 verify_globs 를 지적한다" '죽은 설정이다' \
  "$(python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['promotion']['as_kinds'].pop('test')
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$AK/.claude/harness/stages.json"; ak status 2>&1)"
rm -rf "$AK"

echo "== 훅과 CLI 의 판정이 같아야 한다 (엔진 사본 문제)"
# 실제 버그였다. 래퍼는 프로젝트 사본(.claude/harness/bin/harness.py)을 실행하고,
# 그때 plugin_root() 는 `.claude/harness/` 를 가리켜 templates/ 가 없다. 그래서
# 어휘 기본값을 못 찾았다 — 훅(플러그인 엔진)은 알고 CLI(사본)는 모르는 상태가 됐고,
# 래퍼로 advance 하면 satisfied_by 를 몰라 **디스크의 계획 파일을 인정하지 않았다.**
# 0.31.1 에서 고친 함정이 다른 문으로 돌아온 것이다.
EW="$(mktemp -d)"
(cd "$EW" && git init -q . && python3 "$ENGINE" init >/dev/null)
check "기본값이 엔진 사본 옆에 복사된다" 'defaults.json' "$(ls "$EW/.claude/harness/bin")"
check "기본값은 커밋 대상이 아니다" '^0$' \
  "$(cd "$EW" && git status --porcelain | grep -c 'defaults.json' || true)"
# 어휘화 이전 문서를 흉내낸다
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg.pop('criteria'); cfg.pop('write_rules')
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$EW/.claude/harness/stages.json"
EPLUG="$( (cd "$EW" && python3 "$ENGINE" advance 2>&1) || true )"
EWRAP="$( (cd "$EW" && .claude/harness/bin/harness advance 2>&1) || true )"
check "플러그인 엔진이 도움말을 안다" 'harness loop intent' "$EPLUG"
check "래퍼도 같은 도움말을 낸다" 'harness loop intent' "$EWRAP"
check "래퍼도 쓰기 규칙 $NRULES 개를 안다" "^$NRULES\$" \
  "$(cd "$EW" && python3 -c "
import sys, os
sys.path.insert(0, '.claude/harness/bin')
import harness as h
print(len(h.write_rules(h.load_config(os.getcwd()))))")"
# 파일 판정도 같아야 한다 — 이게 실제로 갈렸던 지점이다
(cd "$EW" && .claude/harness/bin/harness loop intent "사본 판정" >/dev/null \
  && .claude/harness/bin/harness loop done-when "끝" >/dev/null \
  && .claude/harness/bin/harness advance >/dev/null \
  && .claude/harness/bin/harness advance >/dev/null \
  && .claude/harness/bin/harness advance >/dev/null)
EPRE="$(cd "$EW" && .claude/harness/bin/harness status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["prefix"])')"
mkdir -p "$EW/.dev/plan"; printf '# 계획\n' > "$EW/.dev/plan/${EPRE}p.md"
check "래퍼도 디스크의 계획 파일을 인정한다" '^plan_approved$' \
  "$(cd "$EW" && .claude/harness/bin/harness status --json \
     | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["exit_missing"]))')"
rm -rf "$EW"

echo "== 어휘 안에서 덜어낸 것은 되살리지 않는다"
# "마찰이 크면 stages.json 에서 덜어낸다"가 이 하네스의 작업 방식이다. 템플릿 채움이
# 항목 단위로 병합하면 지운 것이 되살아나 그 자유를 빼앗는다. 영역이 통째로 없을 때만 채운다.
RW="$(mktemp -d)"
(cd "$RW" && git init -q . && python3 "$ENGINE" init >/dev/null)
rw() { (cd "$RW" && python3 "$ENGINE" "$@"); }
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
del cfg['criteria']['plan_approved']          # 사람 승인 단계를 안 쓰겠다
for st in cfg['stages']:
    if st['id'] == 'planning':
        st['exit_criteria'] = [x for x in st['exit_criteria'] if x != 'plan_approved']
        st['stop_requires'] = [x for x in st['stop_requires'] if x != 'plan_approved']
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$RW/.claude/harness/stages.json"
check_empty "덜어낸 것을 문제라고 하지 않는다" \
  "$(rw status 2>&1 | grep '무시되는 설정' || true)"
rw init >/dev/null 2>&1
check "다시 init 해도 지운 항목이 부활하지 않는다" '^False$' \
  "$(python3 -c "
import json, sys
c = json.load(open(sys.argv[1], encoding='utf-8'))
print('plan_approved' in c['criteria'])" "$RW/.claude/harness/stages.json")"
rw loop intent "덜어낸 어휘" >/dev/null
rw loop done-when "끝" >/dev/null
rw advance >/dev/null; rw advance >/dev/null; rw advance >/dev/null
RPRE="$(rw status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["prefix"])')"
mkdir -p "$RW/.dev/plan"; printf '#\n' > "$RW/.dev/plan/${RPRE}p.md"
check "승인 조건을 덜어냈으면 계획 파일만으로 닫힌다" 'Execution' "$(rw advance 2>&1)"
rm -rf "$RW"

echo "== 손상된 stages.json 은 템플릿으로 갈아치우지 않는다"
# 갈아치우면 사용자가 **덜어낸 규칙이 말없이 되살아나고** 이유 모를 차단만 남는다.
# 없는 것(설치 전)과 깨진 것은 다르다.
BW="$(mktemp -d)"
(cd "$BW" && git init -q . && python3 "$ENGINE" init >/dev/null)
echo 'not json at all {' > "$BW/.claude/harness/stages.json"
# set -e 아래에서는 `cmd; RC=$?` 가 통하지 않는다 — 실패한 순간 스크립트가 죽는다.
(cd "$BW" && python3 "$ENGINE" status >/dev/null 2>&1) && BRC=0 || BRC=$?
check "CLI 는 크게 실패한다 (exit 1)" '^1$' "$BRC"
check "무엇이 깨졌는지 말한다" '손상' "$( (cd "$BW" && python3 "$ENGINE" status 2>&1) )"
BH="$(printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s/docs/x.md"}}' "$BW" "$BW" | CLAUDE_PROJECT_DIR="$BW" python3 "$ENGINE" hook 2>&1)"
# 출구를 하나로 모았으므로 **모든 훅**이 알린다. 예전에는 세션 시작만 알렸고,
# 그래서 세션 중간에 깨지면 남은 세션이 조용히 게이트 없이 돌았다.
check_absent "훅은 차단하지 않는다 (세션을 벽돌로 만들지 않는다)" 'permissionDecision' "$BH"
check "PreToolUse 도 게이트가 꺼진 사실을 알린다" '게이트가 꺼졌다' "$BH"
printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s/docs/x.md"}}' "$BW" "$BW" \
  | CLAUDE_PROJECT_DIR="$BW" python3 "$ENGINE" hook >/dev/null 2>&1 && BRC2=0 || BRC2=$?
check "훅 종료 코드도 0" '^0$' "$BRC2"
# 조용히 꺼지지도 않아야 한다 — 게이트가 사라진 것을 모르면 그게 최악이다
check "세션 시작에서 비활성 사실을 알린다" '게이트가 꺼졌다' \
  "$(printf '{"hook_event_name":"SessionStart","cwd":"%s"}' "$BW" \
     | CLAUDE_PROJECT_DIR="$BW" python3 "$ENGINE" hook)"
check "되돌리는 방법을 준다" 'git checkout' \
  "$(printf '{"hook_event_name":"SessionStart","cwd":"%s"}' "$BW" \
     | CLAUDE_PROJECT_DIR="$BW" python3 "$ENGINE" hook)"

echo "== skip 도 advance 와 같은 판정을 쓴다"
# 0.30.0 에서 파일 판정을 넣을 때 skip 경로를 안 맞췄다. 그래서 계획 파일이 디스크에
# 있는데도 "계획 파일을 남겨야 한다"고 거부했다 — 이미 한 일을 하라는 말이고
# 빠져나갈 길이 없다. 직접 돌려 확인한 함정이다.
KW="$(mktemp -d)"
(cd "$KW" && git init -q . && python3 "$ENGINE" init >/dev/null)
kw() { (cd "$KW" && python3 "$ENGINE" "$@"); }
kw loop intent "스킵 대조" >/dev/null
kw loop done-when "끝" >/dev/null
kw advance >/dev/null; kw advance >/dev/null; kw advance >/dev/null
check "계획 파일이 없으면 skip 도 거부한다" '기록은 남겨야 한다' \
  "$(kw skip planning --reason '구조 선행' 2>&1)"
KPRE="$(kw status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["prefix"])')"
mkdir -p "$KW/.dev/plan"; printf '# 계획\n' > "$KW/.dev/plan/${KPRE}plan.md"
check "관측 없이 파일만 있으면 skip 이 통과한다" '스킵(사용자 승인)' \
  "$(kw skip planning --reason '구조 선행' 2>&1)"
rm -rf "$CW" "$BW" "$KW"

echo "== 자기 잠금은 설정으로 열 수 없다 (바닥값)"
# 적대적 리뷰가 일곱 모양으로 뚫었다. 뿌리는 하나였다 — 하네스의 자기 잠금이
# `stages.json` 에 있었고, 그 파일은 `context` 클래스라 모델이 여러 단계에서 쓸 수
# 있다. 엔진을 쓸 수 있으면 **모든 게이트가 끝난다.** 0.30.0 엔진에서도 뚫렸다.
# 이제 바닥값(SELF_LOCK)이 코드에 있고 설정은 여기에 더할 수만 있다.
SLW="$(mktemp -d)"
(cd "$SLW" && git init -q . && python3 "$ENGINE" init >/dev/null)
sled() { python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
exec(sys.argv[2])
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$SLW/.claude/harness/stages.json" "$1"; }
slw() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s/%s"}}' "$SLW" "$SLW" "$1" \
  | CLAUDE_PROJECT_DIR="$SLW" python3 "$ENGINE" hook; }
slb() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Bash","tool_input":{"command":"%s"}}' "$SLW" "$1" \
  | CLAUDE_PROJECT_DIR="$SLW" python3 "$ENGINE" hook; }
# context 쓰기가 허용된 단계로 간다 (여기서 stages.json 을 고칠 수 있다)
(cd "$SLW" && python3 "$ENGINE" loop intent "바닥값" >/dev/null \
  && python3 "$ENGINE" loop done-when "끝" >/dev/null \
  && python3 "$ENGINE" advance >/dev/null)
check_empty "이 단계에서 stages.json 은 쓸 수 있다 (설계상)" \
  "$(slw '.claude/harness/stages.json')"

# 설정을 최대한 무력화한다 — 리뷰가 찾은 일곱 모양을 한꺼번에
sled '
cfg["folder_rules"]["protected_paths"] = []
cfg["write_rules"] = []
cfg["bash"]["readers"].append("rm")
cfg["bash"]["mutator_pattern"] = "(?!)"
'
for F in ".claude/harness/bin/harness.py" ".claude/harness/bin/harness" \
         ".claude/harness/harness.db" ".claude/harness/bin/defaults.json"; do
  check "설정을 비워도 막힌다: $F" '하네스 자신은 수정할 수 없다' "$(slw "$F")"
done
check "allow 로도 stages.json 으로도 못 연다고 말한다" 'stages.json` 을 고쳐서도 열 수 없다' \
  "$(slw '.claude/harness/harness.db')"

check "차단이 self_lock 으로 적립된다" '^self_lock$' \
  "$(python3 -c "
import sqlite3, sys
r = sqlite3.connect(sys.argv[1]).execute(
  \"SELECT rule FROM event WHERE kind='block' ORDER BY rowid DESC LIMIT 1\").fetchone()
print(r[0] if r else '(없음)')" "$SLW/.claude/harness/harness.db")"
echo "  -- 경로 표기를 바꿔 우회할 수 없다"
# macOS·Windows 기본 파일시스템은 대소문자를 구분하지 않는다. `BIN/harness.py` 는
# `bin/harness.py` 와 **같은 파일**인데 glob_match 는 대소문자를 구분해서 Write·Bash
# 양쪽에서 통과했다 — 2차 리뷰 준비 중 직접 확인했다.
for F in ".claude/harness/BIN/harness.py" ".claude/harness/Bin/Harness.PY" \
         ".claude/harness/bin" ".claude/harness/harness.db-journal" \
         ".claude/harness/bin/messages.ko.json" ".claude/harness/bin/./harness.py" \
         ".claude/harness/bin/../bin/harness.py"; do
  check "표기를 바꿔도 막힌다: $F" '하네스 자신은 수정할 수 없다' "$(slw "$F")"
done
for C in "rm .claude/harness/BIN/harness.py" "rm .claude/harness/harness.db-journal" \
         "rm -rf .claude/HARNESS"; do
  check "Bash 도 표기와 무관하게 막힌다: $C" '하네스 자신' "$(slb "$C")"
done
for C in "rm .claude/harness/bin/harness.py" "rm -rf .claude" \
         "dd if=/dev/null of=.claude/harness/harness.db" \
         "find .claude/harness -delete" "printf x >|.claude/harness/harness.db"; do
  check "Bash 도 막힌다: $C" '하네스 자신' "$(slb "$C")"
done
# 과잉 차단은 마찰이 되고, 마찰은 게이트를 끄게 만든다 — 읽기는 여전히 통과해야 한다
check_empty "읽기는 여전히 통과한다 (cat)" "$(slb 'cat .claude/harness/harness.db')"
check_empty "읽기는 여전히 통과한다 (grep)" "$(slb 'grep -c x .claude/harness/bin/harness.py')"
# grant 로도, grant_opens 로도 열리지 않는다
sled 'cfg["write_rules"] = [{"id": "protected", "grant_opens": True, "when": {},
      "require": {"not_matching": "protected_paths"}, "deny": "x"}]'
(cd "$SLW" && python3 "$ENGINE" allow '.claude/harness/**' --reason '시험' >/dev/null 2>&1) || true
check "예외를 등록해도 막힌다" '하네스 자신은 수정할 수 없다' \
  "$(slw '.claude/harness/bin/harness.py')"
# stages.json 자체는 바닥값이 아니다 — 사람이 고쳐야 하는 문서다
check_empty "stages.json 은 여전히 고칠 수 있다" "$(slw '.claude/harness/stages.json')"

echo "  -- 공허한 설정은 진단이 말한다"
check "protected_paths 를 비우면 말한다" '바닥값으로 계속 보호되지만' \
  "$( (cd "$SLW" && python3 "$ENGINE" status 2>&1) )"
sled 'cfg["folder_rules"]["protected_paths"] = [".claude/harness/LEARNED.md"]
cfg["write_rules"] = []'
check "write_rules 를 비우면 말한다" 'write_rules 가 비어 있다' \
  "$( (cd "$SLW" && python3 "$ENGINE" status 2>&1) )"
sled "cfg['write_rules'] = json.load(open('$SLW/.claude/harness/bin/defaults.json', encoding='utf-8'))['write_rules']
cfg['bash']['mutator_pattern'] = '(?!)'"
check "아무것도 안 맞는 정규식을 말한다" '하나도 잡지 못한다' \
  "$( (cd "$SLW" && python3 "$ENGINE" status 2>&1) )"
sled 'cfg["bash"]["mutator_pattern"] = "(^|[;&|]\\s*)(rm|mv)\\b"'
check "변경 명령을 readers 로 선언하면 말한다" '읽기로 선언할 수 없다' \
  "$( (cd "$SLW" && python3 "$ENGINE" status 2>&1) )"
rm -rf "$SLW"

echo "== 재연결은 회차를 올린다 (낡은 산출물이 조건을 채우지 않게)"
# 적대적 리뷰가 찾았다. 재연결 후 접두사가 그대로여서 **1회차 계획서가 이번 회차의
# plan_file 을 채웠고**, skip 까지 통과했다. 0.31.1 에서 skip 을 criterion_met 으로
# 바꾼 것이 파일 판정과 만나 생긴 구멍이다.
AW2="$(mktemp -d)"
(cd "$AW2" && git init -q . && python3 "$ENGINE" init >/dev/null)
aw() { (cd "$AW2" && python3 "$ENGINE" "$@"); }
printf 'check:\n\t@true\n' > "$AW2/Makefile"
aw loop intent "1회차" >/dev/null || true
aw loop done-when "끝" >/dev/null || true
AL="$(aw status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["loop"])')"
AP1="$(aw status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["prefix"])')"
aw advance >/dev/null; aw advance >/dev/null; aw advance >/dev/null || true
mkdir -p "$AW2/.dev/plan" "$AW2/.dev/retrospect"
printf '#\n' > "$AW2/.dev/plan/${AP1}plan.md"
aw approve-plan ".dev/plan/${AP1}plan.md" >/dev/null || true
aw advance >/dev/null; aw verify -- make check >/dev/null; aw advance >/dev/null || true
printf '#\n' > "$AW2/.dev/retrospect/${AP1}r.md"
aw advance --done >/dev/null 2>&1 || true
check "재연결이 회차를 올린다고 말한다" '회차 2 로 다시' \
  "$(aw loop adopt "$AL" --reason '재개' 2>&1)"
aw loop intent "재연결" >/dev/null || true
aw loop done-when "끝" >/dev/null || true
aw advance >/dev/null; aw advance >/dev/null; aw advance >/dev/null || true
AP2="$(aw status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["prefix"])')"
check "접두사가 1회차와 다르다" '[0-9a-f]-2-' "$AP2"
check "1회차 접두사가 아니다" '^0$' "$(printf '%s' "$AP2" | grep -c -- '-1-' || true)"
check "낡은 계획서는 조건을 채우지 못한다" 'plan_file' \
  "$(aw status --json | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["exit_missing"]))')"
check "낡은 계획서로 스킵도 안 된다" '기록은 남겨야 한다' \
  "$(aw skip planning --reason '구조 선행' 2>&1)"
rm -rf "$AW2"

echo "== 내장 조건의 판정 방식을 바꾸면 알린다 (이름과 판정의 어긋남)"
# 적대적 리뷰가 셋을 실증했고 전부 조용했다.
#   promotion_decided → file      : 미결 승격이 남아도 회차가 닫힌다
#   plan_approved → file + "**"   : 접두사만 맞으면 아무 파일이 '사람 승인' 이 된다
#   plan_file → no_pending_promotions : 산출물 없이 통과한다
# 엔진에 이름을 다시 박아 금지하지 않는다 — 기본값과 대조해 **말한다.**
DW="$(mktemp -d)"
(cd "$DW" && git init -q . && python3 "$ENGINE" init >/dev/null)
dw() { (cd "$DW" && python3 "$ENGINE" "$@"); }
dwed() { python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
exec(sys.argv[2])
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$DW/.claude/harness/stages.json" "$1"; }

check_empty "기본 설정에는 아무 말도 하지 않는다" \
  "$(dw status 2>&1 | grep '판정 방식을' || true)"
dwed 'cfg["criteria"]["promotion_decided"]["satisfied_by"] = "file"
cfg["criteria"]["promotion_decided"]["write_glob"] = [".dev/retrospect/**/*.md"]'
check "판정 방식 변경을 말한다" "'no_pending_promotions' → 'file'" "$(dw status 2>&1)"
dwed 'cfg["criteria"]["plan_approved"]["satisfied_by"] = "file"
cfg["criteria"]["plan_approved"]["write_glob"] = ["**"]'
check "글롭이 '**' 로 넓어진 것을 말한다" "'\*\*' 로 넓어졌다" "$(dw status 2>&1)"
dwed 'cfg["criteria"]["plan_approved"].pop("human", None)'
check "human 을 뗀 것을 말한다" 'human 표시를 뗐다' "$(dw status 2>&1)"
# 사용자가 새로 만든 조건은 기본값에 없으므로 아무 말도 하지 않아야 한다 (오진 금지)
dwed 'cfg["criteria"]["my_own"] = {"satisfied_by": "file",
      "write_glob": [".dev/plan/**/*.md"], "help": "내 조건"}'
check_empty "사용자가 만든 조건은 지적하지 않는다" \
  "$(dw status 2>&1 | grep 'my_own' || true)"

echo "  -- 활성 작업이 없어도 세션 시작에서 알린다"
NW="$(mktemp -d)"
(cd "$NW" && git init -q . && python3 "$ENGINE" init >/dev/null)
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['write_rules'] = []
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$NW/.claude/harness/stages.json"
python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.execute(\"DELETE FROM meta WHERE k='head'\"); c.commit()
" "$NW/.claude/harness/harness.db"
check "head 가 없어도 설정 문제를 알린다" '무시되는 설정' \
  "$(printf '{"hook_event_name":"SessionStart","cwd":"%s"}' "$NW" \
     | CLAUDE_PROJECT_DIR="$NW" python3 "$ENGINE" hook)"
rm -rf "$DW" "$NW"

echo "== 진짜 경계는 어디인가 (엔진 사본이 아니라 DB 다)"
# 적대적 리뷰 두 차례가 자기 잠금 우회를 열 가지 넘게 찾았는데, 그 대상인
# `.claude/harness/bin/` 은 **게이트가 아니었다.** 훅은 전부 플러그인 엔진을
# 실행하므로 사본이 쓰레기가 되어도 판정은 그대로다. 반면 DB 를 못 읽으면
# 모든 게이트가 한꺼번에 꺼진다. 무엇을 지켜야 하는지가 뒤바뀌어 있었다.
BW2="$(mktemp -d)"
(cd "$BW2" && git init -q . && python3 "$ENGINE" init >/dev/null)
b2w() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s/%s"}}' "$BW2" "$BW2" "$1" \
  | CLAUDE_PROJECT_DIR="$BW2" python3 "$ENGINE" hook; }
echo 'garbage not python' > "$BW2/.claude/harness/bin/harness.py"
check "엔진 사본이 쓰레기여도 훅은 판정한다" '"permissionDecision": "deny"' "$(b2w 'docs/x.md')"
check "사본은 세션마다 복원된다" 'step-seven-harness engine' \
  "$(printf '{"hook_event_name":"SessionStart","cwd":"%s"}' "$BW2" \
     | CLAUDE_PROJECT_DIR="$BW2" python3 "$ENGINE" hook >/dev/null; \
     head -3 "$BW2/.claude/harness/bin/harness.py")"

echo "  -- DB 를 못 읽으면 게이트가 꺼진다. 그 사실을 **매번** 말한다."
echo 'not a database' > "$BW2/.claude/harness/harness.db"
rm -f "$BW2/.claude/harness/harness.db-wal" "$BW2/.claude/harness/harness.db-shm"
DBOUT="$(b2w 'docs/x.md' 2>/dev/null)"
check "차단하지 않는다 (세션을 벽돌로 만들지 않는다)" '^0$' \
  "$(printf '%s' "$DBOUT" | grep -c permissionDecision || true)"
check "게이트가 꺼졌다고 말한다" '게이트가 꺼졌다' "$DBOUT"
check "복구 방법을 준다" 'init' "$DBOUT"
check "PreToolUse 에서도 말한다 (세션 시작뿐이 아니다)" 'systemMessage' "$DBOUT"
b2rc=0
printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s/docs/x.md"}}' "$BW2" "$BW2" \
  | CLAUDE_PROJECT_DIR="$BW2" python3 "$ENGINE" hook >/dev/null 2>&1 && b2rc=0 || b2rc=$?
check "종료 코드는 여전히 0" '^0$' "$b2rc"
rm -rf "$BW2"

echo "== 사전 승인된 래퍼는 권위 있는 원본을 먼저 실행한다"
# `.claude/settings.json` 이 래퍼를 사전 승인한다(SAFE_PERMS). 래퍼가 프로젝트
# 사본을 먼저 실행하면 **사본을 덮는 것이 곧 승인 없는 임의 코드 실행**이 된다.
# 3차 리뷰가 지적했고 맞다 — 사본은 게이트가 아니라던 내 판단이 이 점에서 틀렸다.
PW6="$(mktemp -d)"
(cd "$PW6" && git init -q . && python3 "$ENGINE" init >/dev/null)
check "정상 동작" '자기검사' "$( (cd "$PW6" && ./.claude/harness/bin/harness status) )"
printf 'print("PWNED")\n' > "$PW6/.claude/harness/bin/harness.py"
check_absent "사본을 덮어도 그 코드가 돌지 않는다" 'PWNED' \
  "$( (cd "$PW6" && ./.claude/harness/bin/harness status 2>&1) )"
check "원본이 계속 판정한다" '강제 중' "$( (cd "$PW6" && ./.claude/harness/bin/harness status 2>&1) )"
rm -rf "$PW6"

echo "== 설치 형태를 주장하지 않고 잰다"
# "훅은 프로젝트 밖 엔진을 실행한다"는 git 소스에서만 참이다. directory 소스로
# 등록하면 플러그인 루트가 프로젝트 안이 되고, 그건 README 가 로컬 테스트에 권하는
# 방식이다. 문서에 주장을 적어두면 설치 형태가 바뀔 때 거짓이 된다.
IW="$(mktemp -d)"
(cd "$IW" && git init -q . && python3 "$ENGINE" init >/dev/null)
check_absent "정상 설치에는 경고하지 않는다" '프로젝트 안에 있다' "$( (cd "$IW" && python3 "$ENGINE" status 2>&1) )"
IW2="$(mktemp -d)"
mkdir -p "$IW2/plugins/step-seven-harness"
cp -r "$(dirname "$ENGINE")" "$IW2/plugins/step-seven-harness/"
cp -r "$(dirname "$ENGINE")/../templates" "$IW2/plugins/step-seven-harness/"
(cd "$IW2" && git init -q . && python3 plugins/step-seven-harness/scripts/harness.py init >/dev/null)
check "엔진이 프로젝트 안이면 그 사실을 말한다" '프로젝트 안에 있다' \
  "$( (cd "$IW2" && python3 plugins/step-seven-harness/scripts/harness.py status 2>&1) )"
check "자기 잠금을 신뢰할 수 없다고 말한다" '신뢰할 수 없다' \
  "$( (cd "$IW2" && python3 plugins/step-seven-harness/scripts/harness.py status 2>&1) )"
# 대소문자만 다른 접두사로 실행해도 담김을 알아채야 한다. `normcase` 는 POSIX 에서
# 항등이고 `realpath` 도 표기를 정규화하지 않으므로(확인했다), macOS 에서
# `/Private/…` 로 실행하고 root 가 `/private/…` 이면 경고가 사라졌다.
# **경로에서 실제로 표기를 바꿔야 한다.** 앞서 `/private/`·`/tmp/` 치환을 썼더니
# mktemp 가 `/var/folders/…` 를 주는 환경에서 아무것도 바뀌지 않아, 같은 경로를 두 번
# 검사하는 공허한 테스트가 됐다 — 변이를 넣어도 통과했다. 두 번째 구성요소를 대문자로
# 바꿔 반드시 다른 표기를 만든다.
IWUP="$(python3 -c "
import sys
parts = sys.argv[1].strip('/').split('/')
if len(parts) > 1:
    parts[1] = parts[1].upper()
print('/' + '/'.join(parts))" "$IW2")"
check "표기가 실제로 달라졌다" '^1$' "$([ "$IWUP" != "$IW2" ] && echo 1 || echo 0)"
if [ -d "$IWUP" ]; then
  check "대소문자가 다른 접두사로 실행해도 알아챈다" '프로젝트 안에 있다' \
    "$( (cd "$IW2" && python3 "$IWUP/plugins/step-seven-harness/scripts/harness.py" status 2>&1) )"
else
  # 대소문자를 구분하는 파일시스템이면 이 시나리오가 성립하지 않는다 — 건너뛴다.
  check "대소문자 무시 비교를 쓴다 (코드 확인)" 'normcase(pr).lower()' \
    "$(grep -o 'normcase(pr)\.lower()' "$(dirname "$0")/../scripts/harness.py" | head -1)"
fi
rm -rf "$IW" "$IW2"

echo "== 재연결은 측정을 건드리지 않는다 (종류를 갈랐다)"
# cycle_close 는 **측정 창 경계**이자 **회차 스냅샷**(detail 을 JSON 으로 파싱)이다.
# 재연결에 그 종류를 쓰면 이전 회차 측정치가 창에서 빠지면서 기록되지도 않고,
# 사유가 JSON 처럼 생기면 가짜 회차로 집계된다 — 3차 리뷰가 둘 다 찾았다.
CW2="$(mktemp -d)"
(cd "$CW2" && git init -q . && python3 "$ENGINE" init >/dev/null)
cw2() { (cd "$CW2" && python3 "$ENGINE" "$@"); }
cw2 loop intent x >/dev/null
cw2 loop done-when y >/dev/null
CWL="$(cw2 status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["loop"])')"
cw2 loop adopt "$CWL" --reason '{"blocks": 999, "refails": 999}' >/dev/null
check "cycle_close 를 쓰지 않는다" '^0$' \
  "$(python3 -c "
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute(
  \"SELECT COUNT(*) FROM event WHERE kind='cycle_close'\").fetchone()[0])" \
  "$CW2/.claude/harness/harness.db")"
check "cycle_adopt 로 기록한다" '^1$' \
  "$(python3 -c "
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute(
  \"SELECT COUNT(*) FROM event WHERE kind='cycle_adopt'\").fetchone()[0])" \
  "$CW2/.claude/harness/harness.db")"
# 3회차가 지키던 성질은 그대로다: **JSON 처럼 생긴 사유가 집계로 새면 안 된다.**
# 달라진 것은 재연결도 이제 집계를 남긴다는 점이다 — 안 남겼더니 마찰이 쌓인
# 회차를 `loop adopt` 한 줄로 표본에서 지울 수 있었다(4회차 C③). 사유는
# `cycle_adopt_reason` 이라는 **다른 행**으로 갈라 둘 다 만족시킨다.
check "사유는 집계와 다른 행에 남는다" '^1$' \
  "$(python3 -c "
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute(
  \"SELECT COUNT(*) FROM event WHERE kind='cycle_adopt_reason'\").fetchone()[0])" \
  "$CW2/.claude/harness/harness.db")"
check "JSON 사유가 가짜 회차가 되지 않는다" '^0$' \
  "$(cw2 metrics 2>&1 | grep -c '999' || true)"
check "그래도 회차 집계는 남는다 (adopt 로 지울 수 없다)" '기록된 회차 1개' \
  "$(cw2 metrics 2>&1)"
check "그래도 접두사는 바뀐다" '[0-9a-f]-2-' \
  "$(cw2 status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["prefix"])')"
rm -rf "$CW2"

echo "== 자기검사: 대리 지표가 아니라 실제 판정을 돌린다"
# 세 번 다르게 추정했고 세 번 다 거짓말했다 — 무력화 모양 예측, 개수 세기,
# '발동 가능' 세기. 셋 다 설정을 들여다보고 결론을 추정했다. 추정은 한 발 늦는다.
# 이제 대표 조작을 **실제 판정 함수**에 넣고 결과를 본다. 거짓말할 수 없다.
SF="$(mktemp -d)"
(cd "$SF" && git init -q . && python3 "$ENGINE" init >/dev/null)
sf() { (cd "$SF" && python3 "$ENGINE" "$@"); }
sfed() { python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
exec(sys.argv[2])
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$SF/.claude/harness/stages.json" "$1"; }

check_selftest "정상이면 전부 통과한다" 0 "$(sf status 2>&1)"
check_absent "정상이면 실패를 내지 않는다" '자기검사 .* 실패' "$(sf status 2>&1)"
check "--json 에도 실린다" 'selftest' "$(sf status --json 2>&1)"
# **탐침 목록이 게이트 목록을 덮는가.** 개수 바닥값으로는 탐침 다섯을 지워도 통과했다 —
# 자기 자신을 재기 때문이다. 설정의 동의 종류마다 그 종류를 실제로 넣어 보는 탐침이
# 있는지 이름으로 대조한다.
sf status --json > "$SF/sel.json" 2>/dev/null
SFCOV="$(python3 - "$(dirname "$ENGINE")" "$SF/sel.json" <<'PYC'
import json, os, sys
labels = [x.get("what", "") for x in
          json.load(open(sys.argv[2], encoding="utf-8")).get("selftest", [])]
cfg = json.load(open(os.path.join(sys.argv[1], "..", "templates", "stages.json"),
                     encoding="utf-8"))
missing = [k for k in cfg["consent"] if not any(k in lb for lb in labels)]
print("동의 %d종 · 탐침 없는 종류 %d개: %s"
      % (len(cfg["consent"]), len(missing), ", ".join(missing) or "없음"))
PYC
)"
check "동의 종류를 전수로 센다" '동의 [1-9][0-9]*종' "$SFCOV"
check "모든 동의 종류에 탐침이 있다" '탐침 없는 종류 0개' "$SFCOV"

echo "  -- 개수 요약이 놓쳤던 것들을 잡는다"
# ① 거짓값 조건: Codex 가 'live_rules 보정도 못 잡는다' 고 한 HIGH
sfed 'for r in cfg["write_rules"]: r["when"] = {"class": "nope"}'
check "죽은 규칙을 잡는다" '자기검사 .*실패' "$(sf status 2>&1)"
check "무엇이 통과했는지 말한다" 'docs/ 쓰기' "$(sf status 2>&1)"
# ② 규칙을 통째로 비움
sfed "cfg['write_rules'] = json.load(open('$SF/.claude/harness/bin/defaults.json', encoding='utf-8'))['write_rules']"
check_selftest "복구하면 다시 통과한다" 0 "$(sf status 2>&1)"
sfed 'cfg["write_rules"] = []'
check "규칙을 비우면 잡는다" '자기검사 .*실패' "$(sf status 2>&1)"
# ③ 바닥값이 버티는 경우는 **통과가 사실**이다 (거짓 경고를 내지 않는다)
sfed "cfg['write_rules'] = json.load(open('$SF/.claude/harness/bin/defaults.json', encoding='utf-8'))['write_rules']
cfg['folder_rules']['protected_paths'] = []"
# `protected_paths` 를 비우면 **설정으로만 보호되던** 경로(LEARNED.md)는 정말 열린다.
# 그러니 자기검사가 그 한 건을 실패로 잡는 것이 사실이다. 확인할 것은 두 가지다:
# 실패가 정확히 그 한 건인가, 그리고 **바닥값 탐침은 여전히 버티는가**.
SFO="$(sf status 2>&1)"
check_selftest "설정으로만 보호되던 경로 한 건만 실패로 잡는다" 1 "$SFO"
check "무엇이 열렸는지 이름을 말한다" '설정의 보호 경로' "$SFO"
check_absent "바닥값 탐침은 실패에 없다 (엔진)" '하네스 엔진 사본 쓰기 → 통과' "$SFO"
check_absent "바닥값 탐침은 실패에 없다 (DB)" '상태 DB 쓰기 → 통과' "$SFO"
check_absent "바닥값 탐침은 실패에 없다 (Bash)" 'Bash 로 엔진 삭제 → 통과' "$SFO"

echo "  -- 종료 조건도 탐침한다 (Codex Claim C HIGH)"
sfed "cfg['folder_rules']['protected_paths'] = ['.claude/harness/LEARNED.md']
cfg['criteria']['plan_approved'] = {'satisfied_by': 'file', 'human': True,
                                    'write_glob': ['**'], 'help': 'x'}"
check "글롭이 '**' 면 무관한 파일도 받는다고 말한다" '무관한 경로도 받는다' "$(sf status 2>&1)"
sfed "cfg['criteria']['plan_approved'] = json.load(open('$SF/.claude/harness/bin/defaults.json', encoding='utf-8'))['criteria']['plan_approved']
cfg['criteria']['verification_evidence']['bash_pattern'] = '.*'"
check "검증 패턴이 넓으면 잡는다" '검증이 아닌 명령도 인정' "$(sf status 2>&1)"
# Codex 가 '탐침 4개를 피하는 패턴이 있다' 고 지적한 것.
#
# witness 가 `true` 였는데 이제 `true` 는 **실행하지 않는 이름**(BASH_NON_EXEC)이라
# 판정이 아예 증거로 세지 않는다 — 탐침이 아니라 아래 층이 막는다. 그러니 여기서는
# **실제로 실행되는** 무해한 명령을 witness 로 써야 검사가 뜻을 갖는다.
sfed "cfg['criteria']['verification_evidence']['bash_pattern'] = '\\\\bgit\\\\s+status\\\\b|\\\\bpytest\\\\b'"
check "무해한 명령을 증거로 삼는 패턴을 잡는다" 'git status' "$(sf status 2>&1)"
rm -rf "$SF"

echo "== 탐침이 조용히 무력해지지 않는다"
# 4차 리뷰가 찾은 것. 탐침에 고정한 단계가 없으면 **조용히 현재 단계로 폴백**해
# 구분력을 잃었다 — stage_write 가 대신 막아 죽은 규칙을 가린 채 통과했다.
# 그리고 관측 신호 셋(bash_pattern·tools·tool_pattern) 중 하나만 탐침하고 있었다.
S4W="$(mktemp -d)"
(cd "$S4W" && git init -q . && python3 "$ENGINE" init >/dev/null)
s4() { (cd "$S4W" && python3 "$ENGINE" "$@"); }
s4ed() { python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
exec(sys.argv[2])
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$S4W/.claude/harness/stages.json" "$1"; }

check_selftest "정상이면 전부 통과" 0 "$(s4 status 2>&1)"
# 고정 단계를 없애면 (단계 id 를 바꾸면) 폴백하지 않고 실패로 보고해야 한다
s4ed '
for st in cfg["stages"]:
    if st["id"] == "scaffolding":
        st["id"] = "x_scaffolding"
cfg["folder_rules"]["new_toplevel_dir_stages"] = ["x_scaffolding"]'
check "고정 단계가 없으면 실패로 보고한다" '탐침을 돌릴 수 없다' "$(s4 status 2>&1)"
check "어느 단계가 없는지 말한다" "scaffolding" "$(s4 status 2>&1)"

echo "  -- 관측 신호 셋을 전부 탐침한다"
S4B="$(mktemp -d)"
(cd "$S4B" && git init -q . && python3 "$ENGINE" init >/dev/null)
s4b() { (cd "$S4B" && python3 "$ENGINE" "$@"); }
s4bed() { python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
exec(sys.argv[2])
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$S4B/.claude/harness/stages.json" "$1"; }
s4bed 'cfg["criteria"]["verification_evidence"]["tools"] = ["Read"]'
# 이 종류는 **진단**이다 — 판정(`criterion_met`)을 지나지 않으므로 탐침이 아니다.
# 탐침 자리에 있던 시절 `자기검사 42/42` 의 대부분이 "설정이 앞뒤가 맞다" 는
# 뜻이었고, 게이트가 실제로 막는지와 무관한 수였다 (4회차).
check "무해한 도구를 증거로 세면 잡는다" '무해한 도구를 증거로 센다: Read' "$(s4b status 2>&1)"
s4bed 'cfg["criteria"]["verification_evidence"]["tools"] = ["Agent", "Task"]
cfg["criteria"]["verification_evidence"]["tool_pattern"] = ".*"'
check "도구 패턴이 너무 넓으면 잡는다" '무해한 도구에도 걸린다' "$(s4b status 2>&1)"
rm -rf "$S4W" "$S4B"

echo "== 사본은 마지막이다 (플러그인 → 캐시 → 사본)"
# 고정 경로가 사라진 순간 사본이 먼저 실행되면, 그 사본은 사전 승인돼 있으므로
# 승인 없는 임의 코드 실행이 된다 — 4차 리뷰가 지적했다. 캐시 탐색이 먼저다.
WO="$(mktemp -d)"
(cd "$WO" && git init -q . && python3 "$ENGINE" init >/dev/null)
check "래퍼가 캐시를 사본보다 먼저 찾는다" '^0$' \
  "$(python3 -c "
import sys
s = open(sys.argv[1], encoding='utf-8').read()
cache = s.index('plugins/cache')
copy = s.index('P=\"\$D/harness.py\"')
print(0 if cache < copy else 1)" "$WO/.claude/harness/bin/harness")"
rm -rf "$WO"

echo "== 래퍼의 고정 경로는 세션 시작마다 갱신된다 (구버전 고정이 지속되지 않는다)"
# 4차 리뷰가 "구버전 캐시가 남아 있으면 래퍼가 계속 구 엔진을 쓴다" 고 지적했다.
# 추론으로 '세션 시작이 다시 쓰니 한 세션에 한정된다' 고 넘겼는데, 추론이 이 세션에서
# 세 번 틀렸으므로 재현해 고정한다.
SW2="$(mktemp -d)"
mkdir -p "$SW2/fake/old/scripts" "$SW2/fake/new/scripts" "$SW2/w"
for V in old new; do
  cp -r "$(dirname "$ENGINE")"/. "$SW2/fake/$V/scripts/"
  cp -r "$(cd "$(dirname "$ENGINE")/../templates" && pwd)" "$SW2/fake/$V/"
  python3 -c "
import sys
p, v = sys.argv[1], sys.argv[2]
s = open(p, encoding='utf-8').read()
s = s.replace('def cli_status(ctx, argv):',
              'def cli_status(ctx, argv):\n    print(\'ENGINE=%s\' % v), ' .replace('%s', v).replace(' % v', '') + 'None', 1)
open(p, 'w', encoding='utf-8').write(s)
" "$SW2/fake/$V/scripts/parts/cli.py" "$V"
done
(cd "$SW2/w" && git init -q . && python3 "$SW2/fake/old/scripts/harness.py" init >/dev/null)
check "래퍼가 자기를 쓴 엔진을 고정한다" 'fake/old'   "$(grep -oE 'P="[^"]*harness\.py"' "$SW2/w/.claude/harness/bin/harness" | head -1)"
check "그 엔진이 실행된다" 'ENGINE=old'   "$( (cd "$SW2/w" && ./.claude/harness/bin/harness status 2>&1) )"
# 새 엔진으로 세션이 시작되면 래퍼가 다시 쓰여야 한다
printf '{"hook_event_name":"SessionStart","cwd":"%s"}' "$SW2/w"   | CLAUDE_PROJECT_DIR="$SW2/w" python3 "$SW2/fake/new/scripts/harness.py" hook >/dev/null 2>&1
check "세션 시작이 고정 경로를 갱신한다" 'fake/new'   "$(grep -oE 'P="[^"]*harness\.py"' "$SW2/w/.claude/harness/bin/harness" | head -1)"
check "이후 CLI 는 새 엔진을 쓴다" 'ENGINE=new'   "$( (cd "$SW2/w" && ./.claude/harness/bin/harness status 2>&1) )"
rm -rf "$SW2"

echo "== 회고 키 확인은 작업 전체를 읽는다 (창을 맞춘다)"
# 키는 시간창(마지막 회차 종료 이후)에서 오는데 파일은 접두사창(이번 회차)에서 왔다.
# loop adopt 가 회차만 올리므로 두 창이 갈라져 이미 적어둔 키가 '못 찾음' 이 됐다.
RK="$(mktemp -d)"
(cd "$RK" && git init -q . && python3 "$ENGINE" init >/dev/null)
rk() { (cd "$RK" && python3 "$ENGINE" "$@"); }
rk loop intent "창 맞추기" >/dev/null
rk loop done-when "끝" >/dev/null
RKL="$(rk status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["loop"])')"
mkdir -p "$RK/.dev/retrospect"
printf '# 회고\nfakerule 를 이렇게 피했다\n' > "$RK/.dev/retrospect/${RKL}-1-r.md"
python3 -c "
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1]); n = time.strftime('%Y-%m-%dT%H:%M:%S+0900')
c.execute('INSERT INTO event(loop_id,stage,kind,rule,target,at) VALUES(?,?,?,?,?,?)',
          (sys.argv[2], 'execution', 'block', 'fakerule', 'f.py', n))
c.commit()" "$RK/.claude/harness/harness.db" "$RKL"
rk loop adopt "$RKL" --reason '재연결' >/dev/null
check "재연결 후에도 1회차 회고에서 키를 찾는다" '^ok$' \
  "$(cd "$RK" && python3 -c "
import sys, os
sys.path.insert(0, os.path.join('$(cd "$(dirname "$ENGINE")" && pwd)'))
import harness as h
root = os.getcwd(); con = h.connect(root); cfg = h.load_config(root)
lid = h.head_loop(con)
# 회고가 덮어야 할 범위는 **측정 창과 다른 창**이다 — 재연결은 측정 창만 새로 연다.
keys, found, missing = h.retro_key_report(con, cfg, root, lid, h.retro_window_start(con, lid))
print('ok' if ('block:fakerule' in found or 'fakerule' in found) else 'missing=%s' % missing)")"
rm -rf "$RK"

echo "== 검사기를 검사한다: 규칙을 죽이면 자기검사가 알아채야 한다"
# 자기검사가 주 신뢰 장치가 됐으니, **탐침 누락**이 곧 조용한 구멍이다. 그것을 내
# 판단으로 지키면 또 놓친다(세 라운드가 그랬다). 규칙마다 죽여 보고 알아채는지
# 기계가 확인한다. 규칙을 새로 더할 때 탐침을 잊으면 여기서 실패한다.
#
# 탐침은 **단계를 고정한다.** 고정하지 않으면 Selection 에서 stage_write 가 거의
# 전부를 막아 다른 규칙이 죽어도 통과한다 — 막히긴 하되 다른 이유로. 직접 확인했다.
MT="$(mktemp -d)"
mtkill() { # mtkill <rule-id> ; 그 규칙만 뺀 프로젝트를 만든다
  rm -rf "$MT/w"; mkdir -p "$MT/w"
  (cd "$MT/w" && git init -q . && python3 "$ENGINE" init >/dev/null)
  [ -n "$1" ] && python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['write_rules'] = [r for r in cfg['write_rules'] if r.get('id') != sys.argv[2]]
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$MT/w/.claude/harness/stages.json" "$1"
  (cd "$MT/w" && python3 "$ENGINE" status 2>&1); }

check_selftest "정상이면 전부 통과" 0 "$(mtkill '')"
# docs_readonly 를 뺀 나머지 여섯은 죽이면 반드시 잡혀야 한다
for R in protected stage_write new_toplevel dev_subdir loop_prefix docs_naming; do
  check "$R 를 죽이면 자기검사가 잡는다" '자기검사 [0-9]*/[0-9]* 실패' "$(mtkill "$R")"
done
# docs_readonly 만 예외다 — **기본 설정에서는 진짜 중복**이기 때문이다.
# 어떤 단계도 docs 쓰기를 허용하지 않으므로 stage_write 가 먼저 막고, grant 가 있으면
# 둘 다 grant_opens 라 함께 열린다. 지워도 결과가 바뀌는 상태가 없다.
check_selftest "docs_readonly 는 기본 설정에서 중복이다" 0 \
  "$(mtkill docs_readonly)"
# 그런데 **죽은 코드는 아니다.** docs 쓰기를 허용하는 단계를 만들면 유일한 차단자가
# 된다. 그 상태를 만들어 확인한다 — 예외를 '중복이다' 한마디로 넘기지 않는다.
rm -rf "$MT/w2"; mkdir -p "$MT/w2"
(cd "$MT/w2" && git init -q . && python3 "$ENGINE" init >/dev/null)
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
for st in cfg['stages']:
    if st['id'] == 'scaffolding':
        st['write'].append('docs')
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$MT/w2/.claude/harness/stages.json"
MTW2="$(printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s/docs/x.md"}}' "$MT/w2" "$MT/w2")"
(cd "$MT/w2" && python3 "$ENGINE" loop intent x >/dev/null \
  && python3 "$ENGINE" loop done-when y >/dev/null && python3 "$ENGINE" advance >/dev/null)
check "docs 를 허용하면 docs_readonly 가 유일한 차단자다" '사람이 기록하는 영역' \
  "$(printf '%s' "$MTW2" | CLAUDE_PROJECT_DIR="$MT/w2" python3 "$ENGINE" hook)"
rm -rf "$MT"

echo "== 강제되는 것을 숫자로 보여준다 (예측이 아니라 결과)"
# 공허한 설정을 모양마다 예측해 진단하려 했더니 끝이 없었다(빈 목록, 아무것도 안 맞는
# 정규식, 이름 바꾸기, 조건 배열 비우기…). 대신 **결과를 보여준다.**
EF="$(mktemp -d)"
(cd "$EF" && git init -q . && python3 "$ENGINE" init >/dev/null)
ef() { (cd "$EF" && python3 "$ENGINE" "$@"); }
check "정상 상태를 숫자로 보여준다" "보호 경로 7 .*쓰기 규칙 $NRULES/$NRULES" "$(ef status 2>&1)"
check "게이트 있는 단계 수를 보여준다" '종료 조건 4/7' "$(ef status 2>&1)"
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
# Codex 가 진단을 빠져나간 모양들: 이름 바꾸기 + 게이트 제거
cfg['criteria']['approval'] = cfg['criteria'].pop('plan_approved')
for st in cfg['stages']:
    if st['id'] == 'planning':
        st['exit_criteria'] = ['plan_file', 'approval']
    if st['id'] == 'compounding':
        st['exit_criteria'] = []; st['stop_requires'] = []
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$EF/.claude/harness/stages.json"
check "게이트를 지우면 숫자가 줄어 보인다" '종료 조건 3/7' "$(ef status 2>&1)"
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['write_rules'] = []; cfg['consent'] = {}
cfg['folder_rules']['protected_paths'] = []
for st in cfg['stages']:
    st['exit_criteria'] = []; st['stop_requires'] = []
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$EF/.claude/harness/stages.json"
check "전부 비우면 0 으로 보인다" '쓰기 규칙 0' "$(ef status 2>&1)"
check "게이트 있는 단계도 0" '종료 조건 0/7' "$(ef status 2>&1)"
check "승인 필요도 0" '승인 필요 0' "$(ef status 2>&1)"
check "바닥값은 남아 있다 (보호 경로 3)" '보호 경로 3' "$(ef status 2>&1)"
check "--json 에도 실린다" 'enforcing' "$(ef status --json 2>&1)"

echo "  -- 요약이 거짓말하지 않는다 (있는 규칙 ≠ 발동하는 규칙)"
# 규칙을 없는 class 로 두면 일곱 규칙이 전부 죽는데 예전 요약은 "쓰기 규칙 7" 이라고
# 말했다. 요약이 거짓말하면 '예측 대신 결과를 보여준다' 가 무의미해진다. 직접 확인했다.
EF2="$(mktemp -d)"
(cd "$EF2" && git init -q . && python3 "$ENGINE" init >/dev/null)
ef2() { (cd "$EF2" && python3 "$ENGINE" "$@"); }
check_absent "정상이면 발동 가능 수를 따로 쓰지 않는다" '발동 가능' "$(ef2 status 2>&1)"
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
for r in cfg['write_rules']:
    r['when'] = {'class': 'nonexistent_class'}
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$EF2/.claude/harness/stages.json"
check "죽은 규칙은 발동 가능 0 으로 보인다" "쓰기 규칙 0/$NRULES" "$(ef2 status 2>&1)"
check "모르는 class 를 지적한다" "path_classes 에 없다" "$(ef2 status 2>&1)"
check "가능한 class 를 알려준다" 'context/dev/docs/source/tests' "$(ef2 status 2>&1)"
check_empty "실제로 아무것도 막지 못한다" \
  "$(printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s/docs/x.md"}}' "$EF2" "$EF2" \
     | CLAUDE_PROJECT_DIR="$EF2" python3 "$ENGINE" hook | grep permissionDecision || true)"
# 판정이 없는 규칙도 죽은 것이다
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['write_rules'] = json.load(open(sys.argv[2], encoding='utf-8'))['write_rules']
cfg['write_rules'][4]['require'] = {}
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$EF2/.claude/harness/stages.json" "$EF2/.claude/harness/bin/defaults.json"
check "판정이 없는 규칙도 발동 불가로 센다" "쓰기 규칙 $((NRULES - 1))/$NRULES" "$(ef2 status 2>&1)"
rm -rf "$EF2"

echo "  -- 검증 패턴은 탐침으로 본다 (텍스트를 읽지 않는다)"
python3 -c "
import json, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding='utf-8'))
cfg['criteria']['verification_evidence']['bash_pattern'] = '.*'
json.dump(cfg, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$EF/.claude/harness/stages.json"
check "검증이 아닌 명령도 인정하면 지적한다" '검증이 아닌 명령도 증거로 인정한다' \
  "$(ef status 2>&1)"
check "어떤 명령이 걸렸는지 보여준다" 'echo hi' "$(ef status 2>&1)"
rm -rf "$EF"

echo "== 재연결은 상태 전이다 (INSERT OR IGNORE 가 아니다)"
AD="$(mktemp -d)"
(cd "$AD" && git init -q . && python3 "$ENGINE" init >/dev/null)
ad() { (cd "$AD" && python3 "$ENGINE" "$@"); }
adinfo() { python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
r = c.execute('SELECT cycle, closed_at FROM loop WHERE id=?', (sys.argv[2],)).fetchone()
n = c.execute(\"SELECT COUNT(*) FROM event WHERE loop_id=? AND kind='cycle_close'\",
              (sys.argv[2],)).fetchone()[0]
a = c.execute(\"SELECT COUNT(*) FROM event WHERE loop_id=? AND kind='cycle_adopt'\",
              (sys.argv[2],)).fetchone()[0]
print('cycle=%s closed=%s measure=%d adopt=%d' % (r[0], r[1] is not None, n, a))
" "$AD/.claude/harness/harness.db" "$1"; }
# ① 없는 ID 는 새로 만드는 것이므로 회차 1 이어야 한다
check "없는 ID 는 새로 만들었다고 말한다" '새로 만들었다' \
  "$(ad loop adopt 999999-zzzzzz --reason '없는 ID' 2>&1)"
check "없는 ID 는 회차 1 에서 시작한다" 'cycle=1' "$(adinfo 999999-zzzzzz)"
# ② 연속 재연결은 회차마다 **경계**를 남겨야 한다 (측정 창이 섞이지 않게)
ad loop intent x >/dev/null; ad loop done-when y >/dev/null
ADL="$(ad status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["loop"])')"
ad loop adopt "$ADL" --reason '1차' >/dev/null
# 재연결은 접두사를 바꾸는 것이 목적이고 **측정 창은 건드리지 않는다.** 예전에는
# cycle_close 를 썼는데 그 종류는 측정 창 경계이자 회차 스냅샷이어서, 이전 회차의
# 측정치가 창에서 빠지면서 기록되지도 않았다 — 적대적 리뷰가 지적했다.
check "재연결이 회차를 올린다" 'cycle=2 closed=False' "$(adinfo "$ADL")"
check "측정 창은 건드리지 않는다" 'measure=0 adopt=1' "$(adinfo "$ADL")"
ad loop adopt "$ADL" --reason '2차' >/dev/null
check "연속 재연결도 회차만 올린다" 'cycle=3 closed=False measure=0 adopt=2' \
  "$(adinfo "$ADL")"
# ③ 닫힌 작업을 재연결하면 closed_at 이 지워져야 한다 (tidy 가 닫힌 것으로 보면 안 된다)
ad loop new --reason '새 작업' >/dev/null 2>&1 || true
check "loop new 는 이전 작업을 닫는다" 'closed=True' "$(adinfo "$ADL")"
ad loop adopt "$ADL" --reason '부활' >/dev/null
check "재연결은 다시 열린 작업으로 만든다" 'closed=False' "$(adinfo "$ADL")"
rm -rf "$AD"

echo "== 중간 그래프: 회차 한정 노드 (path add → 통과 → 회차가 닫히면 사라진다)"
# 설계 합의(.dev/plan/dynamic-middle-graph.md): 동적 그래프 = 앞쪽에 노드가 자라는
# 것. stages.json 은 안정된 틀로 남고, 추가는 CLI 로 DB 에 이번 회차에만 붙는다.
GW="$(mktemp -d)"
(cd "$GW" && git init -q . && python3 "$ENGINE" init >/dev/null)
gw() { (cd "$GW" && python3 "$ENGINE" "$@"); }
gst() { gw status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
gw loop intent "중간 그래프" >/dev/null
gw loop done-when "끝" >/dev/null
gw advance >/dev/null                                  # Scaffolding
check "추가에도 사유는 필수다 (기록이다)" '사용법' "$(gw path add research 2>&1)"
check "노드가 이번 회차에 더해진다" 'research — context 뒤' \
  "$(gw path add research --after context --reason '조사 먼저')"
check "그래프에 회차 한정으로 보인다" '회차 한정' "$(gw path)"
check "틀은 고정이다 — 첫 틀 뒤에는 못 끼운다" '첫 틀' \
  "$(gw path add x1 --after selection --reason x 2>&1)"
check "마지막 틀 뒤에도 못 끼운다" '마지막 틀' \
  "$(gw path add x2 --after compounding --reason x 2>&1)"
check "이미 있는 단계 이름은 못 쓴다" '이미 있는 단계' \
  "$(gw path add planning --reason x 2>&1)"
gw advance >/dev/null                                  # Context
check "지난 자리에는 못 붙인다 — 그래프는 앞쪽으로만 자란다" '이미 지난 자리' \
  "$(gw path add x3 --after scaffolding --reason x 2>&1)"
gw advance >/dev/null                                  # research (지연 생성된 행)
check "회차 한정 노드에 들어선다" '^research$' "$(gst stage)"
check "N/M 이 이번 회차의 그래프 크기를 센다" '4/8' "$(gst stage_label)"
gw advance >/dev/null                                  # Planning
gw path add extra --after execution --reason '실험' >/dev/null
check "미방문 노드는 지울 수 있다 (동의 뒤의 자리)" '노드 삭제' \
  "$(gw path remove extra --reason '불필요')"
check "방문한 노드는 기록이라 못 지운다" '지울 수 없다' \
  "$(gw path remove research --reason x 2>&1)"
check "설정의 단계는 CLI 로 못 지운다" '회차 한정 노드가 아니다' \
  "$(gw path remove execution --reason x 2>&1)"
GPFX="$(gst prefix)"
mkdir -p "$GW/.dev/plan" "$GW/.dev/retrospect"
printf '# 계획\n' > "$GW/.dev/plan/${GPFX}p.md"
gw skip until:compounding --reason '그래프 검사 중단' >/dev/null
printf '# 회고\n' > "$GW/.dev/retrospect/${GPFX}r.md"
gw advance --cycle >/dev/null
check "회차가 닫히면 노드가 사라진다" '^0$' "$(gw path | grep -c '회차 한정')"
check "이력은 이벤트로 남는다" 'research' "$(python3 -c "
import sqlite3,sys
rows=sqlite3.connect(sys.argv[1]).execute(
  \"SELECT rule FROM event WHERE kind='path_add' ORDER BY id\").fetchall()
print(','.join(r[0] for r in rows))" "$GW/.claude/harness/harness.db")"

echo "== 중간 그래프: next 분기와 advance --to"
# 분기 노드에서는 대상과 사유를 요구한다 — 어느 길로 갔는지도 기록이다.
python3 - "$GW/.claude/harness/stages.json" <<'PY'
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
for st in cfg["stages"]:
    if st["id"] == "planning":
        st["next"] = ["verification", "execution"]     # 기본 경로는 verification
json.dump(cfg, open(p, "w"), ensure_ascii=False)
PY
gw advance >/dev/null; gw advance >/dev/null           # 2회차 → Planning
G2PFX="$(gst prefix)"
printf '# 계획\n' > "$GW/.dev/plan/${G2PFX}p.md"
gw approve-plan ".dev/plan/${G2PFX}p.md" >/dev/null
check "분기 노드는 대상 없이 못 간다" '분기 노드다' "$(gw advance 2>&1)"
check "갈 수 없는 대상은 거부한다" '갈 수 있는 다음이 아니다' \
  "$(gw advance --to compounding --reason x 2>&1)"
check "사유 없는 분기는 거부한다" '사유가 필요하다' "$(gw advance --to execution 2>&1)"
# 스킵은 **기본 경로**를 잰다 — 경로 밖 노드는 스킵이 아니라 분기 선택의 문제다.
check "기본 경로 밖의 노드는 스킵 대상이 아니다" '기본 경로에 없다' \
  "$(gw skip execution --reason x 2>&1)"
check "분기를 고르면 간다" 'Execution' "$(gw advance --to execution --reason '코드가 있다')"
check "분기 선택이 기록된다" '^execution$' "$(python3 -c "
import sqlite3,sys
r=sqlite3.connect(sys.argv[1]).execute(
  \"SELECT target FROM event WHERE kind='branch' ORDER BY id DESC LIMIT 1\").fetchone()
print(r[0] if r else '')" "$GW/.claude/harness/harness.db")"
check "지나지 않은 갈래는 pending 으로 남는다 (스킵이 아니다)" '^pending$' \
  "$(gw status --json | python3 -c 'import json, sys
print([s["status"] for s in json.load(sys.stdin)["stages"]
       if s["id"] == "verification"][0])')"

echo "== 중간 그래프: 위상 진단 (틀 고정·전방 전용·도달성)"
python3 - "$GW/.claude/harness/stages.json" <<'PY'
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
for st in cfg["stages"]:
    st.pop("next", None)
    if st["id"] == "execution":
        st["next"] = ["context"]                       # 백엣지
    if st["id"] == "selection":
        st["next"] = ["planning"]                      # 틀 노드 선언
    if st["id"] == "planning":
        st["next"] = ["nowhere"]                       # 없는 대상
    if st["id"] == "context":
        st["next"] = ["planning"]                      # verification 도달 불가...
json.dump(cfg, open(p, "w"), ensure_ascii=False)
PY
GDIAG="$(gw status 2>&1)"
check "틀 노드의 next 선언을 말한다" '틀 노드라 next 를 선언할 수 없다' "$GDIAG"
check "백엣지를 말한다" '뒤로 가는 엣지' "$GDIAG"
check "없는 대상을 말한다" '없는 단계다' "$GDIAG"
python3 - "$GW/.claude/harness/stages.json" <<'PY'
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
for st in cfg["stages"]:
    st.pop("next", None)
    if st["id"] == "context":
        st["next"] = ["execution"]                     # planning 으로 가는 길이 없다
json.dump(cfg, open(p, "w"), ensure_ascii=False)
PY
check "도달하지 않는 노드를 말한다" '결코 방문되지 않는다' "$(gw status 2>&1)"
python3 - "$GW/.claude/harness/stages.json" <<'PY'
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
for st in cfg["stages"]:
    st.pop("next", None)
json.dump(cfg, open(p, "w"), ensure_ascii=False)
PY
check "선언을 지우면 깨끗하다 (기존 설정 무변경 호환)" '^0$' \
  "$(gw status 2>&1 | grep -c '무시되는 설정')"
rm -rf "$GW"

echo "== 중간 그래프: 적대적 리뷰가 찾은 우회들 (1회차 수정 회귀)"
# 세 리뷰어(교차 모델·위상·게이트 우회)가 찾은 것들을 테스트로 못 박는다.
RG="$(mktemp -d)"
(cd "$RG" && git init -q . && python3 "$ENGINE" init >/dev/null)
rg() { (cd "$RG" && python3 "$ENGINE" "$@"); }
rgst() { rg status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

# --- 위상: 게이트를 분기로 우회하는 그래프는 진단이 잡는다 (Q1 위상 검증)
python3 - "$RG/.claude/harness/stages.json" <<'PY'
import json, sys
p = sys.argv[1]; c = json.load(open(p))
for s in c["stages"]:
    if s["id"] == "execution":
        s["next"] = ["verification", "compounding"]   # verification 우회 가능
json.dump(c, open(p, "w"), ensure_ascii=False)
PY
check "게이트를 안 지나는 종료 경로를 진단한다" '분기로 우회할 수 있다' "$(rg status 2>&1)"
python3 - "$RG/.claude/harness/stages.json" <<'PY'
import json, sys
p = sys.argv[1]; c = json.load(open(p))
for s in c["stages"]:
    s.pop("next", None)
json.dump(c, open(p, "w"), ensure_ascii=False)
PY

rg loop intent "우회 회귀" >/dev/null
rg loop done-when "끝" >/dev/null
rg advance >/dev/null                                   # Scaffolding

# --- Q2: path add --write 로 앵커 권한을 넘는 쓰기 자가부여는 거부
check "회차 한정 노드가 앵커 권한을 넘는 쓰기를 못 한다" '넘을 수 없다' \
  "$(rg path add ctxnode --after execution --write context --reason x 2>&1)"
check "앵커 권한 안의 쓰기는 허용된다" '노드 추가' \
  "$(rg path add build --after execution --write source --reason x 2>&1)"

# --- W1: 분기 노드를 지나는 skip 은 거부 (분기는 advance --to 의 일)
python3 - "$RG/.claude/harness/stages.json" <<'PY'
import json, sys
p = sys.argv[1]; c = json.load(open(p))
for s in c["stages"]:
    if s["id"] == "context":
        s["next"] = ["planning", "execution"]
json.dump(c, open(p, "w"), ensure_ascii=False)
PY
rg advance >/dev/null                                   # Context (분기)
check "분기 노드를 지나는 skip 은 거부한다" '분기 노드다' \
  "$(rg skip until:execution --reason x 2>&1)"
check "분기는 advance --to 로 고른다" 'Planning' \
  "$(rg advance --to planning --reason '코드 전 계획' 2>&1)"

# --- Q1: 계획 승인 뒤에는 그래프를 못 바꾼다 (계획 승인 = 그래프 승인)
RGPFX="$(rgst prefix)"
mkdir -p "$RG/.dev/plan"; printf '# 계획\n' > "$RG/.dev/plan/${RGPFX}p.md"
rg approve-plan ".dev/plan/${RGPFX}p.md" >/dev/null
check "계획 승인 뒤 path add 는 거부된다" '계획 승인이 곧 그래프 승인' \
  "$(rg path add late --after execution --reason x 2>&1)"
check "계획 승인 뒤 path remove 도 거부된다" '계획 승인이 곧 그래프 승인' \
  "$(rg path remove build --reason x 2>&1)"

# --- C1: remove_path_row 는 설정 단계·방문 노드를 원자적으로 거른다
# (관문이 조건부 DELETE 라, path_remove_block_reason 통과와 삭제 사이에 advance 가
#  끼어드는 경쟁에서도 active 노드를 지우지 않는다 — Codex CRITICAL)
check "설정 단계는 remove 관문을 통과 못한다 (원자적 거름)" '^False$' \
  "$(python3 -c "
import sys
sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1])
lid = h.head_loop(con)
print(h.remove_path_row(con, lid, h.cycle_of(con, lid), 'execution'))
" "$RG" "$(dirname "$ENGINE")")"
rm -rf "$RG"

echo "== 중간 그래프: 리뷰 노드 프리셋 (결과를 남겨야 통과)"
# 외부 모델(코덱스 등) 결과는 남기지 않으면 휘발된다 — review 프리셋은 결과
# 파일 없이는 노드를 못 끝내게 한다(Verification 이 검증 증거를 요구하는 것과 같은 결).
RP="$(mktemp -d)"
(cd "$RP" && git init -q . && python3 "$ENGINE" init >/dev/null)
rp() { (cd "$RP" && python3 "$ENGINE" "$@"); }
rpst() { rp status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
rpmiss() { rp status --json | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)["exit_missing"]))'; }
rp loop intent "리뷰 프리셋" >/dev/null
rp loop done-when "끝" >/dev/null
rp advance >/dev/null                                   # Scaffolding
check "review 프리셋이 종료 조건을 붙인다" 'review_recorded' \
  "$(rp path add review --after execution --reason '적대적 리뷰')"
rp advance >/dev/null; rp advance >/dev/null            # Context, Planning
RPFX="$(rpst prefix)"
mkdir -p "$RP/.dev/plan"; printf '#\n' > "$RP/.dev/plan/${RPFX}p.md"
rp approve-plan ".dev/plan/${RPFX}p.md" >/dev/null
rp advance >/dev/null; rp advance >/dev/null            # Execution → review
check "리뷰 노드에 들어섰다" '^review$' "$(rpst stage)"
check "결과 없이는 advance 를 거부한다" 'review_recorded' "$(rp advance 2>&1)"
# 파일 존재만으로 충족된다 — 관측(PostToolUse) 없이도. 훅 없는 도구·외부 모델 대응.
mkdir -p "$RP/.dev/review"; printf '# 코덱스 리뷰\n- CRITICAL: ...\n' > "$RP/.dev/review/${RPFX}codex.md"
check_empty "결과 파일이 있으면 종료 조건이 충족된다 (관측 없이)" "$(rpmiss)"
check "결과를 남기면 advance 가 통과한다" 'Verification' "$(rp advance 2>&1)"
# 프리셋 오타는 진단이 잡는다 (조용히 게이트 없는 노드가 되지 않게)
python3 - "$RP/.claude/harness/stages.json" <<'PY'
import json, sys
p = sys.argv[1]; c = json.load(open(p))
c.setdefault("node_presets", {}).setdefault("review", {})["exit_criteria"] = ["nonexistent_crit"]
json.dump(c, open(p, "w"), ensure_ascii=False)
PY
check "프리셋의 없는 종료 조건을 진단한다" 'criteria 에 없다' "$(rp status 2>&1)"
rm -rf "$RP"

# ---------------------------------------------------------------------------
# 현장 보고 (tech-writer, 0.69.0)
#
# **일하는 명령**을 훅에 먹인다. 여기까지의 코퍼스는 전부 "어떻게 뚫나" 에서
# 나왔고, 그래서 잘 만들어진 공격 문자열뿐이었다. 조사할 때 쓰는 평범한 관용구는
# 하나도 없었다 — `find … \;` 는 스모크에 두 번 나오는데 훅에 먹인 것은 하필
# `+` 변종이었고, 버그가 있는 갈래만 정확히 비껴갔다.
#
# 명령은 보고서에 적힌 **원문 그대로** 둔다. 다듬으면 그 사람이 실제로 친 것이
# 아니게 되고, 다시 겪어야 알게 된다.
# ---------------------------------------------------------------------------
echo "== 현장 보고 — 일하는 명령이 과잉 차단되지 않는다"
FR="$(mktemp -d)"
(cd "$FR" && git init -q . && python3 "$ENGINE" init >/dev/null)
mkdir -p "$FR/src" "$FR/.dev/plan" && touch "$FR/src/a.py"

# 따옴표·백슬래시를 파이썬이 JSON 으로 감싼다 — 셸에서 두 번 이스케이프하면
# 시험하려는 문자열 자체가 달라진다(그 실수가 이 버그를 숨긴 이유이기도 하다).
frhook() { # frhook <도구> <키> <값>
  FRD="$FR" python3 -c '
import json, os, sys
print(json.dumps({"hook_event_name": "PreToolUse", "cwd": os.environ["FRD"],
                  "tool_name": sys.argv[1],
                  "tool_input": {sys.argv[2]: sys.argv[3]}}))' "$1" "$2" "$3" \
  | CLAUDE_PROJECT_DIR="$FR" python3 "$ENGINE" hook
}
frb() { frhook Bash command "$1"; }

echo "  -- §3·§4 저장소 밖을 읽는 명령이 '리포 경로를 바꾼다' 가 되지 않는다"
# `BASH_SPLIT` 이 `;` 로 먼저 쪼개면 세그먼트 끝에 백슬래시가 홀로 남고, shlex 가
# 거기서 죽으면 옛 폴백이 `"$HOME"` 을 `_` 로 뭉갰다. 그 `_/…` 가 저장소 상대
# 경로로 판정돼 단계 규칙이 걸렸다 — 차단보다 **사유가 틀린 것**이 비쌌다.
FR1="$(frb 'find "$HOME"/.claude/plugins/marketplaces -maxdepth 1 -name "cc-marketplace" -exec echo {} \;')"
check_absent "지어낸 '_/' 경로가 나오지 않는다" '_/' "$FR1"
check_absent "저장소 밖 읽기를 deny 하지 않는다" '"deny"' "$FR1"
check "모르면 묻는다 — 사유는 find 다" 'find 가' "$FR1"
FR2="$(frb 'find "$HOME"/.claude -name "*.json" -exec grep -l x {} \;')"
check_absent "-exec grep 도 지어내지 않는다" '_/' "$FR2"
check_absent "-exec grep 을 deny 하지 않는다" '"deny"' "$FR2"
# 세그먼트 끝 백슬래시는 `\;` 만의 일이 아니다 — 줄 이어붙임도 같은 모양이다.
check_absent "줄 이어붙임도 지어내지 않는다" '_/' \
  "$(frb 'cp "$HOME/a.txt" /tmp/b.txt \
  && echo done')"

echo "  -- §4 git 리비전 범위는 경로가 아니다"
check_empty "A..B 는 쓰기 대상이 아니다" \
  "$(frb 'git diff origin/main..HEAD > /tmp/x.diff')"
check_empty "A...B 도 마찬가지" \
  "$(frb 'git log origin/main...HEAD --oneline > /tmp/y.txt')"
# 상위 폴더(`..`)는 진짜 경로다 — 문법으로 가르므로 이 구분이 유지돼야 한다.
check_empty "상위 폴더가 있어도 막지는 않는다" "$(frb 'touch ../a.py; touch src/a.py')"

echo "  -- §3·§4 회귀: 해석하지 못하는 것은 여전히 묻는다"
check "따옴표가 안 맞는 변경 명령은 묻는다" '쪼갤 수 없다' "$(frb 'touch "src/a.py')"
check "셸 확장이 섞이면 묻는다" '실행 시점' "$(frb 'mkdir "$HOME/.cache/x"')"
# 인라인 코드는 **우리 영역을 언급할 때만** 묻는다. 그냥 인라인이라는 이유로
# 물으면 `print(1+1)` 까지 사람을 세우고, auto mode 를 켠 사용자도 매번 멈춘다.
check "인라인이 바닥값을 조립하면 묻는다" '읽지 못한다' \
  "$(frb 'python3 -c "open(\".claude/harn\"+\"ess/x\",\"w\")"')"
check_empty "평범한 인라인 스크립트는 묻지 않는다" \
  "$(frb 'python3 -c "print(1+1)"')"
check_empty "인라인으로 소스를 써도 묻지 않는다 (단계 규칙은 기록이다)" \
  "$(frb 'python3 -c "open(\"src/a.py\",\"w\")"')"
check "find -exec rm 은 바닥값에서 막힌다" '하네스 자신' \
  "$(frb "find .claude/harness -name '*.py' -exec rm {} +")"

echo "  -- §5 하네스 자신을 **읽는 것**은 막지 않는다 (보고서가 지키라고 한 쪽)"
check_empty "ls -la 는 통과한다" "$(frb 'ls -la .claude/harness/')"
check_empty "stat 도 통과한다" "$(frb 'stat .claude/harness/harness.db')"
# 쪼개서 읽는 것은 '통과'가 아니라 **ask** 였다. 하네스는 잡았고, 실제로 통과한
# 것은 그 환경의 권한 모드였다 — 계약을 여기 못 박아 둔다.
check "쪼갠 경로로 읽어도 묻기는 한다" '"ask"' \
  "$(frb 'python3 -c "import json; d=json.load(open(\".claude/harn\"+\"ess/stages.json\")); print(1)"')"

echo "  -- §2 남의 작업 기록과 새 파일은 **다른 말**을 듣는다"
printf 'old\n' > "$FR/.dev/plan/260101-0888de-1-other-task-note.md"
FRE="$(frhook Edit file_path '.dev/plan/260101-0888de-1-other-task-note.md')"
check "다른 작업의 기록이라고 말한다" '다른 작업(260101-0888de)' "$FRE"
check "새 파일로 쓰라고 안내한다" '새 파일' "$FRE"
check_absent "접두사를 덧붙인 이름을 제안하지 않는다" '1-260101-0888de-1-' "$FRE"
FRN="$(frhook Write file_path '.dev/plan/014-new-thing.md')"
check "새 파일에는 이름만 고치라고 한다" '대신' "$FRN"
check_absent "새 파일을 남의 기록으로 오해하지 않는다" '다른 작업' "$FRN"

echo "  -- §1 열지 못하는 예외는 등록되지 않는다"
FRA="$( (cd "$FR" && python3 "$ENGINE" allow '.dev/plan/260101-0888de-1-other-task-note.md' \
        --reason '앞 작업 기록 정정' --uses 1 2>&1) || true )"
check "거짓 성공 대신 거절한다" '열지 못한다' "$FRA"
check_absent "성공했다고 말하지 않는다" '예외 등록' "$FRA"
check "소모되지 않은 예외가 남지 않는다" '^0$' \
  "$(python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(len(list(c.execute('select 1 from wgrant'))))" "$FR/.claude/harness/harness.db")"

echo "  -- §6 비표준 러너는 정규식을 넓히지 않고 선언한다"
# 이 저장소의 러너는 직접 만든 셸 스크립트다. 표준 러너 목록과 겹치는 것이 하나도
# 없어서 무엇을 돌려도 증거가 안 잡혔고, help 가 안내하는 탈출구(`verify --`)마저
# **같은 정규식으로 다시** 걸러서 실제로는 존재하지 않는 탈출구였다. 그래서 막힌
# 에이전트가 한 일은 하나뿐이었다 — 거절 기준을 넓혀서 통과했다.
V6="$(mktemp -d)"
mkdir -p "$V6/tests"
(cd "$V6" && git init -q . && python3 "$ENGINE" init >/dev/null)
printf '#!/bin/sh\necho green\nexit 0\n' > "$V6/tests/run.sh"
v6() { (cd "$V6" && python3 "$ENGINE" "$@" 2>&1) || true; }
v6set() { # v6set <json-경로> <값(JSON)>
  python3 -c "
import json, sys
p = sys.argv[1] + '/.claude/harness/stages.json'
c = json.load(open(p, encoding='utf-8'))
c['criteria']['verification_evidence'][sys.argv[2]] = json.loads(sys.argv[3])
json.dump(c, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)" "$V6" "$1" "$2"
}
# Execution 까지 올린다 — verify 는 거기서만 쓴다.
v6 loop intent "러너 선언" >/dev/null
v6 loop done-when "verify 통과" >/dev/null
v6 advance >/dev/null
V6PFX="$( (cd "$V6" && python3 "$ENGINE" status --json) | jq1 prefix | tr -d '"')"
mkdir -p "$V6/.dev/plan" && printf '# 계획\n' > "$V6/.dev/plan/${V6PFX}plan.md"
v6 skip until:execution --reason '시험' >/dev/null
check "Execution 까지 왔다" '^"execution"$' "$( (cd "$V6" && python3 "$ENGINE" status --json) | jq1 stage)"

V6A="$(v6 verify -- bash tests/run.sh)"
check "선언 전에는 거부한다" '검증 명령으로 보이지 않는다' "$V6A"
# **막으면서 갈 곳을 준다.** 이 한 줄이 없어서 남은 길이 정규식뿐이었다.
check "정규식 말고 commands 로 가라고 말한다" 'commands' "$V6A"
check "정규식을 넓히지 말라고 못 박는다" '넓히지 말고' "$V6A"

v6set commands '["bash tests/run.sh"]'
V6B="$(v6 verify -- bash tests/run.sh)"
check "선언하면 하네스가 직접 돌린다" 'green' "$V6B"
check "통과하면 증거가 된다" '증거로 기록했다' "$V6B"
check "정규식은 그대로다" '^0$' \
  "$(python3 -c "
import json, sys
a = json.load(open(sys.argv[1] + '/.claude/harness/stages.json', encoding='utf-8'))
b = json.load(open(sys.argv[1] + '/.claude/harness/bin/defaults.json', encoding='utf-8'))
k = 'bash_pattern'
print(0 if a['criteria']['verification_evidence'][k]
      == b['criteria']['verification_evidence'][k] else 1)" "$V6")"
# 목록이라 넓힐 여지가 없다 — 그 성질을 단정으로 남긴다.
check "선언은 접두 위장을 인정하지 않는다" '검증 명령으로 보이지 않는다' \
  "$(v6 verify -- bash tests/run.sh-evil)"
check "선언은 다른 명령을 인정하지 않는다" '검증 명령으로 보이지 않는다' \
  "$(v6 verify -- rm -rf /tmp/x)"
check "선언과 무관한 명령은 인정하지 않는다" '검증 명령으로 보이지 않는다' \
  "$(v6 verify -- echo hi)"
# 선언을 더했다고 원래 알던 것을 잃지 않는다. (pytest 가 없는 환경이면 실행에서
# 실패하는데, 그건 **판정을 지났다는 증거**다 — 판정이 막았으면 여기 못 온다.)
check_absent "선언이 있어도 표준 러너는 그대로 인정된다" '검증 명령으로 보이지 않는다' \
  "$(v6 verify -- pytest tests/)"
check "metrics 가 무엇을 인정하는지 보여준다" 'bash tests/run.sh' "$(v6 metrics)"

echo "  -- §6 정규식을 넓히면 그 사실이 남는다 (잠그지 않는다 — 보이게 한다)"
v6set commands '[]'
python3 -c "
import json, sys
p = sys.argv[1] + '/.claude/harness/stages.json'
c = json.load(open(p, encoding='utf-8'))
ve = c['criteria']['verification_evidence']
ve['bash_pattern'] = ve['bash_pattern'] + r'|(bash\s+)?(\./)?tests/run\.sh\b'
json.dump(c, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)" "$V6"
check "넓힌 정규식은 실제로 통과시킨다 (사실은 사실대로)" '증거로 기록했다' \
  "$(v6 verify -- bash tests/run.sh)"
V6C="$(v6 status)"
check "status 가 기준이 달라졌다고 말한다" 'bash_pattern 이 기본값과 다르다' "$V6C"
check "기본값을 품은 채 늘었다는 것까지 말한다" '품은 채 늘었다' "$V6C"
check "metrics 도 같은 사실을 말한다" '기본값에서 달라졌다' "$(v6 metrics)"
# 막지는 않는다 — 잠그기로 한 것이 아니다.
check_absent "넓혔다고 차단하지는 않는다" 'Refuse' "$V6C"
rm -rf "$V6"

echo "  -- 3차 §2 다른 언어의 소스를 품은 명령 (본문은 명령이 아니라 데이터다)"
# 시험대가 **한 줄 명령 위주**였다. 실사용의 큰 몫은 본문을 품은 명령인데
# (`python3 - <<'PY'`, `bash -c '…'`, `git commit -F -`) 코퍼스에 거의 없었다.
# 그래서 본문이 셸 명령처럼 토큰화되는 것을 아무도 못 봤다 — `find … \;` 의
# `\;` 변종을 놓쳤던 것과 같은 자리다: 명령의 **핵심부만** 흉내 내고 **껍데기**는
# 흉내 내지 않았다.
frheredoc() { # frheredoc <본문 한 줄> → 훅 판정
  frb "$(printf 'python3 - <<%sPY%s\n%s\nprint(1)\nPY' "'" "'" "$1")"
}
# ① 오탐 — 본문에 든 **문자열**이 명령으로 읽히면 안 된다
check_absent "본문의 문자열 + 2>&1 은 막히지 않는다" '"deny"' \
  "$(frheredoc "needle = 'ls -la .claude/harness/ 2>&1'")"
check_empty "본문의 문자열 + 2>&1 은 묻지도 않는다" \
  "$(frheredoc "needle = 'ls -la .claude/harness/ 2>&1'")"
check_empty "본문의 문자열 + 리다이렉트도 통과" \
  "$(frheredoc "needle = 'ls .claude/harness/ > /tmp/o'")"
check_empty "리다이렉트 없는 언급도 통과" \
  "$(frheredoc "needle = 'ls -la .claude/harness/'")"
# ② 경계 — 본문이 **진짜로** 바닥값을 건드리면 그대로 받는다.
#    이쪽이 빨개지지 않으면 위 셋은 "게이트를 껐다" 와 구분되지 않는다.
check "본문이 DB 를 열면 묻는다" '"ask"' \
  "$(frheredoc "open('.claude/harness/harness.db','w')")"
check "본문이 래퍼를 열면 묻는다" '"ask"' \
  "$(frheredoc "open('.claude/harness/bin/harness','w')")"
check "따옴표 없는 히어독도 받는다" '"ask"' \
  "$(frb "$(printf 'python3 - <<PY\nopen(%s.claude/harness/harness.db%s,%sw%s)\nPY' "'" "'" "'" "'")")"
# 히어독으로 **파일을 쓰는** 것은 본문이 아니라 리다이렉트가 말한다 — 그대로 막힌다.
check "히어독으로 래퍼를 덮어쓰면 막힌다" '"deny"' \
  "$(frb "$(printf 'cat > .claude/harness/bin/harness <<%sEOF%s\nevil\nEOF' "'" "'")")"
# ③ 껍데기마다 계약이 다르다 — 그 차이를 못 박는다.
#    히어독 본문은 셸이 확장도 하지 않는 **데이터**라 안 본다(통과).
#    `-c` 는 **코드**라 읽을 수 없으므로 묻는다(ask). 둘 다 deny 는 아니다.
BC="$(frb "bash -c \"echo 'ls .claude/harness/ 2>&1'\"")"
check "bash -c 는 코드라 묻는다" '"ask"' "$BC"
check_absent "그래도 막지는 않는다" '"deny"' "$BC"

echo "  -- 4차 §1 회차 도중 stages.json 에 끼운 단계 (세 자리가 같은 원인을 말한다)"
# 단계 행은 **회차가 열릴 때** 만들어진다. 회차 도중에 설정에 단계를 끼우면
# 설정 8 · DB 7 이 되고, 예전에는 세 명령이 각자 다른 거짓말을 했다:
#   path       → 8노드를 그냥 보여준다 (갈 수 있는 것처럼)
#   path add   → "이미 있는 단계다 — 다른 이름을 써라" (이름 문제가 아니다)
#   advance    → "다음 단계 행이 없다" (무엇을 하라는 말이 없다)
LT="$(mktemp -d)"
(cd "$LT" && git init -q . && python3 "$ENGINE" init >/dev/null)
lt() { (cd "$LT" && python3 "$ENGINE" "$@" 2>&1) || true; }
lt loop intent x >/dev/null; lt loop done-when y >/dev/null; lt advance >/dev/null
python3 -c "
import json, sys
p = sys.argv[1] + '/.claude/harness/stages.json'
c = json.load(open(p, encoding='utf-8'))
i = [s['id'] for s in c['stages']].index('verification')
c['stages'].insert(i, {'id':'review','label':'Review','summary':'적대적 리뷰',
                       'write':['dev'],'exit_criteria':[],'stop_requires':[]})
json.dump(c, open(p,'w',encoding='utf-8'), ensure_ascii=False, indent=2)" "$LT"
LTP="$(lt path)"
check "path 가 못 가는 노드를 구분해 보여준다" '이번 회차 그래프에 없다' "$LTP"
check "왜 그런지와 언제 되는지를 말한다" '다음 회차부터' "$LTP"
LTA="$(lt path add review --after execution --reason x)"
check_absent "이름을 바꾸라는 거짓 안내를 하지 않는다" '다른 이름을 써라' "$LTA"
check "설정에는 있지만 회차에는 없다고 말한다" '이번 회차의 그래프에는 없다' "$LTA"
# advance 로 그 자리까지 밀고 가서 같은 원인을 같은 말로 받는지 본다
LTPFX="$( (cd "$LT" && python3 "$ENGINE" status --json) | jq1 prefix | tr -d '"')"
mkdir -p "$LT/.dev/plan" && printf '# p\n' > "$LT/.dev/plan/${LTPFX}p.md"
lt advance >/dev/null; lt advance >/dev/null
lt approve-plan ".dev/plan/${LTPFX}p.md" >/dev/null; lt advance >/dev/null
LTADV="$(lt advance)"
check "advance 도 같은 원인을 말한다" '이번 회차의 그래프에는 없는' "$LTADV"
check "advance 가 두 갈래를 제시한다" 'advance --cycle' "$LTADV"
check_absent "원시 traceback 을 내지 않는다" 'Traceback' "$LTADV"
rm -rf "$LT"

echo "  -- 4차 §2 채울 수 없는 종료 조건을 진단한다 (예전에는 완전히 조용했다)"
DM="$(mktemp -d)"
(cd "$DM" && git init -q . && python3 "$ENGINE" init >/dev/null)
python3 -c "
import json, sys
p = sys.argv[1] + '/.claude/harness/stages.json'
c = json.load(open(p, encoding='utf-8'))
c['folder_rules']['dev_subdirs'] = [x for x in c['folder_rules']['dev_subdirs'] if x != 'review']
json.dump(c, open(p,'w',encoding='utf-8'), ensure_ascii=False, indent=2)" "$DM"
DMS="$( (cd "$DM" && python3 "$ENGINE" status 2>&1) )"
check "채울 수 없는 조건을 잡는다" '이 조건은 채울 수 없다' "$DMS"
check "어느 폴더가 없는지 말한다" "dev_subdirs 에 'review' 가 없다" "$DMS"
check "고칠 방법을 준다" 'dev_subdirs 에 넣거나' "$DMS"
rm -rf "$DM"
# 오진 없음 — 갓 설치한 저장소는 조용해야 한다
DM2="$(mktemp -d)"
(cd "$DM2" && git init -q . && python3 "$ENGINE" init >/dev/null)
check_absent "정상 설치에는 오진하지 않는다" '채울 수 없다' "$( (cd "$DM2" && python3 "$ENGINE" status 2>&1) )"
# **덜어낸 것은 문제 삼지 않는다.** 조건 자체를 지우는 것은 마찰을 줄이는 정상
# 행위다 — 템플릿과 대조하면 그 자유를 빼앗는다. 모순만 본다.
DM3="$(mktemp -d)"
(cd "$DM3" && git init -q . && python3 "$ENGINE" init >/dev/null)
python3 -c "
import json, sys
p = sys.argv[1] + '/.claude/harness/stages.json'
c = json.load(open(p, encoding='utf-8'))
del c['criteria']['review_recorded']
c['folder_rules']['dev_subdirs'] = [x for x in c['folder_rules']['dev_subdirs'] if x != 'review']
json.dump(c, open(p,'w',encoding='utf-8'), ensure_ascii=False, indent=2)" "$DM3"
check_absent "조건째로 덜어냈으면 조용하다" '채울 수 없다' \
  "$( (cd "$DM3" && python3 "$ENGINE" status 2>&1) )"
rm -rf "$DM3"
rm -rf "$DM2"

echo "  -- 4차 doctor · 세 번째 문"
DR="$(mktemp -d)"
(cd "$DR" && git init -q . && mkdir -p src && python3 "$ENGINE" init >/dev/null)
check "doctor 가 status 와 같은 일을 한다" '자기검사' "$( (cd "$DR" && python3 "$ENGINE" doctor 2>&1) )"
drw() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"src/a.py"}}' "$DR" \
  | CLAUDE_PROJECT_DIR="$DR" python3 "$ENGINE" hook; }
check "차단 메시지가 세 번째 문을 안내한다" 'path add' "$(drw)"
# **안 되는 때는 말하지 않는다.** 계획 승인 뒤에는 그래프가 고정되므로
# `path add` 를 권하면 "하라고 해서 했는데 거부당하는" 새 마찰이 된다.
(cd "$DR" && python3 "$ENGINE" loop intent x >/dev/null 2>&1
 python3 "$ENGINE" loop done-when y >/dev/null 2>&1
 python3 "$ENGINE" advance >/dev/null 2>&1; python3 "$ENGINE" advance >/dev/null 2>&1
 python3 "$ENGINE" advance >/dev/null 2>&1)
DRPFX="$( (cd "$DR" && python3 "$ENGINE" status --json) | jq1 prefix | tr -d '"')"
mkdir -p "$DR/.dev/plan" && printf '# p\n' > "$DR/.dev/plan/${DRPFX}p.md"
(cd "$DR" && python3 "$ENGINE" approve-plan ".dev/plan/${DRPFX}p.md" >/dev/null 2>&1)
DRL="$(drw)"
check_absent "승인 뒤에는 path add 를 권하지 않는다" 'path add' "$DRL"
check "대신 다음 회차를 가리킨다" 'advance --cycle' "$DRL"
rm -rf "$DR"

echo "  -- §7 이미 무시되는 경로에 다시 붙이지 않는다"
FG="$(mktemp -d)"
(cd "$FG" && git init -q .)
# 현장과 같은 모양 — 폴더를 통째로 막고 규칙 파일만 `!` 로 되살린다.
printf '!.claude/harness/\n.claude/harness/*\n!.claude/harness/POLICY.md\n' > "$FG/.gitignore"
(cd "$FG" && python3 "$ENGINE" init >/dev/null 2>&1)
check "git 이 이미 무시하면 한 줄도 안 붙는다" '^3$' "$(wc -l < "$FG/.gitignore" | tr -d ' ')"
(cd "$FG" && python3 "$ENGINE" init >/dev/null 2>&1)
check "두 번째 init 도 조용하다" '^3$' "$(wc -l < "$FG/.gitignore" | tr -d ' ')"
rm -rf "$FG"
# 회귀: 무시 규칙이 없는 저장소에는 여전히 붙는다
FG2="$(mktemp -d)"
(cd "$FG2" && git init -q . && python3 "$ENGINE" init >/dev/null 2>&1)
check "규칙이 없으면 붙인다" '[1-9]' "$(grep -c 'harness.db' "$FG2/.gitignore")"
rm -rf "$FG2" "$FR"

echo "== 손상 내성 (fail-open 은 종료 코드까지 포함한다)"
echo 'not a database' > "$WORK/.claude/harness/harness.db"
rm -f "$WORK/.claude/harness/harness.db-wal" "$WORK/.claude/harness/harness.db-shm"
# 예전 계약은 "출력 없음" 이었다. 그건 **게이트가 꺼진 사실의 은폐**였다 —
# 세션 중간에 DB가 깨지면 남은 세션 전체가 게이트 없이 돌면서 아무 말이 없었다.
# 새 계약: 판정은 하지 않되(차단하지 않음) 꺼진 사실은 말한다.
DBROKEN="$(hook "$(W docs/x.md)" 2>/dev/null)"
check_absent "DB 손상 시 차단하지 않음" 'permissionDecision' "$DBROKEN"
check "DB 손상을 사용자에게 알린다" '게이트가 꺼졌다' "$DBROKEN"
check "무엇이 문제인지 말한다" 'file is not a database' "$DBROKEN"
CRC=0; hook "$(W docs/x.md)" >/dev/null 2>&1 || CRC=$?
check "DB 손상 시 종료 코드 0" '^0$' "$CRC"
check "traceback 을 내지 않는다" '^0$' \
  "$(hook "$(W docs/x.md)" 2>&1 >/dev/null | grep -c Traceback)"
check "무슨 일인지 stderr 로 알린다" 'step-seven-harness' \
  "$(hook "$(W docs/x.md)" 2>&1 >/dev/null)"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
# 개수 바닥. 절을 통째로 `if false` 로 감싸도 초록이었다(5회차 D11).
if [ "$PASS" -lt 700 ]; then
  echo "체크가 $PASS 개뿐이다 (최소 700) — 절이 통째로 꺼졌다" >&2
  exit 1
fi
[ "$FAIL" -eq 0 ]
