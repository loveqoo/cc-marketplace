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
