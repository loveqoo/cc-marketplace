"""종료 조건 게이트. 엔진(`h`)을 **주입받는다** — 되돌아 import 하지 않는다.

`harness.py` 를 `python3 harness.py` 로 직접 실행하면 그 모듈은 `__main__`
이다. 여기서 `import harness` 하면 **같은 파일이 두 번 로드되어** 게이트가
다른 모듈 객체에 등록된다. 주입이 그 함정을 통째로 없앤다.
"""
import re


def register(h):
    @h.gate
    class CriteriaGate(h.Gate):
        """단계의 **종료 조건**이 자기 산출물만 받아들이나.

        흩어져 있던 것: 판정(`criterion_met`·`verification_hit`·`fs_evidence`),
        요약(`enforcing_summary` 의 `criteria`/`gated_stages`), 탐침(`selftest_criteria`
        70줄 + `MUST_VERIFY`/`NOT_VERIFICATION`/`UNRELATED`/`INNOCUOUS_TOOLS`),
        진단(`config_problems`). 열한 곳이었다.
        """

        knobs = ("criteria", "stages[].exit_criteria")

        key = "criteria"

        # 훅은 `criterion_met` 으로 판정한다. 탐침도 거기를 지나야 한다.
        entry = ("criterion_met",)

        @property
        def name(self):
            return h.t("종료 조건")

        def state(self, cfg):
            stages = cfg.get("stages") or []
            return (sum(1 for st in stages
                        if isinstance(st, dict) and (st.get("exit_criteria") or [])),
                    len(stages))

        def probes(self, ctx):
            """**진짜 판정 함수를 양방향으로 통과시킨다.**

            예전 탐침 12개 중 11개는 설정 모양 검사였다 — `satisfied_by` 가
            맞나, 글롭이 너무 넓나. 그것은 판정을 지나지 않으므로 게이트가
            죽어도 조용하다. 전부 `problems()` 로 옮겼다. 여기 남는 것은
            "증거가 없으면 막고, 있으면 통과한다" 하나뿐이고, 그것이 이
            게이트가 하는 일의 전부다.
            """
            cfg = ctx.cfg
            human = [k for k, v in sorted((cfg.obj("criteria") or {}).items())
                     if isinstance(v, dict) and v.get("human")]
            if not human:
                return []
            key = human[0]

            def unmet():
                with h.probe_loop(ctx.con) as (con, lid):
                    return (not h.criterion_met(con, cfg, ctx.root, lid, key),
                            h.t("증거 없이 충족된다"))

            def met():
                with h.probe_loop(ctx.con) as (con, lid):
                    h.record_evidence(con, lid, ctx.sid, key, "probe")
                    return (not h.criterion_met(con, cfg, ctx.root, lid, key),
                            h.t("증거를 넣어도 충족되지 않는다"))

            return [(h.t("증거 없는 '%s' 는 단계를 막는다") % key, unmet, True),
                    (h.t("증거 있는 '%s' 는 통과한다") % key, met, False)]

        def _shape(self, cfg, name, spec):
            """조건 하나의 **설정 진단.** 판정을 지나지 않으므로 탐침이 아니다.

            예전에는 이것들이 탐침 자리에 있었고, 그래서 `자기검사 42/42` 의
            대부분이 "설정이 앞뒤가 맞다" 는 뜻이었다 — 게이트가 실제로 막는지와
            무관한 수였다(4회차). 진단은 진단 자리에 둔다.
            """
            how = spec.get("satisfied_by")
            out = []

            def bad(cond, msg):
                if cond:
                    out.append(msg)

            if spec.get("human"):
                bad(how != "cli",
                    h.t("criteria.%s 는 human: true 인데 satisfied_by=%s 다 — 파일을 "
                        "쓰거나 도구를 쓰는 것만으로 사람의 승인이 된다") % (name, how))
            if how == "file":
                pats = cfg.seq("criteria.%s.write_glob" % name)
                hits = [x for x in h.UNRELATED if any(h.glob_match(x, g) for g in pats)]
                bad(hits, h.t("criteria.%s 가 무관한 경로도 받는다: %s")
                    % (name, ", ".join(hits)))
            if how in ("cli", "no_pending_promotions") and spec.get("write_glob"):
                bad(True, h.t("criteria.%s 는 satisfied_by=%s 인데 write_glob 이 있다 "
                              "— 그 파일은 조건을 채우지 못한다") % (name, how))
            if how == "no_pending_promotions":
                bad(not cfg.seq("promotion.kinds"),
                    h.t("criteria.%s 는 promotion.kinds 가 비어 있어 늘 충족된다") % name)
            if how == "observed":
                out += self._observed(cfg, name, spec)
            return out

        def _observed(self, cfg, name, spec):
            """관측으로 채우는 조건. 신호가 셋(bash_pattern·tools·tool_pattern)이라
            하나만 보면 나머지로 새어 나간다 — 셋 다 본다."""
            out = []

            def bad(cond, msg):
                if cond:
                    out.append(msg)

            pat = spec.get("bash_pattern")
            bad(not pat, h.t("criteria.%s 에 bash_pattern 이 없어 아무 명령도 검증이 "
                             "되지 못한다") % name)
            hits = [c for c in h.NOT_VERIFICATION if pat and h.verification_hit(cfg, c)]
            bad(hits, h.t("criteria.%s 가 검증이 아닌 명령도 인정한다: %s")
                % (name, ", ".join(hits[:4])))
            missed = [c for c in h.MUST_VERIFY if not h.verification_hit(cfg, c)]
            bad(missed, h.t("criteria.%s 가 실제 검증 명령을 거부한다: %s")
                % (name, ", ".join(missed[:4])))
            tools = [x for x in cfg.seq("criteria.%s.tools" % name)
                     if x in h.INNOCUOUS_TOOLS]
            bad(tools, h.t("criteria.%s 가 무해한 도구를 증거로 센다: %s")
                % (name, ", ".join(tools)))
            tp = spec.get("tool_pattern")
            try:
                tre = re.compile(tp) if tp else None
            except re.error:
                tre = None
            thits = [x for x in h.INNOCUOUS_TOOLS if tre and tre.search(x)]
            bad(thits, h.t("criteria.%s 의 tool_pattern 이 무해한 도구에도 걸린다: %s")
                % (name, ", ".join(thits)))
            return out

        def problems(self, cfg):
            out = []
            for name, spec in sorted(cfg.obj("criteria").items()):
                if not isinstance(spec, dict):
                    out.append(h.t("criteria.%s 가 객체가 아니다 — 이 조건은 무시된다") % name)
                    continue
                how = spec.get("satisfied_by")
                if how is None:
                    out.append(h.t("criteria.%s 에 satisfied_by 가 없다 — cli 로 간주한다")
                               % name)
                elif how not in h.SATISFIED_BY:
                    out.append(h.t("criteria.%s.satisfied_by='%s' 는 모르는 값이다 "
                                 "(%s 중 하나) — 판정이 cli 로 떨어진다")
                               % (name, how, "/".join(h.SATISFIED_BY)))
                if how == "file" and not spec.get("write_glob"):
                    out.append(h.t("criteria.%s 는 satisfied_by=file 인데 write_glob 이 없다 "
                                 "— 어떤 파일도 이 조건을 채우지 못한다") % name)
                out += self._shape(cfg, name, spec)
            return out
