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

        @property
        def name(self):
            return h.t("종료 조건")

        def state(self, cfg):
            stages = cfg.get("stages") or []
            return (sum(1 for st in stages
                        if isinstance(st, dict) and (st.get("exit_criteria") or [])),
                    len(stages))

        def probes(self, ctx):
            cfg = ctx.cfg
            out = []
            for name, spec in sorted((cfg.obj("criteria") or {}).items()):
                if not isinstance(spec, dict):
                    continue
                out += self._one(cfg, name, spec)
            # **막는 쪽.** 사람만 채울 수 있는 조건이 증거 없이 충족되면 안 된다.
            human = [k for k, v in sorted((cfg.obj("criteria") or {}).items())
                     if isinstance(v, dict) and v.get("human")]
            if human:
                k = human[0]
                out.append((h.t("증거 없는 '%s' 는 단계를 막는다") % k,
                            h._bind(lambda: (not h.criterion_met(ctx.con, cfg, ctx.root,
                                                             ctx.lid, k),
                                           h.t("증거 없이 충족된다"))), True))
            # 통과하는 쪽. 둘 다 없으면 게이트가 자기를 증명하지 못한다.
            out.append((h.t("종료 조건이 하나라도 있다"),
                        h._bind(lambda: (not (cfg.obj("criteria") or {}),
                                       h.t("조건이 하나도 없다"))), False))
            return out

        def _one(self, cfg, name, spec):
            """조건 하나의 탐침들. `(설명, 호출, 막혀야 하나)` — 여기서 '막힘' 은
            **결함이 있다** 는 뜻이다(전부 want=True 로 두고 결함을 찾는다)."""
            how = spec.get("satisfied_by")
            out = []

            def bad(desc, fn):
                out.append((desc, fn, False))     # 결함이 없어야 한다 = 통과

            if spec.get("human"):
                bad(h.t("%s 는 사람만 채울 수 있는 방식인가") % name,
                    h._bind(lambda: (how != "cli",
                                   h.t("human: true 인데 satisfied_by=%s 다 — 파일을 쓰거나 "
                                     "도구를 쓰는 것만으로 사람의 승인이 된다") % how)))
            if how == "file":
                pats = cfg.seq("criteria.%s.write_glob" % name)
                hits = [p for p in h.UNRELATED if any(h.glob_match(p, g) for g in pats)]
                bad(h.t("%s 는 무관한 파일을 받지 않는다") % name,
                    h._bind(lambda: (bool(hits),
                                   h.t("무관한 경로도 받는다: %s") % ", ".join(hits))))
            if how in ("cli", "no_pending_promotions") and spec.get("write_glob"):
                bad(h.t("%s 의 write_glob 이 방식과 맞지 않는다") % name,
                    h._bind(lambda: (True, h.t("satisfied_by=%s 인데 write_glob 이 있다") % how)))
            if how == "no_pending_promotions":
                bad(h.t("%s 가 실제로 무언가를 모은다") % name,
                    h._bind(lambda: (not cfg.seq("promotion.kinds"),
                                   h.t("promotion.kinds 가 비어 있어 늘 충족된다"))))
            if how == "observed":
                out += self._observed(cfg, name, spec, bad)
            return out

        def _observed(self, cfg, name, spec, bad):
            """관측으로 채우는 조건. 신호가 셋(bash_pattern·tools·tool_pattern)이라
            하나만 탐침하면 나머지로 새어 나간다 — 셋 다 본다."""
            pat = spec.get("bash_pattern")
            bad(h.t("%s 에 판정 기준이 있다") % name,
                h._bind(lambda: (not pat,
                               h.t("bash_pattern 이 없어 아무 명령도 검증이 되지 못한다"))))
            hits = [c for c in h.NOT_VERIFICATION if pat and h.verification_hit(cfg, c)]
            bad(h.t("%s 는 검증 명령만 인정한다") % name,
                h._bind(lambda: (bool(hits),
                               h.t("검증이 아닌 명령도 인정: %s") % ", ".join(hits[:4]))))
            missed = [c for c in h.MUST_VERIFY if not h.verification_hit(cfg, c)]
            bad(h.t("%s 가 실제 검증 명령을 받아들인다") % name,
                h._bind(lambda: (bool(missed),
                               h.t("이것들을 거부한다: %s") % ", ".join(missed[:4]))))
            tools = [x for x in cfg.seq("criteria.%s.tools" % name) if x in h.INNOCUOUS_TOOLS]
            bad(h.t("%s 는 무해한 도구를 증거로 세지 않는다") % name,
                h._bind(lambda: (bool(tools), h.t("무해한 도구도 증거: %s") % ", ".join(tools))))
            tp = spec.get("tool_pattern")
            try:
                tre = re.compile(tp) if tp else None
            except re.error:
                tre = None
            thits = [x for x in h.INNOCUOUS_TOOLS if tre and tre.search(x)]
            bad(h.t("%s 의 도구 패턴이 너무 넓지 않다") % name,
                h._bind(lambda: (bool(thits), h.t("무해한 도구에도 걸린다: %s") % ", ".join(thits))))
            return []

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
            return out
