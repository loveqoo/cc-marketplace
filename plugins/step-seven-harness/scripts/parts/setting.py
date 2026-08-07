"""설정 읽기와 진단. 무엇이 조용히 무시되는지 말한다.

엔진이 이름공간을 맞춰 준다 — `parts/__init__.py` 참고.
"""


def load_config(root, plugin_root_dir=None):
    """프로젝트의 stages.json. 없는 **최상위 영역만** 템플릿에서 채운다.

    왜 채우나: `install_templates` 는 기존 stages.json 을 덮지 않는다 — 사용자가
    고친 문서이기 때문이다. 그래서 새 설정 영역을 추가하면 **기존 설치는 영원히
    받지 못한다.** 규칙이 코드에 있을 때는 티가 안 났지만, 어휘를 설정으로 옮기는
    순간 그건 게이트가 조용히 어긋나는 것이다.

    왜 최상위 영역만인가: 사용자의 작업 방식이 "마찰이 크면 stages.json 에서
    덜어낸다"이고, 그건 영역 **안의 항목**을 지우는 일이다. 항목 단위로 병합하면
    지운 것이 되살아나 그 자유를 빼앗는다. 그래서 영역이 통째로 없을 때만 채우고,
    비워 둔 것(`[]`, `{}`)은 비운 대로 존중한다.
    """
    path = os.path.join(root, CONFIG_REL)
    cfg = jload(path)
    if plugin_root_dir is None:
        plugin_root_dir = plugin_root()
    # **있는데 못 읽는 것과 없는 것은 다르다.** 손상된 문서를 템플릿으로 갈아치우면
    # 사용자가 덜어낸 규칙이 말없이 되살아나고, 그 사람은 이유 모를 차단만 본다.
    # 없으면(설치 전) 템플릿을 쓰고, 깨졌으면 그대로 알린다.
    if cfg is None and os.path.exists(path):
        return None
    tpl = jload(os.path.join(plugin_root_dir, "templates", "stages.json"))
    if tpl is None:
        # 프로젝트 사본으로 실행 중이다. 기본값은 엔진 옆에 함께 복사돼 있다.
        tpl = jload(os.path.join(root, DEFAULTS_REL))
    if cfg is None:
        cfg = tpl
    elif isinstance(cfg, dict) and isinstance(tpl, dict):
        had_criteria = "criteria" in cfg
        for k, v in tpl.items():
            cfg.setdefault(k, v)
        if not had_criteria:
            _adopt_evidence_signals(cfg)
    return Cfg(cfg) if isinstance(cfg, dict) else cfg


def consent_map(cfg):
    """사람의 승인이 필요한 하네스 명령 → 무엇을 승인하는지의 설명.

    설정이라는 것은 **줄일 수 있다는 뜻**이다. `allow` 다이얼로그가 시끄러우면
    하네스를 끄는 대신 그 항목을 덜어낼 수 있다.
    """
    m = cfg.obj("consent")
    return {k: t(v) for k, v in m.items() if isinstance(v, str)} if m else {}


def drift_problems(cfg, root):
    """**내장 조건의 판정 방식이 기본값에서 바뀐 것**을 알린다.

    조건의 이름과 판정 방식은 의미상 묶여 있다. `promotion_decided` 를
    `satisfied_by: file` 로 바꾸면 미결 승격이 남아 있어도 회차가 닫히고,
    `plan_approved` 를 `file` + `write_glob: ["**"]` 로 바꾸면 아무 파일이 사람의
    승인이 된다. 둘 다 적대적 리뷰에서 실증했고 아무 경고가 없었다.

    엔진에 이름을 다시 박아 금지하지는 않는다 — 그건 어휘화를 되돌리는 것이다.
    대신 **기본값과 대조해 달라진 것을 말한다.** 사용자가 정의한 조건은 기본값에
    없으므로 아무것도 말하지 않는다(오진 없음).
    """
    tpl = jload(os.path.join(plugin_root(), "templates", "stages.json")) \
        or jload(os.path.join(root, DEFAULTS_REL))
    if not isinstance(tpl, dict):
        return []
    base = tpl.get("criteria") or {}
    out = []
    for name, spec in sorted((cfg.obj("criteria") or {}).items()):
        want = base.get(name)
        if not isinstance(want, dict) or not isinstance(spec, dict):
            continue
        if spec.get("satisfied_by") != want.get("satisfied_by"):
            out.append(t("criteria.%s 의 판정 방식을 '%s' → '%s' 로 바꿨다 "
                         "— 이 조건의 이름이 뜻하는 것과 판정이 어긋난다. "
                         "의도한 것이면 그대로 두어라.")
                       % (name, want.get("satisfied_by"), spec.get("satisfied_by")))
        if want.get("human") and not spec.get("human"):
            out.append(t("criteria.%s 에서 human 표시를 뗐다 — 사람만 채울 수 있던 "
                         "조건이 모델도 채울 수 있게 된다") % name)
        # 글롭이 모든 경로로 넓어지면 '그 산출물' 이라는 뜻이 사라진다
        if "**" in cfg.seq("criteria.%s.write_glob" % name) \
                and "**" not in (want.get("write_glob") or []):
            out.append(t("criteria.%s.write_glob 이 '**' 로 넓어졌다 — 이 회차 "
                         "접두사가 붙은 **아무 파일이나** 이 조건을 채운다") % name)
    out += _rules_dropped(cfg, tpl)
    return out


def _rules_dropped(cfg, tpl):
    """기본값에 있는데 설정에서 **사라진** 쓰기 규칙.

    요약의 `쓰기 규칙 n/m` 은 분모가 **설정 자신**(`len(write_rules)`)이라
    규칙을 지우면 `7/7 → 6/6` 이 된다 — 삭제는 결코 결손으로 보이지 않았다
    (4회차 E-F12). 개수로는 잡을 수 없는 종류다. 분모를 템플릿으로 바꾸면
    사용자가 규칙을 **더할** 자유가 사라지므로, 개수 대신 **이름을 대조한다.**

    막지 않는다 — 규칙을 지우는 것은 사용자의 자유다. 지웠다는 **사실**만 말한다.
    단계 순서도 같은 종류다: 이름 집합은 같은데 순서가 바뀌면 우선순위가 바뀐다.
    """
    base = [r.get("id") for r in (tpl.get("write_rules") or []) if isinstance(r, dict)]
    have = {r.get("id") for r in write_rules(cfg) if isinstance(r, dict)}
    gone = [r for r in base if r and r not in have]
    out = []
    if gone:
        out.append(t("기본 쓰기 규칙 %s 가 설정에서 빠졌다 — 그 규칙이 막던 것이 "
                     "지금 아무것도 막지 않는다 (요약의 분모는 설정 자신이라 "
                     "삭제가 보이지 않는다). 의도한 것이면 그대로 두어라.")
                   % ", ".join(gone))
    b_st = [x.get("id") for x in (tpl.get("stages") or []) if isinstance(x, dict)]
    c_st = [x.get("id") for x in (cfg.get("stages") or []) if isinstance(x, dict)]
    if b_st and set(b_st) == set(c_st) and b_st != c_st:
        out.append(t("단계 순서를 바꿨다 (%s → %s) — 단계별 쓰기 허용과 종료 조건이 "
                     "따라 움직인다. 의도한 것이면 그대로 두어라.")
                   % (" → ".join(b_st), " → ".join(c_st)))
    return out + _wiring_changed(cfg, tpl)


# 단계가 소유한 **배선**. 이름이 아니라 값이 바뀌면 게이트가 통째로 옮겨 간다.
STAGE_WIRING = ("write", "exit_criteria", "stop_requires", "skip_requires",
                "skippable")


def _wiring_changed(cfg, tpl):
    """단계의 배선이 기본값에서 달라진 것.

    **이 축은 아무 게이트도 소유하지 않았다.** `stages[verification]` 의
    `exit_criteria` 를 이미 충족된 다른 조건(`intent_set`)으로 **갈아끼우면**
    Verification 게이트가 완전히 죽는데, 개수 기반 카운터(`4/7`)는 배열이
    비지 않았으므로 그대로였고 `criteria` 에도 그 조건이 남아 있어 "criteria 에
    없다" 진단도 안 걸렸다 — `status` 첫 두 줄이 stock 과 바이트 동일했다
    (5회차 B-C1·H8·H10).

    `skippable`/`skip_requires` 도 같다. 스킵 게이트는 `REQUIRED_GATES` 에
    없어서 소유자가 아예 없었다.

    개수로는 잡을 수 없다. 이름과 값을 템플릿과 **그대로 대조**한다. 막지
    않는다 — 바꾸는 것은 사용자의 자유이고, 바꿨다는 사실만 말한다.
    """
    base = {x.get("id"): x for x in (tpl.get("stages") or []) if isinstance(x, dict)}
    out = []
    for st in cfg.get("stages") or []:
        if not isinstance(st, dict):
            continue
        want = base.get(st.get("id"))
        if not want:
            continue                      # 사용자가 더한 단계 — 대조 대상이 아니다
        for key in STAGE_WIRING:
            a, b = want.get(key), st.get(key)
            if a == b:
                continue
            # **덜어낸 것은 말하지 않는다.** 어휘에서 빼는 것은 사용자의 자유이고,
            # 그 결정은 이미 내려져 있다(`덜어낸 것을 되살리지 않는다`). 위험한
            # 것은 축소가 아니라 **교체**다 — 배열이 비지 않으니 개수 카운터는
            # 그대로인데 내용이 딴것이 된다. `exit_criteria: ["intent_set"]` 하나로
            # Verification 게이트가 죽는데 `4/7` 이 유지됐다(5회차 B-C1).
            if isinstance(a, list) and isinstance(b, list):
                added = [x for x in b if x not in a]
                if not added:
                    continue                      # 순수 축소 — 조용
                out.append(t("stages[%s].%s 에 %s 가 들어왔다 (기본: %s) — 그 단계의 "
                             "강제가 따라 움직인다. 의도한 것이면 그대로 두어라.")
                           % (st.get("id"), key,
                              json.dumps(added, ensure_ascii=False),
                              json.dumps(a, ensure_ascii=False)))
            elif b and not a:
                # 불리언이 관대한 쪽으로 뒤집혔다 (`skippable: false → true`)
                out.append(t("stages[%s].%s 를 켰다 — 그 단계를 건너뛸 수 있게 된다. "
                             "의도한 것이면 그대로 두어라.") % (st.get("id"), key))
    return out


def language_problems(root):
    """번역 상태의 문제. config_problems 와 달리 root 가 필요해 따로 둔다."""
    lang, got, total = message_status(root)
    if not lang:
        return []
    if not got:
        return [t("language='%s' 인데 messages.%s.json 을 찾지 못했다 "
                  "— 모든 문장이 원문(한국어)으로 나온다") % (lang, lang)]
    if total and got < total:
        return [t("language='%s' 번역이 %d/%d (%d%%) — 나머지는 원문으로 나온다")
                % (lang, got, total, got * 100 // total)]
    return []


def config_problems(cfg):
    """설정의 **오타**를 찾는다. 규칙 위반이 아니라 어휘 오류만 본다.

    왜 필요한가: 어휘를 설정으로 옮기면 오타가 새로운 실패 방식이 된다. 그리고 그
    실패는 조용하다 — `satisfied_by: "fille"` 은 파일 검사를 말없이 끄고,
    `panels: ["work_candidatez"]` 는 아무것도 하지 않는다. 사용자는 자기가 설정한
    것이 동작한다고 믿는다. 게이트가 조용히 어긋나는 것은 이 하네스가 반복해서
    잡아온 부류이고, 어휘화로 그 표면을 넓혔으니 함께 막아야 한다.

    막지는 않는다 — 설정이 조금 틀렸다고 세션을 벽돌로 만들면 그게 더 나쁘다.
    무엇이 무시되고 있는지 **말한다.**
    """
    # 게이트는 **자기 설정을 스스로 진단한다.** 여기서 게이트마다 적으면 게이트를
    # 더할 때 이 자리를 잊게 되고, 잊어도 조용하다 — 오늘 그것으로 세 번 당했다.
    out = gate_problems(cfg)
    # recall 대상 폴더. 여기 오타가 나면 그 폴더의 기록은 **영원히 안 나온다** —
    # 파일은 있고 키워드도 맞는데 조회에 안 걸린다. 조용한 결함으로 실제로 있었다.
    dev_dirs = set(cfg.seq("folder_rules.dev_subdirs"))
    for d in cfg.seq("recall.dirs", RECALL_DIRS_DEFAULT):
        if dev_dirs and d not in dev_dirs:
            out.append(t("recall.dirs 의 '%s' 가 folder_rules.dev_subdirs 에 없다 "
                       "— 그 폴더에는 쓸 수 없으므로 조회할 것도 없다") % d)
    if not cfg.seq("recall.dirs", RECALL_DIRS_DEFAULT):
        out.append(t("recall.dirs 가 비어 있다 — 과거 회고를 하나도 찾지 못한다"))
    if not retro_questions(cfg):
        out.append(t("retro_questions 가 비어 있다 — 회고에서 무엇을 물을지가 없다"))
    for i, it in enumerate(cfg.seq("retro_questions")):
        if not isinstance(it, dict) or not it.get("q"):
            out.append(t("retro_questions[%d] 에 q 가 없다 — 이 질문은 무시된다") % i)
    # **있는데 공허한 값**을 본다. 예전 진단은 '없는 참조'와 '모르는 값'만 봤고,
    # 그래서 `protected_paths: []`·`write_rules: []`·`mutator_pattern: "(?!)"` 가
    # 아무 말 없이 강제를 껐다. 적대적 리뷰에서 여섯 모양으로 확인했다.
    # 바닥값(SELF_LOCK)이 있으므로 하네스 자기 잠금은 이제 이것들로 풀리지 않지만,
    # 사용자가 지정한 보호 경로와 규칙은 여전히 조용히 사라질 수 있다.
    # 검증 증거 패턴을 **탐침한다.** 설정 텍스트를 읽어 "이게 너무 넓은가"를 판단하는
    # 것은 끝이 없다(`.*`, `.+`, `[\s\S]*`, `|`…). 대신 **증거가 되면 안 되는 명령**을
    # 넣어 보고 걸리는지 본다. 파싱이 아니라 실험이다.
    vpat = cfg.at("criteria.verification_evidence.bash_pattern")
    if vpat:
        try:
            vre = re.compile(vpat)
        except re.error:
            vre = None
        if vre is not None:
            hits = [c for c in ("ls", "echo hi", "cat README.md", "git status")
                    if vre.search(c)]
            if hits:
                out.append(t("criteria.verification_evidence.bash_pattern 이 검증이 "
                             "아닌 명령도 증거로 인정한다 (%s) — 성공한 아무 명령이나 "
                             "Verification 을 통과시킨다") % ", ".join(hits))

    kinds = promote_as(cfg)
    if not [k for k in kinds if k != "declined"]:
        out.append(t("promotion.as_kinds 에 승격 종류가 없다 — 보류밖에 할 수 없다"))
    for k in cfg.obj("promotion.verify_globs"):
        if k not in kinds:
            out.append(t("promotion.verify_globs 의 '%s' 가 as_kinds 에 없다 "
                       "— 아무도 그 종류로 승격할 수 없어 죽은 설정이다") % k)
    pat = cfg.at("bash.mutator_pattern")
    if pat:
        try:
            re.compile(pat)
        except re.error as e:
            out.append(t("bash.mutator_pattern 이 잘못된 정규식이다 (%s) "
                       "— 기본 패턴으로 되돌아간다") % e)

    known = set(cfg.obj("criteria"))
    for st in cfg.get("stages") or []:
        if not isinstance(st, dict):
            continue
        sid = st.get("id", "?")
        for field in ("exit_criteria", "stop_requires", "skip_requires"):
            for k in st.get(field) or []:
                if k not in known:
                    out.append(t("stages[%s].%s 의 '%s' 가 criteria 에 없다 "
                               "— 채울 방법이 없어 이 단계를 끝낼 수 없다")
                               % (sid, field, k))
        for p in st.get("panels") or []:
            if p not in PANELS:
                out.append(t("stages[%s].panels 의 '%s' 는 모르는 패널이다 (%s 중 하나) "
                           "— 조용히 무시된다")
                           % (sid, p, "/".join(sorted(PANELS))))
    return out


def stage_known(cfg, sid):
    return bool(sid) and sid in stage_ids(cfg)


def protected_pats(cfg):
    """보호 경로 = 바닥값 ∪ 설정. 설정이 비어 있어도 바닥값은 남는다."""
    out = list(SELF_LOCK)
    for p in cfg.seq("folder_rules.protected_paths"):
        if isinstance(p, str) and p not in out:
            out.append(p)
    return out
