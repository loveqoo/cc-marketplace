"""승격 게이트. 엔진(`h`)을 **주입받는다** — 되돌아 import 하지 않는다.

`harness.py` 를 `python3 harness.py` 로 직접 실행하면 그 모듈은 `__main__`
이다. 여기서 `import harness` 하면 **같은 파일이 두 번 로드되어** 게이트가
다른 모듈 객체에 등록된다. 주입이 그 함정을 통째로 없앤다.
"""


def register(h):
    @h.gate
    class PromotionGate(h.Gate):
        """반복된 항목이 승격 결정을 **강제하나** (Compounding 의 종료 조건).

        3회차가 찾은 것: `promotion.kinds: []` 는 진단하는데 같은 게이트의 `min_loops`
        와 `exclude_rules` 는 침묵했다. **한 게이트의 손잡이가 흩어져 있으면 하나를 막고
        나머지를 잊는다.** 손잡이를 여기 모아 두고, 하나라도 게이트를 끄면 탐침이 잡는다.
        """

        KINDS = ("block", "tool_fail", "stop_gate", "bypass", "skip")
        knobs = ("promotion.kinds", "promotion.min_loops", "promotion.exclude_rules")

        key = "promotion"

        # Compounding 의 종료 조건은 `pending_promotions` 가 비었나로 판정된다.
        # 예전 탐침은 자기 `_live(cfg)` 를 불렀다 — `state()` 와 **같은 식**이라
        # 요약과 탐침이 함께 틀렸고, 둘이 서로를 확인해 줄 수 없었다(4회차).
        entry = ("pending_promotions",)

        @property
        def name(self):
            return h.t("승격 게이트")

        def _live(self, cfg):
            """이 설정으로 승격 후보가 **모일 수 있나.** 손잡이를 전부 본다."""
            return (bool(cfg.seq("promotion.kinds"))
                    and cfg.num("promotion.min_loops", 3, low=2) <= 20
                    and not set(cfg.seq("promotion.kinds"))
                    <= set(cfg.seq("promotion.exclude_rules", ())))

        def state(self, cfg):
            return (1 if self._live(cfg) else 0), 1

        # 탐침이 심는 합성 이력의 크기. **이것이 "게이트가 산다"의 정의다** —
        # 서로 다른 작업 셋에서 같은 마찰이 반복되면 결정을 요구해야 한다.
        # `min_loops` 를 그보다 크게 두면 이 탐침이 실패한다. 그래야 옳다:
        # 20개 작업을 기다리는 게이트는 사실상 꺼진 것이다(4회차 B#7·D-H4).
        SEED_LOOPS = 3
        PROBE_RULE = "__probe_rule__"

        def _seed(self, con, ctx, kind):
            """서로 다른 작업 여러 개에 같은 마찰을 심는다. 사본이라 새지 않는다."""
            for i in range(self.SEED_LOOPS):
                con.execute(
                    "INSERT INTO event(at,loop_id,stage,kind,rule,target) "
                    "VALUES(?,?,?,?,?,?)",
                    (h.now(), "__probe%d__" % i, ctx.sid, kind,
                     self.PROBE_RULE, self.PROBE_RULE))

        def probes(self, ctx):
            def collects():
                """**훅과 같은 길.** 합성 이력을 심고 `pending_promotions` 에 묻는다."""
                with h.probe_loop(ctx.con) as (con, _lid):
                    self._seed(con, ctx, "block")
                    got = any(it["key"] == "block:%s" % self.PROBE_RULE
                              for it in h.pending_promotions(con, ctx.cfg))
                return got, (h.t("결정을 요구한다") if got else
                             h.t("작업 %d개에서 반복돼도 결정을 요구하지 않는다")
                             % self.SEED_LOOPS)

            def unrelated():
                """승격 종류가 아닌 이벤트까지 붙잡으면 과잉이다."""
                with h.probe_loop(ctx.con) as (con, _lid):
                    self._seed(con, ctx, "edit")
                    got = any(it["key"] == "edit:%s" % self.PROBE_RULE
                              for it in h.pending_promotions(con, ctx.cfg))
                return got, h.t("edit 까지 모은다")

            return [(h.t("승격 게이트: 반복 항목을 모은다"), collects, True),
                    (h.t("승격 게이트: 무관한 이벤트는 모으지 않는다"), unrelated, False)]

        def problems(self, cfg):
            out = []
            bad = [k for k in cfg.seq("promotion.kinds") if k not in self.KINDS]
            if bad:
                out.append(h.t("promotion.kinds 의 %s 는 기록되는 이벤트 종류가 아니다 "
                             "(%s 중에서 골라라) — 그 종류는 아무것도 모으지 않는다")
                           % (", ".join(sorted(bad)), ", ".join(self.KINDS)))
            if not cfg.seq("promotion.kinds"):
                out.append(h.t("promotion.kinds 가 비어 있다 — 반복 항목을 하나도 모으지 "
                             "않으므로 Compounding 의 승격 게이트가 늘 충족된 상태가 된다"))
            lo = cfg.num("promotion.min_loops", 3, low=2)
            if lo > 20:
                out.append(h.t("promotion.min_loops 가 %d 이다 — 그만큼 반복되는 항목은 "
                             "사실상 없으므로 승격 게이트가 늘 충족된 상태가 된다") % lo)
            gone = set(cfg.seq("promotion.kinds")) & set(cfg.seq("promotion.exclude_rules", ()))
            if gone and gone == set(cfg.seq("promotion.kinds")):
                out.append(h.t("promotion.exclude_rules 가 모을 종류를 전부 제외한다 "
                             "(%s) — 승격 게이트가 늘 충족된 상태가 된다")
                           % ", ".join(sorted(gone)))
            return out
    