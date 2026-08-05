"""메시지 카탈로그를 **코드에서 생성**한다.

  usage: python3 gen_catalog.py [repo-root]        # templates/messages.ko.json 갱신

손으로 만들면 코드와 어긋나고, 어긋난 것을 아무도 모른다. 원문이 키이므로 생성이
가능하고, msg_check.py 가 코드와 카탈로그가 맞는지 검사한다. 추출 기준은 _msgs.py
한 곳에 있다 — 두 곳에서 계산하면 어긋난다(실제로 그랬다).

`messages.ko.json` 은 값이 키와 같다 — 번역할 목록 그 자체이고, 번역자는 이 파일을
복사해 값만 바꾼다. 원문 언어에서는 조회조차 하지 않으므로(load_messages) 런타임에
쓰이지 않는다. **번역의 명세**다.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _msgs  # noqa: E402

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                       else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "..", ".."))
PLUGIN = os.path.join(REPO, "plugins/step-seven-harness")
OUT = os.path.join(PLUGIN, "templates/messages.ko.json")

keys = sorted(
    {s for s, _, _, _ in _msgs.engine_strings(os.path.join(PLUGIN, "scripts/harness.py"))}
    | set(_msgs.config_strings(os.path.join(PLUGIN, "templates/stages.json"))))
with io.open(OUT, "w", encoding="utf-8") as fh:
    json.dump({k: k for k in keys}, fh, ensure_ascii=False, indent=2, sort_keys=True)
    fh.write("\n")
print("카탈로그 %d개 → %s" % (len(keys), os.path.relpath(OUT, REPO)))
