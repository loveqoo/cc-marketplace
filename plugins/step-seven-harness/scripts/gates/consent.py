"""동의 게이트. 엔진(`h`)을 **주입받는다** — 되돌아 import 하지 않는다.

`harness.py` 를 `python3 harness.py` 로 직접 실행하면 그 모듈은 `__main__`
이다. 여기서 `import harness` 하면 **같은 파일이 두 번 로드되어** 게이트가
다른 모듈 객체에 등록된다. 주입이 그 함정을 통째로 없앤다.
"""


def register(h):
    @h.gate
    class ConsentGate(h.Gate):
        """사람의 승인이 필요한 명령이 실제로 승인을 요구하나.

        이 게이트의 네 책임이 예전에는 여섯 곳에 흩어져 있었다 — 판정(`ctrl_requests`),
        요약(`enforcing_summary`), 탐침(`consent_probes` + `SELFTEST` 행), 진단
        (`config_problems`). 하나만 고치면 나머지는 조용히 남았다.
        """

        knobs = ("consent",)
        # 엔진이 '결과가 무거워 사람이 봐야 한다' 고 보는 명령들. 설정은 이것을 줄일 수
        # 있고, 줄였다는 **사실이 요약에 보인다** — 막지는 않는다.
        FLOOR = ("skip", "allow", "approve-plan", "auto-skip", "loop new", "loop adopt")

        key = "consent"

        @property
        def name(self):
            return h.t("승인 필요")
        # 감싸는 껍데기와 대표 인자. **게이트가 자기 재료를 소유한다** — 밖에 두면
        # 게이트를 옮길 때 재료가 남고, 남은 재료는 다음 사람이 못 지운다.
        WRAPS = ("%s", '"%s"', "sh -c '%s'", 'bash -lc "%s"')
        ARGS = {"skip": "context --reason x", "allow": "docs/** --reason x",
                "approve-plan": "p.md", "auto-skip": "on --reason x",
                "loop new": "--reason x", "loop adopt": "abcdef --reason x"}

        def state(self, cfg):
            have = h.consent_map(cfg)
            return sum(1 for k in self.FLOOR if k in have), len(self.FLOOR)

        def probes(self, ctx):
            need = h.consent_map(ctx.cfg)

            def ask(cmd):
                subs = [c[0] for c in h.ctrl_requests(cmd)]
                return any(x in need for x in subs), ",".join(subs) or h.t("제어 명령 아님")

            out = []
            for i, name in enumerate(self.FLOOR):
                shape = (h.t("그대로"), h.t("따옴표"), h.t("sh -c"), h.t("중첩 셸"))[i % 4]
                call = "%s %s %s" % (h.WRAPPER_CMD, name, self.ARGS.get(name, ""))
                out.append((h.t("동의 게이트: %s (%s)") % (name, shape),
                            h._bind(ask, self.WRAPS[i % 4] % call.strip()), True))
            # 통과하는 쪽이 없으면 이 게이트는 구분력이 없다 — 조회 명령은 묻지 않는다.
            out.append((h.t("조회 명령은 동의를 묻지 않는다"),
                        h._bind(ask, "%s status" % h.WRAPPER_CMD), False))
            return out

        def problems(self, cfg):
            known = set(h.CLI) | {"loop new", "loop adopt"}
            bad = [k for k in h.consent_map(cfg) if k not in known]
            return [h.t("consent 의 %s 는 실제 명령 이름이 아니다 — 그 항목은 아무것도 "
                      "막지 않는다") % ", ".join(sorted(bad))] if bad else []
