"""사람이 보는 문서의 구조를 검사한다.

  usage: python3 doc_check.py [repo-root]

이 검사가 왜 있나: 문서를 두 파일로 가르면서 원본 끝까지 잘라와 **푸터가 두 번**
들어갔고, 나는 링크·이음새·누수를 확인하면서 정작 파일 꼬리를 보지 않았다.
"검사했다"가 "내가 확인하려고 생각한 것만 검사했다"였던 셈이다.

검사하는 것 (둘 다 실제로 틀린 적이 있다)
  1. 산문 줄 중복 — 잘라 붙이다 같은 문단이 두 번 들어가는 것
  2. 저장소 내부 링크가 실제로 존재하는지
"""
import os
import re
import sys

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                       else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "..", ".."))
DOCS = (
    "README.md",
    "plugins/step-seven-harness/README.md",
    "plugins/step-seven-harness/templates/POLICY.md",
    "plugins/step-seven-harness/templates/rationale.md",
)
LINK_RE = re.compile(r"\]\((?!https?:|mailto:)([^)#\s]+)")


def prose_lines(path):
    """산문 줄만 (번호, 내용). 코드 블록·표·목록·헤딩은 반복이 정상이다."""
    out, fenced = [], False
    for i, raw in enumerate(open(path, encoding="utf-8"), 1):
        line = raw.rstrip()
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or not line:
            continue
        if line.startswith(("|", "#", ">", "-", "*", " ", "\t")):
            continue
        if len(line) < 16:
            continue
        out.append((i, line))
    return out


def main():
    bad = []
    for rel in DOCS:
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            bad.append("%s: 파일이 없다" % rel)
            continue

        seen = {}
        for i, line in prose_lines(path):
            seen.setdefault(line, []).append(i)
        dups = {k: v for k, v in seen.items() if len(v) > 1}
        for line, at in sorted(dups.items(), key=lambda kv: kv[1][0]):
            bad.append("%s: 산문 줄이 %s 에 중복 — %s" % (rel, at, line[:56]))

        base = os.path.dirname(path)
        body = open(path, encoding="utf-8").read()
        for target in LINK_RE.findall(body):
            if not os.path.exists(os.path.normpath(os.path.join(base, target))):
                bad.append("%s: 링크가 가리키는 것이 없다 — %s" % (rel, target))

        print("  %-52s 산문 %3d줄, 중복 %d, 링크 %d"
              % (rel, len(prose_lines(path)), len(dups), len(LINK_RE.findall(body))))

    if bad:
        print("\n문제 %d건" % len(bad))
        for b in bad:
            print("  " + b)
        return 1
    print("\n문제 없음")
    return 0


sys.exit(main())
