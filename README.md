# cc-marketplace

Claude Code 플러그인 마켓플레이스.

## 설치

```bash
/plugin marketplace add loveqoo/cc-marketplace
/plugin install step-seven-harness@cc-marketplace
/reload-plugins
```

> `step-six-harness` 를 쓰던 설치는 이름이 바뀌어 그대로 이어지지 않는다.
> `/plugin uninstall step-six-harness` 후 위 명령으로 다시 설치한다.
> 프로젝트의 `.claude/harness/` 상태는 플러그인 이름과 무관하므로 그대로 쓰인다.

## 플러그인

| 플러그인 | 설명 |
| --- | --- |
| [`step-seven-harness`](plugins/step-seven-harness) | 작업 하네스 — `Selection`(작업·완료 조건) → `Scaffolding` → `Context` → `Planning` → `Execution` → `Verification` → `Compounding` 을 훅으로 강제한다. 마지막 단계에서 **작업 종료**와 **다음 회차**로 갈라져 한 작업이 여러 회차를 돈다. 복리 장치 셋: 같은 도구 실패가 반복되면 **그 자리에서** 과거 기록을 주고, 여러 작업에서 반복된 항목은 **승격을 결정해야** 회차가 닫히며(보류도 결정 — 재발하면 되돌아온다), 승격된 규칙 문서는 **줄 수 상한**이 있어 무한히 자라지 않는다. 완료 조건·폴더 규칙 가드, 스킵과 작업 종료(`loop new`)는 사유와 사용자 승인 요구, 승인을 면제해도 기록은 면제하지 않음. 하네스 자신은 Write/Edit·Bash 양쪽에서 잠긴다. `metrics` 가 **승격 종류별 재발률**과 **마찰 추세를 우회 추세와 나란히** 보여준다 — 점수는 만들지 않는다(합치면 그 하나를 최적화하게 된다). |

## 무엇을 보고 만들었나

`step-seven-harness` 의 "복리" 장치는 직관으로 만든 것이 아니다. 먼저 **다른 곳에서는 AI와
복리를 어떻게 올렸는지** 조사하고, 거기서 공통으로 나온 것만 골라 넣었다.

| 사례 | 무엇을 말하나 | 하네스의 어느 장치가 됐나 |
| --- | --- | --- |
| [ExpeL: LLM Agents Are Experiential Learners](https://arxiv.org/abs/2308.10144) (2023) | 통찰 저장소를 `ADD`/`EDIT`/`UPVOTE`/`DOWNVOTE` 로 관리하고 **중요도가 0이 되면 삭제**한다. 성공·실패 **짝**에서 통찰을 뽑는다 | 승격에 성숙도를 두고, 재발하면 결정을 무효화해 되돌리는 것 |
| [CODESKILL: Learning Self-Evolving Skills for Coding Agents](https://arxiv.org/abs/2605.25430) (2026) | skill bank 를 `ADD`/`MERGE`/**`DROP`** 으로 관리해 **크기가 수렴**한다(무한히 자라지 않는다). 그리고 **event-driven 회수** — 에러 메시지를 키로 실패한 그 순간 주입하는 것이 작업 시작에 한 번 밀어넣기보다 효과가 컸다 | ① `LEARNED.md` 의 줄 수 상한과 `harness tidy` ② **실패 지점 주입** (같은 도구 실패 2번째부터 그 자리에서 과거 기록을 준다) |
| [cass_memory_system](https://github.com/Dicklesworthstone/cass_memory_system) | 세션로그 → 다이어리 → playbook 3층. 규칙마다 `candidate→established→proven` 성숙도와 반감기를 두고, **큐레이션 단계에 LLM을 넣지 않는다** (자기 판단을 다시 판단하면 등급이 표류한다) | 성숙도를 **결정론적으로** 계산하는 것 — "같은 이벤트가 또 걸리는가"만 본다 |
| [Cursor Rules: globs·description](https://techsy.io/en/blog/cursor-rules-guide) | 규칙의 **내용보다 발동 조건**이 레버다. 언제 붙을지가 정확해야 제때 쓰인다 | 실패한 순간에 주입하는 설계. 반대로 `recall` 을 pull 전용으로 둔 것은 **의도적 절충**이다 (근거 문서 참고) |
| [Configuration Smells in AGENTS.md Files](https://arxiv.org/abs/2606.15828) (레포 100개 조사) | 가장 흔한 냄새가 **lint leakage 62%** — 훅이나 린터로 막을 수 있는 것을 산문으로 적어둔 것. 두 번째가 context bloat 42% | **승격 결정 강제**의 직접적 근거. 하네스 자신이 훅이므로 반복되는 실수를 산문이 아니라 기계로 옮길 수 있다 |
| [The AGENTS.md Bloat Problem](https://codex.danielvaughan.com/2026/03/27/agents-md-bloat-problem/) | 실수 → 규칙 한 줄 추가 → 또 실수 → 또 한 줄. 파일이 터지고 초반 지시가 묻힌다 | Scaffolding 을 "**줄이는**" 단계로 정의한 것, `LEARNED.md` 예산 |
| [State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026) | 에이전트 메모리 접근법의 현황 정리 | 배경 조사 |

**공통으로 나온 결론 셋**은 그대로 하네스의 설계 원칙이 됐다.

1. **회수는 자동으로, 그리고 필요한 순간에.** 부르기로 마음먹어야 하는 저장소는 안 쓰인다.
2. **저장소는 수렴해야 한다.** 쌓이기만 하는 기록은 마찰이 이득을 넘는 지점이 온다.
3. **기계화할 수 있으면 산문으로 적지 마라.** 이게 62%가 걸린 함정이다.

각 장치를 왜 그렇게 만들었고 **무엇을 측정할 수 없는지**는
[`rationale.md`](plugins/step-seven-harness/templates/rationale.md) 에 있다.

## 구조

```
.claude-plugin/marketplace.json   # 카탈로그 (플러그인 목록)
plugins/<plugin-name>/
  .claude-plugin/plugin.json      # 플러그인 매니페스트
  skills/<skill>/SKILL.md         # 스킬
  agents/<agent>.md               # 서브에이전트
  hooks/hooks.json                # 훅
```

## 플러그인 추가하기

1. `plugins/<plugin-name>/` 디렉터리를 만들고 `.claude-plugin/plugin.json`을 작성한다.

   ```json
   {
     "name": "<plugin-name>",
     "description": "한 줄 설명",
     "version": "1.0.0",
     "author": { "name": "gunam" }
   }
   ```

2. `.claude-plugin/marketplace.json`의 `plugins` 배열에 엔트리를 추가한다.

   ```json
   {
     "name": "<plugin-name>",
     "source": "./plugins/<plugin-name>",
     "description": "한 줄 설명"
   }
   ```

3. 검증한다.

   ```bash
   claude plugin validate .                      # 마켓플레이스 + 각 엔트리
   claude plugin validate ./plugins/<plugin-name> # 스킬/에이전트/훅 프론트매터까지
   ```

## 규칙

- **버전**: `plugin.json`의 `version`이 최종 권위다. 릴리스마다 반드시 bump할 것 —
  올리지 않으면 커밋을 푸시해도 기존 사용자에게 업데이트가 전달되지 않는다.
  `marketplace.json` 엔트리에는 `version`을 중복으로 쓰지 않는다 (`plugin.json`이 조용히 이김).
- **name은 영구 식별자**: 사용자 설정(`enabledPlugins`)에 그대로 박히므로 바꾸면 기존 설치가 깨진다.
  UI 표시명만 바꾸려면 `displayName`을 쓰고, 불가피하게 바꿀 땐 `marketplace.json`에
  `renames` 맵을 추가한다 (append-only — 과거 엔트리를 지우지 말 것).
- **경로**: 설치 시 플러그인 디렉터리가 캐시로 복사되므로 `../` 로 디렉터리 밖을 참조할 수 없다.
  훅/MCP 설정에서는 `${CLAUDE_PLUGIN_ROOT}`, 업데이트를 넘어 살아남아야 하는 상태는
  `${CLAUDE_PLUGIN_DATA}`를 쓴다.

## 로컬 테스트

```bash
cd ..
claude
> /plugin marketplace add ./cc-marketplace
> /plugin install <plugin-name>@cc-marketplace
> /reload-plugins
```
