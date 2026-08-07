"""상태 저장소. SQL 은 여기에만 있다.

엔진이 이름공간을 맞춰 준다 — `parts/__init__.py` 참고.
"""


ADDED_COLUMNS = (("evidence", "digest", "TEXT"),
                 # 승격 시점의 **이벤트 id**. 재발 판정을 벽시계에서 여기로 옮긴다.
                 ("promotion", "after_id", "INTEGER"))


def migrate(con):
    """빠진 열을 채운다. 이미 있으면 아무것도 하지 않는다."""
    for table, col, typ in ADDED_COLUMNS:
        cols = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
        if cols and col not in cols:
            con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))


def connect(root, create=False):
    path = os.path.join(root, DB_REL)
    if not create and not os.path.isfile(path):
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=float(DB_WAIT_S))
    con.row_factory = sqlite3.Row
    # busy_timeout 이 **먼저**여야 한다. SQLite 는 journal mode 전환에 busy handler 를
    # 부르지 않으므로, 순서가 뒤면 동시 init 이 `database is locked` 로 죽는다.
    con.execute("PRAGMA busy_timeout=%d" % (DB_WAIT_S * 1000))
    con.execute("PRAGMA journal_mode=WAL")
    try:
        migrate(con)
        con.commit()
    except sqlite3.Error:
        pass  # 읽기 전용 파일시스템에서도 판정은 돌아야 한다
    return con


def claim(con, sql, params):
    """조건부 UPDATE 로 **한 번만** 일어나는 일을 차지한다. 이겼으면 True.

    읽고-판단하고-쓰면 병렬 훅이 같은 자원을 여러 번 쓴다. `--uses 1` 예외를 넷이
    동시에 쓰고 `uses_left` 가 -3 이 되는 것을 재현했고, 같은 모양이 자동 승인 횟수와
    단계 전이에도 있었다. SQLite 의 조건부 UPDATE 는 원자적이므로 **rowcount 가
    승자를 정한다** — 판단을 WHERE 절 안으로 옮기는 것이 요점이다.
    """
    return con.execute(sql, params).rowcount > 0


def get_meta(con, k, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def set_meta(con, k, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def head_loop(con):
    return get_meta(con, "head")


def cycle_of(con, lid):
    """이 작업의 현재 회차. Compounding → Scaffolding 으로 돌 때마다 늘어난다."""
    row = loop_row(con, lid)
    try:
        return int(row["cycle"]) if row and row["cycle"] else 1
    except (TypeError, ValueError, IndexError):
        return 1


def create_loop(con, cfg, root, intent=None, loop_id=None, only_if_none=False):
    """작업 하나와 그 단계들을 만든다. 만들어진(또는 이미 있던) 작업 id.

    `only_if_none` 은 **열린 작업이 없을 때만** 만든다. 읽고-판단하고-쓰면 병렬
    호출이 각자 하나씩 만든다 — 보기만 하는 `status` 넷을 동시에 돌렸더니 열린
    작업이 넷 생겼다. 판단을 INSERT 의 WHERE 안으로 옮겨 승자만 만들게 한다.
    """
    lid = loop_id or new_loop_id()
    sql, args = ("INSERT OR IGNORE INTO loop(id,intent,branch,created_at) "
                 "VALUES(?,?,?,?)", (lid, intent, git_branch(root), now()))
    if only_if_none:
        sql = ("INSERT INTO loop(id,intent,branch,created_at) SELECT ?,?,?,? "
               "WHERE NOT EXISTS (SELECT 1 FROM loop WHERE closed_at IS NULL)")
        if not claim(con, sql, args):
            row = con.execute("SELECT id FROM loop WHERE closed_at IS NULL "
                              "ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
            if row:
                return row["id"]
    else:
        con.execute(sql, args)
    for i, st in enumerate(cfg["stages"]):
        con.execute("INSERT OR IGNORE INTO stage(loop_id,stage,status,entered_at) "
                    "VALUES(?,?,?,?)",
                    (lid, st["id"], "active" if i == 0 else "pending",
                     now() if i == 0 else None))
    set_meta(con, "head", lid)
    return lid


def end_cycle(con, cfg, lid, kind):
    """회차를 끝내는 **공통 절차**: 집계를 남기고 진행 중 상태를 버린다.

    `rotate_loop` 과 `close_loop` 이 이것을 각자 갖고 있었다. 그래서 한쪽에만
    스냅샷을 넣었을 때 다른 쪽(`loop adopt`)이 옆문이 됐고, 마찰이 쌓인 회차를
    한 줄로 지울 수 있었다(4회차 C③). **"회차를 끝낸다" 의 뜻은 한 곳에 있다.**

    닫기 자체(누가 이겼나)는 호출자가 정한다 — `rotate_loop` 은 claim 으로,
    `close_loop` 은 무조건. 그것만이 둘의 진짜 차이다.
    """
    stages = cfg.get("stages") or [{}]
    with swallow(t("회차 스냅샷")):
        record_cycle_close(con, cfg, lid,
                           active_stage(con, lid) or stages[0].get("id") or "-", kind)
    for tbl in ("stage", "evidence", "wgrant"):
        con.execute("DELETE FROM %s WHERE loop_id=?" % tbl, (lid,))


def rotate_loop(con, cfg, root, lid, intent=None):
    """이 작업을 닫고 다음을 연다. **닫기를 차지한 쪽만 연다** (진 쪽에는 None).

    닫기와 열기가 두 걸음이면 병렬 호출이 각자 하나씩 연다 — `loop new` 넷을
    동시에 돌렸더니 열린 작업이 셋 남았다.
    """
    if not claim(con, "UPDATE loop SET closed_at=? WHERE id=? AND closed_at IS NULL",
                 (now(), lid)):
        return None
    end_cycle(con, cfg, lid, "cycle_close")
    return create_loop(con, cfg, root, intent)


def close_loop(con, cfg, lid, kind):
    """루프의 작업 상태를 버린다. 영구 기록은 폴더의 파일명이 갖고 있다.

    남기는 것: loop 인덱스(해시·의도·기간)와 event(관측 기록).
    event 를 버리면 복리의 원료가 사라지고, loop 인덱스가 없으면 event 가
    어느 작업의 것인지 알 수 없다. 버리는 것은 진행 중 상태뿐이다.

    **닫기 전에 스냅샷을 남긴다.** `rotate_loop` 에만 있었더니 `loop adopt` 가
    옆문이 됐다 — 진행 중 회차의 마찰이 어느 측정 창에도 속하지 못하고 사라져서,
    "차단이 쌓인 회차를 없애려면 `harness loop adopt <아무 id>` 한 줄" 이 됐다
    (4회차 C③ 실측). 스냅샷은 회차당 하나이므로(`record_cycle_close`) 이미
    남겼으면 여기서 두 번 남지 않는다.
    """
    end_cycle(con, cfg, lid, kind)
    con.execute("UPDATE loop SET closed_at=? WHERE id=?", (now(), lid))


# 단계 전이 SQL. 여섯 자리에 흩어져 있었고 셋은 `claim` 안, 셋은 무조건이었다.
# **문장은 한 곳에, 경쟁 판정은 호출자가.** 흩어져 있으면 열이 늘 때 하나가 빠진다.
STAGE_SET = {
    "enter":   ("UPDATE stage SET status='active', entered_at=? "
                "WHERE loop_id=? AND stage=?"),
    "done":    ("UPDATE stage SET status='done', left_at=? "
                "WHERE loop_id=? AND stage=? AND status='active'"),
    "skipped": ("UPDATE stage SET status='skipped', left_at=?, reason=?, "
                "authorized_by=? WHERE loop_id=? AND stage=? AND status='active'"),
    "skip_ahead": ("UPDATE stage SET status='skipped', left_at=?, reason=?, "
                   "authorized_by=? WHERE loop_id=? AND stage=?"),
    "reset":   ("UPDATE stage SET status='pending', entered_at=NULL, left_at=NULL, "
                "reason=NULL, authorized_by=NULL WHERE loop_id=? AND stage != ?"),
}


def stage_set(con, what, params):
    """단계 전이 하나. **차지했으면 True** — `claim` 과 같은 뜻이다."""
    return claim(con, STAGE_SET[what], params)


def active_stage(con, lid):
    row = con.execute("SELECT stage FROM stage WHERE loop_id=? AND status='active'",
                      (lid,)).fetchone()
    return row["stage"] if row else None


def skips_of(con, lid):
    return con.execute("SELECT stage, reason, authorized_by FROM stage "
                       "WHERE loop_id=? AND status='skipped'", (lid,)).fetchall()


# --------------------------------------------------------------------- evidence

def record_event(con, lid, sid, kind, rule=None, target=None, detail=None):
    """관측을 적는다. **적는 것이 판정을 막지 않는다.**

    이 INSERT 가 터지면 예외가 훅까지 올라가 `inactive()` 로 빠졌다 — 즉
    **판정을 이미 알고 있으면서 적을 수 없다는 이유로 그 답을 버렸다.**
    읽기 전용 파일시스템·디스크 꽉 참·권한 사고에서 게이트가 통째로 열렸다
    (4회차 C⑥ 실측: `chmod a-w` 뒤 `docs/b.md` 쓰기가 허용됐다).

    기록은 복리의 원료이지 강제의 조건이 아니다. 삼키되 `swallow` 로 삼켜
    사실이 `status` 에 남는다. **적지 못한 것과 막지 못한 것은 다른 일이다.**
    """
    with swallow(t("관측 기록(%s)") % kind):
        con.execute("INSERT INTO event(at,loop_id,stage,kind,rule,target,detail) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (now(), lid, sid, kind, rule, target,
                     detail[:400] if detail else None))


def events_where(con, kinds=None, loop_id=None, rule=None, target=None,
                 from_epoch=None, to_epoch=None, after_id=None, upto_id=None):
    """이벤트를 고른다.

    ## 회차 경계는 시각이 아니라 **id** 로 나눈다

    시각은 초 단위라 같은 초에 일어난 두 사건의 순서를 말하지 못한다. 그래서 회차
    경계에 `+1초` 를 두었고, 그 1초 안에 일어난 다음 회차의 이벤트는 **어느 회차에도
    속하지 못하고 영영 사라졌다.** `id` 는 단조 증가하므로 그 문제가 없다 —
    순서를 시각으로 판정하지 말고 정체성으로 판정한다.

    `from_epoch`/`to_epoch` 는 승격 재발 판정처럼 event 가 아닌 것(promotion.at)을
    기준으로 삼는 자리에만 남는다.

    이 함수가 없을 때는 같은 관용구가 네 곳에 복붙돼 있었고, 그중 두 곳이
    SQL 문자열 비교를 쓰고 있었다. 오프셋 유무나 공백 구분 형식이 섞이면
    사전순과 실제 순서가 어긋나서 창 밖의 이벤트가 안으로 들어온다 —
    두 릴리스에 걸쳐 같은 버그를 두 번 냈다. 경계 판정은 여기 한 곳에만 있다.
    """
    sql = ["SELECT id, at, loop_id, stage, kind, rule, target, detail "
           "FROM event WHERE 1=1"]
    params = []
    if kinds:
        sql.append("AND kind IN (%s)" % ",".join("?" * len(kinds)))
        params += list(kinds)
    for col, val in (("loop_id", loop_id), ("rule", rule), ("target", target)):
        if val is not None:
            sql.append("AND IFNULL(%s,'-') = ?" % col)
            params.append(val)
    if after_id is not None:
        sql.append("AND id > ?")
        params.append(after_id)
    if upto_id is not None:
        sql.append("AND id <= ?")
        params.append(upto_id)
    sql.append("ORDER BY id")
    rows = con.execute(" ".join(sql), params).fetchall()
    if from_epoch is None and to_epoch is None:
        return rows
    out = []
    for r in rows:
        it = ts_epoch(r["at"])
        if from_epoch is not None and it < from_epoch:
            continue
        if to_epoch is not None and it >= to_epoch:
            continue
        out.append(r)
    return out


def loop_row(con, lid):
    """작업 한 행. **컬럼을 골라 뽑던 일곱 자리를 여기로 모은다.**

    `SELECT cycle`, `SELECT intent`, `SELECT created_at`, `SELECT *` 가 제각각
    있었다. 열이 늘거나 뜻이 바뀌면 일곱 곳을 다 찾아야 하고, 실제로 그 종류의
    누락이 이 리포에서 반복됐다 — 회차 경계를 id 로 옮겼을 때 `recurrence`
    한 곳만 벽시계에 남은 것이 같은 모양이다(4회차 C④).
    """
    return con.execute("SELECT * FROM loop WHERE id=?", (lid,)).fetchone()


def promotion_rows(con, key=None, decision=None, maturity=None,
                   order="at", limit=None):
    """승격 결정. 없으면 빈 목록, `key` 를 주면 한 행 또는 None."""
    sql, params = ["SELECT * FROM promotion WHERE 1=1"], []
    for col, val in (("key", key), ("decision", decision), ("maturity", maturity)):
        if val is not None:
            sql.append("AND %s = ?" % col)
            params.append(val)
    sql.append("ORDER BY %s" % order)
    if limit:
        sql.append("LIMIT %d" % int(limit))
    rows = con.execute(" ".join(sql), params).fetchall()
    return (rows[0] if rows else None) if key is not None else rows


def live_grants(con, lid):
    """아직 쓸 수 있는 쓰기 예외."""
    return con.execute("SELECT * FROM wgrant WHERE loop_id=? AND uses_left>0",
                       (lid,)).fetchall()


def open_loops(con):
    """열린 작업의 id 들."""
    return {r["id"] for r in con.execute(
        "SELECT id FROM loop WHERE closed_at IS NULL")}


def loops_created_after(con, epoch):
    """그 시각 이후에 만들어진 작업 수. created_at 도 문자열로 비교하면 안 된다."""
    return sum(1 for r in con.execute("SELECT created_at FROM loop")
               if ts_epoch(r["created_at"]) > epoch)


def record_evidence(con, lid, sid, kind, item, root=None):
    con.execute("INSERT OR IGNORE INTO evidence(loop_id,stage,kind,item,at) "
                "VALUES(?,?,?,?,?)", (lid, sid, kind, item, now()))
    dig = evidence_digest(root, item)
    if dig is not None:
        # **다시 적립할 때 지문이 갱신돼야 한다.** `INSERT OR IGNORE` 만 두면 계획을
        # 고치고 다시 승인해도 옛 지문이 남아 영영 만료 상태가 된다. `OR REPLACE` 를
        # 쓰지 않는 이유는 rowid 가 바뀌어 완료 조건의 **입력 순서**가 흐트러지기
        # 때문이다 (`acceptance_of` 가 rowid 순으로 읽는다).
        con.execute("UPDATE evidence SET digest=?, at=? "
                    "WHERE loop_id=? AND kind=? AND item=?",
                    (dig, now(), lid, kind, item))


def acceptance_of(con, lid):
    """이 작업의 완료 조건. 회차를 넘어 유지된다."""
    # rowid 순 = 입력 순. at 으로 정렬하면 같은 초에 넣은 조건들의 순서가 뒤섞인다.
    return [r["item"] for r in con.execute(
        "SELECT item FROM evidence WHERE loop_id=? AND kind='acceptance' ORDER BY rowid",
        (lid,))]


def last_event_id(con):
    """지금까지 기록된 마지막 이벤트 id. 순서의 기준점이다 — 시계가 아니다."""
    row = con.execute("SELECT MAX(id) i FROM event").fetchone()
    return (row["i"] or 0) if row else 0


# ---------------------------------------------------------------- measurement
#
# 측정할 수 있는 것은 **마찰**이고 알고 싶은 것은 **가치**다. 마찰은 대리 지표다.
# 그래서 여러 지표를 하나의 점수로 합치지 않는다 — 합치면 그 하나를 최적화하게 되고,
# 차단은 아무것도 시도하지 않거나 우회하는 것으로도 줄어든다. 나란히 놓고 사람이 읽는다.
#
# 스키마를 바꾸지 않는다. 두 기능 모두 새 event 종류로만 기록하므로 구버전 DB 에서
# "no such column" 으로 하네스가 죽는 일이 없다.

def cycle_window_start(con, lid):
    """이번 회차 창의 시작 — **직전 회차 종료 이벤트의 id** (배타적 하한).

    1회차에는 종료 기록이 없으므로 0 이다 (`loop_id` 로 이미 이 작업만 걸러진다).
    """
    row = con.execute(
        # `cycle_adopt` 도 경계다. 빠뜨렸더니 재연결 뒤 창이 옛 위치에 고정돼
        # **버려진 회차의 마찰이 다음 회차 기록으로 흡수**됐다 ("회차 2: 차단 5").
        "SELECT MAX(id) i FROM event WHERE loop_id=? "
        "AND kind IN ('cycle_close','cycle_adopt')",
        (lid,)).fetchone()
    return (row["i"] or 0) if row else 0


def cycle_seconds(con, lid, lo):
    """회차 창이 열린 뒤 흐른 초. 1회차는 경계 이벤트가 없으므로 작업 생성 시각을 쓴다."""
    if lo:
        row = con.execute("SELECT at FROM event WHERE id=?", (lo,)).fetchone()
        when = row["at"] if row else None
    else:
        row = loop_row(con, lid)
        when = row["created_at"] if row else None
    return max(0, int(time.time() - ts_epoch(when))) if when else 0


def cycle_counters(con, lid, lo):
    """이 회차의 마찰 수치. 회피 지표를 반드시 함께 담는다 — 차단만 보면 속는다.

    `lo` 는 cycle_window_start 가 준 event id 다 (배타적 하한).
    """
    rows = events_where(con, loop_id=lid, after_id=lo)
    tally = {}
    for r in rows:
        tally[r["kind"]] = tally.get(r["kind"], 0) + 1

    # 모드 사전 승인(bypassPermissions)은 **회피가 아니다.** 사용자가 고른 모드다.
    # 같이 세면 무인 실행이 곧바로 "게이트가 연극이 되고 있다" 로 판정된다 —
    # 실제로는 사람이 그렇게 하라고 지시한 것인데. 열을 갈라 둘 다 보이게 한다.
    preauth = sum(1 for r in rows
                  if r["kind"] == "bypass" and r["rule"] == "bypass_mode")

    # 재편집 최대치: 한 파일을 몇 번 고쳤나. 구조 냄새의 대리 지표.
    edits = {}
    for r in rows:
        if r["kind"] == "edit":
            edits[r["target"]] = edits.get(r["target"], 0) + 1

    # 반복 실패 = 이 회차의 실패 중, 같은 명령이 **앞서 이미 한 번 실패한** 것.
    # '앞서' 는 이전 회차·이전 작업뿐 아니라 **이 회차 안의 앞선 실패**도 포함한다.
    # 같은 명령을 두 번 깨뜨린 것은 회차 경계와 무관하게 반복이기 때문이다.
    # (설명이 "이전 회차에도" 로 읽혀 오해를 샀다 — 세는 방식이 아니라 말이 틀렸다.)
    # 첫 회차는 `lo == 0` 이라 `id <= 0` 이 늘 공집합이었다 — **이전 작업의 실패를
    # 하나도 세지 않았다.** 대부분의 작업이 1회차로 끝나므로 반복 실패 지표가 구조적으로
    # 낮게 나왔다. 창이 없으면 '이 작업의 첫 이벤트 이전' 을 경계로 쓴다.
    before = lo
    if not before:
        row = con.execute("SELECT MIN(id) i FROM event WHERE loop_id=?",
                          (lid,)).fetchone()
        before = (row["i"] - 1) if row and row["i"] else 0
    seen_before = {r["target"] for r in
                   events_where(con, kinds=("tool_fail",), upto_id=before)}
    refails, seen_now = 0, set()
    for r in rows:
        if r["kind"] != "tool_fail":
            continue
        if r["target"] in seen_before or r["target"] in seen_now:
            refails += 1
        seen_now.add(r["target"])

    return {
        "dur": cycle_seconds(con, lid, lo),
        "blocks": tally.get("block", 0),
        "fails": tally.get("tool_fail", 0),
        "refails": refails,
        "churn": max(edits.values()) if edits else 0,
        "edits": tally.get("edit", 0),
        "gates": tally.get("stop_gate", 0),
        "bypass": tally.get("bypass", 0) - preauth,
        "preauth": preauth,
        "skips": tally.get("skip", 0),
        "declines": tally.get("promote_declined", 0),
        "promotes": tally.get("promote", 0),
    }


def record_cycle_close(con, cfg, lid, sid, kind="cycle_close"):
    """회차 경계에서 그 회차의 집계를 한 줄로 남긴다.

    stage 행은 작업이 닫힐 때 삭제되므로 나중에 회차별 비용을 되살릴 수 없다.
    경계에서 스냅샷을 남기면 event 는 작업이 닫혀도 살아남아 측정이 가능해진다.

    **회차 하나에 스냅샷 하나.** 이 불변식을 호출자들의 조율에 맡겼더니 깨졌다:
    `advance --done` 은 여기를 부르고 곧이어 `rotate_loop` 을 부르는데 그것도
    여기를 부른다. 두 번째 호출 시점에는 창 시작이 방금 쓴 행으로 옮겨져 있어
    **전부 0 인 유령 회차**가 하나 더 쌓였고, `metrics` 의 회차 추세가 정확히
    반토막 났다 (차단 4건 → 보고 2.0, 4회차 C① 실측). 더 나쁜 것은 `--cycle` 은
    1행, `--done` 은 2행이라 **두 종료 경로가 서로 다른 분모**를 만든 것이다 —
    작업을 자주 끝내는 것이 지표를 좋게 만드는 가장 싼 방법이 됐다.

    호출자를 세는 대신 여기서 못 박는다. 이미 있으면 그 행을 돌려준다.

    `kind` 는 **회차를 어떻게 끝냈나**다. 둘 다 집계를 남기지만 뜻이 다르다:
      · `cycle_close` — 끝냈다. 측정 창과 **회고 창** 둘 다 여기서 새로 연다.
      · `cycle_adopt` — 버렸다(재연결). 측정 창만 새로 연다 — 회고는 이어받은
        작업의 앞선 사실까지 덮어야 하므로(3회차) 창을 옮기면 안 된다.
    한 종류로 뭉치면 둘 중 하나가 늘 틀린다. 실제로 그랬다: 종류를 합쳤더니
    재연결 뒤 1회차 회고가 '못 찾음' 이 됐다(실측).
    """
    cyc = cycle_of(con, lid)
    tgt = "%s-%d" % (lid, cyc)
    c = cycle_counters(con, lid, cycle_window_start(con, lid))
    c["cycle"] = cyc
    # **판단을 INSERT 의 WHERE 안으로.** 읽고-없으면-쓰기는 원자적이지 않아서
    # 병렬 `loop adopt` 다섯이 같은 회차에 스냅샷을 다섯 개 남겼다 — 회차 1개가
    # "기록된 회차 5개" 로 집계됐다(5회차 C②). `claim` 은 이 저장소가 같은
    # 문제에 이미 쓰는 어휘다(`stage_set`, `stop_block`).
    if not claim(con, "INSERT INTO event(at,loop_id,stage,kind,rule,target,detail) "
                      "SELECT ?,?,?,?,?,?,? WHERE NOT EXISTS ("
                      "SELECT 1 FROM event WHERE target=? "
                      "AND kind IN ('cycle_close','cycle_adopt'))",
                 (now(), lid, sid, kind, str(cyc), tgt,
                  json.dumps(c, ensure_ascii=False), tgt)):
        # 다른 호출이 먼저 남겼다. 그 행이 진실이다.
        row = con.execute("SELECT detail FROM event WHERE target=? "
                          "AND kind IN ('cycle_close','cycle_adopt')", (tgt,)).fetchone()
        try:
            return json.loads(row["detail"]) if row else None
        except ValueError:
            return None
    return c


# ----------------------------------------------------------------- retrospect
#
# 회고는 파일이 있는지만 봤고 **무엇을 묻는지는 설계된 적이 없었다.** 밀어주던 것이
# 전부 하네스 내부 사정(어떤 규칙에 걸렸나, 어떤 파일을 다시 고쳤나)이라, "왜 내
# 규칙을 어겼나"를 묻고 "무엇을 배웠나"는 묻지 않았다.
#
# 그리고 형식이 기계적으로 중요하다. 회고는 나중에 **정규화된 명령·규칙 이름으로
# 텍스트 검색**되어 찾아진다. 같은 사실을 담아도 그 토큰이 글자 그대로 없으면
# 영원히 안 찾아진다 — 실험으로 확인했다. 그래서 키를 알려주고 들어갔는지 본다.
#
# 통찰의 질은 채점하지 않는다(판단이다). 찾아지는지만 확인한다(기계적 사실이다).

def cycle_search_keys(con, lid, lo, limit=6):
    """이 회차에 관측된 것들의 **검색 키**.

    나중에 실패 지점 주입과 `recall` 이 바로 이 문자열로 회고를 찾는다. 그러니
    회고에 이 문자열이 글자 그대로 들어 있어야 한다.
    """
    keys = []
    for r in events_where(con, kinds=("tool_fail", "block"), loop_id=lid,
                          after_id=lo):
        k = r["target"] if r["kind"] == "tool_fail" else r["rule"]
        if k and k not in keys:
            keys.append(k)
    return keys[:limit]


# 우리 표 이름. **스키마에서 뽑는다** — 손으로 적으면 표가 늘 때 갈린다.
SCHEMA_TABLES = tuple(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))


def grant_write(con, lid, glob, reason, uses):
    """쓰기 예외를 하나 남긴다. `allow` 명령과 자기검사 탐침이 같은 문장을 쓰던
    자리다 — 파일이 갈라지니 그 중복이 비로소 보였다."""
    con.execute("INSERT INTO wgrant(loop_id,glob,reason,uses_left,at) "
                "VALUES(?,?,?,?,?)", (lid, glob, reason, uses, now()))
