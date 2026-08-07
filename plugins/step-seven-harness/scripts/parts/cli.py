"""CLI 명령. **엔진이 이름공간을 맞춰 준다** — `parts/__init__.py` 참고.

여기 있는 것은 전부 "사람이 친 명령 하나를 처리한다" 는 한 가지 일이다.
판정(게이트)도, 저장(store)도, 그리기(view)도 하지 않는다 — 그것들을 부른다.
"""


def cli_status(ctx, argv):
    data = status_report(ctx)
    probs = (config_problems(ctx.cfg) + drift_problems(ctx.cfg, ctx.root)
             + install_problems(ctx.root)
             + language_problems(ctx.root))
    if probs:
        data["config_problems"] = probs
    dump_json(data) if "--json" in argv else render_status(data, ctx.cfg)
    return 0


def cli_advance(ctx, argv):
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    last = cfg["stages"][-1]["id"]
    want_done = "--done" in argv
    want_cycle = "--cycle" in argv

    if sid != last and (want_done or want_cycle):
        raise Refuse(t("--done / --cycle 은 마지막 단계(%s)에서만 쓴다.")
                     % stage_obj(cfg, last)["label"], code=2)
    if sid == last:
        if want_done and want_cycle:
            raise Refuse(t("--done 과 --cycle 은 함께 쓸 수 없다."), code=2)
        if not (want_done or want_cycle):
            raise Refuse(t("%s 단계에서는 두 갈래 중 하나를 골라야 한다. 스스로 판단하라:")
                         % stage_obj(cfg, last)["label"],
                         t("  harness advance --done    작업이 끝났다 → %s (새 작업 선정)")
                         % stage_obj(cfg, cfg["stages"][0]["id"])["label"],
                         t("  harness advance --cycle   후속 회차가 남았다 → %s (같은 작업 유지)")
                         % stage_obj(cfg, cfg["stages"][1]["id"])["label"], code=1)

    missing = exit_blockers(con, cfg, root, lid, sid)
    if missing:
        print(t("advance 거부 — %s 단계의 종료 조건이 남았다:") % stage_obj(cfg, sid)["label"])
        for k in missing:
            print("  - %s: %s" % (k, criterion_why(con, cfg, root, lid, k)))
        if stage_obj(cfg, sid).get("skippable") is False:
            print(t("이 단계는 건너뛸 수 없다. 조건을 채워야 한다."))
        else:
            print(t("정당한 사유가 있으면 `harness skip %s --reason \"...\"` 로 "
                  "사람의 승인을 받아라.") % sid)
        return 1

    # 회고가 나중에 찾아지는지 확인한다. 통찰의 질은 채점하지 않지만 찾아지는지는
    # 기계적 사실이라 확인할 수 있다. 막지는 않는다 — 무엇을 쓸지는 판단이다.
    # **읽기만 한다.** 기록은 claim 을 이긴 뒤에 (아래 트랜잭션 안에서) 남긴다.
    # 예전에는 여기서 바로 커밋해서, 병렬 advance 넷 중 진 셋도 `retro_keys` 를
    # 남겼다 — 회차 전이는 한 번인데 이벤트가 넷이었다(4회차 C⑨ 실측). 그 종류는
    # `FP_IGNORE_KINDS` 에 없어 진전 지문까지 바꿔, 아무 진전 없이 "진전 있음" 으로
    # 보이게 만들었다. **판정 전에 커밋하지 않는다.**
    retro_note = None
    if sid == last:
        with swallow(t("회고 키 확인")):
            keys, found, missing = retro_key_report(
                con, cfg, root, lid, retro_window_start(con, lid))
            if keys:
                retro_note = (keys, found, missing)

    snap = None
    with con:
        # **이 단계를 끝내는 것은 한 번뿐이다.** 무조건 UPDATE 였을 때는 병렬 advance
        # 둘이 모두 성공해 열린 작업이 둘 생기거나 가짜 회차 스냅샷이 남았다.
        # 판단을 WHERE 절 안으로 옮겨 rowcount 가 승자를 정하게 한다.
        if not stage_set(con, "done", (now(), lid, sid)):
            raise Refuse(t("이미 %s 단계를 벗어났다 — 다른 호출이 먼저 진행했다. "
                           "`harness status` 로 현재 단계를 확인하라.")
                         % stage_obj(cfg, sid)["label"], code=1)
        if retro_note:
            keys, found, missing = retro_note
            record_event(con, lid, sid, "retro_keys", str(len(found)),
                         "%s-%d" % (lid, cycle_of(con, lid)),
                         "found=%d/%d missing=%s"
                         % (len(found), len(keys), ",".join(missing) or "-"))
        # 회차 경계에서만 스냅샷을 남긴다. close_loop 이 stage 를 지우기 전에.
        if sid == last:
            snap = record_cycle_close(con, cfg, lid, sid)
        if sid == last and want_done:
            nlid = rotate_loop(con, cfg, root, lid)
            if nlid is None:
                raise RuntimeError(t("다른 호출이 먼저 이 작업을 닫았다"))
            nsid = cfg["stages"][0]["id"]
            done_task = True
        elif sid == last:
            nlid, nsid, done_task = lid, next_cycle(ctx), False
        else:
            nlid, nsid, _ = _enter(ctx, stage_index(cfg, sid) + 1)
            done_task = False
            if nsid is None:
                raise RuntimeError(t("다음 단계 행이 없다 — 상태와 설정이 어긋났다"))

    if retro_note:
        keys, found, missing = retro_note
        if missing:
            print(t("회고 확인: 검색 키 %d개 중 %d개가 빠졌다 — %s")
                  % (len(keys), len(missing), ", ".join("`%s`" % k for k in missing)))
            print(t("   그 문자열이 회고에 없으면 다음에 같은 일이 생겨도 찾아지지 않는다. "
                  "다음 회차 회고에는 넣어라."))
        else:
            print(t("회고 확인: 검색 키 %d개 전부 들어 있다 — 다시 찾아진다.") % len(keys))
    if snap:
        print(t("회차 %d 기록: 차단 %d · 실패 %d(반복 %d) · 재편집 최대 %d · "
              "우회 %d · 스킵 %d")
              % (snap["cycle"], snap["blocks"], snap["fails"], snap["refails"],
                 snap["churn"], snap["bypass"], snap["skips"]))
    if done_task:
        print(t("작업 %s 종료 — 기록은 각 폴더의 파일에 남아 있다.") % lid)
        print(t("새 작업 %s → 단계 %s") % (nlid, label_of(cfg, nsid)))
    else:
        if sid == last:
            print(t("작업 %s 회차 %d 시작 (같은 작업 유지)")
                  % (nlid, cycle_of(con, nlid)))
            print(t("   파일명 접두사: %s") % file_prefix(con, nlid))
        print(t("→ 단계 %s") % label_of(cfg, nsid))
        print("   %s" % stage_obj(cfg, nsid)["summary"])
    _hint_on_enter(ctx, nlid, nsid)
    return 0


def cli_skip(ctx, argv):
    """PreToolUse 가 사람의 승인을 받은 뒤에만 여기까지 온다."""
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    pos = argv_positional(argv)
    target = pos[0] if pos else None
    reason = argv_value(argv, "reason")
    if not target or not reason:
        raise Refuse(t("사용법: harness skip <stage-id|+N|until:<stage-id>> --reason \"...\""), code=2)
    # 훅과 **같은 함수**로 판정한다. 훅이 이미 막았으므로 보통 여기 오지 않지만,
    # 셸 간접 호출로 훅을 우회해 들어온 경우에도 같은 답을 내야 한다.
    why = skip_block_reason(cfg, sid, target, con, root, lid)
    if why:
        raise Refuse(why, code=1)
    ids = stage_ids(cfg)
    cur = stage_index(cfg, sid)
    if target.startswith("+"):
        dest = min(cur + int(target[1:]) - 1, len(ids) - 1)
    elif target.startswith("until:"):
        dest = ids.index(target.split(":", 1)[1]) - 1
    else:
        dest = ids.index(target)

    # 스킵은 **승인**을 면제하지만 **기록**을 면제하지 않는다.
    # 무인 실행으로 Planning 을 건너뛰어도 계획 파일은 남아야 한다 — 회차 10번을 돌았을 때
    # 계획 10개가 남는 것과 스킵 기록 10개가 남는 것은 복리의 재료로서 다르다.
    for i in range(cur, dest + 1):
        st = cfg["stages"][i]
        for key in st.get("skip_requires", []):
            # advance 와 **같은 판정**을 써야 한다. has_evidence 만 보면 계획 파일이
            # 디스크에 있는데도 "계획 파일을 남겨야 한다"고 거부한다 — 이미 한 일을
            # 하라는 말이고, 사용자는 빠져나갈 길이 없다. 실제로 그렇게 막혔다.
            if not criterion_met(con, cfg, root, lid, key):
                raise Refuse(t("%s 를 건너뛰더라도 기록은 남겨야 한다: %s")
                             % (st["label"], criterion_why(con, cfg, root, lid, key)),
                             t("먼저 그 기록을 남긴 뒤 다시 시도하라. 승인만 면제된다."), code=1)

    # 자동 승인으로 통과한 스킵은 사람이 승인한 것과 구분해 기록한다
    by = "auto" if auto_skip_on(con) else "user"
    left = None
    skipped = []
    with con:
        # 현재 단계를 벗어나는 것은 **한 번뿐이다.** 이 claim 이 병렬 스킵의 단일
        # 소비점이다 — 자동 승인 `--uses 1` 을 둘이 동시에 써도 하나만 통과한다.
        # 종료 조건을 충족했다면 done, 아니면 skipped 로 정직하게 기록한다.
        if dest == cur or exit_blockers(con, cfg, root, lid, sid):
            won = stage_set(con, "skipped", (now(), reason, by, lid, sid))
            skipped.append(sid)
        else:
            won = stage_set(con, "done", (now(), lid, sid))
        if not won:
            raise Refuse(t("이미 %s 단계를 벗어났다 — 다른 호출이 먼저 진행했다. "
                           "`harness status` 로 현재 단계를 확인하라.")
                         % stage_obj(cfg, sid)["label"], code=1)
        for i in range(cur + 1, dest + 1):
            stage_set(con, "skip_ahead", (now(), reason, by, lid, ids[i]))
            skipped.append(ids[i])
        for s in skipped:
            record_event(con, lid, s, "skip", s, by, reason)
        if by == "auto":
            _, left = consume_auto_skip(con)
        nlid, nsid, cycled = _enter(ctx, dest + 1)
        if nsid is None:
            raise RuntimeError(t("다음 단계 행이 없다 — 상태와 설정이 어긋났다"))
    print(t("스킵(%s): %s") % (t("자동 승인") if by == "auto" else t("사용자 승인"),
                            ", ".join(skipped) or t("(없음)")))
    print(t("사유: %s") % reason)
    if by == "auto" and left is not None:
        print(t("자동 승인 남은 횟수: %d%s") % (left, t(" — 소진되어 OFF 로 돌아갔다") if left == 0 else ""))
    if cycled:
        print(t("작업 %s 종료 → 새 작업 %s, 단계 %s") % (lid, nlid, label_of(cfg, nsid)))
    else:
        print(t("→ 단계 %s") % label_of(cfg, nsid))
        _hint_on_enter(ctx, nlid, nsid)
    return 0


def cli_verify(ctx, argv):
    """검증을 **하네스가 직접 돌리고 종료 코드로** 판정한다.

      harness verify -- pytest tests/

    왜 있나: verification_evidence 는 PostToolUse 관측으로만 채워졌다. 훅이
    없는 환경 — 다른 에이전트 도구, 사람이 직접 돌릴 때, 훅이 실패한 세션 —
    에서는 `skip` 밖에 길이 없었고, 그건 '검증했다'가 아니라 '검증을 건너뛰었다'로
    기록된다. 정직한 기록을 남길 방법이 없으면 사람은 부정직한 기록을 남긴다.

    자기 보고는 받지 않는다. "테스트 돌렸습니다"를 증거로 받으면 그 순간 이
    게이트는 장식이 된다 — 하네스가 실행하고 결과를 본다.

    돌릴 수 있는 명령을 검증 패턴으로 **제한한다.** 제한하지 않으면 이 명령이
    PreToolUse 를 우회하는 셸이 된다. 셸 메타문자도 거부한다 —
    `pytest; rm -rf /` 는 패턴에 걸리지만 앞부분만 검증 명령이다.
    """
    import shlex
    import subprocess
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    cmd = " ".join(argv[argv.index("--") + 1:]).strip() if "--" in argv else ""
    if not cmd:
        raise Refuse(t("사용법: harness verify -- <검증 명령>"),
                     t("  예: harness verify -- pytest tests/"), code=2)
    stages = evidence_stages(cfg)
    if sid not in stages:
        raise Refuse(t("verify 는 %s 단계에서 쓴다 (현재 %s).")
                     % (", ".join(stages), label_of(cfg, sid)), code=2)
    if set(cmd) & SHELL_META:
        raise Refuse(t("셸 메타문자가 있는 명령은 거부한다 — 검증 명령 하나만 넘겨라."), code=2)
    # 관측 경로와 **같은 판정**을 쓴다. 두 곳이 갈리면 `verify` 로는 되는데 그냥
    # 돌리면 안 되는(또는 그 반대) 일이 생긴다.
    # 패턴이 없으면 검사를 건너뛰었다 — 그 순간 이 명령은 게이트를 지나는 **셸**이
    # 된다. 없으면 거부한다. 무엇이 검증인지 모르면 아무것도 돌리지 않는 것이 맞다.
    if not cfg.at("criteria.verification_evidence.bash_pattern"):
        raise Refuse(t("`criteria.verification_evidence.bash_pattern` 이 없다 — 무엇이 검증인지 "
                       "정해지지 않았으므로 아무것도 실행하지 않는다."), code=2)
    if not verification_hit(cfg, cmd):
        raise Refuse(t("검증 명령으로 보이지 않는다: %s") % cmd,
                     t("이 자리는 검증을 돌리는 곳이다. 임의의 명령을 돌리는 곳이 아니다."), code=2)
    try:
        rc = subprocess.call(shlex.split(cmd), cwd=root)
    except OSError as e:
        raise Refuse(t("실행할 수 없다: %s") % e, code=2)
    if rc != 0:
        # 실패도 사실이므로 적립한다. 증거로는 세지 않는다.
        with con:
            record_event(con, lid, sid, "tool_fail", "verify", norm_cmd(cmd),
                         "exit %d" % rc)
        raise Refuse(t("\n검증 실패 (exit %d) — 증거로 기록하지 않았다. 고치고 다시 돌려라.") % rc, code=1)
    with con:
        record_evidence(con, lid, sid, "verification_evidence",
                        ("verify: " + cmd)[:120])
    print(t("\n검증 통과 — 증거로 기록했다: %s") % cmd)
    return 0


def cli_allow(ctx, argv):
    con, cfg, lid = ctx.con, ctx.cfg, ctx.lid
    pos = argv_positional(argv)
    glob = pos[0] if pos else None
    reason = argv_value(argv, "reason")
    uses = argv_value(argv, "uses")
    if not glob or not reason:
        raise Refuse(t("사용법: harness allow <glob> --reason \"...\" [--uses N]"), code=2)
    with con:
        grant_write(con, lid, glob, reason, int(uses) if uses else 3)
    # 예전에는 무조건 "사용자 승인" 이라고 적었다. `consent.allow` 를 빼면 아무도
    # 승인하지 않았는데 그렇게 출력됐다 — 기록이 거짓말을 하면 감사가 무의미하다.
    print(t("예외 등록%s: %s — %s")
          % (t(" (사용자 승인)") if "allow" in consent_map(cfg) else "", glob, reason))
    return 0


def cli_approve_plan(ctx, argv):
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    pos = argv_positional(argv)
    path = pos[0] if pos else None
    if not path:
        raise Refuse(t("사용법: harness approve-plan <plan-file>"), code=2)
    rel = rel_to_root(root, path)
    if not rel or not os.path.isfile(os.path.join(root, rel)):
        raise Refuse(t("계획 파일을 찾을 수 없다: %s") % path, code=2)
    # 승인 대상은 **이 회차의 계획 파일**이어야 한다. 예전에는 아무 파일이나 받아서
    # `README.md` 를 승인하면 `plan_file` 게이트까지 함께 열렸고, 지난 회차의 계획서로도
    # 열렸다. 판정은 `fs_evidence` 가 이미 하고 있으므로 그 규칙을 그대로 쓴다.
    want = cfg.seq("criteria.plan_file.write_glob")
    pre = file_prefix(con, lid)
    if want and not (any(glob_match(rel, g) for g in want)
                     and os.path.basename(rel).startswith(pre)):
        raise Refuse(t("계획 파일이 아니다: %s") % rel,
                     t("이 회차의 계획은 %s 아래에 `%s` 로 시작하는 이름이어야 한다.")
                     % (", ".join(want), pre), code=2)
    if evidence_digest(root, rel) is None:
        # 지문을 못 구하면 그 승인은 **만료될 수 없다.** `chmod 000` 한 번으로 승인 후
        # 계획을 통째로 갈아치울 수 있었다. 읽을 수 없으면 승인하지 않는다.
        raise Refuse(t("계획 파일을 읽을 수 없어 승인할 수 없다: %s") % rel,
                     t("읽을 수 있게 한 뒤 다시 시도하라 — 승인은 그 시점의 내용에 대한 것이라 "
                       "내용을 확인할 수 없으면 기록이 거짓이 된다."), code=2)
    with con:
        record_evidence(con, lid, sid, "plan_file", rel, root)
        record_evidence(con, lid, sid, "plan_approved", rel, root)
    print(t("계획 승인 기록: %s") % rel)
    return 0


def cli_tidy(ctx, argv):
    """줄이는 것도 일이다. 쌓이면 복잡해지는 시스템은 복리가 아니다."""
    con, cfg, root = ctx.con, ctx.cfg, ctx.root
    with con:
        sync_promotions(con, cfg)
    refresh_learned(con, cfg, root)
    rep = tidy_report(con, cfg, root)
    if "--json" in argv:
        dump_json({k: ([dict(r) for r in v] if k == "regressed" else v)
                   for k, v in rep.items()})
        return 0
    limit = 12

    print(t("정리 후보 (Scaffolding 단계의 일이다. 삭제·병합 여부는 자율)"))
    if rep["dirs"]:
        print(t("\n인덱스가 필요하거나 낡은 폴더"))
        for d, note in rep["dirs"]:
            print("  %-26s %s" % (d, note))
    if rep["groups"]:
        print(t("\n한 작업이 여러 파일을 남겼다 — 하나로 병합할 후보"))
        for k, files in rep["groups"][:limit]:
            print(t("  작업 %s — %d개") % (k, len(files)))
            for f in files[:4]:
                print("      %s" % f)
            if len(files) > 4:
                print(t("      ... +%d개") % (len(files) - 4))
    if rep["stale"]:
        print(t("\n닫힌 작업의 오래된 파일 — 인덱스에 요약하고 지울 후보"))
        for f, days in rep["stale"][:limit]:
            print(t("  %-52s %d일") % (f[:52], days))
        if len(rep["stale"]) > limit:
            print(t("  ... +%d개") % (len(rep["stale"]) - limit))
    if rep["regressed"]:
        print(t("\n승격했는데 다시 걸린 항목 — 승격이 통하지 않았다"))
        for r in rep["regressed"]:
            print("  %-30s %s: %s" % (r["key"][:30], r["decision"], r["note"] or "-"))
        print(t("  Compounding 에서 다시 결정하게 된다 (`harness promote`)."))
    if rep["learned"]:
        used, budget = rep["learned"]
        print(t("\nLEARNED.md: %d/%d줄%s")
              % (used, budget, t(" — 예산 소진. 새 규칙을 올리려면 먼저 비워라")
                 if used >= budget else ""))
        print(t("  내리기: `harness promote <key> --decline --reason \"...\"`"))
    if not any((rep["dirs"], rep["groups"], rep["stale"], rep["regressed"],
                rep["learned"])):
        print(t("  (없음 — 정리할 것이 없다)"))
    return 0


def cli_metrics(ctx, argv):
    """복리 측정. 점수를 만들지 않는다 — 합치면 그 하나를 최적화하게 된다."""
    data = metrics_report(ctx)
    dump_json(data) if "--json" in argv else render_metrics(data)
    return 0


def cli_promote(ctx, argv):
    """반복된 항목을 승격하거나, 승격하지 않기로 결정한다.

    승인 다이얼로그를 띄우지 않는다 — 이건 게이트 우회가 아니라 기록이기 때문이다.
    대신 결정은 전부 event 에 남아 `stats` 에 드러나고, 승격 후에도 같은 항목이
    다시 걸리면 결정이 무효화되어 다시 올라온다. 무성의한 보류는 되돌아온다.
    """
    con, cfg, root, lid, sid = ctx.con, ctx.cfg, ctx.root, ctx.lid, ctx.sid
    with con:
        changed = sync_promotions(con, cfg)
    # 목록만 보는 경로에서도 갱신한다. 성숙도를 바꿨는데 파일을 그대로 두면
    # 재발한 규칙이 항상 로드되는 문서에 남는다.
    refresh_learned(con, cfg, root)
    if changed:
        # 20줄을 쏟으면 정작 결정해야 할 목록이 스크롤 밖으로 밀린다.
        if len(changed) <= 3:
            for key, mat in changed:
                print(t("성숙도 갱신: %s → %s") % (key, mat))
        else:
            agg = {}
            for _, mat in changed:
                agg[mat] = agg.get(mat, 0) + 1
            print(t("성숙도 갱신 %d건: %s")
                  % (len(changed), ", ".join("%s %d" % kv for kv in sorted(agg.items()))))
    pos = argv_positional(argv)
    as_kind = argv_value(argv, "as")
    decline = "--decline" in argv
    note = argv_value(argv, "note") or argv_value(argv, "reason")

    if not pos:
        data = promote_report(ctx)
        dump_json(data) if "--json" in argv else render_promote(data)
        return 0

    key = pos[0]
    known = {it["key"] for it in repeated_items(con, cfg)}
    if key not in known:
        raise Refuse(t("반복 항목이 아니다: %s") % key,
                     t("`harness promote` 로 목록을 확인하라 (키는 'block:<규칙>' 또는 "
                     "'tool_fail:<명령>' 형식이다)."), code=2)
    if decline:
        as_kind = "declined"
    if not as_kind:
        raise Refuse(t("무엇으로 승격할지 골라야 한다: --as %s, 또는 --decline.")
                     % "|".join(k for k in promote_as(cfg) if k != "declined"), code=2)
    if as_kind not in promote_as(cfg):
        raise Refuse(t("알 수 없는 승격 종류: %s (가능: %s)")
                     % (as_kind, ", ".join(promote_as(cfg))), code=2)
    if not note:
        raise Refuse(t("사유/내용이 필요하다: %s")
                     % (t("--reason \"왜 승격하지 않는가\"") if as_kind == "declined"
                        else t("--note \"무엇을 어떻게 바꿨는가\"")), code=2)

    if as_kind == "rule":
        used = len(learned_lines(con, cfg))
        existing = promotion_rows(con, key=key)
        grows = not (existing and existing["decision"] == "rule")
        if grows and used >= learned_budget(cfg):
            raise Refuse(t("LEARNED.md 예산이 찼다 (%d/%d줄). 항상 로드되는 문서라 상한이 있다.")
                         % (used, learned_budget(cfg)),
                         t("먼저 한 줄을 비워라: `harness promote <기존키> --decline "
                         "--reason \"...\"` (`harness tidy` 로 목록 확인)"), code=1)

    kind = key.split(":", 1)[0]
    # 보류의 성숙도는 'declined' 다. established 로 두면 "확립된 규칙"과 구분되지 않는다.
    maturity = "declined" if as_kind == "declined" else "established"
    seen = promote_change_seen(con, cfg, lid, as_kind)
    with con:
        con.execute(
            "INSERT INTO promotion(key,kind,decision,maturity,note,loop_id,at,"
            "recheck_at,after_id) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "decision=excluded.decision, maturity=excluded.maturity, "
            "note=excluded.note, loop_id=excluded.loop_id, at=excluded.at, "
            # **재발 창은 뒤로 밀지 않는다.** `after_id=excluded.after_id` 였을 때
            # 똑같은 명령을 한 번 더 치는 것만으로 그동안의 재발이 창 밖으로
            # 나가 `regressed → established`, `재발 100% → 0%` 가 됐다
            # (5회차 C① 실측). 승격은 동의 다이얼로그가 없는 '기록' 이라 비용이
            # 0 이다 — 비용 0 으로 되돌릴 수 있는 지표는 지표가 아니다.
            #
            # 결정을 **바꾸면**(다른 `--as`) 새 시도이므로 창을 옮긴다. 같은
            # 결정을 반복하는 것은 새 시도가 아니라 같은 시도다.
            "recheck_at=CASE WHEN promotion.decision=excluded.decision "
            "THEN promotion.recheck_at ELSE excluded.recheck_at END, "
            "after_id=CASE WHEN promotion.decision=excluded.decision "
            "THEN promotion.after_id ELSE excluded.after_id END",
            (key, kind, as_kind, maturity, note, lid, now(), now(),
             last_event_id(con)))
        record_event(con, lid, sid,
                     "promote_declined" if as_kind == "declined" else "promote",
                     as_kind, key, note)
        if seen is not None:
            # 주장과 사실을 따로 남긴다. metrics 가 나란히 보여준다.
            record_event(con, lid, sid, "promote_verify", as_kind, key,
                         "change_seen=%s" % ("yes" if seen else "no"))
    wrote = refresh_learned(con, cfg, root)

    print("%s: %s → %s" % (t("보류 기록") if as_kind == "declined" else t("승격 기록"),
                           key, promote_as(cfg)[as_kind]))
    print("  %s" % note)
    if wrote is None:
        # 승격은 DB 에 남았지만 **복리가 실제로 도는 곳**은 LEARNED.md 다. 그 반영이
        # 실패했는데 성공처럼 보고하면, 다음 세션은 배운 것 없이 시작한다.
        print(t("  ⚠ %s 에 반영하지 못했다 (쓰기 실패 — 권한이나 파일시스템을 확인하라). "
                "결정은 기록됐지만 다음 세션에 실리지 않는다. 고친 뒤 `%s promote` 를 "
                "다시 실행하거나 `%s status` 로 확인하라.")
              % (LEARNED_REL.replace(os.sep, "/"), WRAPPER_CMD, WRAPPER_CMD))
    elif wrote:
        print(t("  %s 갱신 (%d/%d줄)")
              % (LEARNED_REL.replace(os.sep, "/"), len(learned_lines(con, cfg)),
                 learned_budget(cfg)))
    if seen is False:
        print(t("  ⚠ 이 회차에 그에 맞는 파일 변경이 관측되지 않았다 (%s). 막지는 않지만 "
              "기록된다 — `harness metrics` 가 주장과 사실을 나란히 보여준다.")
              % ", ".join(verify_globs(cfg, as_kind) or []))
    elif seen:
        print(t("  설정/구조 변경이 이 회차에 관측됐다 — 주장이 사실로 뒷받침된다."))
    if as_kind == "declined":
        print(t("  보류도 결정이다. 이 항목이 앞으로 %s개 작업에서 다시 걸리면 "
              "결정이 무효화되어 다시 올라온다.")
              % cfg.num("promotion.reopen_after_loops", 2, low=1))
    else:
        print(t("  성숙도 established. 재발 없이 작업 %s개가 지나면 proven 이 된다.")
              % cfg.num("promotion.proven_after_loops", 3, low=1))
    left = pending_promotions(con, cfg)
    print(t("남은 결정: %d개") % len(left))
    return 0


def cli_recall(ctx, argv):
    """과거 관측 기록과 회고 파일을 조회한다 (pull). 무엇이 관련 있는지는 호출자가 판단한다."""
    con, cfg, root, lid = ctx.con, ctx.cfg, ctx.root, ctx.lid
    keywords = argv_positional(argv)
    kind = argv_value(argv, "kind")
    rule = argv_value(argv, "rule")
    from_intent = False
    if not keywords:
        # Scaffolding 에서 정한 작업을 기본 키워드로 쓴다. 6→2 링크의 연결점.
        row = loop_row(con, lid)
        intent = (row["intent"] if row else None) or ""
        picked = [it for it in re.split(r"[\s,·/]+", intent)
                  if len(it) >= 2 and it.lower() not in STOPWORDS][:6]
        if picked:
            keywords = picked
            from_intent = True
    try:
        limit = int(argv_value(argv, "limit") or 12)
    except ValueError:
        limit = 12

    # 괄호 필수 — "A OR B AND C" 는 "A OR (B AND C)" 로 파싱되어
    # 첫 조건만 만족하면 키워드 필터가 통째로 무시된다.
    where, params = ["(kind != 'edit' OR ? = 1)"], [1 if kind == "edit" else 0]
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if rule:
        where.append("rule = ?")
        params.append(rule)
    # 키워드는 OR 로 묶는다. AND 로 묶으면 "src/auth.ts 토큰 갱신 수정" 같은 작업
    # 설명에서 뽑은 키워드를 전부 만족하는 이벤트가 없어 항상 빈 결과가 나온다.
    kw_or = []
    for kw in keywords:
        like = "%" + kw.lower() + "%"
        kw_or.append("(LOWER(target) LIKE ? OR LOWER(IFNULL(rule,'')) LIKE ? "
                     "OR LOWER(IFNULL(detail,'')) LIKE ?)")
        params += [like, like, like]
    if kw_or:
        where.append("(" + " OR ".join(kw_or) + ")")
    rows = con.execute(
        "SELECT kind, rule, target, COUNT(*) c, MAX(at) last, "
        "COUNT(DISTINCT loop_id) loops FROM event WHERE %s "
        "GROUP BY kind, rule, target ORDER BY loops DESC, c DESC LIMIT ?"
        % " AND ".join(where), params + [limit]).fetchall()

    if from_intent:
        head = t("이 루프의 작업에서 추출: %s") % " ".join(keywords)
    elif keywords:
        head = t("키워드: %s") % " ".join(keywords)
    else:
        head = t("전체 — 작업이 정해졌으면 `harness loop intent \"...\"` 로 기록하라")
    print(t("과거 관측 기록 (%s)") % head)
    if not rows:
        print(t("  (없음)"))
    for r in rows:
        mark = t("  ← 여러 작업에서 반복") if r["loops"] > 1 else ""
        print(t("  %-10s %-16s %-34s ×%d (작업 %d)%s")
              % (r["kind"], r["rule"] or "-", (r["target"] or "")[:34],
                 r["c"], r["loops"], mark))

    churn = con.execute(
        "SELECT target, COUNT(*) c, COUNT(DISTINCT loop_id) loops FROM event "
        "WHERE kind='edit' GROUP BY target HAVING c >= 5 "
        "ORDER BY c DESC LIMIT 5").fetchall()
    if churn and not kind:
        matched = [r for r in churn
                   if not keywords or any(k.lower() in (r["target"] or "").lower()
                                          for k in keywords)]
        if matched:
            print(t("\n재편집이 많은 파일"))
            for r in matched:
                print(t("  %-40s ×%d (작업 %d)") % (r["target"][:40], r["c"], r["loops"]))

    indexes, files = _recall_files(cfg, root, keywords)
    if indexes:
        print(t("\n인덱스 — 쌓인 기록의 진입점 (먼저 읽어라)"))
        for f in indexes:
            print("  %s" % f)
    print(t("\n관련 회고·학습 파일 (필요하면 읽어라)"))
    if not files:
        print(t("  (없음)"))
    for f in files:
        print("  %s" % f)
    return 0


def cli_stats(ctx, argv):
    """누적 수치. --loop 를 주면 현재 작업만."""
    con, cfg, root, lid = ctx.con, ctx.cfg, ctx.root, ctx.lid
    only = "--loop" in argv
    cond, params = ("WHERE loop_id = ?", [lid]) if only else ("", [])
    print(t("범위: %s") % (t("현재 작업 %s") % lid if only else t("전체 누적")))

    lc = con.execute("SELECT COUNT(*) c, SUM(closed_at IS NOT NULL) closed "
                     "FROM loop").fetchone()
    print(t("작업: %d개 (완료 %d)") % (lc["c"], lc["closed"] or 0))

    rows = con.execute("SELECT kind, COUNT(*) c FROM event %s GROUP BY kind "
                       "ORDER BY c DESC" % cond, params).fetchall()
    print(t("이벤트: ") + (", ".join("%s %d" % (EVENT_KINDS.get(r["kind"], r["kind"]), r["c"])
                                  for r in rows) or t("(없음)")))

    # 반복 신호는 규칙 단위로 봐야 드러난다. (규칙, 대상) 으로 묶으면
    # 같은 규칙에 다른 파일로 계속 걸리는 패턴이 흩어져 보이지 않는다.
    # tool_fail 만 예외 — 정규화된 명령 자체가 의미 있는 키다.
    for kind, title, key in (("block", t("차단된 규칙"), "rule"),
                             ("tool_fail", t("실패한 도구"), "target"),
                             ("skip", t("건너뛴 단계"), "rule"),
                             ("stop_gate", t("미충족 종료 조건"), "rule"),
                             ("bypass", t("우회한 게이트 (사전승인 포함)"), "rule")):
        q = ("SELECT IFNULL(%s,'-') k, COUNT(*) c, COUNT(DISTINCT loop_id) loops, "
             "COUNT(DISTINCT target) targets FROM event WHERE kind=? %s "
             "GROUP BY k ORDER BY loops DESC, c DESC LIMIT 6"
             % (key, "AND loop_id=?" if only else ""))
        rs = con.execute(q, ([kind] + params)).fetchall()
        if not rs:
            continue
        print("\n%s" % title)
        for r in rs:
            bits = "×%d" % r["c"]
            if key == "rule" and r["targets"] > 1:
                bits += t(", 대상 %d종") % r["targets"]
            if r["loops"] > 1:
                bits += t(" ← %d개 작업에서 반복") % r["loops"]
            print("  %-24s %s" % (r["k"][:24], bits))
    with con:
        sync_promotions(con, cfg)
    refresh_learned(con, cfg, root)
    rows = promotion_rows(con, order="maturity, at")
    if rows:
        print(t("\n승격 이력 — 반복을 기계화한 기록"))
        for r in rows:
            print("  %-28s %-10s %-12s %s"
                  % (r["key"][:28], r["decision"], r["maturity"],
                     (r["note"] or "-")[:36]))
    pend = pending_promotions(con, cfg)
    if pend:
        print(t("\n승격 결정 대기 %d개 — `harness promote`") % len(pend))
    print(t("\n상세 조회: `harness recall <키워드|경로>`"))
    return 0


@auto_skip_sub("off")
def _as_off(ctx, argv, pos):
    con = ctx.con
    with con:
        set_meta(con, "auto_skip", "off")
        set_meta(con, "auto_skip_uses", "")
        set_meta(con, "auto_skip_loop", "")
        set_meta(con, "auto_skip_off_at", now())
    print(t("스킵 자동 승인 OFF — 이제 모든 스킵이 사용자 동의를 요구한다."))
    return 0


@auto_skip_sub("on")
def _as_on(ctx, argv, pos):
    con, cfg, lid = ctx.con, ctx.cfg, ctx.lid
    reason = argv_value(argv, "reason")
    uses = argv_value(argv, "uses")
    scope = argv_value(argv, "scope") or "project"
    if not reason:
        raise Refuse(t("사용법: harness auto-skip on --reason \"...\" "
                     "[--uses N] [--scope loop|project]"), code=2)
    if scope not in ("loop", "project"):
        raise Refuse(t("--scope 는 loop 또는 project 여야 한다."), code=2)
    if uses is not None:
        try:
            if int(uses) < 1:
                raise ValueError
        except ValueError:
            raise Refuse(t("--uses 는 1 이상의 정수여야 한다."), code=2)
    with con:
        set_meta(con, "auto_skip", "on")
        set_meta(con, "auto_skip_reason", reason)
        set_meta(con, "auto_skip_at", now())
        set_meta(con, "auto_skip_uses", str(int(uses)) if uses else "")
        set_meta(con, "auto_skip_loop", lid if scope == "loop" else "")
    print(t("스킵 자동 승인 ON%s — 사유: %s")
          % (t(" (사용자 승인)") if "auto-skip" in consent_map(cfg) else "", reason))
    print(t("범위: %s") % auto_skip_scope_note(con))
    print(t("사유는 계속 필수이고 기록에는 authorized_by=auto 로 남는다. "
          "끄려면 `harness auto-skip off`."))


@auto_skip_sub("status")
def _as_status(ctx, argv, pos):
    con = ctx.con
    active, expired = auto_skip_state(con)
    if active:
        print(t("스킵 자동 승인: ON (since %s) — 사유: %s")
              % (get_meta(con, "auto_skip_at", "-"),
                 get_meta(con, "auto_skip_reason", "-")))
        print(t("  범위: %s") % auto_skip_scope_note(con))
    else:
        print(t("스킵 자동 승인: OFF — 모든 스킵이 사용자 동의를 요구한다.")
              + (" (%s)" % expired if expired else ""))
    return 0


def cli_auto_skip(ctx, argv):
    """스킵 자동 승인 토글. on 은 PreToolUse 가 사람의 동의를 받은 뒤에만 도달한다."""
    pos = argv_positional(argv)
    return dispatch(AUTO_SKIP_SUBS, "auto-skip", pos[0] if pos else "status")(ctx, argv, pos)


@loop_sub("new")
def _loop_new(ctx, argv, pos):
    con, cfg, root, lid = ctx.con, ctx.cfg, ctx.root, ctx.lid
    intent = argv_value(argv, "intent")
    with con:
        nlid = rotate_loop(con, cfg, root, lid, intent)
        if nlid and intent:
            record_evidence(con, nlid, cfg["stages"][0]["id"], "intent_set", intent)
    if not nlid:
        raise Refuse(t("이미 %s 는 닫혔다 — 다른 호출이 먼저 새 작업을 시작했다. "
                       "`harness status` 로 확인하라.") % lid, code=1)
    print(t("작업 %s 종료 → 새 작업 %s, 단계 %s")
          % (lid, nlid, label_of(cfg, cfg["stages"][0]["id"])))
    return 0


@loop_sub("intent")
def _loop_intent(ctx, argv, pos):
    con, lid, sid = ctx.con, ctx.lid, ctx.sid
    text = " ".join(pos[1:]).strip() or (argv_value(argv, "intent") or "").strip()
    if not text:
        raise Refuse(t("사용법: harness loop intent \"<이번 루프에서 할 작업>\""), code=2)
    with con:
        con.execute("UPDATE loop SET intent=? WHERE id=?", (text, lid))
        # Scaffolding 의 종료 조건. 작업을 기록하지 않으면 단계를 넘어갈 수 없다.
        record_evidence(con, lid, sid, "intent_set", text)
    print(t("작업 %s 의 내용: %s") % (lid, text))
    print(t("Context 단계의 `harness recall` 이 이 작업을 기준으로 과거 기록을 찾는다."))
    return 0


@loop_sub("done-when")
def _loop_done_when(ctx, argv, pos):
    con, lid, sid = ctx.con, ctx.lid, ctx.sid
    items = [it for it in pos[1:] if it.strip()]
    if "--clear" in argv:
        with con:
            con.execute("DELETE FROM evidence WHERE loop_id=? AND kind='acceptance'",
                        (lid,))
        print(t("완료 조건을 비웠다. 다시 기록하라."))
        return 0
    if items:
        with con:
            for it in items:
                record_evidence(con, lid, sid, "acceptance", it.strip())
    rows = acceptance_of(con, lid)
    if not rows:
        raise Refuse(t("사용법: harness loop done-when \"<완료 조건>\" [\"<조건2>\" ...] [--clear]"),
                     t("무엇이 '끝'인지 기록한다. Verification 이 이것을 대조하고,"),
                     t("Compounding 이 작업 종료 판단의 근거로 쓴다. 회차가 바뀌어도 유지된다."), code=2)
    print(t("작업 %s 의 완료 조건 (%d개):") % (lid, len(rows)))
    for i, it in enumerate(rows, 1):
        print("  %d. %s" % (i, it))
    return 0


@loop_sub("adopt")
def _loop_adopt(ctx, argv, pos):
    con, cfg, root, lid = ctx.con, ctx.cfg, ctx.root, ctx.lid
    want = pos[1] if len(pos) > 1 else None
    if not want:
        raise Refuse(t("사용법: harness loop adopt <loop-id> --reason \"...\""), code=2)
    # **재연결은 상태 전이다.** 예전에는 `create_loop`(INSERT OR IGNORE)에
    # 회차 +1 을 얹었고, 그래서 세 가지가 어긋났다 — 적대적 리뷰가 전부 찾았다.
    #   ① 없는 ID 를 adopt 하면 cycle=1 행을 만든 뒤 올려 **첫 상태가 회차 2**
    #   ② 연속 adopt 하면 `cycle_close` 없이 회차만 올라 **측정 창에 이전
    #      회차의 이벤트가 섞인다**
    #   ③ 닫힌 작업을 adopt 해도 `closed_at` 이 남아 tidy 가 닫힌 작업으로 본다
    #
    # 뿌리는 하나다. `INSERT OR IGNORE` 가 "만들기"와 "이어받기"를 뭉갰고,
    # `cycle` 이 **접두사 구분자**와 **측정 창 번호** 두 일을 겸했다.
    # 그래서 둘을 갈라 명시적으로 처리한다.
    existed = loop_row(con, want)
    with con:
        # **버리는 것도 끝내는 것이다 — 집계는 남는다.** 3회차에는 재연결이
        # `cycle_close` 를 쓰면 회고 창까지 옮겨 가는 것이 문제라 아예 집계를
        # 안 남겼는데, 그러면 마찰이 쌓인 회차를 `loop adopt` 한 줄로 표본에서
        # 지울 수 있었다(4회차 C③). 두 리뷰가 각자 옳았다 — 뿌리는 `cycle_close`
        # 가 **측정 창**과 **회고 창** 두 경계를 겸한 것이다. 종류를 갈라
        # `cycle_adopt` 에 집계를 담는다: 측정 창은 옮기고 회고 창은 두고.
        close_loop(con, cfg, lid, "cycle_adopt")
        if existed:
            # 이어받는다. 회차를 올리고 다시 열린 작업이므로 closed_at 을 지운다.
            new_cycle = (existed["cycle"] or 1) + 1
            # 사유는 **집계와 다른 행**에 남긴다. 같은 행에 넣었더니 JSON 처럼
            # 생긴 사유가 가짜 회차로 집계됐다(3회차).
            record_event(con, want, cfg["stages"][0]["id"], "cycle_adopt_reason",
                         str(existed["cycle"] or 1),
                         "%s-%s" % (want, existed["cycle"] or 1),
                         argv_value(argv, "reason") or "")
            con.execute("UPDATE loop SET cycle=?, closed_at=NULL WHERE id=?",
                        (new_cycle, want))
            create_loop(con, cfg, root, argv_value(argv, "reason"), loop_id=want)
        else:
            # 없던 ID 다. 새로 만드는 것이므로 1회차에서 시작한다.
            new_cycle = 1
            create_loop(con, cfg, root, argv_value(argv, "reason"), loop_id=want)
    if existed:
        print(t("작업 %s 재연결(사용자 승인). 단계는 1단계부터, 회차 %d 로 다시 "
                "추적한다 — 지난 회차의 산출물은 이번 조건을 채우지 않는다.")
              % (want, new_cycle))
    else:
        print(t("작업 %s 는 기록에 없어 **새로 만들었다**(사용자 승인). "
                "회차 1 · 단계 1부터 시작한다.") % want)
    return 0


@loop_sub("show")
def _loop_show(ctx, argv, pos):
    con, lid = ctx.con, ctx.lid
    row = loop_row(con, lid)
    print("loop %s · branch %s · created %s"
          % (lid, row["branch"] if row else "-", row["created_at"] if row else "-"))
    if row and row["intent"]:
        print("  intent: %s" % row["intent"])
    return 0


def cli_loop(ctx, argv):
    """서브명령을 **표에서** 찾는다. if 체인이었을 때 오타가 조용히 `show` 로
    떨어져 `harness loop inetnt "작업 내용"` 이 rc=0 을 냈다 — 사용자는
    기록됐다고 믿었다."""
    pos = argv_positional(argv)
    return dispatch(LOOP_SUBS, "loop", pos[0] if pos else "show")(ctx, argv, pos)


CLI = {
    "status": cli_status,
    "advance": cli_advance,
    "skip": cli_skip,
    "verify": cli_verify,
    "allow": cli_allow,
    "approve-plan": cli_approve_plan,
    "loop": cli_loop,
    "auto-skip": cli_auto_skip,
    "recall": cli_recall,
    "stats": cli_stats,
    "promote": cli_promote,
    "tidy": cli_tidy,
    "metrics": cli_metrics,
}


def cli_init(argv):
    root = os.path.abspath(argv[0] if argv else os.getcwd())
    pr = plugin_root()
    created = install_templates(root, pr)
    db_made, lid = install_db(root, load_config(root, pr))
    created += db_made
    refresh_wrapper(root)

    nperm = ensure_permissions(root)
    if nperm > 0:
        created.append(t(".claude/settings.json (조회 명령 %d개 허용)") % nperm)
    elif nperm == -2:
        print(t("주의: .claude/settings.json 을 다른 쪽이 동시에 쓰고 있어 권한 허용을 "
              "건너뛰었다. 남의 변경을 덮지 않으려고 포기한 것이다 — `harness init` 을 "
              "다시 실행하면 된다."), file=sys.stderr)
    elif nperm < 0:
        print(t("주의: .claude/settings.json 을 읽을 수 없어 권한 허용을 건너뛰었다."),
              file=sys.stderr)

    created += install_gitignore(root)
    created += install_anchors(root)
    created += install_agents_md(root)
    label = None
    with swallow(t("설치 후 점검")):
        con2 = connect(root)
        if con2 is not None:
            try:
                sid2 = active_stage(con2, lid)
                if sid2:
                    label = label_of(load_config(root, pr), sid2)
            finally:
                con2.close()
    render_init(root, created, lid, label)
    return 0


