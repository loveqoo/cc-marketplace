"""턴 종료 게이트. 엔진(`h`)을 **주입받는다** — 되돌아 import 하지 않는다.

`harness.py` 를 `python3 harness.py` 로 직접 실행하면 그 모듈은 `__main__`
이다. 여기서 `import harness` 하면 **같은 파일이 두 번 로드되어** 게이트가
다른 모듈 객체에 등록된다. 주입이 그 함정을 통째로 없앤다.
"""


def register(h):
    @h.gate
    class StopGate(h.Gate):
        """턴을 끝내기 전에 무엇을 요구하나.

        `stop_requires` 를 비우면 검증 증거·회고·승격 결정의 강제가 통째로 사라지는데
        요약이 한 글자도 안 바뀌었다(2회차). 그것을 고쳤더니 이번엔 `stop_block_limits`
        를 0 으로 두는 길이 남아 있었다(3회차) — **같은 게이트의 다른 손잡이**다.
        손잡이를 여기 모아 두고, 어느 쪽으로 꺼도 탐침이 잡는다.
        """

        knobs = ("stages[].stop_requires", "stop_block_limits", "stop_continue.enabled")

        key = "stop"

        @property
        def name(self):
            return h.t("턴 종료 게이트")

        def _limits_live(self, cfg):
            lim = cfg.get("stop_block_limits")
            if not isinstance(lim, dict):
                return True                      # 없으면 기본값 1 이 쓰인다
            return any(int(v or 0) > 0 for v in lim.values()) if lim else True

        def state(self, cfg):
            stages = cfg.get("stages") or []
            live = sum(1 for st in stages
                       if isinstance(st, dict) and (st.get("stop_requires") or []))
            return (live if self._limits_live(cfg) else 0), len(stages)

        def probes(self, ctx):
            cfg = ctx.cfg

            def requires():
                """요구하는 단계가 하나라도 있나."""
                n = sum(1 for st in (cfg.get("stages") or [])
                        if isinstance(st, dict) and (st.get("stop_requires") or []))
                return n > 0, h.t("%d개 단계가 요구한다") % n

            def budget():
                """차단 예산이 0 이면 첫 시도부터 소진으로 떨어져 한 번도 못 막는다."""
                ok = self._limits_live(cfg)
                return (not ok), h.t("stop_block_limits 가 전부 0 이다")

            return [(h.t("턴 종료를 요구하는 단계가 있다"), requires, True),
                    (h.t("차단 예산이 0 이 아니다"), budget, False)]

        def problems(self, cfg):
            out = []
            if not self._limits_live(cfg):
                out.append(h.t("stop_block_limits 가 전부 0 이다 — 첫 시도부터 '상한 소진' "
                             "으로 떨어져 턴 종료 게이트가 한 번도 막지 못한다"))
            stages = cfg.get("stages") or []
            if stages and not any(isinstance(st, dict) and (st.get("stop_requires") or [])
                                  for st in stages):
                out.append(h.t("어떤 단계도 stop_requires 가 없다 — 검증 증거·회고·승격 "
                             "결정을 미충족 상태로 턴을 끝낼 수 있다"))
            return out
    