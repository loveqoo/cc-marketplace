"""복리 — 승격 결정과 회차 지표.

엔진이 이름공간을 맞춰 준다 — `parts/__init__.py` 참고.
"""


def _adopt_evidence_signals(cfg):
    """`evidence_signals` 를 커스터마이즈한 문서의 그 값을 `criteria` 로 옮긴다.

    0.31.0 에서 `evidence_signals` 가 `criteria` 로 흡수됐다. 템플릿 채움만 하면
    `criteria` 는 들어오지만 **사용자가 고쳐 둔 옛 값은 조용히 무시된다** —
    `bash_pattern` 에 자기 빌드 명령을 넣어 둔 사람은 그게 사라진 것을 모른 채
    검증 게이트가 안 열리는 것만 보게 된다. 이름을 바꿨으면 옮겨주는 것이 맞다.

    새 어휘 필드(`satisfied_by`, `help` 등)는 템플릿 값을 유지한다. 사용자가
    고친 것은 신호 필드뿐이므로 그것만 덮는다.
    """
    old = cfg.get("evidence_signals")
    crit = cfg.get("criteria")
    if not isinstance(old, dict) or not isinstance(crit, dict):
        return
    for kind, sig in old.items():
        if not isinstance(sig, dict):
            continue
        target = crit.setdefault(kind, {"satisfied_by": "cli"})
        if isinstance(target, dict):
            target.update(sig)


def promote_as(cfg):
    """승격의 종류 → 설명. 어떻게 기계화하는지는 프로젝트마다 다르다.

    `test`(회귀 테스트로 승격)나 `lint`(린터 규칙으로) 같은 종류를 쓰는 팀이 있다.
    `declined` 는 `--as` 값이 아니라 보류 **결정**이므로 목록을 보여줄 때 제외한다.
    """
    m = cfg.obj("promotion.as_kinds")
    got = {k: v for k, v in m.items() if isinstance(v, str)} if m else {}
    return got or {k: t(v) for k, v in PROMOTE_AS_DEFAULT.items()}


# ------------------------------------------------------------------- promotion
#
# 기록만 하고 승격하지 않으면 복리가 아니라 일기다. 100개 레포를 조사한 연구에서
# 가장 흔한 설정 냄새가 62% 의 "lint leakage" — 훅으로 막을 것을 산문으로 적어둔
# 것이었다. 우리는 반복을 세고 있으니, 세는 데서 멈추지 않는다.
#
# ExpeL 의 중요도 투표와 CODESKILL 의 add/merge/drop 이 공통으로 말하는 것:
# 저장소는 쌓이면 안 되고 수렴해야 하며, 살아남는 기준은 근거다.

def promo_match(item):
    """block 은 규칙이, 나머지는 대상이 의미 있는 키다. events_where 용 필터."""
    col = "rule" if item["kind"] == "block" else "target"
    return {col: item["name"]}


def repeated_items(con, cfg):
    """여러 작업에서 반복된 항목. 한 작업 안의 반복은 우연일 수 있다."""
    kinds = cfg.seq("promotion.kinds", ("block", "tool_fail"))
    min_loops = cfg.num("promotion.min_loops", 3, low=2)
    # 승격할 수 없는 규칙은 후보에서 뺀다. `no_reason`·`bypass_mode`·`protected` 는
    # 모델이 게이트를 우회하려 한 기록이다 — 하네스가 제대로 동작한 증거이지
    # 기계화할 습관이 아니다. 이걸 승격 대상으로 올리면 "우회 시도를 승격하라"는
    # 뜻이 되고, 사용자가 봐야 할 규율 신호가 결정 절차로 세탁된다. stats 에는 남는다.
    skip = set(cfg.seq("promotion.exclude_rules",
                       ("no_reason", "bypass_mode", "protected", "protected_bash")))
    out = []
    for kind in kinds:
        key_col = "rule" if kind == "block" else "target"
        for r in con.execute(
                "SELECT IFNULL(%s,'-') k, COUNT(*) c, COUNT(DISTINCT loop_id) loops, "
                "MAX(at) last FROM event WHERE kind=? GROUP BY k "
                "HAVING loops >= ? ORDER BY loops DESC, c DESC" % key_col,
                (kind, min_loops)):
            if kind == "block" and r["k"] in skip:
                continue
            out.append({"key": "%s:%s" % (kind, r["k"]), "kind": kind,
                        "name": r["k"], "count": r["c"], "loops": r["loops"],
                        "last": r["last"]})
    out.sort(key=lambda d: (-d["loops"], -d["count"]))
    return out


def recurrence(con, p):
    """승격 이후 같은 항목이 다시 걸렸는지 → (횟수, 작업 수).

    승격이 통했는지의 유일한 객관 증거다.

    **경계는 시각이 아니라 id 다.** 회차 경계는 전부 id 로 옮겼는데 여기 하나만
    벽시계에 남아 있었고, 그래서 두 가지가 함께 틀렸다(4회차 C④):
      · 시계가 앞섰다 되돌아오면(NTP 보정·VM 재개) 승격 **이후**의 재발이
        영원히 보이지 않는다 → `metrics` 가 "재발 0%" 라고 거짓 보고한다.
      · 시계가 정상이어도 승격과 **같은 초**에 일어난 재발은 `+1초` 경계에
        걸려 사라진다. `events_where` 가 바로 그 `+1초` 를 없애려고 만든 것인데
        여기만 남았다.
    id 는 단조 증가하므로 순서 판정에 시계가 필요 없다.

    `after_id` 가 없는 낡은 행은 시각으로 떨어진다 — 그 행들은 이 열이 생기기
    전에 쓰였고, 없는 것을 지어내지 않는다.
    """
    item = {"kind": p["kind"], "name": p["key"].split(":", 1)[1]}
    aid = p["after_id"] if "after_id" in p.keys() else None
    if aid is not None:
        hits = events_where(con, kinds=(item["kind"],), after_id=aid,
                            **promo_match(item))
    else:
        base = p["recheck_at"] or p["at"]
        hits = events_where(con, kinds=(item["kind"],),
                            from_epoch=(ts_epoch(base) + 1) if base else 0.0,
                            **promo_match(item))
    return len(hits), len({r["loop_id"] for r in hits})


def is_regressed(con, cfg, p):
    """저장된 maturity 를 믿지 않고 지금 계산한다.

    sync_promotions 를 아무도 실행하지 않은 세션에서도 게이트가 맞아야 한다.
    저장값만 보면 `stats`/`tidy`/`promote` 를 부르지 않은 채 Compounding 을
    통과할 수 있다 — 실제로 그렇게 새어나갔다.
    """
    if p["maturity"] == "regressed":
        return True
    return recurrence(con, p)[1] >= cfg.num("promotion.reopen_after_loops", 2, low=1)


def sync_promotions(con, cfg):
    """성숙도를 재계산한다. 결정론적이다 — LLM 을 끼우면 등급이 표류한다.

    established → proven: 승격 후 재발이 없고 그 사이 작업이 N개 지났다.
    established → regressed: 승격 후에도 M개 작업에서 다시 걸렸다. 승격이
    통하지 않았다는 뜻이므로 다시 결정 대상으로 돌린다.
    """
    proven_after = cfg.num("promotion.proven_after_loops", 3, low=1)
    reopen_after = cfg.num("promotion.reopen_after_loops", 2, low=1)
    changed = []
    for p in promotion_rows(con):
        if p["maturity"] == "regressed":
            continue
        _, loops = recurrence(con, p)
        if loops >= reopen_after:
            con.execute("UPDATE promotion SET maturity='regressed' WHERE key=?",
                        (p["key"],))
            changed.append((p["key"], "regressed"))
            continue
        if p["maturity"] == "proven" or p["decision"] == "declined":
            continue
        if loops == 0 and loops_created_after(con, ts_epoch(p["at"])) >= proven_after:
            con.execute("UPDATE promotion SET maturity='proven' WHERE key=?",
                        (p["key"],))
            changed.append((p["key"], "proven"))
    return changed


def pending_promotions(con, cfg, limit=None):
    """결정이 필요한 항목. regressed 는 결정이 무효화됐으므로 다시 포함한다."""
    decided = {r["key"]: r for r in promotion_rows(con)}
    out = []
    for item in repeated_items(con, cfg):
        p = decided.get(item["key"])
        if p is None:
            out.append(item)
        elif is_regressed(con, cfg, p):
            out.append(dict(item, regressed=p["decision"]))
    if limit is None:
        limit = cfg.num("promotion.max_per_cycle", 3, low=1)
    return out[:limit]


def verify_globs(cfg, as_kind):
    return cfg.seq("promotion.verify_globs.%s" % as_kind) or None


def promote_change_seen(con, cfg, lid, as_kind):
    """승격 주장에 맞는 파일 변경이 이 회차에 실제로 있었는가.

    `--as hook` 이라고 써놓고 아무것도 고치지 않아도 게이트는 만족된다 —
    노트는 주장이다. 막지는 않는다(무엇을 고쳐야 하는지는 판단이므로). 대신
    관측 사실을 기록해서, 나중에 `metrics` 가 주장과 사실을 나란히 보여준다.
    None 은 '검증 대상 아님'이다 (rule 은 LEARNED.md 이고 하네스가 쓴다).
    """
    pats = verify_globs(cfg, as_kind)
    if not pats:
        return None
    excl = cfg.seq("promotion.verify_exclude")
    for r in events_where(con, kinds=("edit",), loop_id=lid,
                          after_id=cycle_window_start(con, lid)):
        rel = r["target"] or ""
        if any(glob_match(rel, p) for p in excl):
            continue
        if any(glob_match(rel, p) for p in pats):
            return True
    return False


def learned_lines(con, cfg):
    """LEARNED.md 에 실릴 규칙 줄. rule 로 승격되고 살아 있는 것만.

    저장된 maturity 가 아니라 실시간 판정을 쓴다 — 그러지 않으면 재발한 규칙이
    항상 로드되는 문서에 계속 남는다(실제로 남았다).
    """
    return [r for r in promotion_rows(con, decision="rule")
            if not is_regressed(con, cfg, r)]


def learned_budget(cfg):
    return cfg.num("promotion.learned_max_lines", 20, low=1)


def _pct(part, whole):
    return "  -  " if not whole else "%4.0f%%" % (100.0 * part / whole)


def _survival(con, cfg):
    """승격 종류별 재발률. 각 승격이 그 자체로 하나의 실험이다.

    "하네스 있기 전/후"는 비교할 수 없지만 "이 규칙을 승격한 전/후"는 비교할 수
    있다. 이것이 이 하네스에서 유일하게 엄밀한 측정이다.
    """
    verified = {}
    for r in con.execute("SELECT target, detail FROM event "
                         "WHERE kind='promote_verify' ORDER BY id"):
        verified[r["target"]] = (r["detail"] or "").endswith("yes")
    agg = {}
    for p in promotion_rows(con):
        d = agg.setdefault(p["decision"], {"n": 0, "re": 0, "vn": 0, "vy": 0})
        d["n"] += 1
        if is_regressed(con, cfg, p):
            d["re"] += 1
        if p["key"] in verified:
            d["vn"] += 1
            d["vy"] += 1 if verified[p["key"]] else 0
    return agg


def _cycle_rows(con):
    out = []
    # **버린 회차도 표본이다.** `cycle_close` 만 셌더니 `loop adopt` 한 줄로
    # 마찰이 쌓인 회차를 표본에서 지울 수 있었다 (4회차 C③).
    for r in con.execute("SELECT at, detail FROM event "
                         "WHERE kind IN ('cycle_close','cycle_adopt') ORDER BY id"):
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            continue
        if isinstance(d, dict):
            d["at"] = r["at"]
            out.append(d)
    return out


def _bucket(rows, n=3):
    """회차를 n등분한다. 개별 비교는 작업 난이도에 교란되므로 구간으로만 읽는다."""
    if len(rows) < n * 2:
        return [(1, len(rows), rows)] if rows else []
    size = len(rows) // n
    out = []
    for i in range(n):
        lo = i * size
        hi = len(rows) if i == n - 1 else (i + 1) * size
        out.append((lo + 1, hi, rows[lo:hi]))
    return out


def trend_verdict(avgs):
    """Goodhart 가드. 차단은 아무것도 시도하지 않거나 우회해도 줄어든다.

    그래서 마찰과 회피를 **함께** 판정한다. 둘을 한 점수로 합치면 이 구분이
    사라지고, 회피가 개선으로 보인다.
    """
    if len(avgs) < 2:
        return None
    # preauth 는 의도적으로 빠져 있다 — 아래 evasion 계산을 보라.
    first, last = avgs[0], avgs[-1]
    friction = last["blocks"] + last["refails"] < first["blocks"] + first["refails"]
    evasion = (last["bypass"] + last["skips"] + last["declines"]
               > first["bypass"] + first["skips"] + first["declines"])
    if friction and evasion:
        return "evasion"
    if friction:
        return "improving"
    if evasion:
        return "mismatch"
    return "flat"


