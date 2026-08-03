---
description: 현재 프로젝트에 step-six-harness를 설치한다. CLAUDE.md에 앵커 1줄을 추가하고 .claude/harness/ 에 원칙·규칙·상태 파일을 만든다. "하네스 설치", "6단계 원칙 적용", "harness init" 요청 시 사용.
---

# 하네스 설치

이 프로젝트에 6단계 작업 원칙 하네스를 설치한다.

## 절차

1. 엔진 경로를 찾고 `init` 을 실행한다.

   ```sh
   ENGINE="$(ls -t "$HOME"/.claude/plugins/cache/*/step-six-harness/*/scripts/harness.py 2>/dev/null | head -1)"
   [ -n "$ENGINE" ] && python3 "$ENGINE" init
   ```

2. `init` 이 만드는 것을 사용자에게 보고한다.

   | 경로 | 커밋 | 내용 |
   | --- | --- | --- |
   | `CLAUDE.md` | ✅ | `@.claude/harness/POLICY.md` 앵커 **1줄만** 추가 (기존 내용 보존) |
   | `.claude/harness/POLICY.md` | ✅ | 6단계 원칙 요약 — 매 세션 로드됨 |
   | `.claude/harness/rationale.md` | ✅ | 상세 근거 — 필요할 때만 읽힘 |
   | `.claude/harness/stages.json` | ✅ | 단계 정의·폴더 규칙의 단일 출처 |
   | `.claude/harness/harness.db` | ❌ | SQLite 런타임 상태 — 해시 발급 + 현재 단계. gitignore 등록됨 |
   | `.claude/harness/bin/harness` | ❌ | 래퍼 (세션마다 자동 갱신) |

3. `git diff` 로 CLAUDE.md 변경이 1줄인지 확인시킨다.

4. 규칙을 프로젝트에 맞게 조정할지 물어본다. `stages.json` 에서 조정할 수 있는 것:
   - `folder_rules.dev_subdirs` — `.dev/` 하위 허용 폴더
   - `stages[].write` — 단계별 쓰기 허용 클래스
   - `stages[].stop_requires` — 턴 종료를 막을 조건
   - `stop_block_limits` — 조건별 턴 종료 차단 상한 (소진 시 사용자에게 노출됨)
   - `folder_rules.loop_prefixed_dirs` — 파일명에 루프 해시를 강제할 `.dev/` 하위 폴더
   - `path_classes` — 경로 → 클래스 매핑 (예: `src/` 를 별도 클래스로 분리)

5. 훅을 현재 세션에 적용하려면 `/reload-plugins` 가 필요함을 알린다.

## 주의

- 이미 설치된 프로젝트에서는 아무것도 덮어쓰지 않는다. `stages.json` 을 초기화하려면 파일을
  지우고 다시 실행하라고 안내한다.
- 설치 직후 단계는 `1/6 Scaffolding` 이다. 이 단계에서는 구조 변경이 허용된다.
