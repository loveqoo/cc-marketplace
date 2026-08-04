#!/bin/sh
# step-seven-harness 스모크 테스트.
#   usage: sh plugins/step-seven-harness/tests/smoke.sh
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
check "래퍼 생성" 'harness' "$(ls "$WORK/.claude/harness/bin/")"
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
check "동의 필요 명령은 허용하지 않는다" '^0$' \
  "$(python3 -c "import json;a=json.load(open('$WORK/.claude/settings.json'))['permissions']['allow'];print(sum(1 for x in a if 'harness skip' in x or 'auto-skip on' in x))")"
check "작업만 기록해서는 Selection 을 끝낼 수 없다" 'acceptance' "$(cli advance || true)"
check "완료 조건 미정을 알린다" '완료 조건: (미정)' "$(cli status)"
check "완료 조건을 기록한다" '테스트 전부 통과' \
  "$(cli loop done-when '테스트 전부 통과' '응답 200ms 이하')"
check "입력 순서를 보존한다" '1\. 테스트 전부 통과' "$(cli loop done-when)"
check "status 에 완료 조건이 보인다" '완료 조건 (2개)' "$(cli status)"
check "둘 다 기록하면 종료 조건이 충족된다" '충족 intent_set, acceptance' "$(cli status)"

echo "== 엔진 사본이 프로젝트 안에 있다"
check "엔진 사본 생성" 'harness\.py' "$(ls "$WORK/.claude/harness/bin/")"
check "래퍼가 사본을 먼저 가리킨다" 'P="\$D/harness\.py"' "$(cat "$WORK/.claude/harness/bin/harness")"
check "사본으로 실행된다" '단계 1/7 Selection' \
  "$(cd "$WORK" && ./.claude/harness/bin/harness status | head -1)"

echo "== 하네스 자신은 수정할 수 없다"
check "엔진 사본 쓰기 차단" '하네스 자신은 수정할 수 없다' \
  "$(hook "$(W .claude/harness/bin/harness.py)")"
check "래퍼 쓰기 차단" '하네스 자신은 수정할 수 없다' \
  "$(hook "$(W .claude/harness/bin/harness)")"
check "DB 쓰기 차단 (손상시키면 게이트가 꺼진다)" '하네스 자신은 수정할 수 없다' \
  "$(hook "$(W .claude/harness/harness.db)")"
cli allow '.claude/harness/bin/**' --reason '엔진 수정 시도' >/dev/null
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
check "bypassPermissions 에서는 deny" '"permissionDecision": "deny"' \
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
check "단계가 유지된다" 'Planning' "$(cli status | head -1)"

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
check_no_prefix_complaint "증거 적립 후 통과" "$(hook "$(STOP pv2 '[Verification] 끝')")"

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
sql "INSERT OR IGNORE INTO evidence VALUES('$(loopid)','compounding','retro_file','r','x')" >/dev/null
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
    con.execute("INSERT OR REPLACE INTO promotion VALUES(?,?,?,?,?,?,?,?)",
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
check "bypassPermissions 에서는 loop new 거부" '"permissionDecision": "deny"' \
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
 INSERT INTO promotion VALUES('block:syncrule','block','rule','established',
   '동기화 확인용','x','2025-03-15T00:00:00+0900','2025-03-15T00:00:00+0900');" >/dev/null
check "저장값이 established 인 것을 확인" 'established' \
  "$(sql "SELECT maturity FROM promotion WHERE key='block:syncrule'")"
check "sync 없이도 재발이 대기 목록에 뜬다" 'block:syncrule' "$(cli promote)"
setstage compounding
sql "INSERT OR IGNORE INTO evidence VALUES('$(loopid)','compounding','retro_file','r','x')" >/dev/null
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
IOUT="$( (cd "$IW" && python3 "$ENGINE" init) 2>&1 )"; IRC=$?
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
 INSERT OR IGNORE INTO evidence VALUES('$MLID','compounding','retro_file','r',datetime('now'));" >/dev/null
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
  "$(msql "INSERT OR REPLACE INTO promotion VALUES('block:m1','block','hook','established','n','x',datetime('now'),datetime('now'))" >/dev/null; mcli metrics)"

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
check "rule 승격은 검증 대상이 아니다" '^0$' \
  "$(msql "SELECT COUNT(*) FROM event WHERE kind='promote_verify' AND rule='rule'")"
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
con.execute("INSERT INTO promotion VALUES(?,?,?,?,?,?,?,?)",
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
      INSERT OR IGNORE INTO evidence VALUES('$CLID','compounding','retro_file','r',strftime('%Y-%m-%dT%H:%M:%S','now')||'+0900');" >/dev/null
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
  "$(gb "$W_ recall x && $W_ skip context --reason y")"
check "bypassPermissions 에서는 거부" '"permissionDecision": "deny"' \
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
for OK in "mkdir -p .claude/hooks" "cat .claude/harness/LEARNED.md" \
          "find . -name '*.py'" "rm src/tmp.txt" "grep -r foo src/"; do
  check_empty "허용: $OK" "$(gb "$OK")"
done

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

# 회차 경계: 종료와 **같은 초**의 이벤트는 앞 회차에 속해야 한다.
# 포함 경계였을 때 앞 회차의 마지막 이벤트가 다음 회차 창에 겹쳐 두 번 세어졌다.
check "회차 경계의 같은 초 이벤트는 다음 회차로 새지 않는다" 'ok' \
  "$(rpy <<'PYB'
import sys, time; sys.path.insert(0, sys.argv[2])
import harness as h
con = h.connect(sys.argv[1])
lid = "260101-bbbbbb"
close_at = "2026-01-01T12:00:00+0900"
with con:
    con.execute("INSERT OR IGNORE INTO loop(id,created_at) VALUES(?,?)",
                (lid, "2026-01-01T10:00:00+0900"))
    # 종료와 같은 초에 남은 이벤트 + 1초 뒤 이벤트
    for at, tgt in ((close_at, "old cmd"), ("2026-01-01T12:00:01+0900", "new cmd")):
        con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target) "
                    "VALUES(?,?,?,?,?,?)",
                    (at, lid, "execution", "tool_fail", "Bash", tgt))
    con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target,detail) "
                "VALUES(?,?,?,?,?,?,?)",
                (close_at, lid, "compounding", "cycle_close", "1", lid + "-1", "{}"))
keys = h.cycle_search_keys(con, lid, h.cycle_window_start(con, lid))
assert keys == ["new cmd"], ("앞 회차 이벤트가 새어들었다", keys)
print("ok")
PYB
)"
rm -rf "$RW2"

echo "== 측정 산술 (손계산 대조)"
# 합성 이력의 기대값을 미리 종이에 세고 코드가 그 값을 내는지 본다.
# cycle_counters 11개 항목과 _survival 8개 항목.
MC="$(cd "$(dirname "$0")/../../.." && pwd)"
MOUT="$(python3 "$(dirname "$0")/math_check.py" "$MC" 2>&1)"; MRC=$?
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

echo "== 문서 구조 (중복·링크)"
DOUT="$(python3 "$(dirname "$0")/doc_check.py" "$MC" 2>&1)"; DRC=$?
check "문서에 산문 중복·깨진 링크가 없다" '^0$' "$DRC"
check "검사가 실제로 문서를 읽었다" '산문' "$DOUT"
if [ "$DRC" != 0 ]; then printf '%s\n' "$DOUT" | tail -6 | sed 's/^/     /'; fi


echo "== 손상 내성 (fail-open 은 종료 코드까지 포함한다)"
echo 'not a database' > "$WORK/.claude/harness/harness.db"
rm -f "$WORK/.claude/harness/harness.db-wal" "$WORK/.claude/harness/harness.db-shm"
check_empty "DB 손상 시 차단하지 않음" "$(hook "$(W docs/x.md)" 2>/dev/null)"
hook "$(W docs/x.md)" >/dev/null 2>&1; CRC=$?
check "DB 손상 시 종료 코드 0" '^0$' "$CRC"
check "traceback 을 내지 않는다" '^0$' \
  "$(hook "$(W docs/x.md)" 2>&1 >/dev/null | grep -c Traceback)"
check "무슨 일인지 stderr 로 알린다" 'step-seven-harness' \
  "$(hook "$(W docs/x.md)" 2>&1 >/dev/null)"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
