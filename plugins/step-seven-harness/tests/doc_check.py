"""사람이 보는 문서의 구조를 검사한다.

  usage: python3 doc_check.py [repo-root]

이 검사가 왜 있나: 문서를 두 파일로 가르면서 원본 끝까지 잘라와 **푸터가 두 번**
들어갔고, 나는 링크·이음새·누수를 확인하면서 정작 파일 꼬리를 보지 않았다.
"검사했다"가 "내가 확인하려고 생각한 것만 검사했다"였던 셈이다.

검사하는 것 (셋 다 실제로 틀린 적이 있다)
  1. 산문 줄 중복 — 잘라 붙이다 같은 문단이 두 번 들어가는 것
  2. 저장소 내부 링크가 실제로 존재하는지
  3. 문서에 박힌 단계 번호(`N/M 이름`)가 stages.json 과 맞는지
     — 0.10.0 에서 Selection 을 신설한 뒤에도 install 스킬이 `1/6 Scaffolding` 이라고
     적혀 있었고, 모델은 그 문장을 **정확히 따라** 틀린 단계를 보고했다. 환각이 아니라
     내가 준 낡은 문서를 읽은 것이다.
"""
import os
import re
import sys

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                       else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "..", ".."))
DOCS = (
    "plugins/step-seven-harness/skills/install/SKILL.md",
    "plugins/step-seven-harness/skills/harness/SKILL.md",
    "README.md",
    "plugins/step-seven-harness/README.md",
    "plugins/step-seven-harness/templates/POLICY.md",
    "plugins/step-seven-harness/templates/rationale.md",
)
LINK_RE = re.compile(r"\]\((?!https?:|mailto:)([^)#\s]+)")
STAGE_RE = re.compile(r"(\d+)/(\d+)\s+([A-Z][a-z]+)")
STAGES_JSON = "plugins/step-seven-harness/templates/stages.json"


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


def stage_labels():
    """stages.json 이 말하는 실제 번호. 이것이 단일 출처다."""
    import json
    cfg = json.load(open(os.path.join(REPO, STAGES_JSON), encoding="utf-8"))
    n = len(cfg["stages"])
    return {s["label"]: "%d/%d" % (i + 1, n) for i, s in enumerate(cfg["stages"])}


def main():
    bad = []
    real = stage_labels()
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

        for i, line in enumerate(body.splitlines(), 1):
            for m in STAGE_RE.finditer(line):
                got, name = "%s/%s" % (m.group(1), m.group(2)), m.group(3)
                want = real.get(name)
                if want is None:
                    # 이름 오타는 조용히 통과했다 — `1/7 Selectoin` 이 그 예다.
                    # 모르는 이름이면 번호를 비교할 수조차 없으니 그것을 말한다.
                    bad.append("%s:%d: `%s %s` 의 단계 이름이 stages.json 에 없다 "
                               "(오타이거나 사라진 단계다). 실제: %s"
                               % (rel, i, got, name, ", ".join(sorted(real))))
                elif got != want:
                    bad.append("%s:%d: 단계 번호가 stages.json 과 다르다 — "
                               "`%s %s` 라고 적혀 있으나 실제는 %s"
                               % (rel, i, got, name, want))
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
