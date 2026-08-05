# cc-marketplace

**Claude Code 플러그인 마켓플레이스입니다.**

```bash
/plugin marketplace add loveqoo/cc-marketplace
```

등록하면 아래 플러그인을 설치할 수 있습니다.

## 플러그인

### [`step-seven-harness`](plugins/step-seven-harness) — 작업 하네스

AI와 함께 일할 때 **복리가 쌓이도록** 작업 절차를 훅으로 강제합니다.
`Selection` → `Scaffolding` → `Context` → `Planning` → `Execution` → `Verification` →
`Compounding` 일곱 단계를 순서대로 밟게 하고, 마지막 단계에서 **작업 종료**와
**다음 회차**로 갈라져 한 작업이 여러 회차를 돕니다.

같은 실수가 반복되면 그 자리에서 과거 기록을 알려주고, 여러 작업에서 반복되면
승격을 결정해야 회차가 닫힙니다. 승격된 규칙 문서에는 줄 수 상한이 있어 무한히
자라지 않습니다.

절차 강제와 증거 판정은 CLI·파일시스템 층에 있어 **훅이 없는 도구(Codex, opencode)에서도
동작합니다.** 즉시 차단과 말머리만 Claude Code 전용입니다.

```bash
/plugin install step-seven-harness@cc-marketplace
```

> `step-six-harness` 로 설치하셨던 분은 그대로 쓰시면 됩니다 — 이름 변경 매핑(`renames`)이
> 있어 자동으로 이어집니다. `/plugin update` 로 최신 버전을 받으십시오.

📖 **[사용법·설계 근거는 플러그인 README](plugins/step-seven-harness/README.md)** 를 보십시오 —
누구에게 맞는지, 언제 끼어드는지, 무엇이 어디에 저장되는지, 막혔을 때 무엇을 하는지,
그리고 무엇을 조사해 만들었는지가 적혀 있습니다.

---

## 이 저장소에 플러그인을 추가하려면

### 구조

```
.claude-plugin/marketplace.json   # 카탈로그 (플러그인 목록)
plugins/<plugin-name>/
  .claude-plugin/plugin.json      # 플러그인 매니페스트
  README.md                       # 그 플러그인의 사용 문서
  skills/<skill>/SKILL.md         # 스킬
  agents/<agent>.md               # 서브에이전트
  hooks/hooks.json                # 훅
```

문서는 두 층으로 나눕니다. **이 README 는 카탈로그**이고 — 무엇이 있고 어디로 가면
되는지까지 — **사용 문서는 각 플러그인의 `README.md`** 가 갖습니다. 독자가 다릅니다.

### 절차

1. `plugins/<plugin-name>/.claude-plugin/plugin.json` 을 만듭니다.

   ```json
   {
     "name": "<plugin-name>",
     "description": "한 줄 설명",
     "version": "1.0.0",
     "author": { "name": "gunam" }
   }
   ```

2. `.claude-plugin/marketplace.json` 의 `plugins` 배열에 엔트리를 추가합니다.

   ```json
   {
     "name": "<plugin-name>",
     "source": "./plugins/<plugin-name>",
     "description": "한 줄 설명"
   }
   ```

3. `plugins/<plugin-name>/README.md` 에 사용 문서를 씁니다. 그리고 위 플러그인 절에
   짧은 소개와 링크를 추가합니다.

4. 검증합니다.

   ```bash
   claude plugin validate .                       # 마켓플레이스 + 각 엔트리
   claude plugin validate ./plugins/<plugin-name>  # 스킬/에이전트/훅 프론트매터까지
   ```

### 주의할 규칙

- **버전**: `plugin.json` 의 `version` 이 최종 권위입니다. 릴리스마다 반드시 올리십시오 —
  올리지 않으면 푸시해도 기존 사용자에게 업데이트가 전달되지 않습니다.
  `marketplace.json` 엔트리에 `version` 을 중복으로 쓰지 마십시오 (`plugin.json` 이
  조용히 이깁니다).
- **`name` 은 영구 식별자**: 사용자 설정(`enabledPlugins`)에 그대로 박히므로 바꾸면 기존
  설치가 끊깁니다. 표시명만 바꾸려면 `displayName` 을 쓰고, 기존 설치를 이어야 하면
  `marketplace.json` 에 `renames` 맵을 추가합니다 (append-only — 과거 엔트리를 지우지
  마십시오).
- **경로**: 설치 시 플러그인 디렉터리가 캐시로 복사되므로 `../` 로 밖을 참조할 수
  없습니다. 훅/MCP 설정에는 `${CLAUDE_PLUGIN_ROOT}`, 업데이트를 넘어 살아남아야 하는
  상태에는 `${CLAUDE_PLUGIN_DATA}` 를 씁니다.

### 로컬 테스트

```bash
cd ..
claude
> /plugin marketplace add ./cc-marketplace
> /plugin install <plugin-name>@cc-marketplace
> /reload-plugins
```

> ⚠️ **로컬 디렉터리로 등록한 마켓플레이스는 개발 중에만 쓰십시오.**
> `directory` 소스는 플러그인 코드(훅 포함)를 그 로컬 경로에서 직접 읽으므로,
> 그 디렉터리를 Claude Code 가 신뢰하지 않으면 **새 세션에서 플러그인이 미설치로
> 보입니다.** 사용자 스코프로 설치해도 마찬가지입니다 — 스코프는 "어디서 쓸 수
> 있나"를 정할 뿐 "코드를 어디서 읽나"를 바꾸지 않습니다.
>
> 실제로 이 저장소에서 겪은 문제이고, 원인을 찾는 데 오래 걸렸습니다.
> 테스트가 끝나면 되돌리십시오.
>
> ```bash
> /plugin marketplace remove <name>
> /plugin marketplace add <owner>/<repo>     # git 소스
> ```
>
> 등록 형태는 이렇게 확인합니다 — `"source": "git"` 이어야 합니다.
>
> ```bash
> python3 -c "
> import json,os
> d=json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json')))
> print({k:(v.get('source') or {}).get('source') for k,v in d.items()})"
> ```

---

MIT · [loveqoo](https://github.com/loveqoo)
