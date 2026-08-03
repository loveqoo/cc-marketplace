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

echo "== 사용법 안내"
check "인자 없이 실행하면 명령 목록" 'auto-skip on' "$(python3 "$ENGINE")"
check "help 도 같은 목록" 'approve-plan' "$(python3 "$ENGINE" help)"
check "승인 필요 명령을 표시" '사용자 승인 다이얼로그가 뜬다' "$(python3 "$ENGINE" --help)"
check "외울 필요 없다고 안내" '외울 필요는 없다' "$(python3 "$ENGINE" help)"
check "오타는 help 로 안내" 'harness help' "$(cli statuss 2>&1 || true)"
check "하네스 밖에서도 help 동작" 'step-six-harness' "$(cd / && python3 "$ENGINE" help)"

echo "== 작업 선정 (Scaffolding)"
check "작업 미정을 알린다" '작업: (미정)' "$(cli status)"
check "작업을 기록한다" 'src/auth.ts 토큰 갱신' \
  "$(cli loop intent 'src/auth.ts 토큰 갱신 수정')"
check "status 에 작업이 보인다" '작업: src/auth.ts' "$(cli status)"
check "사유 없는 intent 는 사용법 안내" '사용법' "$(cli loop intent || true)"
check "조회 명령이 미리 허용된다" 'harness recall' \
  "$(python3 -c "import json;print(json.load(open('$WORK/.claude/settings.json'))['permissions']['allow'])")"
check "동의 필요 명령은 허용하지 않는다" '^0$' \
  "$(python3 -c "import json;a=json.load(open('$WORK/.claude/settings.json'))['permissions']['allow'];print(sum(1 for x in a if 'harness skip' in x or 'auto-skip on' in x))")"

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

echo "== 말머리는 번호가 아니라 단계 이름으로 검증한다"
check_empty "이름만 써도 통과" "$(hook "$(STOP pn1 '[Execution] 완료')")"
check_empty "대소문자 무시" "$(hook "$(STOP pn2 '[execution] 완료')")"
check_empty "번호를 함께 써도 통과" "$(hook "$(STOP pn3 '[4/6 Execution] 완료')")"
check "이름 없이 번호만 쓰면 차단" '"decision": "block"' "$(hook "$(STOP pn4 '[4/6] 완료')")"
check "닫는 대괄호가 없으면 차단" '"decision": "block"' "$(hook "$(STOP pn5 '[4/6 Execution 완료')")"
check "다른 단계 이름은 차단" '"decision": "block"' "$(hook "$(STOP pn6 '[Planning] 완료')")"
check "차단 메시지가 이름만 제시한다" '\[Execution\] 를 표시하지 않았다' \
  "$(hook "$(STOP pn7 '말머리 없음')")"

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
check "루프 범위 표시" '루프 .* 범위' "$(cli auto-skip status)"
check "같은 루프에서는 활성" '"permissionDecision": "defer"' \
  "$(hook "$(B "$SKIPCMD" default)")"
cli loop new >/dev/null
check "루프가 바뀌면 만료" '루프가 바뀌어' "$(cli auto-skip status)"
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
check "경로 키워드가 조각으로 넓혀져 산문에 걸린다" 'api\.md' "$(cli recall src/api.ts)"
check "무관한 파일은 걸리지 않는다" '^0$' \
  "$(cli recall 비동기 | grep -c unrelated || true)"
check "명령 키워드로 실패 기록을 찾는다" 'npm test' "$(cli recall npm)"
check "무관한 기록은 걸러진다" '^0$' \
  "$(cli recall npm | sed -n '/과거 관측/,/^$/p' | grep -c docs_readonly || true)"
check "키워드가 아무것도 안 맞으면 비어 있다" '(없음)' "$(cli recall zzz존재하지않음)"
check "여러 루프 반복을 표시한다" '여러 루프에서 반복' "$(cli recall docs)"
check "--kind 로 종류를 좁힌다" 'tool_fail' "$(cli recall --kind tool_fail)"
check "작업 미정이면 전체 + 기록 안내" '작업이 정해졌으면' "$(cli recall)"
cli loop intent 'npm test 실패 조사' >/dev/null
check "작업이 정해지면 그것을 기본 키워드로 쓴다" '작업에서 추출' "$(cli recall)"
check "작업 키워드로 관련 기록을 찾는다" 'npm test' "$(cli recall)"
cli loop intent 'src/auth.ts 토큰 갱신 로직 수정' >/dev/null
check "저정보 단어(src·로직·수정)는 키워드에서 제외" '추출: auth.ts 토큰 갱신)' "$(cli recall | head -1)"

echo "== stats (누적 수치)"
check "루프 수를 센다" '루프: ' "$(cli stats)"
check "이벤트 종류별 집계" '규칙 차단' "$(cli stats)"
check "차단된 규칙 상위 표시" 'docs_readonly' "$(cli stats)"
check "규칙 단위로 묶어 대상 종수를 센다" '대상 ' "$(cli stats)"
check "실패한 도구 상위 표시" 'npm test' "$(cli stats)"
check "반복을 명시한다" '루프에서 반복' "$(cli stats)"
check "--loop 는 현재 루프만" "현재 루프 $NEW" "$(cli stats --loop)"
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

echo "== 손상 내성"
echo 'not a database' > "$WORK/.claude/harness/harness.db"
check_empty "DB 손상 시 차단하지 않음" "$(hook "$(W docs/x.md)" 2>/dev/null)"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
