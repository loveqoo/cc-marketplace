"""엔진 구현 조각. **목록을 손으로 적지 않는다** — 이 폴더를 훑는다.

## 왜 갈랐나

`harness.py` 가 6,300줄이었다. 그 안에 추상(게이트·탐침·훅 표)과 구현(CLI 명령,
셸 해석, 설치, 렌더링)이 섞여 있어서, 무엇이 뼈대고 무엇이 살인지 읽어서는 알 수
없었다. 뼈대만 남기고 살을 여기로 옮긴다.

## 어떻게 옮기나 — **코드를 한 글자도 고치지 않는다**

`gates/` 는 `h.t(...)` 처럼 엔진을 접두사로 부른다. 그 방식으로 이 규모를 옮기면
이름 치환이 필요하고, 치환은 **문자열 리터럴 안까지 바꿔** 한 번 크게 당했다
(`"write_rules[%d]"` → `"h.write_rules[%d]"`).

그래서 접두사 대신 **이름공간을 맞춘다.** 엔진이 두 걸음으로 배선한다:

  ① 채택 — 조각이 정의한 이름을 엔진 모듈에 올린다
  ② 주입 — 엔진의 모든 이름을 각 조각의 전역에 넣는다 (조각 자신의 정의는 보존)

②가 ① 다음이라 **조각끼리도 서로를 본다.** 옮긴 코드는 원래 전역을 그대로 쓰던
그대로 돌아간다 — 치환이 없으므로 치환 사고도 없다.

`import harness` 를 하지 않는 이유는 `gates/` 와 같다: `python3 harness.py` 로
직접 실행하면 그 모듈은 `__main__` 이고, 되돌아 import 하면 **같은 파일이 두 번
로드되어** 등록이 엉뚱한 모듈 객체로 간다.
"""
import importlib
import importlib.util
import pkgutil
import sys


def modules():
    return sorted(m.name for m in pkgutil.iter_modules(__path__))


def load(engine):
    """조각을 전부 싣고 엔진과 이름공간을 맞춘다. 실패는 `(모듈, 이유)` 로 돌려준다.

    한 조각이 깨져도 나머지는 싣는다 — 한 파일의 사고가 다른 조각을 끌 이유는 없다.
    """
    fails, mods = [], []
    shared = {k: v for k, v in vars(engine).items() if not k.startswith("__")}
    for name in modules():
        try:
            # **이름을 먼저 넣고 실행한다.** 조각은 import 시점에도 엔진을 쓴다
            # (`LOOP_SUBS, loop_sub = sub_table()` 같은 모듈 수준 배선과 그것을
            # 쓰는 데코레이터). 평범하게 import 하면 그 줄에서 NameError 다.
            spec = importlib.util.find_spec("." + name, __name__)
            mod = importlib.util.module_from_spec(spec)
            mod.__dict__.update(shared)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            mods.append(mod)
        except Exception as exc:            # noqa: BLE001 - 적재 실패도 사실이다
            fails.append((name, "%s: %s" % (type(exc).__name__, exc)))

    for m in mods:                          # ① 채택
        # `_` 로 시작하는 것도 올린다. 이 설계에서 엔진 모듈은 **공유 이름공간**
        # 이고, 조각끼리도 서로의 헬퍼(`_first_violation` 등)를 부른다.
        # 던더만 뺀다.
        for n in dir(m):
            if not n.startswith("__"):
                setattr(engine, n, getattr(m, n))
    later = {k: v for k, v in vars(engine).items() if not k.startswith("__")}
    for m in mods:                          # ② 주입 — 조각끼리도 서로를 본다
        for k, v in later.items():
            m.__dict__.setdefault(k, v)
    return fails
