"""사람이 읽는 화면. 판정하지 않고 그린다.

엔진이 이름공간을 맞춰 준다 — `parts/__init__.py` 참고.
"""


def work_candidates(con, cfg, root, limit=6):
    """하네스가 **자기 기록에서** 아는 할 일. Selection 의 작업 후보다.

    "새 작업이 없다" 가 "할 일이 없다" 는 뜻이 아니다. 승격 결정이 밀려 있고,
    통하지 않은 승격이 있고, 인덱스가 낡았고, 예산이 찼다면 그건 전부 복리를
    유지하는 일이다. 이걸 내놓지 않으면 무인 실행이 Selection 에서 멈춘다 —
    사람에게 물으라고 말해봐야 무인 실행에는 물을 사람이 없다.
    """
    out = []

    def add(kind, what, how):
        if len(out) < limit:
            out.append({"kind": kind, "what": what, "how": how})

    with swallow(t("할 일 후보")):
        for it in pending_promotions(con, cfg):
            add(t("승격 결정"), t("'%s' 가 작업 %d개에서 반복된다 — 훅·구조로 올릴지 결정")
                % (it["key"], it["loops"]),
                "harness promote %s --as hook --note \"...\"" % it["key"])
    with swallow(t("할 일 후보")):
        for r in promotion_rows(con, maturity="regressed"):
            add(t("재발한 승격"), t("'%s' 는 %s 로 승격했는데 다시 걸렸다 — 그 방법이 통하지 "
                "않았다") % (r["key"], r["decision"]),
                t("원인을 다시 보고 `harness promote %s` 로 다시 결정") % r["key"])
    with swallow(t("할 일 후보")):
        rep = tidy_report(con, cfg, root)
        for d, note in rep["dirs"]:
            add(t("기록 정리"), "%s %s" % (d, note), t("인덱스를 만들거나 갱신 (Scaffolding)"))
        if rep["groups"]:
            add(t("기록 정리"), t("한 작업이 여러 파일을 남긴 묶음 %d개 — 하나로 병합")
                % len(rep["groups"]), t("harness tidy 로 목록 확인 (Scaffolding)"))
        if rep["stale"]:
            add(t("기록 정리"), t("닫힌 작업의 오래된 파일 %d개 — 인덱스에 요약하고 정리")
                % len(rep["stale"]), t("harness tidy 로 목록 확인 (Scaffolding)"))
        if rep["learned"] and rep["learned"][0] >= rep["learned"][1]:
            add(t("예산"), t("LEARNED.md 가 %d/%d 줄로 찼다 — 한 줄을 비워야 새 규칙이 들어간다")
                % rep["learned"], t("harness promote <기존키> --decline --reason \"...\""))
    return out


def render_work_candidates(items, mode_note=True):
    if not items:
        return
    print(t("\n하네스가 아는 할 일 (%d개) — 새 작업이 없다면 여기서 고를 수 있다:")
          % len(items))
    for i, it in enumerate(items, 1):
        print("  %d. [%s] %s" % (i, it["kind"], it["what"]))
        print("     → %s" % it["how"])
    if mode_note:
        print(t("  고르면 `harness loop intent \"...\"` 와 `harness loop done-when \"...\"` 로 "
              "기록하고 진행하라. 이것들도 정말 필요 없으면 그렇다고 말하고 멈춰라."))


def plan_preview(root, cmd):
    """승인 다이얼로그에 실을 계획 본문.

    계획 승인은 이 하네스에서 사람이 방해받는 유일하게 값있는 자리인데, 다이얼로그가
    **파일 이름만** 보여주고 있었다. 읽지 않고 찍는 도장은 마찰만 있고 정보가 없다 —
    그렇게 남은 `plan_approved` 기록은 가짜다. 무엇을 승인하는지 보여준다.
    """
    # 표시용이다. 못 쪼개면 거친 분해로도 계획 파일을 찾아본다 — 여기서 판정하는
    # 것은 없으므로 근사가 해롭지 않다.
    pos = [it for it in (sh_tokens(cmd) or cmd.split()) if not it.startswith("--")]
    path = None
    for i, it in enumerate(pos):
        if it == "approve-plan" and i + 1 < len(pos):
            path = pos[i + 1].strip("\"'")
            break
    if not path:
        return t("계획 파일 경로가 없다. `approve-plan <파일>` 형식으로 지정하라.")
    full = path if os.path.isabs(path) else os.path.join(root, path)
    if not os.path.isfile(full):
        return t("⚠ 계획 파일이 없다: %s — 승인하기 전에 확인하라.") % path
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            body = fh.read(PLAN_PREVIEW_CHARS * 2)
    except OSError as exc:
        return t("⚠ 계획 파일을 읽을 수 없다 (%s): %s") % (path, exc)
    lines = body.splitlines()
    shown = lines[:PLAN_PREVIEW_LINES]
    text = "\n".join(shown)[:PLAN_PREVIEW_CHARS]
    more = []
    if len(lines) > len(shown):
        more.append(t("이하 %d줄 생략") % (len(lines) - len(shown)))
    if len(text) < len("\n".join(shown)):
        more.append(t("길이 잘림"))
    tail = t(" (%s — 전문은 %s)") % (", ".join(more), path) if more else ""
    return "%s%s\n%s" % (path, tail, text)


def status_report(ctx):
    """현재 상태. 출력하지 않는다 — 테스트가 값을 검사할 수 있어야 한다."""
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    row = loop_row(con, lid)
    rows = stage_rows(con, lid)
    stage = stage_obj(cfg, sid)
    missing = exit_blockers(con, cfg, root, lid, sid)
    crit = stage.get("exit_criteria") or []
    return {
        "loop": lid,
        "cycle": cycle_of(con, lid),
        "stage": sid,
        "stage_label": label_of(cfg, sid),
        "summary": stage["summary"],
        "intent": (row["intent"] if row else None) or None,
        "acceptance": acceptance_of(con, lid),
        "write": list(stage.get("write", [])),
        "prefix": file_prefix(con, lid),
        # 회차 한정 노드는 행이 지연 생성이라 아직 없을 수 있다 — '?' 가 아니라
        # pending 이 사실이다.
        "stages": [{"id": s["id"], "label": s["label"],
                    "status": (rows[s["id"]]["status"] if s["id"] in rows
                               else ("pending" if s.get("__dyn__") else "?")),
                    "dynamic": bool(s.get("__dyn__"))}
                   for s in cfg["stages"]],
        "exit_met": [k for k in crit if k not in missing],
        "exit_missing": missing,
        "evidence": {r["kind"]: r["c"] for r in con.execute(
            "SELECT kind, COUNT(*) c FROM evidence WHERE loop_id=? GROUP BY kind",
            (lid,))},
        "skips": [dict(r) for r in skips_of(con, lid)],
        "grants": [{"glob": g["glob"], "uses_left": g["uses_left"],
                    "reason": g["reason"]} for g in live_grants(con, lid)],
        "auto_skip": (auto_skip_scope_note(con) if auto_skip_on(con) else None),
        "auto_skip_reason": get_meta(con, "auto_skip_reason", "-"),
        "pending_promotions": [it["key"] for it in pending_promotions(con, cfg)],
        "promoted": promotion_summary(con, cfg),
        "tidy": tidy_headline(con, cfg, root),
        "enforcing": enforcing_summary(cfg),
        "swallowed": swallowed_recent(ctx.root),
        "selftest": [{"what": w, "ok": o, "got": g}
                     for w, o, g in selftest(ctx)],
        # 작업이 정해지지 않았을 때만. 정해졌으면 후보는 소음이다.
        "candidates": ([] if (row and row["intent"])
                       else work_candidates(con, cfg, root)),
    }


def render_status(d, cfg):
    # 무시되는 설정이 있으면 **맨 위**에 말한다. 아래에 묻으면 안 읽는다.
    if d.get("config_problems"):
        print(t("⚠ stages.json 에서 무시되는 설정 %d건:") % len(d["config_problems"]))
        for p in d["config_problems"]:
            print("  - %s" % p)
        print()
    # 삼킨 실패도 사실이다. 곁다리 작업이라 판정은 안 막았지만, **아무도 모르는
    # 실패**를 남기지 않는 것이 이 플러그인의 전제다.
    seen = d.get("swallowed") or []
    if seen or SWALLOWED:
        rows = list(dict.fromkeys(seen + SWALLOWED))
        print(t("⚠ 곁다리 작업 %d건이 실패했다 (판정은 계속됐다):") % len(rows))
        for w in rows[-8:]:
            print("  - %s" % w)
        print(t("   지우려면 `rm %s`") % SWALLOW_LOG_REL.replace(os.sep, "/"))
        print()
    st = d.get("selftest") or []
    fails = [x for x in st if not x.get("ok")]
    if fails:
        # **개수보다 이것이 먼저다.** 대표 조작이 기대와 다르게 판정된다는 것은
        # 설정이 어떻게 생겼든 강제가 깨졌다는 직접 증거다.
        print(t("⚠ 자기검사 %d/%d 실패 — 강제가 기대와 다르게 동작한다:")
              % (len(fails), len(st)))
        for x in fails:
            print("  - %s → %s" % (x["what"], x["got"]))
        print()
    e = d.get("enforcing") or {}
    if e:
        # 무엇이 실제로 강제되는지 **숫자로** 보여준다. 0 이 보이면 그게 신호다.
        if st:
            print(t("자기검사: %d/%d 통과 (대표 조작을 실제 판정에 넣어 확인)")
                  % (len(st) - len(fails), len(st)))
        print(t("강제 중: 보호 경로 %d%s · 언어 %s")
              % (e.get("protected_paths", 0),
                 "".join(" · %s %d/%d" % (n, on, tot)
                         for n, on, tot in e.get("gates", [])),
                 e.get("language", "ko")))
    print(t("작업 %s · 회차 %d · 단계 %s") % (d["loop"], d["cycle"], d["stage_label"]))
    if d["intent"]:
        print(t("  작업 내용: %s") % d["intent"])
    else:
        print(t("  작업 내용: (미정) — %s 단계에서 정하고 "
              "`harness loop intent \"...\"` 로 기록하라")
              % stage_obj(cfg, cfg["stages"][0]["id"])["label"])
    if d["acceptance"]:
        print(t("  완료 조건 (%d개):") % len(d["acceptance"]))
        for i, it in enumerate(d["acceptance"], 1):
            print("    %d. %s" % (i, it))
    else:
        print(t("  완료 조건: (미정) — `harness loop done-when \"<조건>\" ...` 으로 기록하라"))
    print(t("  요약: %s") % d["summary"])
    print(t("  쓰기 허용: %s") % (", ".join(d["write"]) or t("(없음)")))
    print(t("  .dev/ 산출물 파일명 접두사: %s") % d["prefix"])
    print(t("  단계: ") + " → ".join("%s%s(%s)" % (s["label"],
                                                 "+" if s.get("dynamic") else "",
                                                 s["status"])
                                  for s in d["stages"]))
    if d["exit_met"] or d["exit_missing"]:
        print(t("  종료 조건: 충족 %s / 미충족 %s")
              % (", ".join(d["exit_met"]) or "-", ", ".join(d["exit_missing"]) or "-"))
    if d["evidence"]:
        print(t("  증거: %s") % ", ".join("%s×%d" % kv for kv in d["evidence"].items()))
    for r in d["skips"]:
        print(t("  스킵: %s — %s (승인: %s)") % (r["stage"], r["reason"], r["authorized_by"]))
    for g in d["grants"]:
        print(t("  예외: %s (남은 %d회) — %s") % (g["glob"], g["uses_left"], g["reason"]))
    if d["auto_skip"]:
        print(t("  ⚠ 스킵 자동 승인 ON (%s) — 사유: %s. 끄려면 `harness auto-skip off`")
              % (d["auto_skip"], d["auto_skip_reason"]))
    if d["pending_promotions"]:
        print(t("  승격 결정 대기 %d개 (Compounding 의 종료 조건): %s")
              % (len(d["pending_promotions"]), ", ".join(d["pending_promotions"])))
    if d["promoted"]:
        print(t("  승격됨: %s")
              % ", ".join("%s %d" % kv for kv in sorted(d["promoted"].items())))
    if d["tidy"]:
        print("  %s" % d["tidy"])
    if d.get("candidates"):
        render_work_candidates(d["candidates"])


def _hint_on_enter(ctx, lid, sid):
    """단계 진입 시의 안내. **무엇을 보여줄지는 단계가 선언한다.**

    예전에는 이 함수가 단계 id 를 알고 있었다(`if sid == "scaffolding"`). 그래서
    단계를 더하거나 이름을 바꾸면 파이썬을 고쳐야 했고, 실제로 0.10.0 에서
    Selection 을 신설했을 때 이 부류가 낡았다. 엔진이 알아야 하는 것은 **패널의
    종류**이지 어느 단계가 그것을 쓰는지가 아니다 — 그건 `stages[].panels` 가 정한다.

    Context 는 **당겨가는** 단계라 패널이 없다. 과거 기록을 밀어넣으면 이번 task 와
    무관한 실수까지 컨텍스트를 먹는다. 조회 방법만 알려주고 판단은 모델이 한다.
    Compounding 은 반대다. 막 끝낸 루프 자신의 기록은 무조건 관련 있으니 밀어준다.
    """
    con, cfg = ctx.con, ctx.cfg
    hint = stage_obj(cfg, sid).get("hint")
    if hint:
        print("\n%s" % hint)

    panels = stage_obj(cfg, sid).get("panels") or []
    for name in panels:
        fn = PANELS.get(name)
        if fn:
            fn(ctx, lid)

    if "retro" not in panels:
        return

    # 무엇을 물을지가 회고의 값을 정한다. 관측을 나열하기 **전에** 질문을 둔다 —
    # 순서를 뒤집으면 "규칙에 걸린 목록"이 회고의 전부가 된다.
    print(t("\n회고에 답할 것:"))
    for i, (q, why) in enumerate(retro_questions(cfg), 1):
        print("  %d. **%s** — %s" % (i, q, why))

    keys = cycle_search_keys(con, lid, retro_window_start(con, lid))
    if keys:
        print(t("\n이 회차의 검색 키 — 회고 **앞부분**에 이 문자열을 그대로 넣어라:"))
        print("  " + "  ".join("`%s`" % k for k in keys))
        print(t("  나중에 이 키로 찾는다. 없으면 그 회고는 다시 찾아지지 않는다 "
              "(내용이 같아도 그렇다)."))

    sk = skips_of(con, lid)
    if sk:
        print(t("\n이 루프에서 건너뛴 단계 — 회고에 사유와 함께 기록하라:"))
        for r in sk:
            print(t("  - %s: %s (승인: %s)") % (r["stage"], r["reason"], r["authorized_by"]))
    rows = con.execute(
        "SELECT kind, rule, target, COUNT(*) c FROM event "
        "WHERE loop_id=? AND kind IN ('block','tool_fail','bypass') "
        "GROUP BY kind, rule, target HAVING c > 0 ORDER BY c DESC LIMIT 8",
        (lid,)).fetchall()
    if rows:
        print(t("\n이 루프에서 관측된 것 — 회고 대상:"))
        for r in rows:
            print("  - %s/%s %s ×%d" % (r["kind"], r["rule"] or "-", r["target"], r["c"]))
    churn = con.execute(
        "SELECT target, COUNT(*) c FROM event WHERE loop_id=? AND kind='edit' "
        "GROUP BY target HAVING c >= 4 ORDER BY c DESC LIMIT 5", (lid,)).fetchall()
    if churn:
        print(t("\n재편집이 많은 파일 — 구조 문제일 수 있다:"))
        for r in churn:
            print("  - %s ×%d" % (r["target"], r["c"]))

    # 여러 작업에서 반복된 것은 이 단계의 종료 조건이다. 산문으로 적고 끝내면
    # 다음 작업에서 같은 실수가 또 나오고, 그건 복리가 아니다.
    pend = pending_promotions(con, cfg)
    if pend:
        print(t("\n여러 작업에서 반복된 항목 — 이 단계를 끝내려면 결정해야 한다 "
              "(종료 조건 promotion_decided):"))
        for it in pend:
            mark = t("  ← %s 로 승격했는데 다시 걸렸다") % it["regressed"] \
                if it.get("regressed") else ""
            print(t("  - %s ×%d (작업 %d개)%s")
                  % (it["key"], it["count"], it["loops"], mark))
        print(t("  `harness promote` 로 목록과 결정 방법을 본다. "
              "승격하지 않기로 하는 것도 결정이다."))


def _recall_files(cfg, root, keywords, limit=6):
    """회고·학습·트러블슈팅 파일 중 키워드에 걸리는 것. 내용은 읽지 않고 경로만 준다.

    인덱스 파일은 키워드와 무관하게 항상 앞에 놓는다. 파일이 수백 개로 쌓이면
    개별 파일 6개를 보여주는 것보다 전체를 요약한 인덱스 하나가 낫다.
    """
    keywords = _expand_keywords(keywords) if keywords else set()
    indexes, hits = [], []
    for sub in recall_dirs(cfg):
        d = os.path.join(root, ".dev", sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d), reverse=True):
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue
            rel = ".dev/%s/%s" % (sub, name)
            if name in index_names(cfg):
                indexes.append(rel)
                continue
            if not keywords:
                hits.append(rel)
                continue
            hay = name.lower()
            with swallow(t("파일 읽기")):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    hay += "\n" + fh.read(recall_read_bytes(cfg)).lower()
            if any(kw.lower() in hay for kw in keywords):
                hits.append(rel)
    return indexes, hits[:max(0, limit - len(indexes))]


def tidy_report(con, cfg, root):
    """정리 후보. 판정은 전부 파일시스템 사실이고 LLM 이 끼지 않는다.

    "정리하라"는 권고는 아무 일도 만들지 않았다. 무엇을 정리할지의 목록이라야
    행동이 된다. 삭제·병합 자체는 여전히 자율이다 — 후보만 제시한다.
    """
    thr = cfg.num("tidy.dir_file_threshold", 12, low=1)
    age_days = cfg.num("tidy.age_days", 30, low=1)
    group_min = cfg.num("tidy.merge_group", 3, low=2)
    cutoff = time.time() - age_days * 86400
    live = open_loops(con)

    out = {"dirs": [], "stale": [], "groups": [], "learned": None, "regressed": []}
    for sub in recall_dirs(cfg):
        d = os.path.join(root, ".dev", sub)
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        files = [n for n in names
                 if os.path.isfile(os.path.join(d, n)) and n not in index_names(cfg)]
        if not files:
            continue
        idx = os.path.join(d, "INDEX.md")
        has_idx = os.path.isfile(idx)

        def mtime(path):
            """목록을 읽은 뒤 파일이 사라질 수 있다 (병렬 작업, 끊어진 symlink)."""
            try:
                return os.path.getmtime(path)
            except OSError:
                return 0.0

        newest = max([mtime(os.path.join(d, n)) for n in files] or [0.0])
        note = None
        if len(files) >= thr and not has_idx:
            note = t("파일 %d개인데 INDEX.md 가 없다") % len(files)
        elif has_idx and mtime(idx) < newest:
            note = t("INDEX.md 가 최신 파일보다 낡았다 (파일 %d개)") % len(files)
        if note:
            out["dirs"].append((".dev/%s/" % sub, note))

        groups = {}
        for n in files:
            path = os.path.join(d, n)
            m = re.match(r"^(\d{6}-[0-9a-f]{6})-", n)
            lid_of = m.group(1) if m else None
            if lid_of:
                groups.setdefault(lid_of, []).append(".dev/%s/%s" % (sub, n))
            try:
                mt = os.path.getmtime(path)
            except OSError:
                continue
            # 열려 있는 작업의 파일은 후보가 아니다 — 아직 쓰이는 중이다.
            if mt < cutoff and lid_of not in live:
                out["stale"].append((".dev/%s/%s" % (sub, n),
                                     int((time.time() - mt) // 86400)))
        for k, v in groups.items():
            if len(v) >= group_min and k not in live:
                out["groups"].append((k, sorted(v)))

    budget = learned_budget(cfg)
    used = len(learned_lines(con, cfg))
    if used:
        out["learned"] = (used, budget)
    out["regressed"] = promotion_rows(con, maturity="regressed")
    out["stale"].sort(key=lambda it: -it[1])
    return out


def metrics_report(ctx):
    """복리 측정 데이터. 출력하지 않는다."""
    con, cfg = ctx.con, ctx.cfg
    with con:
        sync_promotions(con, cfg)
    lc = con.execute("SELECT COUNT(*) c, MIN(created_at) a, MAX(created_at) b "
                     "FROM loop").fetchone()
    cyc = _cycle_rows(con)
    buckets, avgs = [], []
    for lo, hi, rows in _bucket(cyc):
        avg = {k: sum(r.get(k, 0) for r in rows) / float(len(rows))
               for k, _ in TREND_KEYS}
        avgs.append(avg)
        buckets.append({
            "from": lo, "to": hi, "avg": avg,
            "fails": sum(r.get("fails", 0) for r in rows),
            "refails": sum(r.get("refails", 0) for r in rows),
        })
    return {
        "loops": lc["c"],
        "cycles": len(cyc),
        "span": [(lc["a"] or "")[:10], (lc["b"] or "")[:10]] if lc["a"] else None,
        "survival": _survival(con, cfg),
        "buckets": buckets,
        "verdict": trend_verdict(avgs),
        # **판정 기준 자신도 재는 대상이다.** 다른 셋은 하네스가 무엇을 잡았나를
        # 재는데, 이것은 "무엇을 잡기로 했는가" 가 움직였나를 잰다.
        "evidence": {
            "commands": [c for c in cfg.seq("criteria.verification_evidence.commands")
                         if isinstance(c, str) and c.strip()],
            "drift": _evidence_widened(
                cfg, (default_cfg(ctx.root) or {}).get("criteria") or {}),
        },
    }


def render_metrics(d):
    print(t("복리 측정 — 작업 %d개, 기록된 회차 %d개%s")
          % (d["loops"], d["cycles"],
             " (%s ~ %s)" % tuple(d["span"]) if d["span"] else ""))

    print(t("\n① 승격 생존율 — 무엇이 실제로 막았나"))
    agg = d["survival"]
    if not agg:
        print(t("  (아직 승격이 없다. 여러 작업에서 반복된 항목이 생기면 쌓인다)"))
    else:
        for k in sorted(agg, key=lambda x: -agg[x]["n"]):
            v = agg[k]
            seen = (t("변경관측 %d/%d") % (v["vy"], v["vn"])) if v["vn"] else ""
            print(t("  %-10s %2d건 중 %2d건 재발 (%s)  %s")
                  % (k, v["n"], v["re"], _pct(v["re"], v["n"]).strip(), seen))
        total = sum(v["n"] for v in agg.values())
        if total < 20:
            print(t("  ⚠ 표본 %d건. 비율을 믿지 마라 — 20~30건은 있어야 한다.") % total)
        print(t("  재발 = 승격 이후 같은 항목이 다시 걸린 것. 변경관측 = 그 회차에 "
              "주장에 맞는 파일 변경이 있었나."))

    print(t("\n② 회차 추세 — 마찰과 회피를 나란히 본다"))
    if not d["buckets"]:
        print(t(NO_CYCLES))
    else:
        print("  %-12s %s" % (t("회차구간"), " ".join("%8s" % t(it) for _, it in TREND_KEYS)))
        for b in d["buckets"]:
            print("  %-12s %s" % (t("%d-%d회") % (b["from"], b["to"]),
                                  " ".join("%8.1f" % b["avg"][k] for k, _ in TREND_KEYS)))
        if d["verdict"] in VERDICT_TEXT:
            print("  " + t(VERDICT_TEXT[d["verdict"]]))

    print(t("\n③ 반복 실패 비율 — 실패 주입이 겨냥한 것"))
    # 누적 비율(전체 실패 중 첫 실패가 아닌 것)은 쓰지 않는다. 명령 종류는 적고
    # 실행은 많으니 시간이 지나면 무조건 100% 에 수렴한다 — 창이 없는 비율은
    # 아무것도 말해주지 않는다. 회차 구간별로만 읽는다.
    if not d["buckets"]:
        print(t(NO_CYCLES))
    else:
        for b in d["buckets"]:
            print(t("  %-12s 실패 %3d건 중 이전에도 실패한 것 %3d건 (%s)")
                  % (t("%d-%d회") % (b["from"], b["to"]), b["fails"], b["refails"],
                     _pct(b["refails"], b["fails"]).strip()))
        print(t("  '이전에도 실패한 것' = 그 회차 시작 전에 이미 같은 명령이 실패한 적 있음."))

    print(t("\n④ 판정 기준 — 무엇을 '검증' 으로 인정하나"))
    ev = d.get("evidence") or {}
    if ev.get("commands"):
        print(t("  이 프로젝트가 선언한 검증 명령 %d개:") % len(ev["commands"]))
        for c in ev["commands"]:
            print("    %s" % c)
    else:
        print(t("  선언한 명령 없음 — 표준 러너 패턴만으로 판정한다."))
    if ev.get("drift"):
        # 잠그지 않기로 했으므로, 남는 것은 이 한 줄이다. 조용하면 없는 것과 같다.
        print(t("  ⚠ 표준 러너 패턴이 기본값에서 달라졌다 — 거절 기준을 거절당한 쪽이 "
                "고쳤을 수 있다. status 가 자세히 말한다."))
    else:
        print(t("  표준 러너 패턴은 기본값 그대로."))

    print(t("\n측정하지 못하는 것: 결과물의 품질, 그 회차가 필요했는지, 사람이 아낀 시간."))
    print(t("점수를 만들지 않는 이유: 하나로 합치면 그 하나를 최적화하게 된다."))


# ----------------------------------------------------------------------- render
#
# 계산과 출력을 가른다. 섞여 있을 때는 테스트가 stdout 을 grep 할 수밖에 없었고,
# 그래서 내가 `'^[^g]*$'` 처럼 **실패할 수 없는** 정규식을 썼다 — LEARNED.md
# 표류가 초록 상태로 출하된 직접 원인이다. 보고 명령은 `--json` 으로 구조를
# 그대로 내보내므로, 테스트가 산문이 아니라 값을 검사할 수 있다.

def promote_report(ctx):
    """승격 결정 현황. 계산만 한다."""
    return {
        "pending": pending_promotions(ctx.con, ctx.cfg),
        "options": [{"as": k, "why": v} for k, v in promote_as(ctx.cfg).items()],
        "decided": [dict(r) for r in
                    promotion_rows(ctx.con, order="at DESC", limit=8)],
    }


def render_promote(d):
    pend = d["pending"]
    print(t("승격 결정이 필요한 항목 (%d개)") % len(pend))
    if not pend:
        print(t("  (없음 — 여러 작업에서 반복된 항목이 아직 없다)"))
    for it in pend:
        mark = (t("  ← %s 로 승격했는데 다시 걸렸다") % it["regressed"]
                if it.get("regressed") else "")
        print(t("  %-34s ×%d, 작업 %d개%s")
              % (it["key"][:34], it["count"], it["loops"], mark))
    if pend:
        print(t("\n결정 방법 (하나 고른다):"))
        for o in d["options"]:
            flag = ("--decline --reason \"...\"" if o["as"] == "declined"
                    else "--as %s --note \"...\"" % o["as"])
            print("  harness promote <key> %-32s %s" % (flag, o["why"]))
    if d["decided"]:
        print(t("\n이미 결정된 항목"))
        for r in d["decided"]:
            print("  %-30s %-10s %-12s %s"
                  % (r["key"][:30], r["decision"], r["maturity"],
                     (r["note"] or "-")[:40]))


def render_init(root, created, lid, stage_label=None):
    print(t("하네스 설치 완료: %s") % root)
    for c in created:
        print("  + %s" % c)
    if not created:
        print(t("  (변경 없음 — 이미 설치되어 있다)"))
    # 단계를 **여기서** 말한다. 문서에 적어두면 단계 구성이 바뀔 때 뒤처지고,
    # 모델은 그 낡은 문장을 정확히 따라 틀린 말을 한다 — 실제로 그렇게 됐다
    # (0.10.0 에서 Selection 을 신설한 뒤에도 스킬 문서가 `1/6 Scaffolding` 이었다).
    print(t("활성 작업: %s%s") % (lid, t(" · 단계 %s") % stage_label if stage_label else ""))
    print(t("커밋 대상: .claude/harness/{POLICY.md,LEARNED.md,stages.json,rationale.md}, "
          "CLAUDE.md, AGENTS.md"))
