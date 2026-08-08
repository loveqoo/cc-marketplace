"""중간 그래프 게이트. 엔진(`h`)을 **주입받는다** — 되돌아 import 하지 않는다.

`harness.py` 를 `python3 harness.py` 로 직접 실행하면 그 모듈은 `__main__`
이다. 여기서 `import harness` 하면 **같은 파일이 두 번 로드되어** 게이트가
다른 모듈 객체에 등록된다. 주입이 그 함정을 통째로 없앤다.
"""


def register(h):
    @h.gate
    class GraphGate(h.Gate):
        """샌드위치 구조가 실제로 지켜지나 — 틀 고정, 앞으로만 가는 중간 DAG,
        방향 비대칭 게이트(추가 자유·미방문 삭제는 스킵 동의).

        위상은 `graph_problems` 가 진단하고(설정 오타는 조용히 어긋난다),
        방향 비대칭은 훅이 강제한다. 둘 다 탐침으로 증명한다 — 진단이 죽거나
        훅 배선이 빠져도 여기서 소리가 난다.
        """

        knobs = ("stages[].next",)

        key = "graph"

        # 위상 진단은 `graph_problems` 를 지나고 (hook_session_start →
        # config_problems → gate_problems 가 부른다), 방향 비대칭·후진 스킵은
        # `hook_pre_tool_use` 를 지난다. 탐침도 같은 문으로 들어간다.
        entry = ("hook_pre_tool_use", "graph_problems")

        @property
        def name(self):
            return h.t("중간 그래프")

        def state(self, cfg):
            """(위상 문제 없는 중간 노드 수, 중간 노드 수)."""
            sts = [s for s in (cfg.get("stages") or [])
                   if isinstance(s, dict) and not s.get("__dyn__")]
            mid = [s.get("id") for s in sts[2:-1]] if len(sts) >= 3 else []
            probs = " ".join(h.graph_problems(cfg))
            bad = sum(1 for x in mid if x and ("stages[%s]" % x) in probs)
            return len(mid) - bad, len(mid)

        # 위상 탐침용 합성 그래프. 사용자의 설정을 건드리지 않고 **깨진 모양**을
        # 진짜 진단 함수에 넣어 본다 — 파싱이 아니라 실험이다 (bash_pattern 과
        # 같은 원칙).
        def _cfg(self, mut):
            base = [{"id": x, "label": x.title(), "summary": x}
                    for x in ("s0", "s1", "m1", "m2", "end")]
            mut(base)
            return h.Cfg({"stages": base})

        def probes(self, ctx):
            def diag(mut, needle):
                """깨진 합성 그래프가 진단에 잡히나. 잡히면 '막힘'이다."""
                probs = h.graph_problems(self._cfg(mut))
                hit = any(needle in p for p in probs)
                return hit, (probs[0][:60] if probs else h.t("진단 없음"))

            def clean():
                """기본 틀(선언 없음)은 아무 위상 문제도 없어야 한다 — 과잉 진단은
                기존 설치 호환을 깨는 방향이다."""
                probs = h.graph_problems(self._cfg(lambda sts: None))
                return bool(probs), (probs[0][:60] if probs else h.t("진단 없음"))

            def ask(cmd, cfg, sid=None):
                with h.probe_loop(ctx.con) as (con, lid):
                    d, why = h.probe_decision(
                        h.Ctx(con, cfg, ctx.root, lid, sid or ctx.sid),
                        {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                         "tool_input": {"command": cmd}})
                return bool(d), (why or h.t("판정 없음"))[:60]

            def back_skip():
                """뒤로 가는 스킵은 묻지 않고 거부한다 — 앞으로만 가는 DAG 의
                훅 쪽 절반이다. 마지막 단계에 서서 두 번째 단계를 겨눈다."""
                ids = h.stage_ids(ctx.cfg)
                return ask("%s skip %s --reason x" % (h.WRAPPER_CMD, ids[1]),
                           ctx.cfg, ids[-1])

            def dyn_cfg():
                """미방문 회차 한정 노드 하나가 실린 그래프 — 삭제 탐침의 재료."""
                c2 = h.Cfg(dict(ctx.cfg))
                sts = list(ctx.cfg["stages"])
                dyn = {"id": "__probe_node__", "label": "P", "summary": "p",
                       "write": ["dev"], "exit_criteria": [], "__dyn__": True}
                c2["stages"] = sts[:-1] + [dyn] + sts[-1:]
                return c2

            def remove_asks():
                return ask("%s path remove __probe_node__ --reason x"
                           % h.WRAPPER_CMD, dyn_cfg())

            def add_free():
                return ask("%s path add probe-add --reason x" % h.WRAPPER_CMD,
                           ctx.cfg)

            out = [
                (h.t("뒤로 가는 엣지는 진단에 잡힌다"),
                 h._bind(diag, lambda sts: sts[3].update(next=["m1"]),
                         h.t("뒤로 가는 엣지")), True),
                (h.t("틀 노드의 next 선언은 진단에 잡힌다"),
                 h._bind(diag, lambda sts: sts[0].update(next=["m2"]),
                         h.t("틀 노드")), True),
                (h.t("도달하지 않는 중간 노드는 진단에 잡힌다"),
                 h._bind(diag, lambda sts: sts[2].update(next=["end"]),
                         h.t("도달하지 않는다")), True),
                (h.t("선언 없는 기본 틀은 위상 문제가 없다"), clean, False),
                (h.t("미방문 회차 노드 삭제는 동의를 받는다 (스킵의 위장)"),
                 remove_asks, True),
                (h.t("노드 추가는 동의를 묻지 않는다 (일이 늘 뿐이다)"),
                 add_free, False),
            ]
            if len(h.stage_ids(ctx.cfg)) >= 3:
                out.append((h.t("뒤로 가는 스킵은 묻지 않고 거부한다"),
                            back_skip, True))
            return out

        def problems(self, cfg):
            return h.graph_problems(cfg)
