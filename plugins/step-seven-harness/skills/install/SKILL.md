---
description: 현재 프로젝트에 step-seven-harness를 설치한다. CLAUDE.md에 앵커 2줄을 추가하고 .claude/harness/ 에 원칙·규칙·상태 파일을 만든다. "하네스 설치", "작업 원칙 적용", "7단계 원칙 적용", "6단계 원칙 적용"(옛 이름), "harness init" 요청 시 사용.
---

# 하네스 설치

이 프로젝트에 작업 원칙 하네스(0 Selection → 1~6 → 두 갈래)를 설치한다.

## 절차

1. 엔진 경로를 찾고 `init` 을 실행한다.

   ```sh
   ENGINE="$(ls -t "$HOME"/.claude/plugins/cache/*/step-seven-harness/*/scripts/harness.py 2>/dev/null | head -1)"
   [ -n "$ENGINE" ] && python3 "$ENGINE" init
   ```

2. `init` 이 만드는 것을 사용자에게 보고한다.

   | 경로 | 커밋 | 내용 |
   | --- | --- | --- |
   | `CLAUDE.md` | ✅ | 앵커 **2줄만** 추가 — `@.claude/harness/POLICY.md`, `@.claude/harness/LEARNED.md` (기존 내용 보존) |
   | `.claude/harness/POLICY.md` | ✅ | 작업 원칙 요약 — 매 세션 로드됨. **사람이 소유한다** |
   | `.claude/harness/LEARNED.md` | ✅ | 승격된 규칙 — 매 세션 로드됨. **하네스가 생성한다** (`promote`/`tidy` 로만 바뀌고 쓰기 금지 경로다). 처음에는 비어 있다 |
   | `.claude/harness/rationale.md` | ✅ | 상세 근거 — 필요할 때만 읽힘 |
   | `.claude/harness/stages.json` | ✅ | 단계 정의·폴더 규칙의 단일 출처 |
   | `.claude/harness/harness.db` | ❌ | SQLite 런타임 상태 — 해시 발급 + 현재 단계. gitignore 등록됨 |
   | `.claude/harness/bin/harness` | ❌ | 래퍼 (세션마다 자동 갱신) |
   | `.claude/settings.json` | ✅ | 조회 명령(`status`·`recall`·`stats`·`advance`·`loop intent`·`promote`·`tidy`)을 권한 허용 목록에 추가. 동의가 필요한 명령(`skip`·`allow`·`approve-plan`·`auto-skip on`)은 **의도적으로 제외** |

3. `git diff` 로 CLAUDE.md 변경이 앵커 2줄뿐인지 확인시킨다.

4. 규칙을 프로젝트에 맞게 조정할지 물어본다. `stages.json` 에서 조정할 수 있는 것:
   - `folder_rules.dev_subdirs` — `.dev/` 하위 허용 폴더
   - `stages[].write` — 단계별 쓰기 허용 클래스
   - `stages[].stop_requires` — 턴 종료를 막을 조건
   - `stop_block_limits` — 조건별 턴 종료 차단 상한 (소진 시 사용자에게 노출됨)
   - `folder_rules.loop_prefixed_dirs` — 파일명에 루프 해시를 강제할 `.dev/` 하위 폴더
   - `path_classes` — 경로 → 클래스 매핑 (예: `src/` 를 별도 클래스로 분리)
   - `promotion.min_loops` — 몇 개 작업에서 반복되면 승격 결정을 요구할지 (기본 3)
   - `promotion.learned_max_lines` — `LEARNED.md` 줄 수 상한 (기본 20). 항상 로드되므로 상한이 있다
   - `tidy` — 정리 후보 판정 기준 (폴더 파일 수, 오래됨 기준 일수, 병합 후보 최소 개수)

5. **설치한 세션에서는 하네스가 반쪽만 동작한다는 것을 반드시 알린다.**

   | | 설치한 그 세션 | 새 세션 |
   | --- | --- | --- |
   | PreToolUse·Stop 훅 (차단) | ✅ `/reload-plugins` 후 동작 | ✅ |
   | SessionStart 주입 (현재 단계·제어 명령) | ❌ 이미 지난 이벤트 | ✅ |
   | `CLAUDE.md` → `POLICY.md` (작업 원칙) | ❌ **로드되지 않음** | ✅ |

   `CLAUDE.md` 는 세션 시작 시점에 로드된다. 설치 중에 추가한 앵커는 그 세션에서
   읽히지 않고, **`/reload-plugins` 는 플러그인만 다시 읽고 CLAUDE.md 는 다시 읽지 않는다.**
   그 상태로 작업하면 차단은 걸리는데 모델이 이유(작업 원칙)를 모르는 최악의 조합이 된다.

   그래서 안내는 이렇게 한다:

   > 설치가 끝났다. **새 세션을 시작해야 원칙 문서가 로드된다.** 지금 세션에서 계속하면
   > 차단은 걸리지만 모델이 작업 원칙을 알지 못한다.

6. 새 세션에서 다음을 확인하라고 안내한다.
   - `.claude/harness/bin/harness status` — 작업 해시·회차·단계가 보이는가
   - 응답 말머리에 `[Selection]` 이 자동으로 붙는가 (붙으면 SessionStart 주입이 들어온 것)
   - `/context` 의 Memory files 에 `CLAUDE.md` 가 있는가 (앵커가 로드된 것)

## 주의

- **플러그인을 업그레이드한 뒤에는 `init` 을 다시 실행한다.** 마이그레이션은 하지 않으므로
  DB 스키마가 오래되면 하네스가 비활성 상태가 되고, 세션 시작 시 그 사실을 알린다. `init` 은
  기존 파일을 덮어쓰지 않고 스키마만 갱신한다. 그래도 안 되면 `.claude/harness/harness.db` 를
  지우고 `init` 을 실행한다 — 진행 중 상태만 사라지고 `.dev/` 기록 파일은 남는다.
- 이미 설치된 프로젝트에서는 아무것도 덮어쓰지 않는다. `stages.json` 을 초기화하려면 파일을
  지우고 다시 실행하라고 안내한다.
- 설치 직후 단계는 **`1/7 Selection`** 이다 — 무엇을 할지부터 정하는 자리다.
  단계 이름과 번호를 **추측하지 마라.** `harness status` 가 그대로 알려준다.
