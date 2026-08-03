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

# 실제 훅 실행과 동일하게: CLAUDE_PROJECT_DIR 를 주고 stdin 으로 이벤트를 넣는다
hook() {
  printf '%s' "$1" | CLAUDE_PROJECT_DIR="$WORK" python3 "$ENGINE" hook
}

check() { # check <label> <expect-substring> <actual>
  if printf '%s' "$3" | grep -q "$2"; then
    PASS=$((PASS + 1))
    printf '  ok   %s\n' "$1"
  else
    FAIL=$((FAIL + 1))
    printf '  FAIL %s\n     기대: %s\n     실제: %s\n' "$1" "$2" "$3"
  fi
}

check_empty() { # check_empty <label> <actual>
  if [ -z "$2" ]; then
    PASS=$((PASS + 1))
    printf '  ok   %s\n' "$1"
  else
    FAIL=$((FAIL + 1))
    printf '  FAIL %s\n     기대: (출력 없음)\n     실제: %s\n' "$1" "$2"
  fi
}

stage() { # stage <stage-id>
  python3 - "$WORK/.claude/harness/state.json" "$1" <<'PY'
import json, sys
p, s = sys.argv[1], sys.argv[2]
d = json.load(open(p)); d["stage"] = s
json.dump(d, open(p, "w"))
PY
}

W() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","tool_name":"Write","tool_input":{"file_path":"%s"}}' "$WORK" "$1"; }
B() { printf '{"hook_event_name":"PreToolUse","cwd":"%s","permission_mode":"%s","tool_name":"Bash","tool_input":{"command":"%s"}}' "$WORK" "$2" "$1"; }

echo "== 미설치 프로젝트에서는 조용해야 한다"
check_empty "state.json 없으면 무출력" "$(hook "$(W docs/x.md)")"

echo "== init"
(cd "$WORK" && python3 "$ENGINE" init >/dev/null)
check "CLAUDE.md 앵커 1줄" '^@\.claude/harness/POLICY\.md$' "$(cat "$WORK/CLAUDE.md")"
check "state.json 생성" 'scaffolding' "$(cat "$WORK/.claude/harness/state.json")"
check "gitignore 등록" 'state\.json' "$(cat "$WORK/.gitignore")"
check "래퍼 실행권한" 'harness' "$(ls -l "$WORK/.claude/harness/bin/harness")"

echo "== 폴더 가드"
check "docs/ 쓰기 차단" '"permissionDecision": "deny"' "$(hook "$(W docs/spec/001-a.md)")"
check "docs 차단 이유 노출" '사람이 기록하는 영역' "$(hook "$(W docs/spec/001-a.md)")"
check_empty "Scaffolding 에서 신규 최상위 폴더 허용" "$(hook "$(W src/a.py)")"
check ".dev 하위 폴더 규칙 위반 차단" '규칙 위반' "$(hook "$(W .dev/nope/a.md)")"
check_empty ".dev/plan 허용" "$(hook "$(W .dev/plan/001-a.md)")"

echo "== 단계 게이트"
stage planning
check "Planning 에서 소스 쓰기 차단" 'Planning' "$(hook "$(W src/a.py)")"
check "차단 시 대응 방법 제시" 'advance' "$(hook "$(W src/a.py)")"
check_empty "Planning 에서 .dev 쓰기 허용 (.dev 미존재여도)" "$(hook "$(W .dev/plan/002-b.md)")"
stage execution
mkdir -p "$WORK/src"
check_empty "Execution 에서 기존 소스 폴더 쓰기 허용" "$(hook "$(W src/a.py)")"
check "Execution 에서 신규 최상위 폴더 차단" '신규 최상위 폴더' "$(hook "$(W newdir/a.py)")"

echo "== 스킵 동의 게이트"
stage context
check "사유 없는 skip 거부" '"permissionDecision": "deny"' \
  "$(hook "$(B '.claude/harness/bin/harness skip context' default)")"
check "사유 있는 skip 은 ask" '"permissionDecision": "ask"' \
  "$(hook "$(B '.claude/harness/bin/harness skip context --reason \"단순 오타 수정\"' default)")"
check "ask 에 사유 노출" '단순 오타 수정' \
  "$(hook "$(B '.claude/harness/bin/harness skip context --reason \"단순 오타 수정\"' default)")"
check "bypassPermissions 에서는 동의 불가로 deny" '"permissionDecision": "deny"' \
  "$(hook "$(B '.claude/harness/bin/harness skip context --reason \"x\"' bypassPermissions)")"
check_empty "status 는 통과" "$(hook "$(B '.claude/harness/bin/harness status' default)")"

echo "== CLI"
stage planning
check "종료 조건 미충족 시 advance 거부" '거부' "$(cd "$WORK" && python3 "$ENGINE" advance || true)"
mkdir -p "$WORK/.dev/plan" && echo plan > "$WORK/.dev/plan/001-a.md"
(cd "$WORK" && python3 "$ENGINE" approve-plan .dev/plan/001-a.md >/dev/null)
check "승인 후 advance 성공" 'Execution' "$(cd "$WORK" && python3 "$ENGINE" advance)"
check "skip 은 사유를 기록한다" '사유' \
  "$(cd "$WORK" && python3 "$ENGINE" skip verification --reason '검증 불필요한 문서 작업')"
check "공백 있는 사유가 잘리지 않는다" '검증 불필요한 문서 작업' "$(cd "$WORK" && python3 "$ENGINE" status)"
check "--reason 이 앞에 와도 위치 인자를 찾는다" 'docs/adr' \
  "$(cd "$WORK" && python3 "$ENGINE" allow --reason '사용자가 ADR 작성을 지시했다' 'docs/adr/**')"

echo "== docs 예외"
stage scaffolding
(cd "$WORK" && python3 "$ENGINE" allow 'docs/spec/**' --reason '사용자가 스펙 작성을 지시' >/dev/null)
check_empty "예외 등록 후 docs 쓰기 허용" "$(hook "$(W docs/spec/001-a.md)")"
check "예외에도 명명 규칙은 강제" 'NNN-name.md' "$(hook "$(W docs/spec/bad_name.md)")"

echo "== 턴 종료 게이트"
stage execution
S1='{"hook_event_name":"Stop","cwd":"'"$WORK"'","prompt_id":"p1","last_assistant_message":"작업 끝났습니다"}'
check "말머리 없으면 종료 차단" '"decision": "block"' "$(hook "$S1")"
check_empty "같은 프롬프트에서 재차단 안 함" "$(hook "$S1")"
S2='{"hook_event_name":"Stop","cwd":"'"$WORK"'","prompt_id":"p2","last_assistant_message":"[4/6 Execution] 완료"}'
check_empty "말머리 있으면 통과" "$(hook "$S2")"
stage verification
S3='{"hook_event_name":"Stop","cwd":"'"$WORK"'","prompt_id":"p3","last_assistant_message":"[5/6 Verification] 완료"}'
check "검증 증거 없으면 종료 차단" '검증 증거가 없다' "$(hook "$S3")"
PT='{"hook_event_name":"PostToolUse","cwd":"'"$WORK"'","tool_name":"Bash","tool_input":{"command":"npm test"}}'
hook "$PT" >/dev/null
S4='{"hook_event_name":"Stop","cwd":"'"$WORK"'","prompt_id":"p4","last_assistant_message":"[5/6 Verification] 완료"}'
check_empty "증거 적립 후 통과" "$(hook "$S4")"

echo "== SessionStart"
check "단계 상태 주입" 'additionalContext' \
  "$(hook '{"hook_event_name":"SessionStart","cwd":"'"$WORK"'","source":"startup"}')"

echo "== 손상 내성"
echo 'not json' > "$WORK/.claude/harness/state.json"
check_empty "상태 손상 시 차단하지 않음" "$(hook "$(W docs/x.md)")"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
