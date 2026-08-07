"""게이트 구현 묶음. **목록을 손으로 적지 않는다** — 이 폴더를 훑는다.

적으면 새 게이트가 기본 '등록 안 됨' 이 되고, 등록 안 된 게이트는 조용하다.
그 침묵이 오늘 하루 종일 고친 병이다.
"""
import importlib
import pkgutil


def modules():
    return sorted(m.name for m in pkgutil.iter_modules(__path__))


def register(h):
    """엔진을 주입해 모든 게이트를 등록한다. **한 파일이 깨져도 나머지는 싣는다.**

    예전에는 첫 예외에서 루프가 통째로 중단됐다. `criteria.py` 하나를 문법 오류로
    만들면 알파벳 뒤의 `promotion`·`stop`·`write` 까지 못 실려 **네 게이트가
    한꺼번에 꺼졌다**(4회차 D-M10). 한 파일의 사고가 다른 게이트를 끌 이유는 없다.

    실패는 `(모듈, 이유)` 로 돌려준다 — 삼키지 않는다. 무엇이 왜 안 실렸는지
    말하지 못하면 사용자는 고칠 수 없다.
    """
    fails = []
    for name in modules():
        try:
            importlib.import_module("." + name, __name__).register(h)
        except Exception as exc:            # noqa: BLE001 - 적재 실패도 사실이다
            fails.append((name, "%s: %s" % (type(exc).__name__, exc)))
    return fails
