"""쓰기 게이트. 엔진(`h`)을 **주입받는다** — 되돌아 import 하지 않는다.

`harness.py` 를 `python3 harness.py` 로 직접 실행하면 그 모듈은 `__main__`
이다. 여기서 `import harness` 하면 **같은 파일이 두 번 로드되어** 게이트가
다른 모듈 객체에 등록된다. 주입이 그 함정을 통째로 없앤다.
"""
import os
import re


def register(h):
    @h.gate
    class WriteGate(h.Gate):
        """어느 단계에서 어디에 쓸 수 있나 — 그리고 하네스 자신은 못 건드리나.

        이 게이트의 네 책임이 **15곳**에 흩어져 있었다(줄 539~3754). 그래서 세 회차 동안
        한 자리를 고치고 형제 자리를 남겼다: 대소문자 → symlink → glob → 대입 → 따옴표.
        판정 **함수**는 그대로 둔다(그건 기계다). 여기 모으는 것은 **조율** —
        무엇이 켜져 있나 / 그것을 어떻게 증명하나 / 설정이 옳은가.
        """

        knobs = ("write_rules", "folder_rules.protected_paths", "bash.mutator_pattern",
                 "bash.readers", "bash.interpreters", "path_classes")

        key = "write"

        # 훅이 지나는 문 셋. 예전 탐침은 `floor_hit` + `_first_violation` 을
        # 직접 불렀다 — `check_write` 보다 한 층 아래다. 그래서 `check_write`
        # 본문을 통째로 비워도 `쓰기 규칙 7/7` 이 그대로였다(4회차 D-C1).
        # 훅 표가 지나는 문. 아래 층(`check_write` 등)은 여기를 통해 지나간다.
        entry = ("hook_pre_tool_use",)

        @property
        def name(self):
            return h.t("쓰기 규칙")

        def state(self, cfg):
            rules = h.write_rules(cfg)
            return sum(1 for r in rules if h.rule_reachable(cfg, r)), len(rules)

        def probes(self, ctx):
            """**훅 표에서 출발한다.** 네 헬퍼가 하던 일을 하나로 모았다.

            예전에는 `check_write`·`floor_verdict`·`bash_writes` 를 직접 불렀다.
            그 다섯 함수는 지켰지만 **그 위의 훅 디스패처는 아무도 소유하지
            않았다** — `HOOKS = {}` 한 줄이면 게이트가 전부 죽는데 자기검사
            34/34 가 그대로였다(5회차 D-C1). 문을 하나 위에서 연다.
            """
            cfg, root = ctx.cfg, ctx.root

            def hook(tool, ti, at=None, use=None):
                at = at or ctx.sid
                if not h.stage_known(cfg, at):
                    raise ValueError(h.t("탐침을 돌릴 수 없다 — 단계 '%s' 가 없다") % at)
                with h.probe_loop(ctx.con) as (con, lid):
                    yield con, lid
                    d, why = h.probe_decision(
                        h.Ctx(con, use or cfg, root, lid, at),
                        {"hook_event_name": "PreToolUse", "tool_name": tool,
                         "tool_input": ti})
                    self._last = (bool(d), "%s(%s)" % (d, (why or "")[:60]) if d else "")

            def broken_re(cmd, at=None):
                """**설정 정규식이 깨진 경로.** `bash_mutator_re` 는 `re.error` 에서
                코드 상수로 되돌아간다 — "잘못된 정규식으로 게이트를 열지 않는다".
                그 주장에 탐침도 진단도 없어서, 페일세이프를 무매칭 패턴으로
                바꿔도 검사 전부가 초록이었다(5회차 A6). 깨진 설정으로 한 번
                돌려 본다."""
                bad = h.Cfg(dict(cfg))
                bad["bash"] = dict(cfg.obj("bash"), mutator_pattern="[")
                return _run(hook("Bash", {"command": cmd}, at, use=bad))

            def _run(gen):
                for _ in gen:
                    pass
                return self._last

            def wr(rel, at=None):
                """Write 도구가 훅을 지난다."""
                return _run(hook("Write", {"file_path": rel}, at))

            def wrg(rel, at=None):
                """**예외가 걸린 상태**로. `grant_opens` 규칙(docs_readonly)이 비켜야
                뒤 규칙이 유일한 차단자가 되고, 그때만 탐침이 구분력을 갖는다.
                사본에 **진짜 예외 행**을 넣는다 — 소진돼도 남지 않는다."""
                g = hook("Write", {"file_path": rel}, at)
                for con, lid in g:
                    h.grant_write(con, lid, rel, "selftest", 1)
                return self._last

            def bh(cmd):
                """바닥값도 같은 문으로. 경로를 특정하면 deny, 원문에만 보이면 ask."""
                return _run(hook("Bash", {"command": cmd}))

            def br(cmd, at=None):
                """Bash 쓰기도 훅과 같은 문."""
                return _run(hook("Bash", {"command": cmd}, at))

            def wr_prefixed():
                """접두사는 **탐침이 도는 작업**의 것이어야 한다. 실제 작업의
                접두사를 격리된 작업에 대고 물으면 늘 어긋난다."""
                with h.probe_loop(ctx.con) as (con, lid):
                    rel = ".dev/plan/%sprobe.md" % h.file_prefix(con, lid)
                    d, why = h.probe_decision(
                        h.Ctx(con, cfg, root, lid, ctx.sid),
                        {"hook_event_name": "PreToolUse", "tool_name": "Write",
                         "tool_input": {"file_path": rel}})
                return bool(d), (why or "")

            out = []
            for desc, call, want in (
                    # 바닥값 — 설정으로 열 수 없어야 한다
                    (h.t("하네스 엔진 사본"), h._bind(wr, h.ENGINE_REL.replace(os.sep, "/")), True),
                    (h.t("상태 DB"), h._bind(wr, h.DB_REL.replace(os.sep, "/")), True),
                    (h.t("Bash 로 엔진 삭제"), h._bind(bh, "rm " + h.ENGINE_REL), True),
                    (h.t("Bash 로 .claude 삭제"), h._bind(bh, "rm -rf .claude"), True),
                    # `readers` 는 denylist(`NEVER_BENIGN`)로 막는데, 목록에 없는 이름을
                    # 넣으면 바닥값이 열린다(`perl -i` 는 `sed -i` 와 같은 일을 한다).
                    # 이름을 더 넣는 대신 **결과를 탐침한다.**
                    # 0.75.0: 인자 판정을 확실한 자리로 좁혔다. `perl -i` 는
                    # 어느 인자가 대상인지 문법이 답하지 않아 판정하지 않는다.
                    (h.t("perl -i 는 판정하지 않는다 (인자 순서를 모른다)"),
                     h._bind(bh, "perl -i -pe s/a/b/ " + h.ENGINE_REL), False),
                    # 셸 확장은 펼치지 않는다 — 원문에 보이면 묻는다. 이 탐침이
                    # 없으면 그 층이 자기증명 밖에 남는다(4회차 A·C②).
                    # 하네스가 **펼치지 못하는** 셸 문법. 원문 매칭도 근사였다 —
                    # 조립하면 원문에도 안 나타난다(5회차 A-C1·C③).
                    # 셸 확장을 되묻던 규칙은 0.74.0 에서 지웠다 — 실사용 589개
                    # 재생에서 멈춤의 절반 이상이 이것이었고(67/126), 걸린 것은
                    # 임시 경로를 변수로 뺀 정상 작업이었다. 남은 계약은 **원문에
                    # 바닥값 이름이 보이면 받는다** 이므로 그 모양을 탐침으로 둔다.
                    # 0.75.0: 원문 감시를 판정에서 뺐다. `cp` 는 어느 인자가
                    # 대상인지 모르므로 확장 여부와 무관하게 판정하지 않는다.
                    (h.t("cp 는 확장이 있든 없든 판정하지 않는다"),
                     h._bind(bh, 'cp evil "$(pwd)/.claude/harness/harness.db"'), False),
                    # 통과하는 쪽 — 변수를 썼다는 이유만으로는 되묻지 않는다.
                    # 이 자리가 곧 마찰이었다.
                    (h.t("변수를 썼다는 이유만으로는 묻지 않는다"),
                     h._bind(bh, 'cp evil "$D/x"'), False),
                    # **바닥값에 대고 묻는다.** 예전에는 `touch docs/probe.md` 로
                    # 물었는데, Bash 의 단계 규칙이 기록으로 내려가면서 그 질문은
                    # 늘 "안 막힘" 이 됐다 — 페일세이프를 시험하지 못한다.
                    # 깨진 설정으로도 지켜야 하는 것은 이제 바닥값이다.
                    (h.t("설정 정규식이 깨져도 막는다"),
                     h._bind(broken_re, "rm " + h.ENGINE_REL, "selection"), True),
                    # 래퍼는 **경로가 아니라 내용**이 지킨다 — 이 경로를 열어도
                    # 다음 Bash 앞에서 `wrapper_intact` 가 복구하고 거절한다.
                    (h.t("래퍼는 내용 검사가 지킨다 (경로 판정 없이)"),
                     h._bind(bh, 'cp evil "$(pwd)/' + h.WRAPPER_CMD + '"'), False),
                    # 인라인 코드 안이라도 **이름 그대로** 부르면 원문 감시가
                    # 잡는다. "인라인이면 되묻는다" 규칙을 지운 뒤 바닥값을
                    # 지키는 것은 이것이므로, 여기가 그 계약의 자리다.
                    (h.t("인라인 코드는 판정하지 않는다 (의미는 문법 밖)"),
                     h._bind(bh, 'python3 -c "open(\'' + h.DB_REL.replace(os.sep, "/")
                             + '\',\'w\')"'), False),
                    # 통과하는 쪽이 없으면 "묻는다" 탐침만으로는 규칙을 무조건
                    # 참으로 바꿔도 초록이다. 그리고 이 자리가 곧 마찰이다 —
                    # `.claude/*` 를 만지는 정상 작업은 되묻지 않는다.
                    (h.t("인라인이라는 이유만으로는 묻지 않는다"),
                     h._bind(bh, 'python3 -c "print(open(\'.claude/settings.json\').read())"'),
                     False),
                    # 클래스를 지우는 것은 규칙을 지우는 게 아니라 그 경로를 가장 넓은
                    # 클래스로 **옮기는** 것이다 — `context` 를 비우면 훅·CLAUDE.md·
                    # stages.json 이 한꺼번에 열린다.
                    (h.t("context 경로가 분류를 잃지 않는다"),
                     h._bind(wr, ".claude/hooks/probe.json", "execution"), True),
                    # 단계별 쓰기 규칙 — Write 와 Bash 가 **같은 판정**을 받아야 한다
                    (h.t("단계별 쓰기 허용 (Write)"), h._bind(wr, ".claude/settings.json",
                                                    "selection"), True),
                    # **Bash 의 단계 규칙은 막지 않는다 — 기록한다.** 예전에는 이
                    # 둘이 "Write 와 같은 판정" 을 단정했다. 그 불변식은 은퇴했다:
                    # 같으려면 명령에서 정확한 경로를 알아내야 하는데 그건 정적으로
                    # 결정 불가능하다(`.dev/shell-write-detection-is-undecidable.md`).
                    # 통과하는 쪽 단정으로 남긴다 — 과잉 차단이 되돌아오면 여기가
                    # 먼저 빨개진다.
                    (h.t("Bash 단계 규칙은 막지 않는다"),
                     h._bind(br, "printf x > .claude/settings.json", "selection"), False),
                    (h.t("sed -i 도 막지 않는다"),
                     h._bind(br, "sed -i s/a/b/ .claude/settings.json", "selection"), False),
                    (h.t("docs/ 쓰기 (사람의 영역)"), h._bind(wr, "docs/probe.md"), True),
                    (h.t("설정의 보호 경로"),
                     h._bind(wr, ".claude/harness/LEARNED.md", "scaffolding"), True),
                    (h.t("신규 최상위 폴더"),
                     h._bind(wr, "__probe_top__/a.py", "execution"), True),
                    (h.t("예외가 있어도 docs 명명 규칙"),
                     h._bind(wrg, "docs/spec/__probe__.md", "scaffolding"), True),
                    (h.t(".dev/ 하위 폴더 규칙"), h._bind(wr, ".dev/__probe__/a.md"), True),
                    (h.t(".dev/ 산출물 접두사"), h._bind(wr, ".dev/plan/__probe__.md"), True),
                    # 통과해야 하는 것 — 과잉 차단은 마찰이고, 마찰은 게이트를 끈다
                    (h.t("예외가 있으면 docs 에 쓸 수 있다"),
                     h._bind(wrg, "docs/spec/001-probe.md", "scaffolding"), False),
                    (h.t("접두사 붙인 산출물은 통과"), wr_prefixed, False),
                    (h.t("변수를 써도 읽기는 통과"), h._bind(bh, 'git log "$REF"'), False),
                    (h.t("읽기는 통과"), h._bind(bh, "cat " + h.DB_REL), False),
                    (h.t("읽기 명령은 쓰기 대상을 만들지 않는다"),
                     h._bind(br, "grep -rn foo .claude/settings.json", "selection"), False)):
                out.append((desc, call, want))
            return out

        def problems(self, cfg):
            return self._syntax(cfg) + self._empty(cfg)

        def _syntax(self, cfg):
            """규칙 하나하나의 **문법**. 오타는 특히 조용하다 — 규칙이 아무것도 막지
            않거나, 반대로 아무 경로에도 해당하지 않아 통째로 죽는다."""
            out = []
            fr = cfg.obj("folder_rules")
            seen_ids = set()
            for i, r in enumerate(cfg.get("write_rules") or []):
                at = "write_rules[%d]" % i
                if not isinstance(r, dict):
                    out.append(h.t("%s 가 객체가 아니다 — 이 규칙은 무시된다") % at)
                    continue
                rid = r.get("id")
                if not rid:
                    out.append(h.t("%s 에 id 가 없다 — 차단 기록이 '?' 로 남아 승격에 쓸 수 없다") % at)
                elif rid in seen_ids:
                    out.append(h.t("%s 의 id '%s' 가 중복이다 — 통계가 두 규칙을 한 덩어리로 센다")
                               % (at, rid))
                else:
                    seen_ids.add(rid)
                at = "write_rules[%s]" % (rid or i)
                if not r.get("deny"):
                    out.append(h.t("%s 에 deny 메시지가 없다 — 막으면서 무엇을 하라는 말이 없다") % at)
                when, req = r.get("when") or {}, r.get("require") or {}
                for k in when:
                    if k not in h.WRITE_SELECTORS:
                        out.append(h.t("%s.when 의 '%s' 는 모르는 선택자다 (%s 중 하나) "
                                   "— 이 조건은 무시된다") % (at, k, "/".join(h.WRITE_SELECTORS)))
                tests = [k for k in req if k in h.WRITE_TESTS]
                unknown = [k for k in req if k not in h.WRITE_TESTS]
                for k in unknown:
                    out.append(h.t("%s.require 의 '%s' 는 모르는 판정이다 (%s 중 하나)")
                               % (at, k, "/".join(h.WRITE_TESTS)))
                if not tests:
                    out.append(h.t("%s 에 판정이 없다 — 이 규칙은 아무것도 막지 않는다") % at)
                elif len(tests) > 1:
                    out.append(h.t("%s 에 판정이 %d개다 (%s) — 하나만 쓴다. 첫 것만 적용된다")
                               % (at, len(tests), ", ".join(sorted(tests))))
                pname = req.get("predicate")
                if pname and pname not in h.WRITE_PREDICATES:
                    out.append(h.t("%s.require.predicate '%s' 라는 파이썬 술어가 없다 "
                               "— 이 규칙은 아무것도 막지 않는다") % (at, pname))
                # folder_rules 의 어느 목록을 가리키는 자리들. 없는 이름을 가리키면 조용히 죽는다.
                for field, holder in (("subdir_in", when), ("basename_not_in", when),
                                      ("subdir_in", req), ("not_matching", req),
                                      ("stage_in", req)):
                    name = holder.get(field)
                    if isinstance(name, str) and name not in fr:
                        out.append(h.t("%s 의 %s='%s' 가 folder_rules 에 없다 — %s")
                                   % (at, field, name,
                                      h.t("이 규칙이 어떤 경로에도 해당하지 않는다")
                                      if holder is when else h.t("아무것도 막지 못한다")))
                name = req.get("basename_matches")
                if isinstance(name, str) and cfg.at(name) is None:
                    out.append(h.t("%s.require.basename_matches='%s' 가 설정에 없다 "
                               "— 아무것도 막지 못한다") % (at, name))
            return out

        def _empty(self, cfg):
            """**있는데 공허한 값.** 빈 목록·아무것도 안 맞는 정규식·모르는 클래스."""
            out = []
            fr = cfg.get("folder_rules")
            if isinstance(fr, dict) and "protected_paths" in fr\
                    and not cfg.seq("folder_rules.protected_paths"):
                out.append(h.t("folder_rules.protected_paths 가 비어 있다 — 하네스 자신은 "
                             "코드의 바닥값으로 계속 보호되지만, 여기에 적었던 경로는 "
                             "더 이상 보호되지 않는다"))
            if "write_rules" in cfg and not (cfg.get("write_rules") or []):
                out.append(h.t("write_rules 가 비어 있다 — 폴더·파일명 규칙이 하나도 없다 "
                             "(하네스 자기 잠금만 바닥값으로 남는다)"))
            if not isinstance(cfg.get("write_rules", []), list):
                out.append(h.t("write_rules 가 배열이 아니다 — 규칙이 하나도 적용되지 않는다"))
            mpat = cfg.at("bash.mutator_pattern")
            if mpat:
                try:
                    probe = re.compile(mpat)
                except re.error:
                    probe = None
                if probe is not None and not any(
                        probe.search(x) for x in
                        ("rm x", "mv a b", "echo x > y", "sed -i s/a/b/ f")):
                    out.append(h.t("bash.mutator_pattern 이 흔한 변경 명령을 하나도 잡지 "
                                 "못한다 — 문법은 맞지만 사실상 꺼진 것이다"))
            dead = [(r.get("id") or i) for i, r in enumerate(h.write_rules(cfg))
                    if not h.rule_reachable(cfg, r)]
            if dead:
                out.append(h.t("write_rules 의 %s 는 어떤 경로에도 해당할 수 없다 "
                             "(모르는 class 이거나 판정이 없다) — 아무것도 막지 못한다")
                           % ", ".join(str(d) for d in dead[:5]))
            for i, r in enumerate(h.write_rules(cfg)):
                w = r.get("when")
                cls = w.get("class") if isinstance(w, dict) else None
                if cls is not None and cls not in h.known_classes(cfg):
                    out.append(h.t("write_rules[%s].when.class='%s' 는 path_classes 에 없다 "
                                 "(%s 중 하나) — 이 규칙은 죽어 있다")
                               % (r.get("id") or i, cls,
                                  "/".join(sorted(h.known_classes(cfg)))))
            # readers·interpreters 는 둘 다 '무해 선언' 이고 둘 다 잠금을 푸는 데 쓰였다.
            for key, what in (("readers", h.t("읽기")), ("interpreters", h.t("인터프리터"))):
                bad = [r for r in cfg.seq("bash." + key) if r in h.NEVER_BENIGN]
                if bad:
                    out.append(h.t("bash.%s 의 %s 는 변경 명령이라 %s로 선언할 수 없다 "
                                 "— 무시된다") % (key, ", ".join(sorted(bad)), what))
            return out
