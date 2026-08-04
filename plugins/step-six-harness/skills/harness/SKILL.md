---
description: 작업 하네스의 현재 단계를 확인하고 제어한다 — 단계 진행/스킵, 작업 종료·회차 반복, 쓰기 예외, 계획 승인, 작업 해시 확인. **하네스가 무언가를 차단했을 때 항상 이 스킬을 먼저 본다.** 트리거 — "지금 몇 단계", "하네스 상태", "단계 확인", "다음 단계로", "advance", "이 단계 건너뛰자", "스킵하자", "작업 끝났다", "다음 회차", "docs 수정해야 한다", "왜 막혔어", "차단됐다", "예외 등록", "계획 승인해줘", "작업 해시 뭐야", "회차 뭐야", "파일명 접두사", "harness help", "이 단계에서 뭘 할 수 있어".
---

# 하네스 제어

제어 명령은 프로젝트 안의 래퍼로 실행한다. 없으면 `/step-six-harness:install` 이 먼저다.

```sh
.claude/harness/bin/harness status
```

## 단계 구조

```
0 Selection → 1 Scaffolding → 2 Context → 3 Planning
            → 4 Execution → 5 Verification → 6 Compounding
                                 ├─ 작업 끝    → 0 Selection
                                 └─ 회차 계속  → 1 Scaffolding
```

한 작업이 1~6단계를 **여러 회차** 돌 수 있다. 작업 해시는 그대로이고 회차만 올라간다.

## 명령

| 명령 | 용도 | 사용자 승인 |
| --- | --- | --- |
| `status` | 현재 작업·회차·단계, 종료 조건 충족 여부, 증거, 스킵·예외 기록 | |
| `advance` | 다음 단계로. 종료 조건이 남아 있으면 **거부되고 무엇이 남았는지 알려준다** | |
| `advance --done` | (Compounding 에서만) 작업 종료 → Selection | |
| `advance --cycle` | (Compounding 에서만) 다음 회차 → Scaffolding, 같은 작업 유지 | |
| `skip <stage\|+N\|until:<stage>> --reason "..."` | 단계 건너뛰기 | ✅ 다이얼로그 |
| `allow <glob> --reason "..." [--uses N]` | 쓰기 금지 경로에 예외 등록 (`docs/` 등) | ✅ 다이얼로그 |
| `approve-plan <file>` | 계획에 대한 사람의 승인 기록 | ✅ 다이얼로그 |
| `recall [키워드\|경로] [--kind K] [--rule R]` | 과거 차단·실패·재편집 기록과 관련 회고 파일을 찾는다. **Context 단계의 주 도구** | |
| `stats [--loop]` | 누적 수치. 어떤 규칙에 몇 번 걸렸는지, 무엇이 여러 작업에 걸쳐 반복되는지 | |
| `loop` | 현재 작업 해시·브랜치 | |
| `loop intent "<작업>"` | 이번 작업을 기록 (Selection 의 종료 조건) | |
| `loop new [--intent "..."]` | 작업을 닫고 새 해시로 시작 | |
| `loop adopt <hash> --reason "..."` | DB를 잃었을 때 기존 해시로 재연결 | ✅ 다이얼로그 |
| `auto-skip on --reason "..." [--uses N] [--scope loop\|project]` | 스킵 동의 다이얼로그를 끄고 자동 승인. 범위를 좁히는 것을 권한다 | ✅ 다이얼로그 (켜는 것 자체는 승인 필요) |
| `auto-skip off` / `auto-skip status` | 자동 승인 해제 / 현재 상태 | 끄기는 승인 불필요 |

`skip` 은 **현재 단계부터 대상 단계까지**를 건너뛴 것으로 기록한다. 현재 단계를 정상적으로
끝낸 뒤 다음 단계만 건너뛰려면 `advance` 로 넘어간 다음 `skip` 하라.

**Selection 과 Compounding 은 건너뛸 수 없다.** 작업은 반드시 선정하고, 중단하더라도 회고는
남긴다.

**스킵은 승인을 면제하지만 기록을 면제하지 않는다.** Planning 을 건너뛰려면 계획 파일이
`.dev/plan/` 에 있어야 한다 — 없으면 거부되고 무엇을 남겨야 하는지 알려준다. 무인 실행에서도
회차마다 계획이 축적되어야 다음 회차의 `recall` 과 Compounding 이 쓸 재료가 된다.

## Selection 단계에서 (0번)

작업이 불분명하면 후보를 나열해 사용자와 합의한다(선택 — 이미 명확하면 건너뛴다). 정해지면
기록한다.

```sh
.claude/harness/bin/harness loop intent "결제 모듈 리팩터링"
```

기록하지 않으면 이 단계를 끝낼 수 없다. 이 기록이 Context 의 `recall` 기준이 된다.

## Context 단계에서 (2번)

`recall` 은 **당기는 도구다.** 하네스가 과거 실수를 밀어주지 않는 이유는, 이번 작업과 무관한
실수까지 컨텍스트를 먹기 때문이다. Selection 에서 정한 작업을 근거로 무엇이 관련 있는지 직접
판단해서 조회하라.

```sh
.claude/harness/bin/harness recall                # 인자 없으면 이번 작업에서 키워드를 뽑는다
.claude/harness/bin/harness recall src/api        # 이번에 만질 경로
.claude/harness/bin/harness recall "npm test"     # 이번에 쓸 명령
```

- `← 여러 작업에서 반복` 표시가 붙은 항목은 **같은 실수를 되풀이하고 있다는 뜻이다.**
- `재편집이 많은 파일` 은 구조 문제 신호다. 이번 작업이 그 파일을 건드린다면 Scaffolding 에서
  구조를 고칠지 먼저 판단하라.
- 회고 파일 목록은 **경로만** 준다. 관련 있어 보이는 것만 골라 읽어라 — 전부 읽지 마라.

## Compounding 단계에서 (6번) — 두 갈래

회고를 쓴 뒤 **스스로 판단해** 하나를 고른다. 하네스는 어느 쪽인지 판정하지 않는다.

```sh
.claude/harness/bin/harness advance --done     # 작업이 끝났다 → 0번, 새 작업 선정
.claude/harness/bin/harness advance --cycle    # 후속 회차가 남았다 → 1번, 같은 작업 유지
```

맨손 `advance` 는 두 선택지를 보여주고 멈춘다. 회차가 바뀌면 계획·검증 증거가 초기화되므로
2회차 Planning 은 새 계획과 새 승인이 필요하다.

## 회차 중간에 선행 작업이 드러났을 때

되돌아가지 않는다. 단계는 항상 앞으로만 간다. Planning 에서 "구조를 먼저 바꿔야 한다"가
드러나면:

```sh
# 1. 사람과 합의해 Compounding 까지 이동 (승인 다이얼로그가 뜬다)
.claude/harness/bin/harness skip until:compounding --reason "Planning에서 구조 선행 필요 발견"

# 2. 중단 사유를 회고로 남긴다 — 이게 회차를 닫는 조건이다
#    .dev/retrospect/<작업해시>-<회차>-abort-structure-first.md

# 3. 같은 작업의 다음 회차를 Scaffolding 부터 시작
.claude/harness/bin/harness advance --cycle
```

중첩 루프를 만들지 않는 이유: Planning 이 구조 결함을 드러낸 것은 **새 작업이 아니라
Scaffolding 이 불완전했다는 증거**다. 자식 루프로 감싸면 그 신호가 별개 작업처럼 보여 사라진다.
회고 파일이 남으면 그 사실이 git 에 기록되고, `harness stats` 의 스킵 집계에 반복 패턴으로
드러난다.

## 차단당했을 때

차단 이유는 훅이 그대로 알려준다. 대응 순서:

1. **규칙이 맞다** → 하라는 것을 한다. 예: Planning 에서 소스를 고치려다 막혔으면 계획을 먼저
   `.dev/plan/` 에 쓰고 `approve-plan` 을 받는다.
2. **이 작업에는 그 단계가 불필요하다** → 사유를 만들어 `skip` 을 요청한다. 사유는 사용자에게
   그대로 노출되니 납득 가능한 문장으로 쓴다. 무단 우회를 시도하지 않는다.
3. **규칙 자체가 틀렸다** → 우회하지 말고 Compounding 에서 `stages.json` 수정을 논의한다.

## 규칙

- 모든 답변 말머리에 현재 단계 이름을 붙인다 — `[Selection]` `[Scaffolding]` `[Context]`
  `[Planning]` `[Execution]` `[Verification]` `[Compounding]`. 없으면 턴 종료가 차단된다.
  대소문자는 무시된다(`[compounding]` 도 통과). **번호는 붙이지 않는다** — `[6/7 Compounding]`
  은 차단된다. 번호는 이름에서 도출되는 중복 정보다.
- `.dev/` 산출물 파일명은 `<작업해시>-<회차>-` 로 시작해야 한다: `.dev/plan/260804-a3f9c1-1-이름.md`.
  `scratch/` 만 예외다. 접두사는 `status` 로 확인한다.
- 상태 DB(`.claude/harness/harness.db`)는 커밋하지 않는 런타임 전용이고, 작업이 닫히면 그
  작업의 행은 버려진다. 영구 기록은 파일명에 해시가 박힌 md 파일들이다.
- 스킵 사유는 반드시 노출한다. 사유 없는 `skip` 은 거부된다.
- `bypassPermissions` 모드에서는 동의 다이얼로그가 뜨지 않으므로 `skip`/`allow`/`approve-plan`
  이 모두 거부된다. 권한 모드를 낮추고 다시 시도한다.
- 계획과 검증 결과를 **다른 모델이나 서브에이전트에게 적대적으로 검토받는 것을 고려한다**
  (권고 — 강제하지 않는다). 사용자가 원하면 여러 모델에 돌린다.
- 상세 근거는 `.claude/harness/rationale.md` 에 있다. 필요할 때 읽는다.
