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

        # 훅은 `hook_stop` 으로 판정한다. 예전 탐침은 설정을 세고 자기
        # `_limits_live` 를 부를 뿐 이 함수를 한 번도 지나지 않았다 — 그래서
        # `hook_stop` 본문을 통째로 비워도 `턴 종료 게이트 2/7` 이 그대로였고
        # `stop_continue.enabled: false` 도, 항목별 `stop_block_limits: 0` 도
        # 조용했다(4회차 B#3·D-H3).
        entry = ("hook_stop",)

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

        def _pair(self, ctx):
            """**증거로 뒤집을 수 있는** (단계, 조건) 하나를 찾는다.

            양방향 탐침의 재료다 — 같은 단계를 증거 없이/있이 두 번 돌려
            `hook_stop` 이 실제로 막고 실제로 놓아주는지 본다. 고정 목록을
            적지 않는다: 설정이 바뀌면 재료도 따라 바뀌어야 한다.
            """
            for st in ctx.cfg.get("stages") or []:
                if not isinstance(st, dict):
                    continue
                for key in st.get("stop_requires") or []:
                    with h.probe_loop(ctx.con) as (con, lid):
                        if h.criterion_met(con, ctx.cfg, ctx.root, lid, key):
                            continue          # 증거 없이도 충족 — 뒤집을 수 없다
                        h.record_evidence(con, lid, st["id"], key, "probe")
                        if h.criterion_met(con, ctx.cfg, ctx.root, lid, key):
                            return st, key
            return None, None

        def probes(self, ctx):
            st, key = self._pair(ctx)
            if st is None:
                # 뒤집을 수 있는 조건이 하나도 없으면 이 게이트는 아무것도 막지
                # 못한다. 그 사실 자체가 결과다 — `hook_stop` 을 지나지 않았으므로
                # 진입점 검사가 이것을 실패로 낸다.
                return []

            def run(seed):
                """훅과 **같은 길**로 Stop 을 돌리고, 그 조건으로 막았는지 본다."""
                with h.probe_loop(ctx.con) as (con, lid):
                    if seed:
                        for k in st.get("stop_requires") or []:
                            h.record_evidence(con, lid, st["id"], k, "probe")
                    h.hook_stop({"prompt_id": "__probe__",
                                 "last_assistant_message": ""},
                                h.Ctx(con, ctx.cfg, ctx.root, lid, st["id"]))
                    why = h.criterion_why(con, ctx.cfg, ctx.root, lid, key)
                hit = any(why and why in str(o.get("reason", ""))
                          for o in h.PROBE_EMITS
                          if isinstance(o, dict) and o.get("decision") == "block")
                return hit, why

            return [(h.t("증거 없는 '%s' 는 턴 종료를 막는다") % key,
                     h._bind(run, False), True),
                    (h.t("증거 있는 '%s' 로는 막지 않는다") % key,
                     h._bind(run, True), False)]

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
    