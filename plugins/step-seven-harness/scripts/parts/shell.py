"""Bash 명령 해석. **셸을 재구현하지 않는다** — 모르면 묻는다.

엔진이 이름공간을 맞춰 준다 — `parts/__init__.py` 참고.
"""


# Bash 가 파일을 건드릴 가능성이 있는 명령. 이 경우에만 경로 토큰을 훑는다.
BASH_MUTATORS = re.compile(
    r"(^|[;&|]\s*)(rm|mv|cp|mkdir|touch|tee|dd|truncate|install|ln|shred)\b"
    r"|>\s*\S|sed\s+-i"
    # find 는 -delete/-exec 로 쓴다. 읽기 명령으로 분류했다가 통째로 통과했다.
    r"|\s-delete\b|\s-exec(dir)?\b")


# 두 번째 토큰까지 잡는다. `loop new` 는 루프를 닫고 새로 만들므로 모든 단계
# 게이트를 우회하는데, 첫 토큰만 보면 subcommand 가 'loop' 로 잡혀 동의 판정이
# 아예 일어나지 않았다 — 승격 게이트가 그 구멍으로 그대로 새어나갔다.
CTRL_SUB2 = {"loop": ("new", "adopt"), "path": ("remove",)}


CTRL_NAMES = ("harness", "harness.py")


# 따옴표 안은 값이다. 먼저 한 토큰으로 뭉개야 `--reason "a b"` 의 'b' 가 위치
# 인자로 오인되지 않는다 — 그 오인이 subcommand 판정을 틀리게 만들었다.
QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')


# `BASH_SPLIT` 이 `;`·`\n` 을 구분자로 **지우면서**, 그것을 이스케이프하던
# 백슬래시가 세그먼트 끝에 홀로 남는다. `find … -exec echo {} \;` 와 줄 이어붙임
# (`\` + 개행)이 그 모양이다. 쪼개면서 우리가 낸 상처이므로 우리가 되돌린다.
# 이미 이스케이프된 백슬래시(`\\`)는 진짜 데이터라 건드리지 않는다.
DANGLING_BS_RE = re.compile(r"(?<!\\)\\$")


def sh_tokens(seg):
    """셸 토큰으로 쪼갠다. **못 쪼개면 None** — 지어내지 않는다.

    예전에는 `QUOTED_RE.sub("_", seg)` 로 따옴표 구간을 `_` 로 뭉갰다. 목적은
    `--reason "a b"` 의 `b` 가 위치 인자로 오인되지 않게 하는 것이었는데, 같은
    마스킹이 **따옴표로 감싼 실행 경로까지 지웠다.** 그래서
    `"...bin/harness" auto-skip on` 이 제어 명령으로 보이지 않아 **모든 동의
    게이트가 사라졌다** — 5차 리뷰가 찾았고 재현했다.

    근사를 고치지 않고 진짜 파서를 쓴다(`symtable` 때와 같은 판단). shlex 는
    따옴표를 소비하고 `--reason "a b"` 를 한 토큰으로 준다.

    그때 그 폴백을 **남겨 둔 것**이 다음 결함이었다. shlex 가 실패하면 같은
    마스킹이 다시 돌았고, 이번에는 지우는 대신 **지어냈다**:
    `"$HOME"/.claude/plugins` → `_/.claude/plugins` 라는 존재하지 않는 리포
    경로가 만들어져 그 가짜 경로에 단계 규칙이 적용됐다. 사용자는 저장소 밖을
    읽는 명령이 "신규 최상위 폴더를 만든다"로 거부되는 것을 봤다 — 차단보다
    **사유가 틀린 것**이 비쌌다(현장 보고 §3·§4, tech-writer).

    그리고 그 실패는 드문 일이 아니었다. `BASH_SPLIT` 이 `;` 로 먼저 쪼개므로
    `find … \\;` 는 **매번** 이 경로로 떨어졌다.

    이제 둘로 나눈다. 쪼개면서 우리가 만든 상처(끝에 남은 백슬래시)는 되돌려
    다시 파싱하고, 그래도 안 되면 **None 을 준다.** 모르면 지어내는 것이 아니라
    모른다고 말한다 — `bash_opaque`·`bash_unresolved` 와 같은 정책이고, 그 둘이
    ask 로 받는다. 판정을 못 하는 것이 **틀린 판정보다 낫다.**
    """
    try:
        return shlex.split(seg)
    except ValueError:
        pass
    repaired = DANGLING_BS_RE.sub("", seg)
    if repaired != seg:
        try:
            return shlex.split(repaired)
        except ValueError:
            pass
    return None


BASH_READERS_DEFAULT = ("cat", "less", "more", "head", "tail", "grep", "rg", "wc",
                        "file", "stat", "ls", "diff", "shasum", "md5", "md5sum", "cut")


BASH_INTERPRETERS_DEFAULT = ("python", "python3", "sh", "bash", "zsh", "env",
                             "exec", "node")


def bash_mutator_re(cfg):
    pat = cfg.at("bash.mutator_pattern")
    try:
        return re.compile(pat) if pat else BASH_MUTATORS
    except re.error:
        return BASH_MUTATORS       # 잘못된 정규식으로 게이트를 열지 않는다


def floor_hit(root, path):
    """바닥값에 걸리나 — **경로가 가리키는 것**으로 본다.

    `self_lock_hit` 은 문자열 검사다. 이것은 symlink 별칭까지 본다. 바닥값을 판정하는
    곳은 전부 이것을 써야 한다. 이유는 `rel_aliases` 에 적었다.
    """
    for rel in rel_aliases(root, path):
        if self_lock_hit(rel):
            return rel
    return None


# 이 이름들은 **무해하다고 선언할 수 없다.** `bash.readers`(읽기니까 건너뛴다) 와
# `bash.interpreters`(다음 인자는 실행 대상이니까 건너뛴다) 는 둘 다 "이 이름은
# 안전하다"는 설정이고, 둘 다 변경 명령을 넣으면 **설정만으로 자기 잠금이 풀린다.**
#
# readers 쪽은 `readers: ["rm"]` 로 확인해 막았는데 interpreters 쪽은 열려 있었다 —
# `interpreters: ["rm"]` 이면 `rm <엔진>` 의 경로가 '실행 대상'으로 건너뛰어진다.
# 5차 리뷰가 찾았다. 같은 결함을 두 번 겪었으므로 **목록을 하나로 합친다.** 앞으로
# 세 번째 '무해 선언' 설정이 생겨도 같은 바닥을 공유한다.
NEVER_BENIGN = ("rm", "mv", "cp", "dd", "tee", "truncate", "shred", "install",
                "ln", "sed", "mkdir", "touch", "find", "chmod", "chown", "sqlite3")


def benign_head(cfg, key, head, default=()):
    """`head` 가 이 '무해 선언' 목록에 있고, 위장이 아닌가.

    설정은 잠금을 **푸는** 방향으로는 쓰이지 않는다.
    """
    return head in cfg.seq("bash." + key, default) and head not in NEVER_BENIGN


BASH_SPLIT = re.compile(r"\|\||&&|[;&|\n]")


# 따옴표로 감싼 히어독 구분자 (`<<'EOF'`·<<"EOF"`). 그 본문은 셸이 아무것도
# 펼치지 않는 리터럴 데이터다.
HEREDOC_QUOTED_RE = re.compile(r"<<-?\s*(['\"])(\w+)\1")


def strip_quoted_heredocs(cmd):
    """따옴표 구분자 히어독의 **본문**을 지운다.

    BASH_SPLIT 이 `\\n` 으로 쪼개므로 히어독 본문의 각 줄이 명령 세그먼트로
    판정됐다 — `cat > tests/run.sh <<'EOF'` 로 스크립트를 쓰는 것이 **본문
    내용** 때문에 거부됐다(6회차). 쓰기 대상은 리다이렉트(`> tests/run.sh`)가
    이미 말하고 있고, 따옴표 구분자 본문은 확장이 없는 데이터라 명령이 아니다.
    따옴표 없는 히어독은 `$()` 가 본문에서 실행되므로 그대로 둔다(보수적)."""
    if "\n" not in cmd or not HEREDOC_QUOTED_RE.search(cmd):
        return cmd
    out, skip_until = [], None
    for ln in cmd.split("\n"):
        if skip_until is not None:
            if ln.strip() == skip_until:
                skip_until = None
            continue
        out.append(ln)
        m = HEREDOC_QUOTED_RE.search(ln)
        if m:
            skip_until = m.group(2)
    return "\n".join(out)


ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# 대상이 명령 문자열이 아니라 **실행 결과 안**에 있는 파괴. `find . -name harness.db
# -delete` 는 토큰에 보호 경로가 없어 어떤 문자열 검사도 지나간다.
#
# 특정하려 들지 않는다 — 그 방향은 셸 재구현이고, 근사는 이미 여러 번 틀렸다.
# **모른다는 사실 자체가 판정이다:** 막지도(정상 정리가 막힌다) 통과시키지도(구멍이다)
# 않고 사람에게 묻는다. 하네스에 이미 있는 어휘(`ask`)를 쓴다.
#
# 이것은 경계가 아니라 **가시성**이다. 경계는 바닥값과 래퍼 무결성이다.
BASH_OPAQUE = (
    (re.compile(r"\bfind\b(?=.*\s-(?:delete|exec|execdir)\b)"),
     "find 가 **찾아낸 것**을 지우거나 그것에 명령을 실행한다"),
    (re.compile(r"\bxargs\b(?=.*\b(?:rm|mv|tee|truncate|shred|sed|chmod)\b)"),
     "xargs 가 **넘겨받은 목록**을 대상으로 삼는다"),
)


# 바닥값 경로의 **문자열 형태**. 글롭을 떼고 접두사만 남긴다.
FLOOR_TEXT = tuple(sorted({p.rstrip("*").rstrip("/") for p in SELF_LOCK}, key=len))


FLOOR_RE = re.compile("|".join(re.escape(p) for p in FLOOR_TEXT))


# 경로 토큰의 **나머지**를 먼저 삼킨다. `FLOOR_TEXT` 는 짧은 것부터라
# `.claude/harness/bin` 이 먼저 맞고, 그 뒤 `/harness` 가 남는다.
WORD_RE = re.compile(r"""[^\s'"]*['"]?\s+([A-Za-z][\w-]*)(?:\s+([A-Za-z][\w-]*))?""")


def _invocation(cmd, at):
    """`at` 위치에서 이어지는 것이 **하네스 호출**인가.

    `<래퍼> status`, `python3 <엔진> advance`, `sh -c '<래퍼> skip …'` 은 전부
    실행이지 언급이 아니다. 토큰 위치로 가르려 했더니 `sh -c '<래퍼> advance'`
    가 과잉 차단됐다(실측). "무엇이 하네스 호출인가" 의 답은 `ctrl_known` 이
    이미 갖고 있다 — **답이 둘이면 갈린다.** 그 답을 다시 쓴다.
    """
    m = WORD_RE.match(cmd, at)
    if not m:
        return False
    one, two = m.group(1), m.group(2)
    return ctrl_known(one) or (bool(two) and ctrl_known("%s %s" % (one, two)))


def floor_named(cfg, cmd):
    """명령 **원문**이 바닥값을 이름으로 부르나. 그 이름 또는 None.

    왜 원문인가. 토큰 분석은 셸이 하는 일을 다시 구현해야 하고, 4회차 리뷰가
    그것을 세 방향에서 뚫었다 — 전부 실행까지 재현됐다:

        cp evil "$(pwd)/.claude/harness/bin/harness" && <래퍼> status
        cp evil $'.claude/harness/bin/harness'
        python3 -c "open('.claude/harness/harness.db','w')"

    셋 다 토큰으로는 경로가 아니다. 명령 치환·ANSI-C 인용·인터프리터 인라인
    코드를 `sh_expand` 가 펼치지 않기 때문이다. 그런데 **문자열에는 그대로
    들어 있다.** 확장을 하나씩 구현하는 길은 목록을 늘리는 길이고, 그 목록에는
    항상 다음 항목이 남는다.

    막지 않는다 — 하네스에 이미 있는 어휘로 **묻는다**. `bash_opaque` 와 같은
    정책이고 이유도 같다: 모르면 통과가 아니라 물음이다. 바닥값을 이름으로
    부르는 명령은 드물다. 드문 것을 사람이 한 번 보는 비용은 작다.

    **실행은 언급이 아니다.** 래퍼를 부르는 것(`<래퍼> status`)과 엔진을 돌리는
    것(`python3 <엔진> status`)은 정상 동작이라 세지 않는다.
    """
    for seg in BASH_SPLIT.split(cmd):
        # 읽기는 막지 않는다 — `cat <DB>` 까지 물으면 마찰이고, 마찰은 게이트를 끈다.
        # **엔진 기본값만** 면제한다. `readers` 설정으로는 이 면제를 넓힐 수 없다:
        # 그 길을 열면 `readers: ["rsync"]` 한 줄로 바닥값이 다시 열린다(4회차 B#1).
        # 리다이렉트가 붙으면 읽기가 아니다 (`cat x > <래퍼>`).
        toks = re.findall(r"\S+", seg)
        i = 0
        while i < len(toks) and ASSIGN_RE.match(toks[i]):
            i += 1
        if (i < len(toks)
                and os.path.basename(toks[i].strip("\"'")) in BASH_READERS_DEFAULT):
            # 읽기 명령은 **리다이렉트 대상만** 쓴다. `>` 유무로 가르면
            # `ls … 2>&1` 이 변경 명령이 된다 (2차 현장 보고 §1).
            wrote = redirect_targets(toks[i:])
            if not wrote:
                continue
            # 쓰는 곳이 있으면 그 자리만 본다.
            if not any(FLOOR_RE.search(w) for w in wrote):
                continue
        for m in FLOOR_RE.finditer(seg):
            if not _invocation(seg, m.end()):
                return m.group(0)
    return None


def floor_verdict(cfg, root, cmd):
    """Bash 명령이 바닥값을 건드리나. `(판정, 근거)` 또는 `(None, None)`.

    **바닥값에 대한 답은 하나여야 한다.** 처음에는 원문 감시(`floor_named`)를
    경로 분석(`bash_protected_hit`) **옆에** 붙였다. 그러면 같은 질문에 판정기가
    둘이고, 둘 중 하나만 게이트의 `entry` 에 들어가 나머지는 자기증명 밖에 남는다
    — ①에서 고친 바로 그 모양을 내가 다시 만든 것이다.

    두 판정은 **확신의 차이**이지 다른 질문이 아니다:
      · 경로를 특정했다  → `deny`  (대상이 분명하다)
      · 원문에만 보인다  → `ask`   (셸 치환·인용·인라인 코드로 실행 시점에 정해진다)
    하나로 묶으면 호출자도 하나, 탐침도 하나다.
    """
    hit, unsure = bash_protected_scan(cfg, root, cmd)
    if hit:
        return "deny", hit, None
    if unsure:
        # 바닥값을 담은 폴더가 대상인데 변경 명령인지 알 수 없다(`myrm -rf
        # .claude`). 모르면 통과도 차단도 아니라 **묻는다** — deny 로 두면
        # `git add .claude` 같은 읽기 명령이 막다른 길에 갇혔다(6회차 실측).
        return "ask", unsure, t(
            "이 명령이 하네스 자신을 담은 폴더(%s)를 대상으로 삼는데, 내용을 "
            "바꾸는 명령인지 하네스가 판정하지 못했다. 바닥값은 엔진과 상태이고 "
            "사전 승인된 래퍼를 포함하므로 사람이 봐야 한다. 읽기라면 허용해도 "
            "된다.") % unsure
    named = floor_named(cfg, cmd)
    if named:
        return "ask", named, t(
            "이 명령의 원문에 하네스 바닥값(%s)이 보이는데, 무엇을 대상으로 "
            "삼는지 하네스가 특정하지 못했다. 셸 치환·인용·인터프리터 인라인 "
            "코드가 섞이면 실행 시점에야 정해진다. 바닥값은 엔진과 상태이고 "
            "사전 승인된 래퍼를 포함하므로 사람이 봐야 한다. 경로를 그대로 "
            "적으면 하네스가 대신 판정한다.") % named
    return None, None, None


# 하네스가 **펼치지 못하는** 셸 문법. `sh_expand` 는 글롭만 편다.
EXPAND_RE = re.compile(r"\$\(|`|\$\{|\$[A-Za-z_]|\$'")


# 인터프리터에 코드를 인라인으로 넘기는 플래그. **묶음 단축옵션까지 본다** —
# `bash -lc "…"` 는 `-c` 와 같은 일을 하는데 `-c` 만 찾으면 안 보인다. 안 보이면
# 그 안은 평평한 경로 목록으로 읽히고, 실행 대상이 변경 대상이 됐다(실측:
# `bash -lc "<래퍼> auto-skip on"` → "하네스 자신은 변경할 수 없다" 로 거절).
INLINE_FLAG_RE = re.compile(r"^-(?:[A-Za-z]*c|e|-eval)$")


# 그 형태가 명령 안에 있나. 그 안은 셸 토큰이 아니다.
INLINE_CODE_RE = re.compile(r"(?:^|\s)-(?:[A-Za-z]*c|e|-eval)(?:\s|=|$)|<<\s*\w")


# 하네스가 자기 영역이라고 부르는 **낱말**. 경로 상수에서 뽑는다 — 손으로 적으면
# `HARNESS_DIR` 이 바뀔 때 조용히 어긋난다. 앞의 `.` 은 떼고, 마디 그대로 쓴다.
TERRITORY_RE = re.compile(
    "|".join(re.escape(p.lstrip(".")) for p in HARNESS_DIR.split(os.sep) if p),
    re.I)


def bash_unresolved(cfg, cmd):
    """이 명령이 무엇을 쓸지 **내가 해석할 수 있나.** 못 하면 그 이유.

    4회차에 원문 감시(`floor_named`)를 만들며 "확장을 하나씩 구현하는 길은
    목록을 늘리는 길" 이라고 적었다. 맞았지만 **원문 매칭 자체가 또 하나의
    근사**였다 — 5회차가 셋으로 뚫었고 전부 실행까지 재현됐다:

        D=$(printf %s .claude/harness); python3 -c "open('$D/harness.db','w')"
        cp evil "$(echo LmNsYXVkZS9oYXJuZXNz… | base64 -d)"
        S=$(printf %s .claude/harness); ln -s "$S/bin/${B}ess" hh && ./hh auto-skip on

    조립하면 원문에도 안 나타난다. 문자열을 더 잘 보려는 방향에는 끝이 없다.

    그래서 묻는 것을 바꾼다. **"바닥값이 보이나" 가 아니라 "내가 이 세그먼트를
    해석할 수 있나."** 못 하면 막지도 통과시키지도 않고 사람에게 넘긴다 —
    `bash_opaque` 와 같은 정책이고, 이 저장소가 세 번 뚫린 이유가 전부
    "모르면 통과" 였다.

    범위를 좁게 잡는다. 읽기는 묻지 않고, 변경하지 않는 세그먼트도 묻지 않는다.
    `git log --oneline "$REF"` 는 지나가고 `cp x "$D/y"` 는 묻는다.
    """
    mut = bash_mutator_re(cfg)
    for seg in BASH_SPLIT.split(strip_quoted_heredocs(cmd)):
        toks = re.findall(r"\S+", seg)
        i = 0
        while i < len(toks) and ASSIGN_RE.match(toks[i]):
            i += 1
        if i >= len(toks):
            # `VAR=$(...)` 만 있는 세그먼트. 대입 자체는 아무것도 안 쓴다.
            continue
        head = os.path.basename(toks[i].strip("\"'"))
        # **제어 판정에는 제대로 쪼갠 토큰을 준다.** 위의 `re.findall` 은 따옴표를
        # 모르므로 `sh -c '<래퍼> status'` 의 인라인 코드가 `'<래퍼>` 에서 잘리고,
        # 그러면 조회 명령 하나가 "읽지 못하는 코드"로 되물어진다. 예전에는 잘린
        # 조각을 옛 폴백이 다시 뭉개서 우연히 통했다 — 근사가 근사를 가린 자리다.
        ctrl_toks = sh_tokens(seg) or toks
        j = 0
        while j < len(ctrl_toks) and ASSIGN_RE.match(ctrl_toks[j]):
            j += 1
        # **인라인 코드라고 무조건 묻지 않는다 — 우리 영역을 언급할 때만 묻는다.**
        #
        # 이 물음은 5회차 적대적 리뷰가 실제 우회 셋을 재현해 만든 것이고, 그때는
        # 옳았다. 그런데 0.71.0 이 층을 가르면서 전제가 바뀌었다: Bash 의 단계별
        # 쓰기 규칙은 이제 막지 않으므로, 이 물음이 지키는 것은 **바닥값 하나**다.
        #
        # 그 바닥값을 이름 그대로 부르면 `floor_named` 가 이미 잡는다 — 더 정확한
        # 사유까지 달아서. 그래서 뭉뚱그린 물음의 **유일한 고유 기여**는 경로를
        # 쪼개 조립하는 경우뿐이었는데(`'.claude/harn'+'ess'`), 그건 규칙을
        # 지키려는 모델이 실수로 하는 일이 아니다. README 가 범위 밖이라고
        # 선언한 상대다.
        #
        # 그 대가로 `python3 -c "print(1+1)"` 까지 매번 사람을 세웠다 — auto mode
        # 를 켠 사용자에게도(현장 관측). **선언하지 않은 보증을 위해 모든 인라인
        # 스크립트가 비용을 내고 있었다.**
        #
        # 원문에 우리 영역 낱말이 보일 때만 묻는다. 조립도 대부분 남는다 —
        # `'.claude/harn'+'ess'` 에는 `claude` 가, `'.cla'+'ude/harness'` 에는
        # `harness` 가 그대로 있다. 낱말까지 전부 쪼개는 난독화는 놓치고,
        # 그것은 알고 놓는다.
        #
        # **세그먼트가 아니라 명령 전체를 본다.** `BASH_SPLIT` 은 인라인 코드
        # **안의** `;` 로도 쪼개므로, 영역 낱말이 인터프리터 머리와 다른 조각에
        # 떨어진다 — `python3 -c "import json; …open('.claude/…')"` 가 그 모양이고,
        # 세그먼트만 보면 그냥 지나갔다(회귀 단정이 잡았다). 넓게 보는 쪽이
        # 안전한 방향이고, 이 물음의 값은 정밀도가 아니라 존재다.
        if (benign_head(cfg, "interpreters", head, BASH_INTERPRETERS_DEFAULT)
                and INLINE_CODE_RE.search(seg) and TERRITORY_RE.search(cmd)
                and not ctrl_exec_seg(ctrl_toks[j:])):
            return t("인터프리터에 인라인으로 넘긴 코드는 하네스가 읽지 못한다")
        if not mut.search(seg.strip()):
            continue
        # 읽기 명령은 리다이렉트 **대상만** 쓴다. 쓰는 곳이 없으면 물을 것도 없다
        # — `ls … 2>&1` 을 되묻는 것이 §1 오탐의 다른 얼굴이다.
        if benign_head(cfg, "readers", head, BASH_READERS_DEFAULT) \
                and not redirect_targets(toks[i:]):
            continue
        # 쪼개지 못한 변경 세그먼트. `bash_writes` 는 여기서 아무 대상도 모으지
        # 않으므로, 받아 주지 않으면 단계 규칙이 조용히 사라진다.
        if sh_tokens(seg) is None:
            return t("따옴표가 맞지 않아 이 명령을 토큰으로 쪼갤 수 없다")
        if EXPAND_RE.search(seg):
            return t("셸 확장이 섞여 무엇을 쓸지 실행 시점에야 정해진다")
    return None


def ctrl_exec_seg(toks, depth=0):
    """이 세그먼트가 **하네스를 실행**하나. 그러면 인라인 코드가 아니다.

    `sh -c '<래퍼> status'` 의 하네스 이름은 `toks[:2]` 밖 — 인라인 코드 **안**에
    있다. 밖만 보면 조회 명령 하나가 "읽지 못하는 코드" 로 되물어진다. 껍데기를
    벗기고 같은 질문을 다시 한다.
    """
    if any(os.path.basename(x.strip("\"'")) in CTRL_NAMES for x in toks[:2]):
        return True
    code = _inline_code(toks) if depth < INLINE_DEPTH else None
    inner = sh_tokens(code) if code else None
    # 못 쪼개면 "하네스 실행이 아니다" — 이 면제를 확신 없이 주지 않는다.
    return bool(inner) and ctrl_exec_seg(inner, depth + 1)


def bash_opaque(cmd):
    """대상을 특정할 수 없는 파괴인가. 그 이유 또는 None."""
    for rex, why in BASH_OPAQUE:
        if rex.search(cmd):
            return t(why)
    return None


def bash_protected_scan(cfg, root, cmd, depth=0):
    """Bash 명령이 보호 경로를 **대상으로** 삼는지 → `(확정 hit, 불확실 hit)`.

    check_write 는 Write/Edit 만 본다. Bash 는 `rm`, `sed -i`, 리다이렉트,
    `sqlite3 ... UPDATE` 로 같은 파일을 바꿀 수 있었고 그 경로는 검사되지 않았다.

    확정 hit 은 deny 감이고, 불확실 hit(바닥값을 담은 폴더가 대상인데 변경
    명령인지 모름)은 ask 감이다 — 판정은 `floor_verdict` 가 내린다.
    """
    # 바닥값 ∪ 설정. 설정을 비워도(`protected_paths: []`) 바닥값은 남는다 —
    # 예전에는 여기서 `if not pats: return None` 으로 통째로 꺼졌다.
    pats = protected_pats(cfg)
    floor = set(SELF_LOCK)

    # **본문은 명령이 아니라 데이터다.** `bash_writes`·`bash_unresolved` 는 이미
    # 이렇게 보는데 여기만 빠져 있었다. 그래서 `BASH_SPLIT` 이 `\n` 으로 쪼갤 때
    # 히어독 본문의 각 줄이 명령 세그먼트가 됐고, **본문에 든 문자열** 때문에
    # 프로그램 실행이 거부됐다(3차 현장 보고 §2, 실측):
    #
    #     python3 - <<'PY'
    #     needle = 'ls -la .claude/harness/ 2>&1'      ← 이 줄이 명령으로 읽혔다
    #     print(repr(needle))
    #     PY
    #     → "하네스 자신(.claude/harness)은 Bash 로도 변경할 수 없다"
    #
    # 리다이렉트를 뺀 같은 스크립트는 지나갔다 — `2>&1` 이 그 줄을 '변경 명령'
    # 으로 만들었기 때문이다. 0.71.0 이 명령줄 쪽 리다이렉트는 고쳤는데 본문
    # 쪽에는 그 고침이 닿지 않았다.
    #
    # 이건 추측을 하나 더 다듬는 것이 아니다. **문법이 답을 갖고 있다** —
    # 따옴표 구분자 히어독은 셸이 확장조차 하지 않는 리터럴이다. 그래서 새 규칙을
    # 만들지 않고 이미 있는 함수를 빠진 자리에 적용한다.
    #
    # 경계는 약해지지 않는다. 본문이 **진짜로** 바닥값을 건드리면
    # (`python3 - <<'PY'` 안에서 `open('.claude/harness/harness.db','w')`)
    # 원문 감시(`floor_named`)가 명령 전체 텍스트를 따로 훑어 ask 로 받는다.
    # 층이 겹쳐 있어서 이쪽을 비워도 바닥이 남는다 — 실측으로 확인했다.
    cmd = strip_quoted_heredocs(cmd)
    mutating = bool(bash_mutator_re(cfg).search(cmd))
    # 바닥값 containment 는 **설정** mutator 를 믿지 않되(아래 주석) 변경 여부
    # 자체는 봐야 한다 — 안 보면 `git add .claude`·`du -sh .claude` 같은 읽기
    # 명령까지 막았고, README 가 안내하는 커밋 절차가 첫 명령에서 거부됐다
    # (6회차 실측). 코드 상수와 설정의 **합집합**이므로 설정을 좁혀 잠금을 푸는
    # 방향은 여전히 막혀 있다.
    floor_mut = mutating or bool(BASH_MUTATORS.search(cmd))
    unsure = []
    # `>|경로` 의 `|` 를 BASH_SPLIT 이 파이프로 보고 쪼개면 경로가 다음 세그먼트의
    # **명령어 자리**로 밀려 '실행 대상' 으로 건너뛰어진다. 먼저 떼어놓는다.
    cmd = cmd.replace(">|", "> ")

    def candidates(tok):
        """`of=경로`, `>|경로` 처럼 붙어 오는 형태까지 경로로 본다.

        둘 다 실제로 통과했다: `dd if=/dev/null of=<db>`, `printf x >|<LEARNED>`.
        """
        out = []
        for raw in sh_expand(root, tok):      # 셸이 펼치는 것을 우리도 편다
            out.append(raw.lstrip("<>|&"))
            if "=" in raw:
                out.append(raw.split("=", 1)[1].lstrip("<>|&"))
        return [it for it in out if it]

    def protected(tok):
        use = pats
        for cand in candidates(tok):
            # 문자열 그대로와 symlink 를 푼 것 **둘 다** 본다 — 별칭 경로로 바닥값을
            # 건드리는 것을 막는다. 이유는 `rel_aliases` 에 적었다.
            for rel in rel_aliases(root, cand) or []:
                # 리포 루트(`.`)는 바닥값을 **담고 있다.** 예전에는 여기서 건너뛰었고,
                # 그래서 `find . -name harness.db -delete` 가 바닥값을 지나갔다.
                # 담고 있는 것에 대한 판정은 아래 containment 검사로 내려보낸다.
                if rel == ".":
                    continue
                if any(glob_match(rel, p) for p in use):
                    return rel
                # 바닥값은 대소문자를 무시하고도 본다 (macOS 에서 BIN == bin 이다).
                if self_lock_hit(rel):
                    return rel
                # 보호 경로를 **담고 있는** 디렉터리도 변경 명령의 대상이 될 수 없다.
                # `find .claude/harness -delete` 나 `rm -rf .claude` 가 그 경우다.
                # 바닥값에 대해서는 설정 mutating 판정만 믿지 않는다 — `mutator_pattern`
                # 을 `(?!)` 같은 '문법은 맞고 아무것도 안 맞는' 정규식으로 두면 이
                # 검사가 통째로 꺼졌다. 그래서 코드 상수를 합친 `floor_mut` 로 본다.
                low = rel.lower()
                if any(p.lower().startswith(low + "/") for p in floor):
                    if floor_mut:
                        return rel
                    # 변경 명령이 아니면(또는 모르는 명령이면) 확정하지 않고
                    # 모아 둔다 — `floor_verdict` 가 ask 로 올린다.
                    if rel not in unsure:
                        unsure.append(rel)
                if mutating and any(p.startswith(rel + "/") for p in use):
                    return rel
        return None

    for seg in BASH_SPLIT.split(cmd):
        toks = re.findall(r"\S+", seg)
        if not toks:
            continue
        # 앞의 `VAR=값` 은 명령이 아니라 대입이다. **값 안의 경로를 검사하고** 넘긴다.
        # 예전에는 버리기만 해서 `DB=<보호경로>; rm $DB` 가 그대로 지나갔다 — 주석은
        # "검사 대상으로 남긴다" 였는데 코드가 달랐다.
        while toks and ASSIGN_RE.match(toks[0]):
            hit = protected(toks[0])
            if hit:
                return hit, None
            toks = toks[1:]
        if not toks:
            continue
        head = os.path.basename(toks[0].strip("\"'"))
        # 리다이렉트가 있으면 읽기 명령도 쓰기가 된다 (`cat x > 엔진`).
        # `readers` 에 `rm` 을 넣어 잠금을 우회한 것을 확인했으므로, 읽기로 분류된
        # 세그먼트도 **바닥값만은** 검사한다.
        # `readers` 에 `rm` 을 넣어 잠금을 우회한 것을 확인했다. 그래서 **변경 명령
        # 이름은 읽기로 선언될 수 없다**(NEVER_READERS). 그 위장만 막으면 되고,
        # 읽기는 원래대로 통째로 건너뛴다 — `cat` 으로 DB를 읽는 것까지 막으면
        # 과잉 차단이고, 마찰은 게이트를 끄게 만든다.
        if benign_head(cfg, "readers", head, BASH_READERS_DEFAULT):
            # 읽기 명령이 쓰는 것은 **리다이렉트 대상뿐이다.** 그 대상만 검사하고
            # 인자는 원래 성격(읽기)대로 둔다. `>` 유무로 통째로 가르면
            # `ls -la .claude/harness/ 2>&1` 이 "하네스를 변경한다" 가 됐다
            # (2차 현장 보고 §1). `cat x > <엔진>` 은 그 대상이 잡아낸다.
            for w in redirect_targets(toks):
                hit = protected(w)
                if hit:
                    return hit, None
            continue
        # 인터프리터도 같은 검사를 받는다. `interpreters: ["rm"]` 로 다음 인자를
        # '실행 대상'으로 건너뛰게 만들면 설정만으로 잠금이 풀렸다 — 5차 리뷰.
        interp = benign_head(cfg, "interpreters", head,
                             BASH_INTERPRETERS_DEFAULT) and len(toks) > 1
        # **인라인 코드는 경로 목록이 아니라 또 하나의 명령이다.** 평평한 토큰으로
        # 훑으면 그 안의 **실행 대상**이 변경 대상으로 읽힌다 — `sh -c '<래퍼>
        # status'` 가 "하네스 자신은 Bash 로도 변경할 수 없다" 로 거절됐다(출시
        # 시나리오 ③ 실측). 조회 명령인데 변경으로 판정한 것이다.
        #
        # `floor_named` 는 이미 "실행은 언급이 아니다" 를 알고 `_invocation` 으로
        # 가른다. 여기만 그것을 몰랐다. 안쪽에 같은 질문을 다시 던지면 답이 하나가
        # 된다 — 실행이면 지나가고, `sh -c 'rm <DB>'` 는 그대로 거절된다.
        parsed = sh_tokens(seg)
        if interp and parsed is None:
            # 인터프리터인데 쪼개지 못했다. 평평한 토큰으로 훑으면 인라인 코드
            # **안**의 실행 대상이 변경 대상으로 읽힌다. 여기서 지어내지 않고
            # 넘긴다 — 원문 감시(`floor_named`)와 `bash_unresolved` 가 뒤에서
            # 같은 명령을 ask 로 받고, 그 둘은 **맞는 사유**를 말할 수 있다.
            continue
        code = _inline_code(parsed) if interp else None
        if code is not None:
            if depth < INLINE_DEPTH:
                hit, uns = bash_protected_scan(cfg, root, code, depth + 1)
                if hit:
                    return hit, None
                if uns and uns not in unsure:
                    unsure.append(uns)
            continue
        for tok in toks[2 if interp else 1:]:
            hit = protected(tok)
            if hit:
                return hit, None
    return None, (unsure[0] if unsure else None)


def bash_protected_hit(cfg, root, cmd, depth=0):
    """`bash_protected_scan` 의 확정 hit 만. 자기검사가 이 이름으로 묻는다."""
    return bash_protected_scan(cfg, root, cmd, depth)[0]


# 인라인 코드 안의 인라인 코드까지는 본다. 그보다 깊으면 원문 감시(`floor_named`)와
# `bash_unresolved` 가 받는다 — 둘 다 '모르면 묻는다' 로 끝나므로 통과가 아니다.
INLINE_DEPTH = 3


def _inline_code(toks):
    """인터프리터에 **코드로** 넘긴 문자열. 없으면 None.

    `INLINE_CODE_RE` 는 그것이 **있는지**를 보고, 여기서는 **무엇인지**를 꺼낸다.
    같은 어휘를 둘로 적지 않도록 플래그 모양은 `INLINE_FLAG_RE` 하나로 둔다.
    """
    for i, tok in enumerate(toks[1:], 1):
        if INLINE_FLAG_RE.match(tok):
            return toks[i + 1] if i + 1 < len(toks) else ""
        head, sep, rest = tok.partition("=")
        if sep and INLINE_FLAG_RE.match(head):
            return rest
    return None


# ------------------------------------------------- Bash 가 무엇을 쓰는가

# ## 왜 필요한가
#
# 쓰기 판정은 `check_write` 하나인데 **Write·Edit 만 그 문을 지났다.** Bash 는 바닥값만
# 받았고, 단계별 쓰기 규칙 일곱 중 여섯은 Bash 에 없었다 — `sed -i` 한 줄로 "단계마다
# 쓸 수 있는 곳이 다르다"는 약속이 통째로 우회됐다.
#
# ## 구조: 판정은 하나, 수집만 둘
#
# 갈리는 것은 **무엇을 모으는가**여야 하고, **어떤 판정을 적용하는가**는 갈려서는 안 된다.
#
#   바닥값   — 걸리면 대가가 전부다. 토큰을 하나도 빼지 않는다 (`bash_protected_hit`).
#   단계 규칙 — 과잉 차단은 마찰이고 마찰은 게이트를 끈다. 변경 세그먼트만 본다 (여기).
#
# 명령별 표를 두지 않는다. 무엇이 변경인지는 이미 `bash.mutator_pattern` 이 알고,
# 무엇이 읽기인지는 이미 `bash.readers` 가 안다. 새 어휘를 만들면 그 표가 현실과
# 어긋나는 자리가 하나 더 생긴다.

REDIRECT_RE = re.compile(r"^\d*(>>?\|?|&>>?)$")


# 경로가 붙은 리다이렉트 (`>docs/y.md`, `2>err.txt`, `&>log`).
ATTACHED_REDIR_RE = re.compile(r"^(\d*>>?|&>>?)(.+)$")


def redirect_targets(toks):
    """이 세그먼트가 리다이렉트로 **쓰는 곳**. 파일 디스크립터 복제는 뺀다.

    `2>&1` 의 `&1` 은 파일이 아니라 다른 fd 다. `>&2` 도 같다.
    """
    out, k = [], 0
    while k < len(toks):
        tok = toks[k]
        if REDIRECT_RE.match(tok) and k + 1 < len(toks):
            if not toks[k + 1].startswith("&"):
                out.append(toks[k + 1])
            k += 2
            continue
        m = ATTACHED_REDIR_RE.match(tok)
        if m and not m.group(2).startswith("&"):
            out.append(m.group(2))
        k += 1
    return out


def reader_writes(cfg, toks):
    """읽기 명령인데 **리다이렉트로 무언가를 쓰나.** 그 대상들 (아니면 None).

    예전에는 세그먼트에 `>` 가 하나라도 있으면 읽기 면제를 통째로 껐다. 목적은
    `cat x > <엔진>` 이었는데, 그 한 글자가 **꼬리표까지** 잡았다:

        ls -la .claude/harness/ 2>&1        → "하네스 자신은 변경할 수 없다"
        ls -la .claude/harness/ 2>/dev/null → 같은 메시지

    파이프는 멀쩡한데 리다이렉트만 뒤집혔다. 그런데 `2>&1` 은 쓰는 곳이 아예
    없고, `2>/dev/null` 이 쓰는 곳은 `/dev/null` 이다 — 둘 다 `.claude/harness`
    와 무관하다. **리다이렉트는 명령의 성격을 바꾸지 않는다.** 쓰는 것은
    리다이렉트 대상이지 인자가 아니다(2차 현장 보고 §1).

    이 오탐의 값이 특히 비쌌다. 에이전트가 치는 명령에는 꼬리표가 거의 항상
    붙어서 **평소에** 걸렸고, "변경할 수 없다"는 메시지를 반복해서 받은 모델은
    **읽는 것조차 금지됐다고 학습**했다. 1차 보고 §5 의 "쪼개면 새어 나간다"는
    우회는 그 학습의 결과였다 — 오탐 하나가 우회 하나를 만들었다.
    """
    return redirect_targets(toks) or None


# 이 명령들은 **마지막 경로만** 바꾼다. `cp src/a.py /tmp/b` 가 src 를 쓴다고 보면
# 읽기만 하는 명령이 거부되고, 그 오판이 곧 마찰이다. `sed`·`perl` 도 여기 있다 —
# 앞 인자는 파일이 아니라 식(`s/x/y/`)이다. `mv` 는 없다: 원본도 사라진다.
BASH_TARGET_LAST = ("cp", "ln", "install", "sed", "perl")


def sh_expand(root, tok):
    """셸이 실행 시점에 펼칠 것을 **우리도 펼친다.** 결과들(없으면 원래 토큰).

    glob 문자가 있는 토큰을 "해석할 수 없다" 며 통과시켰다. 그 한 줄이 실패 개방이었다 —
    `cp evil .claude/harness/bi?/harness` 한 글자로 바닥값·격리·래퍼 무결성이 전부
    뚫렸고, 사전 승인된 래퍼가 남의 코드로 바뀌어 실행되는 것까지 재현됐다.

    모르면 통과가 아니다. 셸과 **같은 확장**을 해서 결과를 본다. 아무것도 안 맞으면
    셸도 리터럴을 그대로 넘기므로 원래 토큰을 돌려준다.
    """
    tok = tok.strip("\"'")
    if not tok:
        return []
    if not any(ch in tok for ch in "*?["):
        return [tok]
    base = tok if os.path.isabs(tok) else os.path.join(root, tok)
    try:
        hits = sorted(globlib.glob(base))
    except OSError:
        hits = []
    return hits or [tok]


def _target(root, tok, sure):
    """이 토큰이 가리키는 리포 안 경로. 아니면 None.

    `sure` 는 리다이렉트 피연산자처럼 **문법이 경로임을 증명한** 자리다.
    나머지는 추측이므로, 있는 파일이거나 `/` 를 포함한 자리만 경로로 본다 —
    `chmod 755 f` 의 `755`, `sed -i s/a/b/ f` 의 `s/a/b/` 를 거르기 위해서다.
    """
    # 펼쳐지지 않은 `$VAR`·`~` 는 리포 경로가 **아니다** — 글자 그대로 이어붙이면
    # `mkdir "$HOME/.cache"` 가 "source 클래스에 쓸 수 없다" 는 엉뚱한 판정과
    # 메시지를 받았다(6회차 실측). 여기서 버리면 변경 세그먼트는
    # `bash_unresolved` 가 "셸 확장이 섞였다" 로 **묻는다** — 설계("해석 불가 →
    # ask")가 말하는 그 자리다.
    bare = tok.strip("\"'")
    if "$" in bare or "`" in bare or bare.startswith("~"):
        return None
    for one in sh_expand(root, tok):
        for rel in rel_aliases(root, one):
            if rel == ".":
                continue
            if sure or os.path.lexists(os.path.join(root, rel)):
                return rel
            # ## 여기 있던 예외 셋을 지웠다 — 고친 것이 아니라 **지운 것**이다
            #
            # `sure=False` 는 추측이다. 그 추측을 정확하게 만들려고 예외가 셋
            # 붙어 있었다: 콜론 든 마디 제외(`https:/`·`org/app:1.0`), `..` 든
            # 마디 제외(`origin/main..HEAD`), find 문법으로 자르기(`-name '.git'`).
            # 넷째가 오는 것은 시간 문제였다.
            #
            # 정확해질 수 없기 때문이다 — 셸이 무엇을 쓰는지는 정적으로 **결정
            # 불가능**하고, 같은 도구도 플래그로 규약이 뒤집힌다(`cp` 와 `cp -t`
            # 는 쓰는 인자가 반대다). 조사는
            # `.dev/shell-write-detection-is-undecidable.md`.
            #
            # 그래서 정확성 **요구**를 없앴다. 이 추측의 결과는 이제 아무도 막지
            # 않고 기록만 남긴다(`bash_write_seen`). 틀린 기록은 지저분할 뿐이라
            # 예외를 붙일 이유가 없다. 예외를 남겨 두면 "정확해야 한다"는 압력이
            # 남고, 그러면 다섯 번째가 붙는다.
            if "/" in rel:
                return rel
    return None


def bash_writes(cfg, root, cmd, proven_only=False):
    """이 명령의 **변경 세그먼트**가 대상으로 삼는 경로들 (순서 보존).

    확실하지 않은 것은 넣지 않는다. 여기서 빠진 것을 바닥값이 놓치지는 않는다.
    """
    out, mut = [], bash_mutator_re(cfg)

    def add(rel):
        if rel and rel not in out:
            out.append(rel)

    for seg in BASH_SPLIT.split(strip_quoted_heredocs(cmd).replace(">|", "> ")):
        toks = sh_tokens(seg)
        if toks is None:
            # 못 쪼갠 세그먼트에서는 **아무 대상도 수집하지 않는다.** 지어낸
            # 경로에 규칙을 적용하는 것이 §3·§4 였다. 통과가 아니다 —
            # `bash_unresolved` 가 같은 세그먼트를 ask 로 받는다.
            continue
        # **정규화해서 넘긴다.** `mutator_pattern` 은 명령 **전체**에 대해 정의됐고
        # `(^|[;&|]\s*)` 로 앵커돼 있다. BASH_SPLIT 은 구분자를 지우므로 세그먼트 앞에
        # 공백이 남고, 그러면 `^` 가 안 맞아 `a && touch x` 의 touch 가 통째로 샜다.
        # 어휘를 재사용하는 것은 옳았지만 **정의된 형태로 주어야** 한다.
        if not toks or not mut.search(seg.strip()):
            continue
        head = os.path.basename(toks[0].strip("\"'"))
        # 읽기 명령에 리다이렉트가 붙은 것(`cat a > b`)은 **b 만** 쓴다.
        reads_only = benign_head(cfg, "readers", head, BASH_READERS_DEFAULT)
        args, k = [], 1
        while k < len(toks):
            tok = toks[k]
            if REDIRECT_RE.match(tok) and k + 1 < len(toks):
                add(_target(root, toks[k + 1], True))   # 문법이 경로임을 증명한다
                k += 2
                continue
            if tok in ("<", "<<", "<<<"):
                k += 2                                  # 입력은 읽는다
                continue
            m = ATTACHED_REDIR_RE.match(tok)
            if m:
                # `>docs/y.md` 처럼 경로가 **붙은** 리다이렉트. 낱개 토큰만 보면
                # 이 형태가 수집에서 통째로 빠져 단계 규칙 전체가 조용히
                # 우회됐다 — deny 도 ask 도 기록도 없이(6회차 실측). `2>&1` 은
                # fd 복제라 경로가 아니다.
                if not m.group(2).startswith("&"):
                    add(_target(root, m.group(2), True))
                k += 1
                continue
            if tok.startswith("of="):                   # dd
                add(_target(root, tok[3:], True))
            elif "=" not in tok.split("/", 1)[0] \
                    and not tok.startswith(("-", "<", ">", "&")):
                args.append(tok)          # `k=v` 는 옵션이지 경로가 아니다
            k += 1
        if reads_only or proven_only:
            args = []
        elif head in BASH_TARGET_LAST:
            args = args[-1:]
        for tok in args:
            add(_target(root, tok, False))
    return out


# 하네스 호출을 찾는다. 경로 앞머리에 `$`·`~`·`{`·`}` 도 온다 (`$PWD/…/harness`).
CTRL_CALL_RE = re.compile(r"(?:^|[\s=;&|(])(?:[\w./\\$~{}-]*[/\\])?"
                          r"(?:harness\.py|harness)(?=\s|$)([^;&|\n]*)")


def ctrl_requests(cmd):
    """제어 호출 전부 → `(sub, pos, direct, seg)`.

      sub    하위 명령. `argv_positional` 과 **같은 규칙**으로 뽑는다.
      pos    위치 인자 전체. 판정하는 쪽이 다시 파싱하지 않게 함께 준다.
      direct 세그먼트의 첫 토큰이 하네스인가. 아니면 무엇이 실행될지 확신할 수 없다.

    ## 세 번 틀린 자리다

      따옴표 제거   실행 경로가 사라져 게이트가 안 걸렸다
      shlex 토큰    `sh -c '… harness auto-skip on'` 이 한 토큰이라 안 걸렸다
      원문 정규식   `sk''ip`·`$(echo skip)` 을 못 읽고, 반대로 커밋 메시지의 `harness`
                    까지 제어 명령으로 봐서 그 뒤 검사가 통째로 꺼졌다

    셋의 공통점은 **모르면 통과**였다. 이제 모르면 묻는다 — 하네스를 부르는 것 같은데
    하위 명령을 못 읽으면 그 사실 자체가 판정이다(`sub` 이 아는 이름이 아니면 호출자가
    `ask` 를 낸다). 따옴표는 **지워서** 본다: 셸이 `sk''ip` 를 `skip` 으로 붙여 주므로
    우리도 붙여야 같은 것을 본다.
    """
    out = []
    for seg in BASH_SPLIT.split(cmd):
        bare = seg.replace('"', "").replace("'", "")
        head = (bare.split() or [""])[0]
        for tail in CTRL_CALL_RE.findall(bare):
            pos, skip = [], False
            for a in tail.split():
                if skip:
                    skip = False
                    continue
                if a.startswith("--"):
                    skip = "=" not in a       # `--flag value` 는 값도 건너뛴다
                    continue
                pos.append(a)
            if not pos:
                continue
            name = pos[0]
            if len(pos) > 1 and pos[1] in CTRL_SUB2.get(name, ()):
                name = "%s %s" % (name, pos[1])
            direct = os.path.basename(head) in CTRL_NAMES or head.endswith(
                tuple("/" + n for n in CTRL_NAMES))
            out.append((name, pos, direct, seg))
    return out


def ctrl_known(sub):
    # `help`·`init`·`hook` 은 CLI 표가 아니라 `run_cli`/`main` 의 특례다. 표만 보면
    # 하네스가 **스스로 안내하는 명령**(`harness help`, 복구의 `init`)이 모르는
    # 이름이 되어 ask 다이얼로그를 띄웠다(6회차).
    return sub in set(CLI) | {"help", "init", "hook"} | {
        "%s %s" % (k, v) for k, vs in CTRL_SUB2.items() for v in vs}


def ctrl_decision(con, cfg, root, sub, pos, direct, cmd, mode, lid, sid):
    """제어 명령에 대한 판정을 **돌려준다**(emit 하지 않는다).

    한 Bash 명령에 제어 호출이 여러 개 있을 수 있어서, 호출자가 전부 순회하며
    동의가 필요한 것을 찾아야 한다. 이전에는 첫 매치만 보고 즉시 emit 해서
    `harness status; harness loop new` 가 통째로 통과했다.
    """
    if sub == "auto-skip":
        # off 는 게이트 복원이므로 동의 없이 허용한다. on 은 게이트를 무력화하므로
        # 반드시 사람의 동의를 받는다 — 그러지 않으면 모델이 스스로 켤 수 있다.
        # 원문을 다시 뒤지지 않는다. `auto-skip --reason x on` 과 `auto-skip "on"` 이
        # CLI 에서는 on 인데 이 정규식에서는 아니어서 게이트가 통째로 사라졌다.
        if "on" not in pos[1:]:
            return
    elif sub == "path remove":
        # **미방문 노드 삭제는 스킵의 위장** — 기존 스킵 동의 게이트를 그대로 쓴다.
        # 새 동의 항목을 만들지 않는 이유 둘: 기존 설치의 consent 영역에는 새
        # 항목이 병합되지 않고(최상위 영역만 채운다), 방향 비대칭 게이트의 합의가
        # "새 규칙을 발명하지 않는다" 였다. 사용자가 skip 동의를 덜어냈다면 삭제
        # 동의도 같이 꺼진다 — 같은 게이트니까 그게 맞다.
        if "skip" not in consent_map(cfg):
            return
    elif sub not in consent_map(cfg):
        return

    if not direct:
        # 하네스를 부르는 것 같지만 세그먼트의 머리가 아니다 — 무엇이 실행될지
        # 확신할 수 없다. **거부하지 않고 묻는다.** 여기서 거부하면 커밋 메시지에
        # `harness skip` 을 쓴 것만으로 빠져나갈 길이 없어진다(막다른 길).
        # 이 검사가 아래 skip 거부보다 **먼저**여야 한다 — 뒤에 두면 커밋 메시지의
        # `harness skip gate` 가 "알 수 없는 대상: gate" 로 거부되는 바로 그
        # 막다른 길이 재현된다(6회차 실측).
        return pre_decision("ask", t("이 명령 안에 하네스 제어 호출(%s)이 보인다. "
                                     "실제로 실행되는지 하네스가 확신할 수 없어 사람에게 "
                                     "묻는다: `%s`") % (sub, cmd.strip()[:200]))

    if sub == "skip":
        # 불가능한 스킵은 **묻지 않고** 거부한다. 승인을 받아봐야 거부되고,
        # 그러면 모델이 다시 시도해 다이얼로그만 반복된다.
        # 훅이 `--reason` 의 **값**을 위치 인자로 세는 바람에 CLI 는 실행하는 명령을
        # 훅이 "알 수 없는 대상: x" 로 막았다. 같은 pos 를 쓴다.
        tgt = pos[1] if len(pos) > 1 else None
        if tgt:
            why = skip_block_reason(cfg, sid, tgt, con, root, lid)
            if why:
                record_event(con, lid, sid, "block", "skip_impossible", tgt, why)
                return pre_decision("deny", why)

    if sub == "path remove":
        # 불가능한 삭제도 스킵과 같다 — **묻지 않고** 거부한다. 승인을 받아봐야
        # CLI 가 거부하고, 모델은 안내받은 명령을 다시 시도해 다이얼로그가 반복된다.
        tgt = pos[2] if len(pos) > 2 else None
        if tgt:
            why = path_remove_block_reason(con, cfg, root, lid, tgt)
            if why:
                record_event(con, lid, sid, "block", "path_remove_impossible",
                             tgt, why)
                return pre_decision("deny", why)

    reason = raw_flag(cmd, "reason")
    if sub != "approve-plan" and not reason:
        record_event(con, lid, sid, "block", "no_reason", sub, cmd[:200])
        return pre_decision("deny",
            t("사유 없이 %s 할 수 없다. --reason \"...\" 로 사유를 명시하라.") % sub)

    if sub == "skip" and auto_skip_on(con):
        # 자동 승인이 켜져 있다. 다이얼로그는 생략하되 사실은 사용자에게 노출한다.
        out = pre_decision("defer", None)
        out["systemMessage"] = (t("harness: 단계 스킵을 자동 승인했다 (사유: %s · %s). "
                                "끄려면 `harness auto-skip off`.")
                                % (reason, auto_skip_scope_note(con)))
        return out
    if sub == "path remove" and auto_skip_on(con):
        # 같은 게이트이므로 자동 승인도 같이 적용된다 — 사실은 노출한다.
        out = pre_decision("defer", None)
        out["systemMessage"] = (t("harness: 회차 노드 삭제를 스킵 자동 승인으로 "
                                "통과시켰다 (사유: %s · %s). 끄려면 "
                                "`harness auto-skip off`.")
                                % (reason, auto_skip_scope_note(con)))
        return out

    what = consent_map(cfg).get(sub, sub + t(" 요청"))
    if sub == "path remove":
        what = t("미방문 회차 노드 삭제 요청 — 스킵의 위장이므로 스킵과 같은 동의를 받는다")
    detail = "%s: `%s`" % (what, cmd.strip())
    if reason:
        detail += t("\n사유: %s") % reason
    if sub == "approve-plan":
        # 무엇을 승인하는지 보여준다. 이름만 보고 찍는 승인은 기록으로도 가짜다.
        detail += t("\n\n─── 계획 ───\n%s\n───────────") % plan_preview(root, cmd)
    detail += t("\n승인하면 하네스 상태에 기록된다.")
    if mode == "bypassPermissions" and sub == "auto-skip":
        # 하나의 예외. `auto-skip on` 의 효과는 **세션을 넘어 지속된다**(meta 에 저장되고
        # scope 를 project 로 두면 이후 세션에도 남는다). 세션 단위 사전 승인으로
        # 세션을 넘는 결정을 덮을 수는 없다. 이건 진짜 사람의 판단이 필요하다.
        record_event(con, lid, sid, "block", "bypass_mode", sub, cmd[:200])
        return pre_decision("deny", detail +
            t("\nbypassPermissions 는 이 세션의 사전 승인이지만 `auto-skip on` 은 효과가 "
            "세션을 넘어 남는다. 그래서 이것만은 거부한다 — 권한 모드를 낮추고 사람의 "
            "판단을 받아라. 이 세션의 스킵은 이미 사전 승인으로 통과한다."))

    if mode == "bypassPermissions":
        # `--dangerously-skip-permissions` 는 **사람이 세션 단위로 미리 승인한** 상태다.
        # "동의를 받을 수 없는 상태" 로 읽고 거부했더니, 무인 실행 전용 모드에서
        # `approve-plan` 이 불가능해져 Planning 이 교착됐다 — 모델은 산문으로
        # "승인하면 진행한다" 며 사람을 기다리고, 루프가 멈춘다.
        #
        # 승인은 면제하되 **기록은 면제하지 않는다.** auto-skip on 과 같은 취급이다.
        # 우회 사실은 bypass 이벤트로 남아 `stats` 와 `metrics` 의 회피 열에 드러난다.
        record_event(con, lid, sid, "bypass", "bypass_mode", sub, cmd[:200])
        out = pre_decision("defer", None)
        out["systemMessage"] = (
            t("harness: %s 을 bypassPermissions 사전 승인으로 통과시켰다%s. "
            "기록은 남는다 — `harness stats` 의 '게이트 우회'.")
            % (sub, t(" (사유: %s)") % reason if reason else ""))
        return out
    return pre_decision("ask", detail)
