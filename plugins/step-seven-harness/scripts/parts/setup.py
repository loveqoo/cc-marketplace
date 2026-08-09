"""설치·엔진 사본·권한. 하네스를 프로젝트에 놓고 온전히 유지하는 일.

엔진이 이름공간을 맞춰 준다 — `parts/__init__.py` 참고.
"""


WRAPPER_REL = os.path.join(HARNESS_DIR, "bin", "harness")


def install_problems(root):
    """설치 형태에서 **하네스가 자신을 보호할 수 없는 경우**를 알린다.

    "훅은 프로젝트 밖의 플러그인 엔진을 실행하므로 프로젝트 사본은 게이트가 아니다"
    라고 문서에 적었는데, 그건 `git`/`github` 소스일 때만 참이다. 마켓플레이스를
    `directory` 소스로 등록하면(README 가 로컬 테스트에 권하는 방식) 플러그인 루트가
    **프로젝트 안**이 되고, 그러면 모델이 훅 엔진 자체를 고칠 수 있다.

    주장을 문서에 적어두면 설치 형태가 바뀔 때 거짓이 된다. **런타임에 재어 말한다.**
    개발 중에는 엔진을 고치는 것이 목적이므로 이것은 고장이 아니다 — 다만 그 상태에서
    자기 잠금을 신뢰해서는 안 된다는 사실은 보여야 한다.
    """
    try:
        pr = os.path.realpath(plugin_root())
        rp = os.path.realpath(root)
    except Exception:
        return []
    # **`normcase` 로는 부족하다.** POSIX 에서 그것은 항등 함수이고, `realpath` 도
    # 표기를 정규화하지 않는다(확인했다: `/PLUGINS` → `/PLUGINS`). 그래서 macOS 에서
    # `/Private/…` 로 엔진을 실행하고 root 가 `/private/…` 이면 — 같은 경로인데 —
    # 담김 판정이 실패해 경고가 나오지 않았다. 자기 잠금에서 쓴 것과 같은 방식으로,
    # 대소문자를 접어 비교한다. 구분하는 파일시스템에서 과잉 경고가 되는 것은
    # 받아들인다 — 경고를 놓치는 것보다 낫다.
    prn, rpn = os.path.normcase(pr).lower(), os.path.normcase(rp).lower()
    if prn == rpn or prn.startswith(rpn + os.sep):
        return [t("플러그인 엔진이 프로젝트 안에 있다 (%s) — 이 설치 형태에서는 "
                  "모델이 훅 엔진을 고칠 수 있으므로 자기 잠금을 신뢰할 수 없다. "
                  "개발 중이면 정상이다.") % os.path.relpath(pr, rp)]
    return []


def refresh_learned(con, cfg, root):
    """LEARNED.md 를 promotion 테이블에서 다시 생성한다.

    손으로 고친 내용이 성숙도와 어긋나지 않게 하려면 생성이 유일한 경로여야 한다.
    """
    rows = learned_lines(con, cfg)
    body = [t(LEARNED_HEAD) % learned_budget(cfg)]
    if rows:
        for r in rows:
            body.append("- [%s] %s <!-- %s -->"
                        % (r["maturity"], (r["note"] or "").strip(), r["key"]))
    else:
        body.append(t("(아직 없다 — 반복된 실수가 승격되면 여기에 쌓인다.)"))
    return _write_if_changed(os.path.join(root, LEARNED_REL),
                            "\n".join(body) + "\n")


def _write_if_changed(path, body, mode=None):
    """True=썼다, False=이미 같다, **None=쓰지 못했다.**

    셋을 둘로 뭉개면 실패가 "바꿀 것이 없었다" 와 구분되지 않는다. 읽기 전용
    파일시스템에서 승격이 LEARNED.md 반영에 실패했는데도 성공으로 보고됐다.
    """
    tmp = None
    try:
        if os.path.isfile(path) and open(path, encoding="utf-8").read() == body:
            return False
        # rename 은 디렉터리 권한만 본다 — 검사 없이 바꾸면 사람이 읽기 전용으로
        # 잠근 파일까지 소리 없이 갈아치운다. 잠근 것은 존중한다.
        if os.path.exists(path) and not os.access(path, os.W_OK):
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 임시파일 + rename. 제자리 덮어쓰기는 갱신 도중 다른 세션의 훅이 잘린
        # 래퍼를 읽어 "변조를 되돌렸다" 로 오판했고, 셸이 그 순간 실행하면 잘린
        # 스크립트가 돌았다(6회차). rename 은 같은 디렉터리 안에서 원자적이다.
        tmp = "%s.tmp-%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        return True
    except Exception:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return None


def engine_sources(scripts_dir):
    """엔진을 이루는 파이썬 파일 전부 (scripts_dir 기준 상대경로).

    목록을 손으로 적지 않는다 — 구현이 파일로 갈라질 때 새 파일이 사본에서 빠지면
    그 게이트가 조용히 사라진다.
    """
    out = []
    for dirpath, _dirs, names in os.walk(scripts_dir):
        for n in sorted(names):
            if n.endswith(".py"):
                out.append(os.path.relpath(os.path.join(dirpath, n), scripts_dir))
    return sorted(out)


def _copy_engine(src_dir, dst_dir):
    """엔진 파일 전부를 사본으로. **부분 복사를 남기지 않는다.**

    한 파일이라도 못 쓰면 **사본의 파이썬 파일을 전부 지운다.** 반쯤 복사되거나
    낡은 사본은 실행되면 게이트가 빠진 채로 돌고, 그게 곧 게이트 해제다. 없는 편이
    낫다 — 래퍼는 원본(플러그인)으로 떨어지고, 그것이 옳은 엔진이다.
    """
    changed = False
    for rel in engine_sources(src_dir):
        try:
            with open(os.path.join(src_dir, rel), encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            body = None
        r = (_write_if_changed(os.path.join(dst_dir, rel), body, 0o644)
             if body is not None else None)
        if r is None:                      # 못 읽었거나 못 썼다
            _purge_engine_copy(dst_dir)
            return None
        changed = changed or bool(r)
    # **사본은 원본과 같아야 한다.** 원본에 없는데 사본에 있는 것은 지운다.
    #
    # 없었을 때 두 구멍이 있었다(4회차 D-M9): ① `gates/zzz.pyc` 를 심으면
    # `pkgutil` 이 발견해 임포트하는데 복사·정리 어느 쪽에도 안 걸렸다 —
    # 사본 경로로 남의 게이트를 심을 수 있었다. ② 플러그인 업그레이드로 게이트
    # 파일이 없어져도 사본에는 영원히 남아 낡은 게이트가 계속 돌았다.
    keep = {os.path.normpath(r) for r in engine_sources(src_dir)}
    for rel in _importable(dst_dir):
        if os.path.normpath(rel) not in keep:
            with swallow(t("사본 정리(%s)") % rel):
                os.remove(os.path.join(dst_dir, rel))
            changed = True
    return changed


# 파이썬이 **임포트할 수 있는** 것. `.py` 만 보면 `.pyc` 가 남고, `pkgutil` 은
# 그것을 발견한다. 확장자 목록이지만 파이썬의 것이지 우리 어휘가 아니다.
IMPORTABLE = (".py", ".pyc", ".pyo", ".so")


def _importable(dst_dir):
    """사본 안에서 임포트될 수 있는 파일 (dst_dir 기준 상대경로)."""
    out = []
    for dirpath, _dirs, names in os.walk(dst_dir):
        for n in sorted(names):
            if n.endswith(IMPORTABLE):
                out.append(os.path.relpath(os.path.join(dirpath, n), dst_dir))
    return out


def _purge_engine_copy(dst_dir):
    """사본에서 **임포트될 수 있는 것을 전부** 없앤다. 낡은 엔진이 도는 것보다
    없는 것이 낫다 — 래퍼는 원본(플러그인)으로 떨어지고 그것이 옳은 엔진이다."""
    for rel in _importable(dst_dir):
        with swallow(t("사본 삭제(%s)") % rel):
            os.remove(os.path.join(dst_dir, rel))


def refresh_engine(root):
    """엔진 사본을 프로젝트 안에 둔다.

    모델이 실행하는 명령이 작업 디렉터리 밖의 파일을 가리키면 분류기·샌드박스가
    막는다. 사본은 gitignore 되고 세션 시작마다 갱신되므로 버전이 어긋나지 않는다.
    """
    src = ENGINE_FILE
    dst = os.path.join(root, ENGINE_REL)
    if src == os.path.abspath(dst):
        return False  # 사본 자신이 실행 중이면 덮어쓰지 않는다
    changed = _copy_engine(os.path.dirname(src), os.path.dirname(dst))
    # **기본값도 사본과 함께 가야 한다.** 사본이 실행될 때 plugin_root() 는
    # `.claude/harness/` 를 가리키고 거기엔 templates/ 가 없다. 그래서 어휘 기본값을
    # 못 찾고, 훅(플러그인 엔진)과 CLI(사본)의 판정이 갈렸다 — 래퍼로 advance 하면
    # satisfied_by 를 몰라 디스크의 계획 파일을 인정하지 않았다. 재현해 확인했다.
    tdir = os.path.join(plugin_root(), "templates")
    for src_rel, dst_abs in [("stages.json", os.path.join(root, DEFAULTS_REL))] + [
            (n, os.path.join(root, HARNESS_DIR, "bin", n))
            for n in sorted(os.listdir(tdir)) if n.startswith("messages.")
    ] if os.path.isdir(tdir) else []:
        with swallow(t("엔진 사본 갱신")):
            with open(os.path.join(tdir, src_rel), encoding="utf-8") as fh:
                _write_if_changed(dst_abs, fh.read(), 0o644)
    return changed


def refresh_wrapper(root):
    """래퍼를 우리 것으로 맞춘다. 쓰지 못했으면 None (그것도 사실이다)."""
    refresh_engine(root)
    return _write_if_changed(os.path.join(root, WRAPPER_REL),
                             t(WRAPPER) % ENGINE_FILE, 0o755)


def wrapper_code(body):
    """래퍼에서 **실행되는 줄만** 남긴다.

    바이트로 비교하면 주석(한국어 설명)이 `language` 설정에 따라 달라져 정상 파일을
    변조로 오판한다. 오판은 마찰이고, 마찰은 게이트를 끄게 만든다. sh 에서 `#` 줄은
    실행되지 않으므로 빼고 본다. 첫 줄(shebang)은 **인터프리터를 정하므로** 남긴다.
    """
    lines = (body or "").splitlines()
    keep = lines[:1] + [ln for ln in lines[1:]
                        if ln.strip() and not ln.strip().startswith("#")]
    return "\n".join(keep)


def wrapper_shape(body):
    """엔진 경로를 지운 래퍼 코드. 플러그인이 업데이트되면 그 경로만 바뀐다."""
    return ENGINE_LINE_RE.sub('P=""', wrapper_code(body))


def wrapper_intact(root):
    """래퍼가 우리가 쓴 그것인가. 아니면 **복구하고** False.

    `SAFE_PERMS` 는 래퍼를 **경로로** 사전 승인한다. 그런데 그 파일은 프로젝트 안에
    있어 모델이 쓸 수 있는 자리다. 경로 검사를 우회하는 길이 하나만 남아도 그 즉시
    승인 없는 임의 코드 실행이 된다. 그래서 **신뢰를 경로가 아니라 내용에 건다** —
    우회가 성공해도 남의 코드는 실행되지 않는다.

    복구까지 하는 이유: 거부만 하면 `harness init` 조차 래퍼를 거쳐 사용자가 갇힌다.
    """
    path = os.path.join(root, WRAPPER_REL)
    want = t(WRAPPER) % ENGINE_FILE
    try:
        with open(path, encoding="utf-8") as fh:
            have = fh.read()
    except Exception:
        have = None
    if have is not None and wrapper_code(have) == wrapper_code(want):
        return True                              # 온전하다
    # **엔진 경로만 다르면 변조가 아니라 낡은 것이다.** 플러그인을 업데이트하면 캐시
    # 디렉터리 이름이 바뀌어 그 줄이 달라진다 — 아무도 손대지 않았는데 보안 경고가
    # 뜨고 `wrapper_tampered` 가 통계를 오염시켰다. 조용히 맞춰 놓는다.
    # 파일이 아예 없는 것은 변조가 아니다 — 새 클론·워크트리에는 원래 없다(gitignore).
    stale = have is None or wrapper_shape(have) == wrapper_shape(want)
    ok = refresh_wrapper(root) is not None
    if not ok:
        return None                              # 복구도 못 했다
    return True if stale else False              # 갱신했다 / 변조를 되돌렸다


# 읽기 전용·정상 진행 명령만 미리 허용한다. 동의가 필요한 명령
# (skip / allow / approve-plan / auto-skip on / loop new|adopt) 은 의도적으로 제외한다.
SAFE_PERMS = ["Bash(%s %s)" % (WRAPPER_CMD, c)
              for c in ("status", "advance", "loop", "help", "tidy", "promote", "metrics")] \
    + ["Bash(%s %s:*)" % (WRAPPER_CMD, c)
       for c in ("recall", "stats", "loop intent", "promote")] \
    + ["Bash(%s auto-skip status)" % WRAPPER_CMD]


def ensure_permissions(root, tries=3):
    """하네스 조회 명령을 프로젝트 설정에 미리 허용한다.

    매번 권한 프롬프트를 요구하면 모델이 조회를 포기하고 파일을 직접 읽는
    우회로 간다 — 실제 세션에서 관측된 문제다.

    **비교-교환으로 쓴다.** 이 파일은 Claude Code 도 쓴다(`enabledPlugins` 등).
    읽고-고치고-쓰는 사이에 저쪽이 쓰면 우리가 그걸 덮어 없앤다 — 플러그인
    활성화가 통째로 사라지는 방향이다. 쓰기 직전에 다시 읽어 우리가 읽었던 것과
    같은지 확인하고, 다르면 새 내용으로 다시 병합한다.

    반환값: 추가한 규칙 수 / 0 = 더할 것 없음 / -1 = 손상되어 건드리지 않음
            / -2 = 다른 쪽이 계속 쓰고 있어 포기
    """
    path = os.path.join(root, ".claude", "settings.json")

    def read_raw():
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return False          # 읽을 수 없음 (None 과 구분한다)

    for _ in range(max(1, tries)):
        raw = read_raw()
        if raw is False:
            return -1
        if raw is None:
            data = {}
        else:
            try:
                data = json.loads(raw) if raw.strip() else {}
            except ValueError:
                return -1         # 손상된 설정은 건드리지 않는다
            if not isinstance(data, dict):
                return -1

        # 최상위가 dict 인 것만 확인하고 setdefault 를 연달아 호출하면, permissions 가
        # list/문자열인 정상 JSON 에서 AttributeError 로 init 이 중간에 죽어
        # 부분 설치 상태를 남긴다. 모양을 단계마다 확인한다.
        perms = data.get("permissions")
        if perms is None:
            perms = data["permissions"] = {}
        if not isinstance(perms, dict):
            return -1
        allow = perms.get("allow")
        if allow is None:
            allow = perms["allow"] = []
        if not isinstance(allow, list):
            return -1
        added = [p for p in SAFE_PERMS if p not in allow]
        if not added:
            return 0
        allow.extend(added)

        # 쓰기 직전에 다시 읽는다. 우리가 읽은 뒤 누가 바꿨으면 그 내용으로 다시 한다.
        if read_raw() != raw:
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return len(added)
    return -2


# 설치는 여섯 가지 서로 다른 일이다. 한 함수에 154줄로 뭉쳐 있으면 하나가
# 예외를 던졌을 때 어디까지 됐는지 알 수 없고(실제로 부분 설치 상태가 남았다),
# 각각을 따로 테스트할 수도 없다. 단계마다 "무엇을 만들었나"를 돌려준다.

def install_templates(root, pr):
    """원칙·근거·설정 문서. **이미 있으면 덮어쓰지 않는다** — 사용자가 고친 것이다."""
    made = []
    for rel, src in ((CONFIG_REL, "templates/stages.json"),
                     (POLICY_REL, "templates/POLICY.md"),
                     (RATIONALE_REL, "templates/rationale.md")):
        dst = os.path.join(root, rel)
        if os.path.exists(dst):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(os.path.join(pr, src), encoding="utf-8") as fh:
            body = fh.read()
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(body)
        made.append(rel)
    return made


def quarantine_db(root):
    """**하네스 DB 로 쓸 수 없으면** 옆으로 치우고 그 경로를 돌려준다. 멀쩡하면 None.

    예전에는 "열리나" 만 물었다(`PRAGMA schema_version`). 0바이트 파일은 **유효한
    빈 SQLite** 라 그 탐침을 통과했고, 그래서 `python3 -c "open(<DB>,'w')"` 한 줄로
    상태를 날려도 격리되지 않고 `.corrupt-N` 백업 없이 조용히 재초기화됐다 —
    기록이 전소되는데 아무 흔적이 없다(4회차 C②).

    질문을 바꾼다: **이게 우리 DB 인가.** 하네스 DB 라면 우리 표가 하나는 있다.
    (전부를 요구하지 않는다 — `CREATE TABLE IF NOT EXISTS` 가 업그레이드 경로라
    낡은 DB 는 새 표가 없는 것이 정상이다.)

    그런데 표 목록만 보는 것도 부족했다. **`sqlite_master` 는 1페이지에 있어서
    데이터 페이지가 깨져도 멀쩡히 읽힌다.** 그래서 "열리지만 읽을 수 없는" —
    훨씬 흔한 손상 모양이 통과했고, 곧이어 `install_db` 의 `head_loop` 이
    `DatabaseError` 로 죽었다. `init`·`status`·훅이 전부 사망하고 안내 메시지는
    **죽는 명령을 실행하라고** 했다. 격리도 백업도 없었다(5회차 C⑦).

    손상 판정을 손으로 만들지 않는다 — `PRAGMA quick_check` 가 SQLite 자신의
    답이다. 근사를 재구현하지 말고 진짜 도구를 쓴다.
    """
    path = os.path.join(root, DB_REL)
    if not os.path.isfile(path):
        return None
    try:
        probe = sqlite3.connect(path)
        try:
            names = {r[0] for r in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            ok = probe.execute("PRAGMA quick_check(1)").fetchone()
        finally:
            probe.close()
        if names & set(SCHEMA_TABLES) and ok and ok[0] == "ok":
            return None
    except sqlite3.Error:
        pass
    for n in range(1, 100):
        dst = "%s.corrupt-%d" % (path, n)
        if not os.path.exists(dst):
            try:
                os.replace(path, dst)
            except OSError:
                return None
            # 부속 파일은 지우지 않고 백업 옆으로 **옮긴다** — wal 에는 체크포인트
            # 안 된 최근 커밋이 남아 있어, 지우면 "사람이 나중에 열어볼" 백업에서
            # 마지막 기록이 떨어져 나간다(6회차). 원본 자리는 어차피 비워야 한다.
            for suffix in ("-wal", "-shm"):
                try:
                    os.replace(path + suffix, dst + suffix)
                except OSError:
                    try:
                        os.remove(path + suffix)
                    except OSError:
                        pass
            return dst
    return None


def install_db(root, cfg):
    """스키마를 적용하고 활성 작업을 보장한다. 업그레이드 경로가 이 함수다 —
    `CREATE TABLE IF NOT EXISTS` 라서 재실행이 곧 스키마 갱신이다."""
    made = []
    fresh = not os.path.isfile(os.path.join(root, DB_REL))
    # **`init` 은 복구 경로다.** 훅이 "복구: harness init" 이라고 안내하는데 정작
    # 손상된 DB 앞에서 traceback 으로 죽으면 게이트가 영구히 꺼진다. 읽을 수 없으면
    # 지우지 않고 **옆으로 치운다** — 사람이 나중에 열어볼 수 있어야 한다.
    moved = quarantine_db(root)
    if moved:
        made.append(t("%s (읽을 수 없어 %s 로 옮겼다)") % (DB_REL, os.path.basename(moved)))
        fresh = True
    con = connect(root, create=True)
    try:
        con.executescript(SCHEMA)
        con.commit()
        lid = head_loop(con)
        if not lid or not active_stage(con, lid):
            with con:
                lid = create_loop(con, cfg, root)
            made.append("%s (loop %s)" % (DB_REL, lid))
        elif fresh:
            made.append(DB_REL)
        # 앵커가 가리키는 파일이 없으면 CLAUDE.md 임포트가 깨진다. 빈 상태로라도 만든다.
        if refresh_learned(con, cfg, root):
            made.append(LEARNED_REL)
    finally:
        con.close()
    return made, lid


def _git_ignores(root, pattern):
    """git 이 이 규칙이 덮을 경로를 **이미** 무시하나.

    gitignore 의 의미론(부정 `!`, 폴더 제외, 우선순위)을 우리가 다시 구현하지
    않는다 — 셸을 재구현하지 않기로 한 것과 같은 판단이다. 답을 아는 프로그램에
    묻는다. git 이 없거나 저장소가 아니면 예전처럼 행동한다(붙인다).
    """
    import subprocess
    probe = pattern.rstrip("/").replace("*", "x")   # 글롭·폴더는 대표 경로로 묻는다
    if pattern.endswith("/"):
        probe += "/x"
    try:
        with open(os.devnull, "w") as null:
            # `--no-index` 로 "추적 중인가"가 아니라 **규칙이 덮는가**만 묻는다.
            return subprocess.call(
                ["git", "-C", root, "check-ignore", "-q", "--no-index", probe],
                stdout=null, stderr=null) == 0
    except OSError:
        return False


def install_gitignore(root):
    """런타임 상태를 커밋 대상에서 뺀다."""
    gi = os.path.join(root, ".gitignore")
    # 격리한 손상 DB 사본도 런타임 상태다 — 커밋 대상이 아니다.
    want = [".claude/harness/harness.db", ".claude/harness/harness.db-wal",
            ".claude/harness/harness.db.corrupt-*",
            ".claude/harness/harness.db-shm", ".claude/harness/bin/"]
    have = open(gi, encoding="utf-8").read() if os.path.isfile(gi) else ""
    # 행 단위로 본다. substring 으로 보면 주석에 경로가 언급된 것만으로
    # "이미 있다"고 판단해 실제 ignore 규칙을 넣지 않는다.
    lines = {ln.strip() for ln in have.splitlines()}
    add = [w for w in want if w not in lines]
    # 줄이 없다고 **무시되지 않는 것은 아니다.** `.claude/harness/*` 한 줄이
    # 폴더를 통째로 막고 규칙 파일만 `!` 로 되살린 저장소에서는 우리가 넣을
    # 다섯 줄이 이미 전부 무시되는데도 매번 다시 붙었다. 지우면 `init` 마다
    # 되살아나므로 사용자는 결국 군더더기를 그대로 두게 된다(현장 보고 §7).
    # 줄을 찾지 말고 **git 에게 결과를 묻는다** — 판정자가 하나가 된다.
    add = [w for w in add if not _git_ignores(root, w)]
    if not add:
        return []
    with open(gi, "a", encoding="utf-8") as fh:
        if have and not have.endswith("\n"):
            fh.write("\n")
        fh.write(t("\n# step-seven-harness (런타임 상태 — 커밋하지 않는다)\n"))
        fh.write("\n".join(add) + "\n")
    return [".gitignore"]


def install_agents_md(root):
    """AGENTS.md 에 절차 안내를 한 번 붙인다.

    왜 CLAUDE.md 와 따로 다루나: AGENTS.md 는 `@import` 를 모른다. 그건 Claude
    Code 의 기능이고, Codex·Cursor·Copilot·Gemini CLI·Aider·Windsurf·Zed 는
    이 파일을 **그냥 마크다운으로 읽는다.** 그러니 앵커 한 줄이 아니라 읽으면
    바로 쓸 수 있는 문장이어야 한다.

    이미 표시가 있으면 **아무것도 하지 않는다.** 다시 써서 갱신하는 편이
    깔끔하겠지만, 사용자가 이 블록 안을 고쳤을 때 그것을 지운다. 남의 글을
    잃는 것은 낡은 안내보다 나쁘다 — CLAUDE.md 앵커와 같은 규칙이다.
    """
    p = os.path.join(root, "AGENTS.md")
    body = open(p, encoding="utf-8").read() if os.path.isfile(p) else ""
    if AGENTS_MARK in body:
        return []
    with open(p, "a", encoding="utf-8") as fh:
        if body and not body.endswith("\n"):
            fh.write("\n")
        fh.write(("\n" if body else "") + t(AGENTS_BLOCK))
    return ["AGENTS.md (%s)" % (t("절차 안내 추가") if body else t("새로 만듦"))]


def install_anchors(root):
    """CLAUDE.md 에 앵커 두 줄. POLICY 는 사람이 정한 원칙, LEARNED 는 하네스가
    승격한 규칙 — 한 파일에 섞으면 생성 대상과 손으로 쓴 것이 구분되지 않는다."""
    cm = os.path.join(root, "CLAUDE.md")
    body = open(cm, encoding="utf-8").read() if os.path.isfile(cm) else ""
    # 행 단위로 본다. 코드 예시나 설명문에 앵커 문자열이 있으면 substring 판정은
    # 실제 import 행이 없는데도 있다고 착각한다.
    lines = {ln.strip() for ln in body.splitlines()}
    add = [a for a in ("@%s" % POLICY_REL.replace(os.sep, "/"),
                       "@%s" % LEARNED_REL.replace(os.sep, "/"))
           if a not in lines]
    if not add:
        return []
    # 읽고-쓰는 사이에 다른 `init` 이 넣을 수 있다. 동시 넷이 앵커를 세 번 겹쳐
    # 넣었다 — 이 파일은 세션마다 로드되므로 중복은 그대로 컨텍스트 낭비다.
    with open(cm, "a+", encoding="utf-8") as fh:
        fh.seek(0)
        now_lines = {ln.strip() for ln in fh.read().splitlines()}
        add = [a for a in add if a not in now_lines]
        if not add:
            return []
        fh.seek(0, os.SEEK_END)
        if fh.tell() and not body.endswith("\n"):
            fh.write("\n")
        fh.write("\n" + "\n".join(add) + "\n")
    return [t("CLAUDE.md (앵커 %d줄)") % len(add)]
