# 셸 명령이 무엇을 쓰는지는 알아낼 수 없다 (조사, 2026-08-09)

**이 문서를 꺼내야 할 때** — "Bash 명령을 잘못 막았다"는 보고가 또 왔을 때.
고치기 전에 여기부터 읽는다. 지금까지 19번 고쳤고, 매번 다음이 있었다.

## 검색 키

다음에 이 문제는 이런 모습으로 온다. 아래 문자열 중 하나라도 보이면 이 문서다.

```
과잉 차단 · 오탐 · false positive · "이 명령이 ... 를 바꾼다"
stage_write · bash_writes · _target · sure=False · BASH_MUTATORS
find -name · git diff A..B · 2>&1 · 2>/dev/null · cp -t · sed -i 다중 파일
"신규 최상위 폴더" · "'source' 클래스 경로에 쓸 수 없다"
undecidable · 결정 불가능 · 샌드박스 · seatbelt · landlock · denyWrite
```

## 우리가 풀려던 문제

훅이 Bash 명령을 실행 **전에** 가로채서 답해야 했다:

> 이 명령이 지금 단계에서 금지된 경로에 쓰는가?

답하려면 임의의 셸 문자열에서 **쓰이는 경로 집합**을 알아내야 한다.

## 결론: 그 문제는 답이 없다

**정적으로 결정 불가능(undecidable)하다.** `eval`, `$(...)`, 변수 확장 때문에
명령이 실행 중에 조립된다. Oil Shell 저자가 정리했다 — 셸은 **파싱조차**
정적으로 안 되고(배열 첨자 등), 모든 주요 셸이 파싱의 일부를 실행 시점으로
미룬다. 파싱이 안 되는데 그 위의 부작용 분석은 더 안 된다.

- <https://www.oilshell.org/blog/2016/10/20.html> — "Parsing Bash is Undecidable"
- <https://sigops.org/s/conferences/hotos/2025/papers/hotos25-364.pdf> — Static Analysis for Unix Shell Programs (HotOS '25)

**즉 19번의 실패는 실력 문제가 아니었다.** 답이 없는 문제를 풀고 있었다.

근사가 유한하지 않다는 실측 증거도 남긴다. 같은 도구도 **플래그로 규약이
뒤집힌다** — 아래 셋은 이 조사 시점에 하네스가 틀리게 판정하고 있었다.

| 명령 | 실제로 쓰는 곳 | 하네스가 본 것 |
| --- | --- | --- |
| `cp a b` | `b` (마지막) | `b` ✅ |
| `cp -t dir a b` | `dir` (첫째) | **아무것도 못 봄** |
| `sed -i s/x/y/ a b` | 둘 다 | `b` 만 |
| `install -d x y` | 둘 다 | `y` 만 |

## 업계는 어떻게 하나 — 셋 다 **파싱이 아니다**

| 방식 | 누가 | 무엇으로 |
| --- | --- | --- |
| **OS 격리** | OpenAI Codex, Claude Code 샌드박스 | macOS Seatbelt · Linux Landlock+bubblewrap+seccomp · Windows 제한 토큰 |
| **허용목록 + 물어보기** | Claude Code 기본, Cursor | 명령 **접두사** 매칭. 무엇을 쓰는지 보지 않는다 |
| **컨테이너/VM** | Devin 류 | 통째로 격리 |

**아무도 "이 명령이 어느 파일을 쓰는가"를 계산하지 않는다.** 우리만 했다.

그리고 접두사 매칭의 한계는 공개적으로 알려져 있다 — `&&` 로 이어 붙이면
뚫린다(claude-code#28784). Anthropic 자신의 권고가 *"민감한 작업은 PreToolUse
훅으로 검증하거나 **OS/컨테이너 수준에서 제한하라**"* 다.

Codex 에도 우리 "바닥값"과 같은 개념이 있다 — *"특정 경로는 항상 읽기 전용,
재귀적으로 강제(예: `.git/hooks/`)"*. 다만 **커널로** 강제한다.

- <https://deepwiki.com/openai/codex/5.6-sandboxing-implementation>
- <https://simonwillison.net/2025/Nov/9/codex-sandbox-investigation/>
- <https://github.com/anthropics/claude-code/issues/28784>

## Claude Code 가 이미 주는 것

```json
"sandbox": { "filesystem": { "denyWrite": ["<경로>"] } }
```

- 모든 Bash 명령과 **그 자식 프로세스**에 OS 가 강제한다
- `python3 -c` 로 쪼개 쓰든 `eval` 로 조립하든 상관없다 — **커널은 철자를 안 본다**
- 1차 현장 보고 §5("쪼개면 새어 나간다")가 구조적으로 사라진다

한계: 옵트인이고, macOS·Linux·WSL2 만이며(네이티브 Windows 불가),
`dangerouslyDisableSandbox` 탈출구가 있다.

- <https://code.claude.com/docs/en/sandboxing>
- <https://code.claude.com/docs/en/permissions>

## 그래서 내린 결정 (2026-08-09)

| 층 | 결정 | 근거 |
| --- | --- | --- |
| **Bash 단계 규칙** | 차단 → **기록**. 추측이 틀려도 아무도 막지 않는다 | 답이 없는 문제에 정확성을 요구하지 않는다. 설계: `.dev/plan/bash-stage-rules-to-record.md` |
| **바닥값(엔진·DB·래퍼)** | 문자열 검사 **유지** + 샌드박스 `denyWrite` 위임을 **별도 회차**로 | 대상이 3개 글롭으로 고정이라 유지비가 거의 없다. 커널 강제는 그 위에 얹는다 |
| **Write/Edit 단계 규칙** | 그대로 차단 | 도구가 경로를 그대로 준다. **추측이 없고 버그도 없었다** |

## 다음에 같은 보고가 오면

1. **이 문서를 먼저 읽는다.**
2. 그 보고가 **Bash 경로 추측**에 관한 것이면 — 고치지 않는다. 기록이
   지저분한 것은 버그가 아니다.
3. **Write/Edit** 또는 **바닥값**에 관한 것이면 — 그건 진짜 버그다. 고친다.
4. 이 구분이 안 서면 그것부터 답한다. 답하기 전에 코드를 고치지 않는다.

## 왜 이걸 여섯 번의 적대적 리뷰가 못 잡았나

리뷰 코퍼스가 **뚫는 명령**으로만 이루어져 있었다. 오탐은 **일하는 명령**에서만
나오고, 일하는 명령은 실제로 그 명령을 친 사람만 안다. 그래서 현장 보고
2회가 리뷰 6회보다 많이 찾았다.

그리고 더 근본적으로 — 우리는 *"같은 실수가 3번 반복되면 멈추고 구조를
결정하라"* 는 규칙을 가진 도구를 만들어 놓고, **그 규칙을 우리 자신에게 겨누지
않았다.** `_target()` 한 줄에 반창고가 네 번 붙는 동안 아무도 세지 않았다.
