#!/bin/bash
# 검사 **전부**. 커밋 전에 이것 하나만 돌린다.
#   usage: bash tests/all.sh [repo-root]
#
# 왜 있나: 검사기가 여덟 개인데 한 번에 도는 입구가 없었다. 그래서 "smoke 만
# 돌리고 커밋" 을 두 번 했다. **입구가 여럿이면 하나는 빠뜨린다.**
#
# ## 이 파일은 자기 자신도 검사한다
#
# 5회차 변이 테스트가 이 파일의 세 구멍을 보였다 — 셋 다 `전부 통과` 가 나왔다:
#   · `rc=$?` → `rc=0`      한 글자로 모든 실패를 삼킨다
#   · for 루프에서 이름 하나 삭제  검사기가 조용히 빠진다
#   · smoke 의 `check()` 무조건 PASS  766 체크가 전부 죽어도 초록
#
# 그래서 셋을 구조로 막는다:
#   ① 목록을 손으로 적지 않는다 — `tests/*.py` 를 훑는다 (빠뜨릴 자리가 없다)
#   ② 돌린 개수를 세고 바닥을 둔다
#   ③ **카나리아** — 반드시 실패하는 검사를 하나 끼워 넣고, 그것을 잡지 못하면
#      이 파일 자체가 고장난 것이다. "검사를 검사하는 것" 은 검사밖에 없다.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${1:-$(cd "$HERE/../../.." && pwd)}"
FAILED=""
RAN=0

run() { # run <이름> <명령...>
  local name="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  RAN=$((RAN + 1))
  if [ "$rc" -eq 0 ]; then
    printf '  %-16s ok\n' "$name"
  else
    printf '  %-16s FAIL (rc=%d)\n' "$name" "$rc"
    printf '%s\n' "$out" | tail -12 | sed 's/^/      /'
    FAILED="$FAILED $name"
  fi
  return 0
}

# ③ 카나리아를 먼저 돌린다. 실패를 못 잡으면 나머지 결과도 믿을 수 없다.
CANARY="$(mktemp)"; printf 'import sys\nsys.exit(3)\n' > "$CANARY"
CFAILED_BEFORE="$FAILED"
run canary python3 "$CANARY"
rm -f "$CANARY"
if [ "$FAILED" = "$CFAILED_BEFORE" ]; then
  echo "카나리아가 잡히지 않았다 — 이 파일의 실패 감지가 고장났다" >&2
  exit 1
fi
FAILED="$CFAILED_BEFORE"      # 카나리아는 실패로 세지 않는다
RAN=0

# ① 목록은 훑어서 만든다. `_` 로 시작하는 것과 생성기는 검사기가 아니다.
CHECKERS="$(cd "$HERE" && ls *.py | grep -v '^_' | grep -v '^gen_' | sed 's/\.py$//' | sort)"

echo "== 검사 전부"
run smoke      bash "$HERE/smoke.sh" "$REPO"
for c in $CHECKERS; do
  run "$c" python3 "$HERE/$c.py" "$REPO"
done
run init_diff  bash "$HERE/init_diff.sh" "$REPO"

# ② 개수 바닥. 검사기가 조용히 빠지면 여기서 걸린다.
if [ "$RAN" -lt 8 ]; then
  echo "검사를 $RAN 개밖에 돌리지 않았다 (최소 8) — 목록이 깨졌다" >&2
  exit 1
fi

echo
if [ -n "$FAILED" ]; then
  echo "실패:$FAILED"
  exit 1
fi
echo "전부 통과 ($RAN 개)"
