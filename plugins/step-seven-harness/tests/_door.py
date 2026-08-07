"""훅을 **선언된 문으로** 보낸다.

`smoke.sh` 는 `python3 harness.py hook` 을 직접 부른다. 그래서 `hooks/hooks.json`
이 틀려도 검사는 전부 초록이다 — matcher 에서 `Bash` 가 빠져도, 명령 문자열이
깨져도, 이벤트 이름을 하나 지워도. 실제 세션에서 훅을 부르는 것은 Claude Code 이고,
**그것이 읽는 것은 이 파일이 아니라 `hooks.json` 이다.** 인수 시나리오는 그쪽으로
보낸다.

  usage: python3 _door.py <plugin-root> <project-dir> <event> <subject>
         (훅 입력 JSON 은 stdin)

`subject` 는 matcher 가 비교하는 대상이다 — PreToolUse/PostToolUse 는 도구 이름,
SessionStart 는 source(`startup` 등).

**아무도 부르지 않는 것은 통과가 아니다.** matcher 에 걸린 훅이 하나도 없으면 빈
출력(=판정 없음=허용)이 아니라 rc=9 로 끊는다. 이 구분이 없으면 hooks.json 을
통째로 비워도 "허용" 으로 읽혀 시나리오가 전부 초록이 된다.
"""
import json
import os
import re
import subprocess
import sys


def groups(plugin_root, event):
    path = os.path.join(plugin_root, "hooks", "hooks.json")
    with open(path, encoding="utf-8") as fh:
        return (json.load(fh).get("hooks") or {}).get(event, [])


def matches(matcher, subject):
    """matcher 가 없거나 `*` 면 전부. 그 외에는 정규식."""
    if matcher in (None, "", "*"):
        return True
    try:
        return re.search(matcher, subject) is not None
    except re.error:
        return False


def commands(plugin_root, event, subject):
    out = []
    for g in groups(plugin_root, event):
        if not matches(g.get("matcher"), subject):
            continue
        for hk in g.get("hooks") or []:
            if hk.get("type") == "command" and hk.get("command"):
                out.append(hk["command"])
    return out


def main(argv):
    if len(argv) != 5:
        sys.stderr.write(__doc__)
        return 2
    plugin_root, project, event, subject = argv[1:]
    payload = sys.stdin.read()
    cmds = commands(plugin_root, event, subject)
    if not cmds:
        sys.stderr.write("hooks.json 에 %s/%s 를 받는 훅이 없다 — "
                         "아무도 부르지 않는 것은 통과가 아니다\n" % (event, subject))
        return 9
    # `${CLAUDE_PLUGIN_ROOT}` 는 **우리가 치환하지 않는다.** sh 가 펼치게 둔다 —
    # Claude Code 도 그렇게 하고, 우리가 대신 펼치면 hooks.json 이 sh 로는 못 펼칠
    # 모양이어도 여기서는 통한다.
    env = dict(os.environ, CLAUDE_PLUGIN_ROOT=plugin_root, CLAUDE_PROJECT_DIR=project)
    rc = 0
    for cmd in cmds:
        p = subprocess.Popen(["sh", "-c", cmd], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             cwd=project, env=env, universal_newlines=True)
        out, err = p.communicate(payload)
        sys.stdout.write(out)
        sys.stderr.write(err)
        rc = rc or p.returncode
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
