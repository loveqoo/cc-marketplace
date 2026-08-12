"""실사용 명령을 훅에 재생해 **잘못 막는 것**을 센다.

    usage: python3 .dev/replay.py [<engine.py>] [<commands.json>]

## 왜 이게 있나

합성 코퍼스로는 증상을 못 찾았다. 검증 단계 명령 30개를 지어내 재보니 멈춤이
3% 였는데, 도그푸딩 저장소의 **실제** 세션 기록 589개로 재보니 21.4% 였다.
지어낸 명령은 우리가 상상한 모양이라 우리가 만든 결함을 비껴간다.

그래서 이 계측기의 입력은 **사람이 만들지 않는다.** Claude Code 세션 기록
(`~/.claude/projects/<encoded>/*.jsonl`)에서 Bash 호출을 통째로 꺼낸다.

## 무엇을 세나

`ask`·`deny` 를 규칙별로 센다. 다만 **총 멈춤 수가 목표가 아니다** — 제어 명령과
승인 게이트(`allow`·`skip`)는 설계대로 멈춘다. 줄여야 하는 것은 그 밖의 것이고,
이 스크립트는 둘을 갈라 보여준다.
"""
import json
import os
import subprocess
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    HERE, "..", "plugins", "step-seven-harness", "scripts", "harness.py")
CMDS = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "replay-commands.json")

# 사유 문구 → 규칙 이름. 문구가 바뀌면 여기도 바뀌어야 한다 — 그러라고 `?` 를 남긴다.
RULES = (("담은 폴더", "containment"), ("원문에 하네스 바닥값", "floor_named"),
         ("find 가", "opaque:find"), ("xargs", "opaque:xargs"),
         ("따옴표가 맞지 않아", "parse-fail"), ("셸 확장이 섞여", "expand"),
         ("예외 요청", "consent"), ("건너뛰", "consent"),
         ("변경할 수 없다", "floor-deny"), ("클래스", "stage"))

# **설계대로 멈추는 것.** 이것들은 줄일 대상이 아니다.
BY_DESIGN = {"consent", "floor-deny", "floor_named", "제어명령"}


def rule_of(reason):
    for key, name in RULES:
        if key in reason:
            return name
    return "제어명령"          # harness 자신을 부르는 명령의 동의 게이트


def main():
    cmds = json.load(open(CMDS, encoding="utf-8"))
    work = os.environ.get("REPLAY_ROOT")
    if not work:
        sys.exit("REPLAY_ROOT 에 하네스가 설치된 시험 저장소 경로를 주어라")
    env = dict(os.environ, CLAUDE_PROJECT_DIR=work)
    stopped = []
    for cmd in cmds:
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "tool_input": {"command": cmd}})
        out = subprocess.run([sys.executable, ENGINE, "hook"], input=payload,
                             capture_output=True, text=True, env=env).stdout.strip()
        if not out:
            continue
        d = json.loads(out)["hookSpecificOutput"]
        stopped.append((cmd, d["permissionDecision"], rule_of(d["permissionDecisionReason"])))

    tally = Counter(r for _, _, r in stopped)
    noise = sum(n for r, n in tally.items() if r not in BY_DESIGN)
    print("실사용 %d개 재생 — 멈춤 %d건 (%.1f%%)"
          % (len(cmds), len(stopped), 100.0 * len(stopped) / len(cmds)))
    print("  설계대로 %d · **잘못 막은 것 %d (%.1f%%)**"
          % (len(stopped) - noise, noise, 100.0 * noise / len(cmds)))
    for rule, n in tally.most_common():
        mark = "   " if rule in BY_DESIGN else " ← "
        print("  %-14s %4d%s" % (rule, n, mark))
    if noise:
        print("\n잘못 막은 것 (앞 8건):")
        for cmd, dec, rule in [s for s in stopped if s[2] not in BY_DESIGN][:8]:
            print("  %-5s %-12s %s" % (dec, rule, cmd.replace("\n", " ⏎ ")[:64]))
    return 1 if noise else 0


if __name__ == "__main__":
    sys.exit(main())
