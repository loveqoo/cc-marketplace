---
description: 단계 스킵의 자동 승인을 끄고 사용자 동의 다이얼로그를 복원한다. "스킵 자동 승인 꺼", "다시 동의받게 해줘", "auto skip off" 요청 시 사용. 게이트 복원이므로 승인 없이 즉시 적용된다.
---

# 스킵 자동 승인 끄기

```sh
.claude/harness/bin/harness auto-skip off
```

게이트를 복원하는 방향이라 사용자 승인이 필요하지 않다. 즉시 적용되고, 이후 모든 스킵은
다시 사용자 동의 다이얼로그를 거친다.

`--uses N` 이나 `--scope loop` 로 켜져 있었다면 조건이 다 되면 **자동으로 만료**되므로
이 명령이 필요하지 않을 수도 있다. 현재 상태를 먼저 확인하라:
`.claude/harness/bin/harness auto-skip status`

## 절차

1. 명령을 실행한다.
2. 상태를 확인시킨다: `.claude/harness/bin/harness auto-skip status`
3. 켜져 있던 동안 자동 승인으로 처리된 스킵이 있으면 알린다 —
   `.claude/harness/bin/harness status` 의 스킵 목록에서 `승인: auto` 로 표시된 항목들이다.
   Compounding 단계에서 회고에 옮겨 적을 대상이다.

## 주의

자동 승인이 꺼져 있는 것이 기본 상태다. 스킵이 자꾸 막혀서 불편하다면 자동 승인을 켜기 전에
`stages.json` 의 단계 정의가 이 프로젝트에 맞는지 먼저 검토하는 게 낫다 — 게이트를 끄는 것보다
규칙을 고치는 것이 옳은 해결이고, 그게 Compounding 단계에서 할 일이다.
