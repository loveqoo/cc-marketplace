"""게이트 구현 묶음. **목록을 손으로 적지 않는다** — 이 폴더를 훑는다.

적으면 새 게이트가 기본 '등록 안 됨' 이 되고, 등록 안 된 게이트는 조용하다.
그 침묵이 오늘 하루 종일 고친 병이다.
"""
import importlib
import pkgutil


def modules():
    return sorted(m.name for m in pkgutil.iter_modules(__path__))


def register(h):
    """엔진을 주입해 모든 게이트를 등록한다."""
    for name in modules():
        importlib.import_module("." + name, __name__).register(h)
