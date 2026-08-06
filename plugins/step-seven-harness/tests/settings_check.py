"""`.claude/settings.json` 을 안전하게 쓰는지 검사한다.

  usage: python3 settings_check.py [repo-root]

왜 있나: 이 파일은 Claude Code 도 쓴다(`enabledPlugins` 등). 하네스가 읽고-고치고-
쓰는 사이에 저쪽이 쓰면 우리가 그걸 덮어 없앤다 — 플러그인 활성화가 통째로
사라지는 방향이다. 남의 설정을 잃는 것은 어떤 기능보다 나쁘므로 따로 검사한다.
"""
import builtins
import json
import os
import shutil
import sys
import tempfile

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                       else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "plugins/step-seven-harness/scripts"))
import harness as h  # noqa: E402

FAILS = []


def ck(label, cond, detail=""):
    print("  %-46s %s%s" % (label, "ok" if cond else "FAIL",
                            "" if cond else "  " + str(detail)))
    if not cond:
        FAILS.append(label)


def fixture(content=None):
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, ".claude"))
    p = os.path.join(root, ".claude", "settings.json")
    if content is not None:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
    return root, p


def load(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


# 1) 남의 키를 보존한다
root, p = fixture(json.dumps({
    "enabledPlugins": {"step-seven-harness@cc-marketplace": True},
    "model": "opus",
    "permissions": {"allow": ["Bash(ls)"], "deny": ["Bash(rm -rf /)"]},
}))
rc = h.ensure_permissions(root)
d = load(p)
ck("추가한 규칙 수를 돌려준다", rc > 0, rc)
ck("enabledPlugins 를 보존한다", "enabledPlugins" in d)
ck("무관한 최상위 키를 보존한다", d.get("model") == "opus")
ck("기존 allow 항목을 보존한다", "Bash(ls)" in d["permissions"]["allow"])
ck("permissions 의 다른 키를 보존한다", "deny" in d["permissions"])
shutil.rmtree(root)

# 2) 읽고-쓰는 사이에 남이 쓰면 그 내용 위에 병합한다 (덮지 않는다)
root, p = fixture(json.dumps({"permissions": {"allow": []}}))
_real_open = builtins.open
state = {"raced": False}


def _external_write():
    """Claude Code 가 이 파일을 쓴 것처럼 만든다."""
    d = json.load(_real_open(p, encoding="utf-8"))
    d["enabledPlugins"] = {"step-seven-harness@cc-marketplace": True}
    with _real_open(p, "w", encoding="utf-8") as w:
        json.dump(d, w)


class _RaceOnRead(object):
    """`read()` 가 **끝난 뒤** 한 번만 외부 쓰기를 일으킨다.

    예전에는 open 직후에 썼다. 그러면 호출자가 아직 읽지 않았으므로 **첫 스냅샷이
    이미 새 내용**이고, 비교-교환의 불일치가 발생하지 않아 재시도 경로에 도달하지
    못했다 — "경쟁 쓰기가 있어도 성공한다"가 경쟁 없이 통과했다. 적대적 리뷰가
    지적했고, 실제로 그랬다.
    """

    def __init__(self, fh):
        self._fh = fh

    def read(self, *a, **k):
        out = self._fh.read(*a, **k)
        if not state["raced"]:
            state["raced"] = True
            _external_write()
        return out

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        self._fh.__enter__()
        return self

    def __exit__(self, *exc):
        return self._fh.__exit__(*exc)


def racing_open(path, *a, **k):
    fh = _real_open(path, *a, **k)
    if path == p and not state["raced"] and (not a or "r" in str(a[0])):
        return _RaceOnRead(fh)
    return fh


builtins.open = racing_open
try:
    rc = h.ensure_permissions(root)
finally:
    builtins.open = _real_open
d = load(p)
ck("경쟁이 실제로 일어났다", state["raced"])
ck("경쟁 쓰기가 있어도 성공한다", rc > 0, rc)
ck("남이 쓴 enabledPlugins 가 살아남는다", "enabledPlugins" in d)
ck("우리 권한도 함께 들어간다", len(d["permissions"]["allow"]) > 0)
shutil.rmtree(root)

# 3) 손상된 설정은 건드리지 않는다
for label, body in (("깨진 JSON", "{not json"),
                    ("최상위가 배열", "[1,2,3]"),
                    ("permissions 가 배열", '{"permissions": []}')):
    root, p = fixture(body)
    before = _real_open(p, encoding="utf-8").read()
    rc = h.ensure_permissions(root)
    after = _real_open(p, encoding="utf-8").read()
    ck("%s: -1 을 돌려준다" % label, rc == -1, rc)
    ck("%s: 파일을 바꾸지 않는다" % label, before == after)
    shutil.rmtree(root)

# 4) 더할 것이 없으면 파일을 건드리지 않는다
#
# `before == after` 만 보면 **같은 내용을 다시 써도 통과한다** — 라벨은 "쓰지 않는다"
# 인데 검사는 "내용이 같다"였다. 적대적 리뷰가 지적했다. 쓰기 모드로 열리는지 센다.
root, p = fixture(json.dumps({"permissions": {"allow": list(h.SAFE_PERMS)}}))
before = _real_open(p, encoding="utf-8").read()
writes = {"n": 0}


def counting_open(path, *a, **k):
    mode = str(a[0]) if a else str(k.get("mode", "r"))
    if path == p and any(c in mode for c in "wax+"):
        writes["n"] += 1
    return _real_open(path, *a, **k)


builtins.open = counting_open
try:
    rc = h.ensure_permissions(root)
finally:
    builtins.open = _real_open
after = _real_open(p, encoding="utf-8").read()
ck("이미 다 있으면 0 을 돌려준다", rc == 0, rc)
ck("이미 다 있으면 내용이 그대로다", before == after)
ck("이미 다 있으면 쓰기 모드로 열지 않는다", writes["n"] == 0, writes["n"])
shutil.rmtree(root)

# 5) 계속 경쟁하면 포기한다 (남의 변경을 덮느니 포기가 낫다)
root, p = fixture(json.dumps({"permissions": {"allow": []}}))
counter = {"n": 0}


def always_racing_open(path, *a, **k):
    fh = _real_open(path, *a, **k)
    if path == p and (not a or "r" in str(a[0])):
        counter["n"] += 1
        with _real_open(p, "w", encoding="utf-8") as w:
            json.dump({"permissions": {"allow": []}, "n": counter["n"]}, w)
    return fh


builtins.open = always_racing_open
try:
    rc = h.ensure_permissions(root, tries=2)
finally:
    builtins.open = _real_open
ck("계속 경쟁하면 -2 로 포기한다", rc == -2, rc)
shutil.rmtree(root)

print("\n실패 %d개: %s" % (len(FAILS), FAILS or "없음"))
sys.exit(1 if FAILS else 0)
