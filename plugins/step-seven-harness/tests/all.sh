#!/bin/bash
# 검사 **전부**. 커밋 전에 이것 하나만 돌린다.
#   usage: bash tests/all.sh [repo-root]
#
# 왜 있나: 검사기가 아홉 개인데 한 번에 도는 입구가 없었다. 그래서 "smoke 만
# 돌리고 커밋" 을 두 번 했다 — 한 번은 `msg_check` 가 빨간불이었고, 한 번은
# 방금 만든 `doc_check` 의 숫자 대조가 빨간불이었다. **입구가 여럿이면 하나는
# 빠뜨린다.** 게이트를 하나로 모은 것과 같은 이유로 검사도 하나로 모은다.
#
# 파이프로 넘기지 마라 — 종료 코드가 마지막 명령의 것이 된다. 그 실수는 이
# 저장소가 이미 두 번 했다(`init_diff.sh` 의 `RA=$?`, 내 실험 스크립트).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${1:-$(cd "$HERE/../../.." && pwd)}"
FAILED=""

run() { # run <이름> <명령...>
  local name="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ]; then
    printf '  %-16s ok\n' "$name"
  else
    printf '  %-16s FAIL (rc=%d)\n' "$name" "$rc"
    printf '%s\n' "$out" | tail -12 | sed 's/^/      /'
    FAILED="$FAILED $name"
  fi
}

echo "== 검사 전부"
run smoke      bash "$HERE/smoke.sh" "$REPO"
for c in rules_check msg_check ctx_check doc_check math_check settings_check; do
  run "$c" python3 "$HERE/$c.py" "$REPO"
done
run init_diff  bash "$HERE/init_diff.sh" "$REPO"

echo
if [ -n "$FAILED" ]; then
  echo "실패:$FAILED"
  exit 1
fi
echo "전부 통과"
