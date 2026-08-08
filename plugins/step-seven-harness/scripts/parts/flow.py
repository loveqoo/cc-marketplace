"""단계 진행과 종료 조건. 무엇이 채워져야 다음으로 가나.

엔진이 이름공간을 맞춰 준다 — `parts/__init__.py` 참고.
"""


SATISFIED_BY = ("cli", "file", "observed", "no_pending_promotions")


def retro_questions(cfg):
    """회고에서 물을 것. 무엇을 묻는지가 회고의 값을 정하므로 사람이 정해야 한다."""
    out = []
    for it in cfg.seq("retro_questions"):
        if isinstance(it, dict) and it.get("q"):
            out.append((t(it["q"]), t(it.get("why") or "")))
    return out


def stage_ids(cfg):
    return [s["id"] for s in cfg["stages"]]


def stage_index(cfg, sid):
    """설정에서의 자리. **모르는 id 는 0 이 아니다** — 호출 전에 걸러야 한다.

    예전에는 `else 0` 이었다. `stages.json` 에서 단계 id 하나만 바꾸면 DB 의 활성
    단계가 조용히 1단계로 읽혔고, 그 단계의 종료 조건(사람만 채울 수 있는
    `plan_approved` 포함)이 **아무 경고 없이 사라졌다.** 자기검사도 22/22 를 냈다.
    이제 그 상태는 `stage_known` 으로 미리 걸러 `inactive()` 가 소리 내어 말한다.
    """
    ids = stage_ids(cfg)
    return ids.index(sid) if sid in ids else 0


def stage_obj(cfg, sid):
    return cfg["stages"][stage_index(cfg, sid)]


def label_of(cfg, sid):
    return "%d/%d %s" % (stage_index(cfg, sid) + 1, len(cfg["stages"]),
                         stage_obj(cfg, sid)["label"])


# -------------------------------------------------------------------- 중간 그래프
#
# 샌드위치 구조: 처음 둘(Selection·Scaffolding)과 마지막(Compounding)은 **틀**이라
# 코드가 고정한다 — `next` 선언 불가, 위치 고정. 그 사이(중간)만 그래프다:
# `next` 선언으로 분기하되 **앞으로만** 간다(백엣지 금지). 선언이 없으면 다음
# 항목이다 — 기존 설정은 무변경으로 동작한다.
#
# 뒤로는 가지 않는다(POLICY 4). 되돌아갈 일은 회차를 빠르게 닫는 것으로 해결하므로
# 재진입 의미론은 설계하지 않는다 — 이 결정이 이 그래프를 DAG 로 유지한다.

def frame_idx(cfg):
    """틀 노드의 자리 — 처음 둘과 마지막. id 가 아니라 **자리**다: 엔진의 나머지
    (create_loop 의 0, next_cycle 의 1, advance 의 -1)가 이미 자리로 말한다."""
    n = len(cfg.get("stages") or [])
    return {0, 1, n - 1} if n >= 3 else set(range(n))


def declared_next(st):
    """선언된 next. 문자열 하나도 받는다 — 목록 강제는 마찰만 늘린다."""
    v = st.get("next")
    if isinstance(v, str):
        v = [v]
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def valid_next(cfg, sid):
    """이 노드의 선언 중 **실제로 쓰는** 엣지. 틀 노드의 선언, 모르는 대상,
    뒤로 가는 엣지, **중복**은 버린다 — 버린 사실은 `graph_problems` 가 말한다.

    중복을 안 걸렀을 때 `["execution","execution"]` 가 유령 분기 노드를 만들었다
    (successors 가 2개라 분기로 판정 → advance 가 `--to` 를 강요) — 위상 리뷰가 찾았다.
    """
    ids = stage_ids(cfg)
    i = ids.index(sid)
    st = cfg["stages"][i]
    if i in frame_idx(cfg) or st.get("__dyn__"):
        return []
    out = []
    for x in declared_next(st):
        if x in ids and ids.index(x) > i and x not in out:
            out.append(x)
    return out


def successors(cfg, sid):
    """이 노드에서 갈 수 있는 다음 노드들 (이번 회차의 실행 그래프 기준).

    회차 한정 노드는 앵커 **바로 뒤에** 끼워지므로: 앵커의 다음은 그 노드이고,
    사슬의 끝은 앵커가 원래 가던 곳(선언 또는 다음 항목)으로 간다 — 앵커가
    분기 노드면 분기 결정이 사슬 뒤로 미뤄진다.
    """
    sts, ids = cfg["stages"], stage_ids(cfg)
    if sid not in ids:
        return []
    i = ids.index(sid)
    if i >= len(sts) - 1:
        return []
    if sts[i + 1].get("__dyn__"):
        return [ids[i + 1]]
    j = i
    while j >= 0 and sts[j].get("__dyn__"):    # 사슬의 주인(앵커)을 찾는다
        j -= 1
    return valid_next(cfg, ids[j]) or [ids[i + 1]]


def default_path(cfg, sid):
    """sid 에서 끝까지의 **기본 경로** — 분기에서는 첫 선언을 따른다.

    스킵의 `+N`/`until:` 이 이 경로를 기준으로 잰다. 엣지가 전부 앞으로만 가므로
    (valid_next 가 보장) 이 걸음은 반드시 끝난다.
    """
    path, node = [sid], sid
    while True:
        nxt = successors(cfg, node)
        if not nxt:
            return path
        node = nxt[0]
        path.append(node)


def graph_splice(con, cfg, lid):
    """cfg 의 stages 를 **이번 회차의 실행 그래프**로 바꾼다 (그 자리에서).

    설정 단계 + 회차 한정 노드(path_node, 앵커 바로 뒤). 멱등이다 — 결합된
    상태에서 다시 불러도 같은 답이 나온다. 계획 승인 = 그래프 승인이라는 결정의
    구현 절반이 여기다: Planning 에서 더한 노드가 그대로 실행 그래프가 된다.
    """
    base = [s for s in (cfg.get("stages") or [])
            if not (isinstance(s, dict) and s.get("__dyn__"))]
    chains = {}
    for r in path_rows(con, lid, cycle_of(con, lid)):
        try:
            write = json.loads(r["write"]) if r["write"] else ["dev"]
        except ValueError:
            write = ["dev"]
        chains.setdefault(r["after_node"], []).append({
            "id": r["node"], "label": r["label"] or r["node"],
            "summary": r["summary"] or t("회차 한정 노드"),
            "write": write if isinstance(write, list) else ["dev"],
            "exit_criteria": [], "stop_requires": [], "__dyn__": True,
        })
    out = []

    def emit(node):
        out.append(node)
        for d in chains.pop(node["id"], []):    # 사슬 뒤에 또 사슬이 붙을 수 있다
            emit(d)

    for s in base:
        emit(s)
    orphan = [d for lst in chains.values() for d in lst]
    if orphan and out:
        # 앵커가 사라진 노드(설정이 바뀐 경우). 버리면 조용한 소실이므로 마지막
        # 틀 앞에 두어 눈에 보이게 한다 — 지우는 것은 사람의 결정이다.
        out[-1:-1] = orphan
    cfg["stages"] = out
    return cfg


def _bfs_reach(sts, start, block=None):
    """start 에서 successors 로 닿는 노드 집합. block 노드는 지나지 않는다.

    필수 관문 검증에 쓴다 — 관문 하나를 막고도 끝에 닿으면 그 관문은 우회 가능하다.
    """
    plain = {"stages": sts}
    seen, frontier = ({start} if start != block else set()), [start]
    if start == block:
        frontier = []
    while frontier:
        node = frontier.pop()
        for x in successors(plain, node):
            if x != block and x not in seen:
                seen.add(x)
                frontier.append(x)
    return seen


def graph_problems(cfg):
    """중간 그래프의 위상 진단. 막지 않는다 — 무엇이 무시되는지 **말한다**.

    잡는 것, 전부 조용히 어긋나는 부류다: ① 틀 노드의 `next` 선언(무시된다)
    ② 문자열이 아닌 `next` 항목(무시된다 — 필터링이 진단보다 앞서 증발했다)
    ③ 중복 대상(유령 분기를 만든다) ④ 모르는·뒤로 가는 엣지(무시된다 — 뒤로 가는
    그래프는 이 하네스가 설계로 거부한 것이다) ⑤ 어떤 경로로도 도달하지 않는 중간
    노드(결코 방문되지 않는다) ⑥ **게이트를 가진 중간 노드가 분기로 우회 가능한 것**
    (모든 종료 경로가 검증·계획 게이트를 지나야 한다 — 위상 검증).
    """
    out = []
    sts = [s for s in (cfg.get("stages") or [])
           if isinstance(s, dict) and not s.get("__dyn__")]
    ids = [s.get("id") for s in sts]
    n = len(sts)
    frame = {0, 1, n - 1} if n >= 3 else set(range(n))
    for i, st in enumerate(sts):
        raw = st.get("next")
        if raw is None:
            continue
        # **원본을 본다.** `declared_next` 가 비문자열을 걸러낸 뒤 진단하면 그 항목은
        # 진단 없이 증발한다 — 선언이 통째로 무선언 취급되는데 사용자는 모른다.
        if not isinstance(raw, (str, list)):
            out.append(t("stages[%s].next 는 문자열이거나 목록이어야 한다 (지금 %s) "
                         "— 선언이 통째로 무시된다")
                       % (st.get("id"), type(raw).__name__))
            continue
        items = [raw] if isinstance(raw, str) else raw
        strs = []
        for x in items:
            if not isinstance(x, str):
                out.append(t("stages[%s].next 의 항목 %r 은 문자열이 아니다 — 그 "
                             "엣지는 무시된다") % (st.get("id"), x))
            else:
                strs.append(x)
        if i in frame:
            out.append(t("stages[%s] 는 틀 노드라 next 를 선언할 수 없다 — 선언이 "
                         "무시된다. 틀 사이(중간)에서만 그래프를 그려라") % st.get("id"))
            continue
        seen_x = set()
        for x in strs:
            if x in seen_x:
                out.append(t("stages[%s].next 에 '%s' 가 중복됐다 — 유령 분기가 되지 "
                             "않게 한 번만 남기고 무시한다") % (st.get("id"), x))
                continue
            seen_x.add(x)
            if x not in ids:
                out.append(t("stages[%s].next 의 '%s' 는 없는 단계다 — 그 엣지는 "
                             "무시된다") % (st.get("id"), x))
            elif ids.index(x) <= i:
                out.append(t("stages[%s].next 의 '%s' 는 뒤로 가는 엣지다 — 중간 "
                             "그래프는 앞으로만 간다. 그 엣지는 무시된다. 되돌아갈 "
                             "일은 회차를 닫고 다음 회차로 해결하라")
                           % (st.get("id"), x))
    if n < 3:
        return out
    # 도달성: 두 번째 틀(Scaffolding 자리)에서 걸어 본다. 엣지가 전부 앞으로만
    # 가므로 도달한 노드는 마지막 틀에도 반드시 닿는다 — 남는 질문은 이것 하나다.
    seen = _bfs_reach(sts, ids[1])
    for i in range(2, n - 1):
        if ids[i] not in seen:
            out.append(t("stages[%s] 는 어떤 경로로도 도달하지 않는다 — 이 단계는 "
                         "결코 방문되지 않는다. 앞 노드의 next 에 넣거나 단계를 "
                         "빼라") % ids[i])
    # **필수 관문.** 종료 조건·턴 종료 조건을 가진 중간 노드는 검증·계획 같은
    # 게이트다. 그 노드를 막고도 마지막 틀에 닿으면(= 어떤 종료 경로가 그 노드를
    # 안 지나면) 분기로 게이트를 우회할 수 있다는 뜻이다 — `advance --to` 가
    # 실제로 그렇게 Verification 을 건너뛰는 것을 게이트 리뷰가 재현했다. 위상으로
    # 잡는다: 우회 가능한 그래프는 설정 단계에서 진단된다.
    for i in range(2, n - 1):
        if not ids[i] in seen:
            continue
        gate = (sts[i].get("exit_criteria") or []) or (sts[i].get("stop_requires") or [])
        if not gate:
            continue
        if ids[n - 1] in _bfs_reach(sts, ids[1], block=ids[i]):
            out.append(t("stages[%s] 는 게이트(종료 조건)를 가졌는데 그 노드를 지나지 "
                         "않는 종료 경로가 있다 — 분기로 우회할 수 있다. 모든 경로가 "
                         "지나도록 그래프를 고치거나, 게이트를 틀 노드로 옮겨라")
                       % ids[i])
    return out


def _graph_locked_by_plan(con, root, lid):
    """계획이 승인됐으면 그래프는 고정이다 — **계획 승인 = 그래프 승인.**

    승인 전(Selection~Planning)에는 자유롭게 그래프를 짜고, 승인 뒤에는 바꿀 수
    없다. 승인 뒤 노드를 더하거나 빼면 사람이 본 계획과 실제 실행 그래프가
    갈라진다(게이트 리뷰가 재현: 승인 후 `audit` 추가). 되돌아갈 일은 회차를
    닫고 다음 회차에서 다시 계획한다 — 뒤로 가지 않는다는 원칙과 같은 답이다.
    """
    if has_valid_evidence(con, root, lid, "plan_approved"):
        return t("계획이 이미 승인됐다 — 계획 승인이 곧 그래프 승인이라 이후에는 "
                 "그래프를 바꿀 수 없다. 승인 전에 짜거나, 회차를 닫고(`advance "
                 "--cycle`) 다음 회차에서 다시 계획하라.")
    return None


def path_add_block_reason(con, cfg, root, lid, node, after):
    """`path add` 가 불가능한 이유. 가능하면 None. 훅과 CLI 가 같은 함수를 쓴다.

    추가는 동의가 필요 없지만 **아무 데나·아무 때나** 자랄 수는 없다: 그래프는
    앞쪽으로만 자라고(지난 자리 뒤에는 못 붙인다), 틀은 고정이며(첫 틀 뒤·마지막
    틀 뒤는 끼울 자리가 아니다), 계획 승인 뒤에는 고정된다.
    """
    ids = stage_ids(cfg)
    if not node or not re.match(r"^[a-z][a-z0-9_-]{0,31}$", node):
        return t("노드 이름은 소문자·숫자·'-'·'_' 32자 이내여야 한다: %s") % (node or "")
    if node in ids:
        return t("'%s' 는 이미 있는 단계다 — 다른 이름을 써라") % node
    cur = stage_index(cfg, active_stage(con, lid) or ids[0])
    if cur >= len(ids) - 1:
        return t("마지막 틀(%s)에 있다 — 더 할 일이 남았으면 `advance --cycle` 로 "
                 "다음 회차를 열어라") % ids[-1]
    locked = _graph_locked_by_plan(con, root, lid)
    if locked:
        return locked
    if after not in ids:
        return t("앵커 '%s' 가 이번 회차의 그래프에 없다. `path` 로 그래프를 "
                 "확인하라") % after
    ai = ids.index(after)
    if ai == 0:
        return t("첫 틀(%s) 뒤에는 끼울 수 없다 — 틀 셋은 고정이고, 중간은 %s 뒤부터다") \
            % (ids[0], ids[1])
    if ai >= len(ids) - 1:
        return t("마지막 틀(%s) 뒤에는 끼울 수 없다 — 더 할 일이 남았으면 "
                 "`advance --cycle` 로 다음 회차를 열어라") % ids[-1]
    if ai < cur:
        return t("'%s' 는 이미 지난 자리다 — 그래프는 앞쪽으로만 자란다. 지금(%s) "
                 "이후의 노드를 앵커로 잡아라") % (after, active_stage(con, lid))
    return None


def path_remove_block_reason(con, cfg, root, lid, node):
    """`path remove` 가 불가능한 이유. 가능하면 None. 훅과 CLI 가 같은 함수를
    쓴다 — 다르면 사용자가 승인한 **뒤에** CLI 가 거부하고 다이얼로그가 반복된다."""
    st = next((s for s in cfg["stages"] if s.get("id") == node), None)
    if st is None or not st.get("__dyn__"):
        return t("'%s' 는 회차 한정 노드가 아니다 — stages.json 의 단계는 CLI 로 "
                 "지울 수 없다. 설정을 바꾸려면 그 파일을 고쳐라 (진단이 따라온다)") \
            % node
    row = con.execute("SELECT status FROM stage WHERE loop_id=? AND stage=?",
                      (lid, node)).fetchone()
    if row and row["status"] in ("active", "done", "skipped"):
        return t("'%s' 는 이미 방문했거나 방문 중이다 — 지나간 노드는 기록이라 "
                 "지울 수 없다") % node
    locked = _graph_locked_by_plan(con, root, lid)
    if locked:
        return locked
    return None


def file_prefix(con, lid):
    """`.dev/` 산출물 파일명 접두사. 앞단 해시로 grep 하면 한 작업이 모인다."""
    return "%s-%d-" % (lid, cycle_of(con, lid))


def evidence_digest(root, item):
    """이 증거가 가리키는 **파일의 지문.** 파일이 아니면 None.

    ## 왜 필요한가 — 증거에는 유효기간이 있다

    증거는 "언제 무엇을 봤다"를 적는데, **본 것이 그 뒤에 변할 수 있다.**
    `plan_approved` 는 계획 **파일**을 가리킨다. 승인을 받은 뒤 그 파일을 고쳐도
    승인 기록은 그대로 살아 있었고, 그래서 **사람이 보지 않은 계획으로 진행**할 수
    있었다 — 5차 리뷰가 HIGH 로 찾았다.

    승인만의 문제가 아니다. 파일을 가리키는 증거는 전부 같은 성질을 갖는다. 그래서
    `plan_approved` 만 따로 손보지 않고 **증거라는 것 자체에 지문을 붙인다.** 지문이
    다르면 그 증거가 말하는 사실은 더 이상 참이 아니다.

    파일이 아닌 증거(완료 조건 문장, `agent:Task`, 명령 문자열)는 지문이 없고,
    지문이 없는 증거는 늘 유효하다 — 변할 근거가 없다.
    """
    # 예전에는 `"/" not in item` 으로 걸렀다 — 명령 문자열을 빼려던 것인데,
    # **리포 루트의 파일은 상대경로에 슬래시가 없다.** `README.md` 를 승인하면
    # 지문이 안 붙어 만료 기능이 통째로 꺼졌다. 조건을 지운다. 파일이 아닌 것은
    # 아래 `isfile` 이 이미 걸러낸다.
    if not item or root is None:
        return None
    rel = rel_to_root(root, item)
    if not rel:
        return None
    p = os.path.join(root, rel)
    if not os.path.isfile(p):
        return None
    dig = hashlib.sha256()
    try:
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                dig.update(chunk)
    except OSError:
        return None
    return dig.hexdigest()


def evidence_rows(con, root, lid, kind):
    """(item, 아직 유효한가). 지문이 없으면 유효하다."""
    out = []
    for r in con.execute("SELECT * FROM evidence WHERE loop_id=? AND kind=?",
                         (lid, kind)):
        keys = r.keys()
        dig = r["digest"] if "digest" in keys else None
        out.append((r["item"], dig is None or dig == evidence_digest(root, r["item"])))
    return out


def has_valid_evidence(con, root, lid, kind):
    return any(ok for _, ok in evidence_rows(con, root, lid, kind))


def stale_evidence(con, root, lid, kind):
    """근거가 바뀌어 만료된 증거들."""
    return [it for it, ok in evidence_rows(con, root, lid, kind) if not ok]


def retro_window_start(con, lid):
    """**회고가 덮어야 할 범위**의 시작. 측정 창과 다른 질문이다.

    측정 창은 `cycle_adopt` 에서 새로 열려야 한다 — 그러지 않으면 버려진 회차의
    마찰이 다음 회차 기록으로 흡수돼 "회차 2: 차단 5" 같은 거짓 문장이 나온다.
    반대로 회고는 **이어받은 작업의 앞선 사실까지 덮어야** 한다 — 재연결했다고 해서
    이미 적어 둔 회고가 '못 찾음' 이 되면 안 된다.

    한 창이 두 뜻을 갖고 있어서 둘 중 하나가 늘 틀렸다. 뜻이 둘이면 창도 둘이다.
    """
    row = con.execute(
        "SELECT MAX(id) i FROM event WHERE loop_id=? AND kind='cycle_close'",
        (lid,)).fetchone()
    return (row["i"] or 0) if row else 0


def retro_files_of_loop(con, cfg, root, lid):
    """이 **작업**의 회고·학습 파일 (회차를 가리지 않는다).

    회차별로 좁히는 것은 `retro_files_of_cycle` 이다. 둘을 구분하는 이유는 질문이
    다르기 때문이다 — "이번 회차가 회고를 썼나" 와 "이 키가 나중에 찾아지나".
    """
    out = []
    for sub in recall_dirs(cfg):
        d = os.path.join(root, ".dev", sub)
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if n.startswith(lid + "-") and os.path.isfile(os.path.join(d, n)):
                out.append(os.path.join(d, n))
    return out


def retro_key_report(con, cfg, root, lid, lo):
    """(키, 찾은 키, 못 찾은 키). 검색과 **같은 범위**를 읽어 확인한다."""
    keys = cycle_search_keys(con, lid, lo)
    if not keys:
        return [], [], []
    # **검색 키의 창과 파일의 창을 맞춘다.** 키는 시간창(마지막 회차 종료 이후)에서
    # 오는데 파일은 접두사창(이번 회차)에서 왔다. `loop adopt` 가 회차만 올리고 측정
    # 창은 건드리지 않으므로 두 창이 갈라져, 이미 적어둔 키가 "못 찾음" 으로 나왔다 —
    # 4차 리뷰가 지적했다. 이 확인의 질문은 "나중에 찾아지나" 이므로 작업 전체가 맞다.
    hay = ""
    for path in retro_files_of_loop(con, cfg, root, lid):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                hay += "\n" + fh.read(recall_read_bytes(cfg)).lower()
        except OSError:
            continue
    found = [k for k in keys if k.lower() in hay]
    return keys, found, [k for k in keys if k not in found]


def fs_evidence(cfg, root, prefix, kind):
    """그 산출물이 **디스크에 실제로 있나**. 있으면 상대 경로, 없으면 None.

    증거를 PostToolUse 관측에만 의존하면 훅이 없는 환경에서 조건이 영원히 안
    채워진다. 훅이 없는 환경은 예외가 아니라 다수다 — 다른 에이전트 도구
    (Codex 는 셸만 가로챈다), 사람이 직접 쓴 파일, 훅이 실패한 세션.
    파일 존재는 관측 없이도 알 수 있는 사실이므로 관측을 기다리지 않고 본다.

    이 회차의 접두사를 **요구한다**. 요구하지 않으면 지난 회차의 계획서가
    이번 회차의 게이트를 열고, 그건 사람 없이 게이트가 열리는 것이다.
    접두사가 면제된 누적 문서(INDEX.md)도 이 요구에 걸려 제외된다 — 맞다,
    인덱스는 이번 회차가 무엇을 했다는 증거가 아니다.
    """
    if not prefix:
        return None
    for pat in cfg.seq("criteria.%s.write_glob" % kind):
        # 글롭의 앞쪽 리터럴만 떼어 그 디렉터리부터 걷는다. 저장소 전체를 걷지 않는다.
        base = pat.split("*")[0].rstrip("/")
        d = os.path.join(root, base)
        if not os.path.isdir(d):
            continue
        for dirpath, _, names in os.walk(d):
            for n in sorted(names):
                if not n.startswith(prefix):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, n), root).replace(os.sep, "/")
                if glob_match(rel, pat):
                    return rel
    return None


def exit_blockers(con, cfg, root, lid, sid):
    return [k for k in stage_obj(cfg, sid).get("exit_criteria", [])
            if not criterion_met(con, cfg, root, lid, k)]


def criterion_met(con, cfg, root, lid, kind):
    """종료 조건 충족 여부. **어떻게 판정하는지는 어휘가 정한다.**

    `criteria.<이름>.satisfied_by` 가 판정 방식을 고른다. 예전에는 이 함수가
    조건 이름을 알고 있었고(`promotion_decided` 특수 분기), 그래서 조건을 더하거나
    이름을 바꾸려면 파이썬을 고쳐야 했다. 엔진이 알아야 하는 것은 **방식**이지
    이름이 아니다.

      cli                    사람·모델이 명령으로 기록한다 (evidence 행)
      file                   산출물이 디스크에 있으면 충족 (관측 불필요)
      observed               도구 사용을 관측해 적립한다 (실패한 실행은 제외)
      no_pending_promotions  프로젝트 전체의 미결 승격이 없으면 충족

    file·observed 도 evidence 행이 있으면 먼저 인정한다 — 관측이 잡혔다면 그게
    가장 이른 사실이다. 게이트는 **아무도 아무것도 미리 실행하지 않아도** 맞아야
    한다(is_regressed 와 같은 원칙). 여기서 evidence 행을 새로 쓰지는 않는다.
    판정 경로가 쓰기를 하면 훅의 트랜잭션 상태에 얹혀 실패할 수 있고, 판정은
    매번 다시 하면 되는 계산이다.
    """
    how = cfg.at("criteria.%s.satisfied_by" % kind, "cli")
    if how == "no_pending_promotions":
        # evidence 행으로 심어두면 안 된다 — 다음 회차에 새로 생긴 반복을 놓친다.
        return not pending_promotions(con, cfg)
    # **증거가 있는 것과 그 증거가 아직 참인 것은 다르다.** 파일을 가리키는 증거는
    # 그 파일이 바뀌면 만료된다 — 자세한 근거는 `evidence_digest` 에 적었다.
    if has_valid_evidence(con, root, lid, kind):
        return True
    if how == "file":
        return fs_evidence(cfg, root, file_prefix(con, lid), kind) is not None
    return False


def criterion_help(cfg, kind):
    return t(cfg.at("criteria.%s.help" % kind, kind))


def criterion_why(con, cfg, root, lid, kind):
    """왜 아직 안 됐는지. **만료된 증거가 있으면 그 사실을 먼저 말한다.**

    "계획 승인이 필요하다" 만 보면 이미 승인한 사람은 왜 또냐고 생각하고, 하네스가
    고장 난 것으로 읽는다. 막을 때는 최적 행동을 함께 준다 — 그러려면 무엇이 달라졌는지
    부터 말해야 한다.
    """
    stale = stale_evidence(con, root, lid, kind)
    if stale:
        return t("'%s' 이(가) 그때 본 것과 달라졌다 — 승인·관측은 그 시점의 내용에 "
                 "대한 것이므로 만료됐다. %s") % (", ".join(stale),
                                            criterion_help(cfg, kind))
    return criterion_help(cfg, kind)


def evidence_stages(cfg, kind="verification_evidence"):
    """그 증거를 관측해 적립하는 단계들."""
    return tuple(cfg.seq("criteria.%s.stages" % kind))


def auto_skip_state(con):
    """(활성, 만료사유) — 범위·횟수 만료까지 반영한 실제 상태."""
    if get_meta(con, "auto_skip") != "on":
        return False, None
    scope = get_meta(con, "auto_skip_loop")
    if scope and scope != head_loop(con):
        return False, t("작업 %s 범위였고 작업이 바뀌어 만료됐다") % scope
    uses = get_meta(con, "auto_skip_uses")
    if uses:
        try:
            if int(uses) <= 0:
                return False, t("사용 횟수를 모두 소진해 만료됐다")
        except ValueError:
            pass
    return True, None


def auto_skip_on(con):
    return auto_skip_state(con)[0]


def auto_skip_uses_left(con):
    uses = get_meta(con, "auto_skip_uses")
    try:
        return int(uses) if uses else None
    except ValueError:
        return None


def auto_skip_scope_note(con):
    bits = []
    scope = get_meta(con, "auto_skip_loop")
    if scope:
        bits.append(t("작업 %s 범위") % scope)
    left = auto_skip_uses_left(con)
    if left is not None:
        bits.append(t("남은 %d회") % left)
    return ", ".join(bits) or t("무제한")


def skip_route(cfg, sid, target):
    """`(불가 사유 | None, 스킵할 노드들[현재..대상])` — **기본 경로** 기준.

    스킵의 `+N`/`until:` 은 배열이 아니라 경로를 잰다. 분기가 있으면 첫 선언을
    따른다 — 경로 밖의 노드로 가는 것은 스킵이 아니라 **분기 선택**이고, 그건
    `advance --to` 의 일이다. 두 어휘를 섞으면 스킵이 분기 결정을 삼킨다.
    """
    ids = stage_ids(cfg)
    cur = stage_index(cfg, sid)
    path = default_path(cfg, sid)
    back = (t("뒤로 갈 수는 없다 — 이미 %s 단계이거나 그보다 뒤다. "
              "단계는 항상 앞으로만 간다.") % label_of(cfg, sid))

    def off_path(want):
        return (t("'%s' 는 여기서의 기본 경로에 없다 — 분기의 다른 갈래라면 "
                  "`advance --to <단계> --reason \"...\"` 로 고르고, 어느 경로로도 "
                  "닿지 않는 노드라면 stages.json 을 고쳐야 한다. 그래프는 `path` 로 "
                  "본다.") % want)

    if target.startswith("+"):
        try:
            n = int(target[1:])
        except ValueError:
            return t("잘못된 형식: %s") % target, None
        if n < 1:
            return back, None
        return None, path[:min(n, len(path))]
    if target.startswith("until:"):
        want = target.split(":", 1)[1]
        if want not in ids:
            return t("알 수 없는 단계: %s") % want, None
        if ids.index(want) <= cur:
            return back, None
        if want not in path:
            return off_path(want), None
        return None, path[:path.index(want)]
    if target not in ids:
        return t("알 수 없는 대상: %s") % target, None
    if ids.index(target) < cur:
        return back, None
    if target not in path:
        return off_path(target), None
    return None, path[:path.index(target) + 1]


def skip_block_reason(cfg, sid, target, con=None, root=None, lid=None):
    """skip 이 **불가능한** 이유. 가능하면 None.

    훅과 CLI 가 같은 함수를 쓴다. 다른 규칙을 쓰면 사용자가 승인한 뒤에 거부되고,
    모델은 안내받은 명령을 다시 시도해 **다이얼로그가 무한 반복된다** — 실제로
    도그푸딩에서 그렇게 됐다.
    """
    ids = stage_ids(cfg)
    cur = stage_index(cfg, sid)
    last = cfg["stages"][-1]["id"]
    err, route = skip_route(cfg, sid, target)
    if err:
        return err

    # **분기를 지나는 스킵은 어느 갈래인지 정하지 못한다.** skip 은 앞으로 곧장
    # 건너뛰는 단순 동작이라 갈림길을 만나면 첫 엣지를 조용히 골랐다 — branch
    # 이벤트도, `--to` 게이트도 없이(위상 리뷰가 재현). 분기 결정은 `advance --to`
    # 의 일이다. route 안의 분기(default_path 가 이미 첫 엣지를 택한 자리)와,
    # 도착 뒤 이어질 분기(_enter 가 첫 엣지를 택할 자리) 둘 다 거절한다.
    for x in route[:-1]:
        if len(successors(cfg, x)) > 1:
            return (t("'%s' 는 분기 노드다 — 지나는 스킵은 어느 갈래인지 정하지 "
                      "못한다. `advance --to <단계> --reason \"...\"` 로 먼저 지나라.")
                    % x)
    if route[-1] != last and len(successors(cfg, route[-1])) > 1:
        return (t("건너뛴 도착지(%s) 다음이 분기다 — 스킵이 갈래를 대신 고를 수 없다. "
                  "거기까지 간 뒤 `advance --to <단계> --reason \"...\"` 로 골라라.")
                % route[-1])

    locked = [x for x in route if stage_obj(cfg, x).get("skippable") is False]
    if not locked:
        # 여기까지 왔으면 이동 자체는 가능하다. 남은 것은 **CLI 가 실제로 요구하는
        # 기록**이다. 훅이 이것을 모르면 사용자가 승인한 **뒤에** CLI 가 거부하고,
        # 모델은 안내받은 명령을 다시 시도해 다이얼로그가 무한 반복된다 — 이 함수가
        # 존재하는 이유가 바로 그것인데 정작 이 축을 안 보고 있었다.
        if con is not None:
            for x in route:
                for key in stage_obj(cfg, x).get("skip_requires") or []:
                    if not criterion_met(con, cfg, root, lid, key):
                        return (t("%s 를 건너뛰더라도 기록은 남겨야 한다: %s "
                                  "먼저 그 기록을 남긴 뒤 다시 시도하라 — 승인만 "
                                  "면제된다.")
                                % (stage_obj(cfg, x)["label"],
                                   criterion_why(con, cfg, root, lid, key)))
        return None

    names = ", ".join(stage_obj(cfg, x)["label"] for x in locked)
    if ids[cur] == ids[0]:
        # 여기서 예전에는 `skip until:selection` 을 안내했다. 그건 dest 가 -1 이 되어
        # **항상 실패하는 명령**이고, 모델이 그대로 반복해 승인 요청이 무한히 떴다.
        return (t("%s 단계는 건너뛸 수 없다 — 작업을 정하지 않고 넘어가면 이후 모든 단계가 "
                "기준 없이 돌아간다. 스킵이 아니라 **작업을 고르는 것**이 다음 행동이다: "
                "`harness status` 가 하네스가 아는 할 일을 후보로 보여준다(승격 결정, "
                "재발한 승격, 낡은 인덱스, 예산 소진). 고르면 "
                "`harness loop intent \"...\"` 와 `harness loop done-when \"...\"` 로 "
                "기록하고 진행하라. 그 후보들까지 정말 필요 없으면 그렇다고 말하고 "
                "멈춰라 — 그건 교착이 아니라 정상 종료다.") % names)
    if cur >= len(ids) - 1:
        # 마지막 단계에서 `until:<last>` 는 dest = index-1 이라 **항상 실패한다.**
        # Selection 쪽에서 고친 것과 같은 막다른 길이 반대편 끝에 남아 있었다.
        return (t("%s 단계는 건너뛸 수 없다 — 여기가 마지막이므로 건너뛸 곳이 없다. "
                  "이 회차를 닫는 것이 다음 행동이다: 중단 사유를 회고로 남기고 "
                  "`harness advance --cycle` (후속 회차) 또는 `harness advance --done` "
                  "(작업 종료) 을 실행하라.") % names)
    return (t("%s 단계는 건너뛸 수 없다. 이 회차를 중단하려면 "
            "`harness skip until:%s --reason \"...\"` 로 %s 까지 이동한 뒤, 중단 사유를 "
            "회고로 남기고 `harness advance --cycle` (또는 `--done`) 으로 닫아라.")
            % (names, last, stage_obj(cfg, last)["label"]))


def verification_hit(cfg, cmd):
    """이 명령이 **검증을 실행하나.** 문자열 어딘가에 이름이 있는 것과 다르다.

    `bash_pattern` 을 명령 전체에 `re.search` 했더니 아래가 전부 증거로 적립됐다.

        echo "npm test"        git commit -m "ran npm test"        cat tsc.log

    패턴이 아니라 **어디에 대고 맞추는가**가 문제였다. 세그먼트로 쪼개고, 따옴표 안은
    데이터이므로 지우고, 읽기 명령(`bash.readers`)은 아무것도 실행하지 않으므로 건너뛴다.
    `sh -c "npm test"` 도 이제 증거가 아니다 — 게이트가 잘못 열리는 것보다 `harness
    verify` 를 한 번 더 치는 편이 낫다.
    """
    pat = cfg.at("criteria.verification_evidence.bash_pattern")
    if not pat:
        return False
    try:
        vre = re.compile(pat)
    except re.error:
        return False
    readers = tuple(cfg.seq("bash.readers", BASH_READERS_DEFAULT)) + ("echo", "printf")
    for seg in BASH_SPLIT.split(cmd):
        bare = QUOTED_RE.sub(" ", seg).strip()
        if not bare:
            continue
        # 앞의 `VAR=값`(과 `env` 접두)은 대입이지 프로그램이 아니다. 건너뛰지
        # 않으면 `CI=true npm test` 가 머리 앵커에 영원히 안 맞아 **정직하게
        # 테스트를 돌린 명령**이 증거가 안 됐다(6회차 실측). `bash_unresolved`·
        # `floor_named` 는 이미 같은 이유로 ASSIGN_RE 를 건너뛴다.
        toks = bare.split()
        while toks and ASSIGN_RE.match(toks[0]):
            toks.pop(0)
        if toks and os.path.basename(toks[0].strip("\"'")) == "env":
            toks.pop(0)
            while toks and (ASSIGN_RE.match(toks[0]) or toks[0].startswith("-")):
                toks.pop(0)
        if not toks:
            continue
        bare = " ".join(toks)
        if os.path.basename(toks[0].strip("\"'")) in readers:
            continue
        # **머리에서부터** 맞아야 한다. `search` 였을 때 `true npm test` 가 통과했다 —
        # 실행되는 프로그램은 `true` 인데 인정된 것은 `npm test` 였다. `# npm test`,
        # `false npm test`, `sleep 0 npm test` 도 같았다. 무해한 머리 이름을 목록으로
        # 모으는 것은 끝이 없다(`time`·`env`·`nice` 는 정상이다). 대신 패턴이
        # **프로그램 자리**를 가리키게 한다 — 그것이 패턴의 원래 뜻이다.
        if vre.match(bare):
            return True
    return False


def tool_failed(inp):
    """이 도구 호출이 실패했나. 판단이 안 되면 False (실패라고 단정하지 않는다).

    왜 필요한가: `bash_pattern` 은 **명령 문자열만** 봤다. 그래서 `pytest` 를
    돌려 3개가 깨져도 verification_evidence 가 적립되고 Verification 게이트가
    열렸다. 실제로 그렇게 통과하는 것을 확인했다. 테스트를 돌린 것과 통과한
    것은 다른 사실인데 같은 것으로 세고 있었다.

    실패가 PostToolUse 로 오는지 PostToolUseFailure 로만 오는지는 문서에 없다.
    어느 쪽이든 안전하게 두 곳 다 이 검사를 통과해야 적립된다 — 실패가 저쪽으로
    간다면 이 검사는 한 번도 걸리지 않을 뿐이고, 이쪽으로 온다면 구멍이 닫힌다.
    """
    resp = inp.get("tool_response")
    if isinstance(resp, dict):
        if resp.get("isError") or resp.get("interrupted") or resp.get("is_error"):
            return True
        code = resp.get("exit_code", resp.get("exitCode"))
        if isinstance(code, int) and code != 0:
            return True
    for key in ("tool_error", "error"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            return True
        if v is True:
            return True
    return False


def self_stop_budget(con, cfg, lid, sid, stage, prompt_id, problems, limits):
    """차단 예산을 소비하고 `(막을 것, 소진된 것)` 을 돌려준다."""
    blocked, exhausted = [], []
    with con:
        for key, text in problems:
            # 세고-넣으면 동시에 뜬 Stop 훅 넷이 상한 1 을 넷 다 쓴다. 세는 것을
            # INSERT 의 WHERE 안으로 옮겨 rowcount 가 승자를 정하게 한다.
            if not claim(con, "INSERT INTO stop_block(prompt_id,key,at) "
                              "SELECT ?,?,? WHERE (SELECT COUNT(*) FROM stop_block "
                              "WHERE prompt_id=? AND key=?) < ?",
                         (prompt_id, key, now(), prompt_id, key,
                          int(limits.get(key, 1)))):
                exhausted.append(key)
                continue
            record_event(con, lid, sid, "stop_gate", key, stage["id"], text)
            blocked.append(text)
        for key in exhausted:
            record_event(con, lid, sid, "bypass", key, stage["id"],
                         t("차단 상한 소진으로 미충족 상태 종료"))
    return blocked, exhausted


# 하네스가 **스스로** 남기는 기록은 진전이 아니다. 이걸 빼지 않으면 이어붙임
# 이벤트가 이벤트 수를 늘려 지문이 매번 바뀌고, 진전 감지가 자기 자신을 진전으로
# 세면서 영원히 발동하지 않는다 — 실제로 그렇게 만들어서 5회 헛돌았다.
FP_IGNORE_KINDS = ("stop_continue", "stop_stalled", "stop_gate", "bypass",
                   "cycle_close", "cycle_adopt")


def progress_fingerprint(con, lid, sid):
    """'하네스가 아는 진전'의 지문.

    읽기만 한 턴은 지문이 그대로다 — 그건 의도한 것이다. 단계 종료 조건은
    증거·모델 활동·단계 전이로만 채워지므로, 그 셋이 그대로면 종료에 가까워지지
    않았다. 지문이 연속으로 같으면 이어붙여도 같은 자리를 돈다.
    """
    ev = con.execute("SELECT COUNT(*) c FROM evidence WHERE loop_id=?",
                     (lid,)).fetchone()["c"]
    n = con.execute(
        "SELECT COUNT(*) c FROM event WHERE loop_id=? AND kind NOT IN (%s)"
        % ",".join("?" * len(FP_IGNORE_KINDS)),
        (lid,) + FP_IGNORE_KINDS).fetchone()["c"]
    return "%s:%d:%d" % (sid, ev, n)


def stalled_rounds(con, lid, prompt_id, fp):
    """이 프롬프트에서 지문이 연속 몇 번 그대로였나.

    작업(loop_id)으로도 가둔다. prompt_id 가 없는 환경에서 과거 기록이 새 작업의
    예산을 깎는 것을 막는다.
    """
    seen = [r["detail"] for r in con.execute(
        "SELECT detail FROM event WHERE kind='stop_continue' AND target=? "
        "AND loop_id=? ORDER BY id", (prompt_id, lid))]
    stalled = 0
    for d in reversed(seen):
        if d != fp:
            break
        stalled += 1
    return stalled, len(seen)


def continue_or_stop(con, cfg, root, lid, sid, stage, prompt_id):
    """종료 조건을 다 채웠는데 단계가 남았으면 턴 종료를 막아 이어붙인다.

    하네스는 원래 반응만 하고 턴을 시작하지 않는다. 이건 그 한계를 Stop 훅으로
    미는 것이고, 무인 실행의 유일한 추진 장치다.

    **진전 감지가 이 기능을 켤 수 있게 만든 조건이다.** 없이 켰을 때 첫 e2e 에서
    모델이 불가능한 명령을 4회 반복하며 헛돌았다. 진전이 없으면 이어붙이지
    않으므로, 진전이 있을 때는 상한을 넉넉히 줄 수 있다.
    """
    if not cfg.at("stop_continue.enabled"):
        return
    left = con.execute("SELECT COUNT(*) c FROM stage WHERE loop_id=? "
                       "AND status IN ('pending','active')", (lid,)).fetchone()["c"]
    if not left:
        return
    # 사람만 채울 수 있는 조건이 남았으면 **밀지 않는다.** 여기서 밀면 모델이
    # 만들 수 없는 것을 만들려 애쓰고, 그 시도가 매번 승인 다이얼로그가 된다.
    # Selection 에 작업이 없는 것은 교착이 아니라 **사람을 기다리는 상태**다.
    waiting = [k for k in exit_blockers(con, cfg, root, lid, sid)
               if k in human_criteria(cfg)]
    if waiting:
        note = ""
        if "intent_set" in waiting:
            try:
                n = len(work_candidates(con, cfg, root))
            except Exception:
                n = 0
            if n:
                note = (t(" 다만 하네스가 아는 할 일이 %d개 있다 — `harness status` 로 "
                        "확인하고 고를 수 있다.") % n)
        return emit({"systemMessage":
                     t("harness: %s 단계에서 사람의 입력을 기다린다 (%s). 턴을 끝낸다.%s")
                     % (stage["label"], ", ".join(waiting), note)})
    limit = cfg.num("stop_continue.max_per_prompt", 6, low=1)
    no_prog = cfg.num("stop_continue.no_progress_limit", 2, low=1)
    fp = progress_fingerprint(con, lid, sid)
    stalled, used = stalled_rounds(con, lid, prompt_id, fp)

    if stalled >= no_prog:
        # 조용히 놓아주지 않는다. 헛돈 사실이 기록되고 사용자에게 보인다.
        with con:
            record_event(con, lid, sid, "stop_stalled", stage["id"], prompt_id,
                         t("지문 %s 가 %d회 연속 그대로 — 이어붙이기를 멈춘다") % (fp, stalled))
        return emit({"systemMessage": (
            t("harness: %d회 이어붙였으나 진전이 없어 멈춘다 (%s 단계). 같은 지시를 "
            "반복하는 대신 무엇이 막고 있는지 사람에게 물어라.") % (used, stage["label"]))})
    if used >= limit:
        with con:
            record_event(con, lid, sid, "bypass", "continue_limit", stage["id"],
                         t("이어붙임 상한 %d 소진") % limit)
        return emit({"systemMessage":
                     t("harness: 이어붙임 상한 %d회를 소진해 턴을 끝낸다 (%s 단계).")
                     % (limit, stage["label"])})

    with con:
        record_event(con, lid, sid, "stop_continue", str(used + 1), prompt_id, fp)
    missing = exit_blockers(con, cfg, root, lid, sid)
    todo = (t("이 단계의 남은 종료 조건: %s") % ", ".join(missing) if missing
            else t("이 단계의 종료 조건은 채웠다 — `harness advance` 로 넘어가라"))
    return emit({"decision": "block", "reason": (
        t("작업이 아직 끝나지 않았다 (현재 %s, 남은 단계 %d). 멈추지 말고 이어서 진행하라. "
        "%s. 작업이 정말 끝났으면 Compounding 에서 `harness advance --done` 으로 닫아라. "
        "(이어붙임 %d/%d)") % (stage["label"], left, todo, used + 1, limit))})


# 종료 조건 쪽 탐침. "이 조건이 자기 산출물이 아닌 것도 받아들이나" 를 본다.
# `write_glob: ["**"]` 로 넓히면 접두사만 맞는 아무 파일이 사람의 승인이 됐다 —
# 개수를 세는 요약으로는 안 보였다(Codex Claim C HIGH).
UNRELATED = ("src/a.py", "README.md", ".claude/settings.json", "Makefile")


# 검증 증거로 인정되면 안 되는 명령. 텍스트를 읽지 않고 넣어 본다.
# 검증이 **아닌** 명령. 5차 리뷰가 뚫은 모양(문자열 안에 이름만 있는 것)을 넣었다 —
# 탐침이 실제로 뚫린 자리를 담고 있어야 자기검사가 거짓말을 하지 않는다.
NOT_VERIFICATION = ("ls", "echo hi", "true", "cat README.md", "git status", "pwd",
                    'echo "npm test"', 'git commit -m "ran npm test"',
                    "cat tsc.log", "grep -rn pytest src/", "ls | grep vitest",
                    # **무해한 머리 + 진짜 테스트 이름.** 이 모양이 없어서 머리 앵커링을
                    # 없애는 뮤테이션이 조용히 통과했다 — 탐침이 막는 것을 증명해야 한다.
                    "true npm test", "sleep 0 pytest", "# npm test", "false npm test")


# 이 도구를 썼다는 사실만으로 검증이 됐다고 볼 수 없다. 읽기·탐색은 검증이 아니다.
# MCP 이름도 넣는다 — 브라우저 목록 조회가 검증으로 세어졌는데 탐침이 그 자리를 못 봤다.
INNOCUOUS_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit", "WebFetch", "TodoWrite",
                   "mcp__claude-in-chrome__list_connected_browsers",
                   "mcp__claude-in-chrome__tabs_close_mcp")


# **인정되어야 하는** 검증 명령. 과다만 재고 과소를 안 재면, 정직하게 테스트를 돌린
# 프로젝트일수록 게이트가 안 열리고 사용자는 스킵으로 빠진다 — 그것이 게이트를 끄는 길이다.
MUST_VERIFY = ("npm test", "pnpm -r test", "yarn workspace app test", "bun test",
               "python -m pytest", "uv run pytest", "poetry run pytest", "tox",
               "npx playwright test", "npx vitest run", "pytest -q", "vitest",
               "go test ./...", "cargo test", "cargo nextest run",
               "mvn -q test", "gradle check", "./gradlew test", "dotnet test",
               "deno test", "swift test", "ctest", "rspec", "bin/rails test",
               "make check", "make test", "npx tsc --noEmit", "ruff check .", "mypy .")


def _enter(ctx, dest_idx):
    """dest_idx 단계를 active 로. 범위를 넘으면 루프를 닫고 새 루프를 만든다."""
    con, cfg, root, lid = ctx.con, ctx.cfg, ctx.root, ctx.lid
    if dest_idx >= len(cfg["stages"]):
        close_loop(con, cfg, lid, "cycle_close")
        return create_loop(con, cfg, root), cfg["stages"][0]["id"], True
    sid = cfg["stages"][dest_idx]["id"]
    # 행은 **회차 한정 노드에 한해** 지연 생성한다. 작업 생성 때는 없던 노드(회차
    # 중 `path add`)가 여기서 처음 실체를 얻는다. **설정 단계에는 만들지 않는다** —
    # 설정 단계인데 행이 없으면 그건 상태·설정 드리프트(id 변경)이고, 그때 아래
    # claim 이 0행으로 져서 "다음 단계 행이 없다"가 살아나야 한다. 모든 id 에
    # ensure 하면 그 방어선이 죽어 낡은 행이 유령으로 남는다(위상 리뷰가 지적).
    if cfg["stages"][dest_idx].get("__dyn__"):
        ensure_stage_row(con, lid, sid)
    # **들어가는 쪽도 차지해야 한다.** 떠나는 쪽에만 claim 을 붙였더니, 대상 단계 행이
    # 없을 때(설정에서 id 가 바뀐 경우) 0행 갱신인데도 "→ 단계 N" 이라고 말하고
    # 활성 단계가 0개인 루프가 남았다. 다음 명령이 그 작업을 말없이 버렸다.
    if not stage_set(con, "enter", (now(), lid, sid)):
        return lid, None, False
    return lid, sid, False


def next_cycle(ctx):
    """같은 작업의 다음 회차. Selection 은 유지하고 나머지 단계를 초기화한다.

    증거를 초기화하지 않으면 2회차 Planning 이 1회차 계획서로 통과한다.
    intent_set 과 acceptance(완료 조건)는 남긴다 — 작업은 그대로이므로 다시
    선정할 필요가 없다. 이전 회차의 계획·회고 파일은 파일로 남고, 파일명의
    회차로 구분된다.
    """
    con, cfg, lid = ctx.con, ctx.cfg, ctx.lid
    # **회차 한정 노드는 회차와 함께 사라진다.** 행과 그래프에서 걷어낸다 —
    # path_node 행은 (loop, cycle) 스코프라 회차가 오르면 저절로 조회 밖이고,
    # 이력은 event(path_add) 가 갖고 있다.
    for s in [x for x in cfg["stages"] if x.get("__dyn__")]:
        con.execute("DELETE FROM stage WHERE loop_id=? AND stage=?",
                    (lid, s["id"]))
    cfg["stages"] = [x for x in cfg["stages"] if not x.get("__dyn__")]
    ids = stage_ids(cfg)
    # 작업 정의(무엇을·무엇이 끝인지)는 회차를 넘어 유지한다. 회차마다 다시 선언하게
    # 하면 긴 작업에서 기준이 표류한다 — 그게 완료 조건을 두는 이유와 정면으로 어긋난다.
    con.execute("DELETE FROM evidence WHERE loop_id=? "
                "AND kind NOT IN ('intent_set','acceptance')", (lid,))
    con.execute("DELETE FROM wgrant WHERE loop_id=?", (lid,))
    stage_set(con, "reset", (lid, ids[0]))
    con.execute("UPDATE loop SET cycle=cycle+1 WHERE id=?", (lid,))
    stage_set(con, "enter", (now(), lid, ids[1]))
    return ids[1]
