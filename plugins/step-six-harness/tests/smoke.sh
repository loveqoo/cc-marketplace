#!/bin/sh
# step-six-harness 스모크 테스트.
#   usage: sh plugins/step-six-harness/tests/smoke.sh
# 임시 프로젝트를 만들고 훅 이벤트 JSON 을 엔진에 먹여 판정을 검증한다.
set -e

ENGINE="$(cd "$(dirname "$0")/.." && pwd)/scripts/harness.py"
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

sql() { python3 - "$WORK/.claude/harness/harness.db" "$1" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
for row in con.execute(sys.argv[2]):
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
check "래퍼 생성" 'harness' "$(ls "$WORK/.claude/harness/bin/")"
check "6단계 모두 등록" '^6$' "$(sql 'SELECT COUNT(*) FROM stage')"

echo "== 폴더 가드"
check "docs/ 쓰기 차단" '"permissionDecision": "deny"' "$(hook "$(W docs/spec/001-a.md)")"
check_empty "Scaffolding 에서 신규 최상위 폴더 허용" "$(hook "$(W src/a.py)")"
check ".dev 하위 폴더 규칙 위반 차단" '규칙 위반' "$(hook "$(W .dev/nope/a.md)")"

echo "== 루프 해시 파일명 강제"
check "해시 접두사 없으면 차단" '루프 해시로 시작' "$(hook "$(W .dev/plan/my-plan.md)")"
check "차단 시 올바른 이름 제시" "$LID-my-plan.md" "$(hook "$(W .dev/plan/my-plan.md)")"
check_empty "해시 접두사 있으면 허용" "$(hook "$(W ".dev/plan/$LID-my-plan.md")")"
check_empty "scratch 는 해시 강제 안 함" "$(hook "$(W .dev/scratch/tmp.txt)")"

echo "== 단계 게이트"
setstage planning
check "Planning 에서 소스 쓰기 차단" 'Planning' "$(hook "$(W src/a.py)")"
check_empty "Planning 에서 .dev 쓰기 허용" "$(hook "$(W ".dev/plan/$LID-b.md")")"
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
check "bypassPermissions 에서는 deny" '"permissionDecision": "deny"' \
  "$(hook "$(B '.claude/harness/bin/harness skip verification --reason \"x\"' bypassPermissions)")"
check_empty "status 는 통과" "$(hook "$(B '.claude/harness/bin/harness status' default)")"

echo "== 결함 A 회귀: 완료한 단계를 스킵으로 기록하지 않는다"
SKIPOUT="$(cli skip verification --reason '문서 작업이라 검증 불필요')"
check "Execution 은 done" '^done$' "$(sql "SELECT status FROM stage WHERE stage='execution'")"
check "Verification 은 skipped" '^skipped$' "$(sql "SELECT status FROM stage WHERE stage='verification'")"
check "스킵 사유가 잘리지 않는다" '문서 작업이라 검증 불필요' "$(cli status)"
check "Compounding 진입 시 스킵을 회고용으로 노출" '회고에 사유와 함께 기록' "$SKIPOUT"

echo "== 결함 B 회귀: 턴 중 단계 전이 시 이전 말머리 허용"
setstage scaffolding
hook '{"hook_event_name":"PreToolUse","cwd":"'"$WORK"'","prompt_id":"pz","tool_name":"Read","tool_input":{}}' >/dev/null
setstage execution
check_empty "그 턴에 관여한 단계의 말머리는 통과" "$(hook "$(STOP pz '[1/6 Scaffolding] 완료')")"
check "무관한 단계 말머리는 차단" '"decision": "block"' "$(hook "$(STOP pz2 '[2/6 Context] 완료')")"

echo "== 턴 종료 게이트와 상한 소진 노출"
setstage verification
check "1회차 차단" '검증 증거가 없다' "$(hook "$(STOP pv '[5/6 Verification] 끝')")"
check "2회차 차단 (limit 2)" '"decision": "block"' "$(hook "$(STOP pv '[5/6 Verification] 끝')")"
check "3회차는 우회를 사용자에게 노출" 'systemMessage' "$(hook "$(STOP pv '[5/6 Verification] 끝')")"
hook '{"hook_event_name":"PostToolUse","cwd":"'"$WORK"'","tool_name":"Bash","tool_input":{"command":"npm test"}}' >/dev/null
check_empty "증거 적립 후 통과" "$(hook "$(STOP pv2 '[5/6 Verification] 끝')")"

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
check "bypassPermissions 에서는 켤 수 없다" '"permissionDecision": "deny"' \
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
check "SessionStart 가 ON 을 경고" '자동 승인이 켜져 있다' \
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

echo "== 루프 종료 시 행을 버린다"
setstage compounding
mkdir -p "$WORK/.dev/retrospect"
hook "$(printf '{"hook_event_name":"PostToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":".dev/retrospect/%s-r.md"}}' "$WORK" "$LID")" >/dev/null
OUT="$(cli advance)"
check "루프 종료 후 새 루프 시작" '새 루프' "$OUT"
check "이전 루프 stage 행 삭제" '^0$' "$(sql "SELECT COUNT(*) FROM stage WHERE loop_id='$LID'")"
check "이전 루프 evidence 행 삭제" '^0$' "$(sql "SELECT COUNT(*) FROM evidence WHERE loop_id='$LID'")"
if [ "$(loopid)" != "$LID" ]; then
  PASS=$((PASS + 1)); echo "  ok   새 해시가 발급된다"
else
  FAIL=$((FAIL + 1)); echo "  FAIL 새 해시가 발급된다"
fi

echo "== 손상 내성"
echo 'not a database' > "$WORK/.claude/harness/harness.db"
check_empty "DB 손상 시 차단하지 않음" "$(hook "$(W docs/x.md)" 2>/dev/null)"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
