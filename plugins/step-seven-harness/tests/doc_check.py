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
import json
import os
import re
import glob
import sys

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                       else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "..", ".."))
def _docs():
    """검사할 문서를 **찾는다.** 손목록이면 새 문서가 조용히 미검사로 남는다.

    실제로 그랬다: `skills/auto-skip-{on,off}/SKILL.md` 두 개가 목록에 없어서
    `9/9 Nonexistent` 같은 단계 오타와 깨진 링크를 넣어도 rc=0 이었다(4회차
    E-F10). `_msgs.engine_files` 가 엔진 파일 목록에서 같은 함정을 이미 고쳤는데
    문서 목록은 그대로였다.
    """
    out = ["README.md"]
    base = "plugins/step-seven-harness"
    for pat in ("README.md", "skills/*/SKILL.md", "templates/*.md",
                "commands/*.md", "docs/*.md"):
        out += sorted(glob.glob(os.path.join(REPO, base, pat)))
    got = [x if os.path.isabs(x) else os.path.join(REPO, x) for x in out]
    got = [os.path.relpath(x, REPO) for x in got if os.path.isfile(x)]
    if len(got) < 6:
        raise SystemExit("문서를 %d개밖에 못 찾았다 — 발견이 죽었다" % len(got))
    return tuple(dict.fromkeys(got))


DOCS = _docs()
LINK_RE = re.compile(r"\]\((?!https?:|mailto:)([^)#\s]+)")
# 대소문자를 가리지 않는다. `[A-Z][a-z]+` 만 잡으면 `1/7 selectoin` 같은 오타가
# regex 에 아예 안 걸려 조용히 통과했다 — 적대적 리뷰가 지적했다.
STAGE_RE = re.compile(r"(\d+)/(\d+)\s+([A-Za-z][A-Za-z]+)")
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
    cfg = json.load(open(os.path.join(REPO, STAGES_JSON), encoding="utf-8"))
    n = len(cfg["stages"])
    return {s["label"]: "%d/%d" % (i + 1, n) for i, s in enumerate(cfg["stages"])}


PLUG = "plugins/step-seven-harness"


def live_probe_count():
    """살아 있는 엔진의 탐침 수. **문서에 적힌 수를 여기에 맞춘다.**

    개수를 문서에 박고 검사하지 않으면 반드시 낡는다 — README 가 28/28 이라
    적고 있을 때 실제는 34/34 였다(4회차 E-F11).
    """
    import subprocess
    import tempfile
    d = tempfile.mkdtemp()
    eng = os.path.join(REPO, PLUG, "scripts/harness.py")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    subprocess.run([sys.executable, eng, "init"], cwd=d, capture_output=True)
    out = subprocess.run([sys.executable, eng, "status"], cwd=d,
                         capture_output=True, text=True).stdout
    m = re.search(r"자기검사[:  ]\s*(\d+)/(\d+)", out)
    if not m:
        raise SystemExit("살아 있는 엔진에서 탐침 수를 세지 못했다")
    return int(m.group(2))


def main():
    bad = []
    total_links = total_stages = 0
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
                total_stages += 1
                got, name = "%s/%s" % (m.group(1), m.group(2)), m.group(3)
                # 이름 비교도 대소문자를 가리지 않는다
                low = {k.lower(): (k, v) for k, v in real.items()}
                want = real.get(name) or (low.get(name.lower()) or (None, None))[1]
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
        total_links += len(LINK_RE.findall(body))
        for target in LINK_RE.findall(body):
            if not os.path.exists(os.path.normpath(os.path.join(base, target))):
                bad.append("%s: 링크가 가리키는 것이 없다 — %s" % (rel, target))

        print("  %-52s 산문 %3d줄, 중복 %d, 링크 %d"
              % (rel, len(prose_lines(path)), len(dups), len(LINK_RE.findall(body))))

    # 훅 예산과 SQLite 잠금 대기가 어긋나면 잠금을 기다리다 프로세스가 강제 종료되고,
    # 그러면 fail-open 경고조차 나오지 않는다 — 사용자는 게이트가 꺼진 줄도 모른다.
    # 두 값은 **다른 파일**에 있으므로 여기서 대조한다.
    sys.path.insert(0, os.path.join(REPO, "plugins/step-seven-harness/scripts"))
    import harness as eng
    hooks = json.load(open(os.path.join(REPO, "plugins/step-seven-harness",
                                        "hooks", "hooks.json"), encoding="utf-8"))
    touts = [hk.get("timeout") for grp in hooks["hooks"].values()
             for entry in grp for hk in entry["hooks"]]
    off = [x for x in touts if x != eng.HOOK_TIMEOUT_S]
    print("  hooks.json timeout %d개 = HOOK_TIMEOUT_S(%d)   %s"
          % (len(touts), eng.HOOK_TIMEOUT_S, "ok" if not off else "FAIL %s" % off))
    if off:
        bad.append("hooks.json 의 timeout %s 가 HOOK_TIMEOUT_S(%d) 와 다르다"
                   % (off, eng.HOOK_TIMEOUT_S))
    # PreToolUse matcher 가 엔진이 판정하는 도구를 덮는가. 스모크 검사는 훅 JSON 을
    # 엔진 stdin 에 직접 먹이므로 matcher 를 지나지 않는다 — 이 한 줄을 지워도
    # 스위트는 초록이고 실제로는 모든 게이트가 사라진다.
    pre = [e for grp, entries in hooks["hooks"].items() if grp == "PreToolUse"
           for e in entries]
    covered = set()
    for e in pre:
        covered |= set((e.get("matcher") or "").split("|"))
    need = set(eng.WRITE_TOOLS) | {"Bash"}
    gap = sorted(need - covered)
    print("  PreToolUse matcher 가 판정 대상을 덮는다        %s"
          % ("ok" if not gap else "FAIL %s" % gap))
    if gap:
        bad.append("hooks.json 의 PreToolUse matcher 에 %s 가 없다 — 그 도구는 훅이 "
                   "아예 뜨지 않아 게이트가 통째로 사라진다" % ", ".join(gap))

    fits = eng.DB_WAIT_S * 2 <= eng.HOOK_TIMEOUT_S
    print("  잠금 대기(%ds)가 훅 예산(%ds) 안에 든다        %s"
          % (eng.DB_WAIT_S, eng.HOOK_TIMEOUT_S, "ok" if fits else "FAIL"))
    if not fits:
        bad.append("DB_WAIT_S(%d) 가 훅 예산(%d)에 비해 크다 — 잠금 대기 중 "
                   "강제 종료되면 fail-open 경고도 못 낸다"
                   % (eng.DB_WAIT_S, eng.HOOK_TIMEOUT_S))

    # 추출이 죽으면 "문제 없음" 이 된다. `STAGE_RE`·`LINK_RE` 를 아무것도 안 맞는
    # 정규식으로 바꿔도 초록이었다 — 이 파일의 존재 이유가 통째로 꺼져도 티가 안 났다.
    if total_links < 5:
        bad.append("링크를 %d개밖에 못 찾았다 — LINK_RE 가 깨졌다" % total_links)
    # 개수를 박지 않는다. "추출이 살아 있는가" 만 본다 — 0 이면 정규식이 죽은 것이다.
    if total_stages < 1:
        bad.append("단계 표기를 하나도 못 찾았다 — STAGE_RE 가 깨졌다")

    # --- 문서가 적은 수가 실제와 같은가 -----------------------------------
    # 4회차 E-F11: README 가 `자기검사: 28/28`(실제 34), `messages.ko.json, 470개`
    # (실제 506), 버전 `0.46.1`(실제 0.58.0) 을 말하고 있었다. **파생할 수 있는
    # 수만** 검사한다 — 파생할 수 없는 수(테스트 개수 등)는 애초에 산문에서 뺀다.
    # 검사할 수 없는 수를 적으면 그 수는 반드시 낡는다.
    ver = json.load(open(os.path.join(REPO, PLUG, ".claude-plugin/plugin.json"),
                         encoding="utf-8"))["version"]
    ncat = len(json.load(open(os.path.join(REPO, PLUG, "templates/messages.ko.json"),
                              encoding="utf-8")))
    nprobe = live_probe_count()
    for rel in DOCS:
        body = open(os.path.join(REPO, rel), encoding="utf-8").read()
        # 예시 출력은 `N/N` 으로 적는다 — 탐침을 하나 더할 때마다 문서를 고치게
        # 만드는 마찰이고, 그 마찰이 "검사를 문서에 맞추는" 방향을 만든다.
        # **숫자를 적었으면** 실제와 같아야 한다는 계약은 그대로 남긴다.
        for m in re.finditer(r"자기검사[:  ]\s*\d+/(\d+)", body):
            if int(m.group(1)) != nprobe:
                bad.append("%s: `자기검사 …/%s` 인데 실제 탐침은 %d개다"
                           % (rel, m.group(1), nprobe))
        for m in re.finditer(r"messages\.ko\.json`?,?\s*(\d+)개", body):
            if int(m.group(1)) != ncat:
                bad.append("%s: 카탈로그를 %s개라 적었는데 실제는 %d개다"
                           % (rel, m.group(1), ncat))
        for m in re.finditer(r"(\d+\.\d+\.\d+) 기준", body):
            if m.group(1) != ver:
                bad.append("%s: `%s 기준` 인데 plugin.json 은 %s 다"
                           % (rel, m.group(1), ver))

    if bad:
        print("\n문제 %d건" % len(bad))
        for b in bad:
            print("  " + b)
        return 1
    print("\n문제 없음")
    return 0


sys.exit(main())
