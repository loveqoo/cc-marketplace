#!/bin/sh
# init 이 **무엇을 잃지 않았는지** 차등 실행으로 확인한다.
#   usage: sh init_diff.sh [repo-root]
# 스모크 스위트에 넣지 않는다 — git 히스토리를 필요로 하고 8개 픽스처에
# 엔진을 16번 돌려 느리다. 설치 경로를 건드릴 때 손으로 돌린다.
#
# 원래는 0.18.0(77d6065) 과 출력이 **똑같은지** 봤다. 그 등식은 유지될 수 없었다 —
# 그 뒤로 단계 표시와 AGENTS.md 안내가 의도적으로 늘었고, 검사는 영구히 빨간불이
# 되었다. 항상 실패하는 검사는 아무도 보지 않으므로 없는 것보다 나쁘다.
#
# 그래서 불변식을 한 방향으로 바꿨다: **옛 엔진이 만든 것을 새 엔진이 잃지 않는다.**
#   - 파일 트리: 옛 것 ⊆ 새 것 (추가는 허용하고 보고만 한다)
#   - 사용자 파일 내용(CLAUDE.md/.gitignore/settings.json): 완전히 같아야 한다
#     — 남의 글을 덮는 것이 이 검사가 잡아야 할 진짜 사고다
#   - 종료 코드: 같아야 한다
# 출력 문구 차이는 보고하되 실패로 세지 않는다.
REPO=${1:-$(cd "$(dirname "$0")/../../.." && pwd)}
NEW=$REPO/plugins/step-seven-harness/scripts/harness.py
BASE=$(mktemp -d)

# 0.18.0 엔진을 templates 와 함께 재구성한다 (plugin_root 가 ../templates 를 본다)
mkdir -p "$BASE/old/scripts" "$BASE/old/templates"
git -C $REPO show 77d6065:plugins/step-six-harness/scripts/harness.py > "$BASE/old/scripts/harness.py"
for f in stages.json POLICY.md rationale.md; do
  git -C $REPO show 77d6065:plugins/step-six-harness/templates/$f > "$BASE/old/templates/$f"
done
OLD="$BASE/old/scripts/harness.py"

# 현재 엔진도 같은 방식으로 격리한다 (실제 플러그인 트리를 쓰면 templates 가 다르다)
mkdir -p "$BASE/new/scripts" "$BASE/new/templates"
cp "$NEW" "$BASE/new/scripts/harness.py"
for f in stages.json POLICY.md rationale.md; do
  cp "$REPO/plugins/step-seven-harness/templates/$f" "$BASE/new/templates/$f"
done
NEWI="$BASE/new/scripts/harness.py"

fixture() { # fixture <dir> <case>
  d=$1; c=$2
  mkdir -p "$d"; (cd "$d" && git init -q .)
  case $c in
    empty) ;;
    claude_md) printf '# 내 프로젝트\n\n기존 내용\n' > "$d/CLAUDE.md" ;;
    gitignore) printf 'node_modules\n*.log\n' > "$d/.gitignore" ;;
    settings_dict) mkdir -p "$d/.claude"
      printf '{"permissions":{"allow":["Bash(ls)"]},"other":1}\n' > "$d/.claude/settings.json" ;;
    settings_empty) mkdir -p "$d/.claude"; printf '{}\n' > "$d/.claude/settings.json" ;;
    all) printf '# P\n내용\n' > "$d/CLAUDE.md"; printf 'node_modules\n' > "$d/.gitignore"
      mkdir -p "$d/.claude"; printf '{"permissions":{"allow":[]}}\n' > "$d/.claude/settings.json" ;;
    anchor_in_prose) printf '# P\n\n예시: `@.claude/harness/POLICY.md` 를 넣는다\n' > "$d/CLAUDE.md" ;;
    settings_list) mkdir -p "$d/.claude"; printf '{"permissions":[]}\n' > "$d/.claude/settings.json" ;;
  esac
}

# 해시·시각처럼 매번 달라지는 값을 지운다
norm() { sed -E 's/[0-9]{6}-[0-9a-f]{6}/<HASH>/g; s/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+[+-][0-9]{4}/<TS>/g; s#(/private)?/var/folders/[^ ]*#<DIR>#g; s#/tmp\.[A-Za-z0-9]+/p#<DIR>#g; s/step-(six|seven)-harness/<PLUGIN>/g'; }  # 이름 변경은 의도된 차이다
tree() { (cd "$1" && find . -path ./.git -prune -o -type f -print | LC_ALL=C sort \
  | grep -v 'harness\.db' | grep -v '/bin/'); }
bodies() { (cd "$1" && for f in CLAUDE.md .gitignore .claude/settings.json; do
  [ -f "$f" ] && { echo "--- $f"; norm < "$f"; }; done); }

SAME=0; DIFF=0
for c in empty claude_md gitignore settings_dict settings_empty all anchor_in_prose settings_list; do
  A=$(mktemp -d)/p; B=$(mktemp -d)/p
  fixture "$A" $c; fixture "$B" $c
  OA=$( (cd "$A" && python3 "$OLD" init) 2>&1 | norm ); RA=$?
  OB=$( (cd "$B" && python3 "$NEWI" init) 2>&1 | norm ); RB=$?
  # 재실행 멱등성도 같은 픽스처에서 확인
  OA2=$( (cd "$A" && python3 "$OLD" init) 2>&1 | norm )
  OB2=$( (cd "$B" && python3 "$NEWI" init) 2>&1 | norm )
  T=$(mktemp -d)
  tree "$A" > "$T/ta"; tree "$B" > "$T/tb"
  # 옛 엔진이 만들었는데 새 엔진이 만들지 않은 파일. 이것만 실패다.
  LOST=$(comm -23 "$T/ta" "$T/tb")
  GAIN=$(comm -13 "$T/ta" "$T/tb")
  if [ -z "$LOST" ] && [ "$(bodies "$A")" = "$(bodies "$B")" ] && [ "$RA" = "$RB" ]; then
    echo "  보존   $c$([ -n "$GAIN" ] && printf ' (추가: %s)' "$(echo $GAIN)")"
    SAME=$((SAME+1))
    rm -rf "$T"
  else
    echo "  퇴행   $c   (exit $RA vs $RB)"
    DIFF=$((DIFF+1))
    [ -n "$LOST" ] && printf '         잃음 %s\n' "$(echo $LOST)"
    printf '%s\n' "$OA" > "$T/oa"; printf '%s\n' "$OB" > "$T/ob"
    bodies "$A" > "$T/ba"; bodies "$B" > "$T/bb"
    diff "$T/oa" "$T/ob" | head -8 | sed 's/^/         출력 /'
    diff "$T/ba" "$T/bb" | head -10 | sed 's/^/         파일 /'
    diff "$T/ta" "$T/tb" | head -6 | sed 's/^/         트리 /'
    rm -rf "$T"
  fi
done
echo
echo "보존 $SAME / 퇴행 $DIFF"
[ "$DIFF" -eq 0 ]
