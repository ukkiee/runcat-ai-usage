# runcat-ai-usage

[English](README.md) | **한국어**

**Claude Code**와 **Codex**의 사용량 — 세션 / 주간 / 모델별 한도와 리셋까지 남은 시간 — 을 [RunCat Neo](https://github.com/runcat-dev/RunCatNeo) 대시보드 카드와 메뉴바 미터로 보여줍니다.

```
Claude Code Max 20x            메뉴바: 45% · 45%
  현재 세션: 45%
  ▓▓▓▓▓░░░░░
  재설정: 1시간 53분 후

  주간 한도: 45%
  ▓▓▓▓▓░░░░░
  재설정: 4일 9시간 후

  Fable: 45%
  ▓▓▓▓▓░░░░░
  재설정: 4일 9시간 후
```

라벨은 macOS 시스템 언어에 따라 한국어/영어로 자동 전환됩니다 ([설정](#설정) 참고).

## 동작 원리

RunCat Neo는 로컬 JSON 파일을 커스텀 메트릭 카드로 렌더링할 수 있습니다. 이 프로젝트는 작은 파이썬 폴러를 담고 있고, `launchd` 에이전트가 `runcat-poll.py`를 통해 5분마다 실행합니다. 실행할 때마다:

1. **이미 이 맥에 있는 OAuth 자격증명을 재사용**합니다(별도 로그인 불필요):
   - **Claude** — 로그인 키체인(`Claude Code-credentials`)에서 Apple 서명된 `security` CLI로 읽고, Claude Code가 자격증명을 파일에 두는 환경(헤드리스·원격 로그인)에서는 `~/.claude/.credentials.json`으로 폴백
   - **Codex** — `~/.codex/auth.json`에서 읽음
2. 각 서비스의 **전용 usage 엔드포인트**를 호출합니다. 모델 요청이 **아니라** 단순 메타데이터 `GET`이라 **토큰을 전혀 소모하지 않고** rate limit에도 영향을 주지 않습니다:
   - Claude: `GET https://api.anthropic.com/api/oauth/usage`
   - Codex: `GET https://chatgpt.com/backend-api/wham/usage`
3. `~/.claude/runcat-usage.json`과 `~/.codex/runcat-usage.json`을 씁니다. RunCat Neo가 이 파일들을 감시하다가 변경되면 카드를 갱신합니다.

표시되는 값은 공식 앱이 보여주는 것과 동일한 **계정 단위 실측값**이며, 앱이 꺼져 있어도 조회됩니다.

> 크레딧: usage 엔드포인트와 OAuth 세부사항은 동일한 역설계 조회를 수행하는 메뉴바 앱 [`openusage`](https://github.com/robinebers/openusage)에서 배웠습니다.

## 요구 사항

- macOS
- `python3` (표준 라이브러리만 사용 — `pip install` 불필요)
- [RunCat Neo](https://github.com/runcat-dev/RunCatNeo)
- **Claude Code**(`~/.claude`) 또는 **Codex**(`~/.codex`)에 로그인되어 있을 것 — 둘 중 하나만 있어도 됩니다

## 설치

```sh
git clone https://github.com/ukkiee/runcat-ai-usage.git
cd runcat-ai-usage
./install.sh
```

그다음 **RunCat Neo → Settings → Metrics → Custom Metrics → Add Custom Metrics Source**에서 아래 두 파일을 추가합니다:

- `~/.claude/runcat-usage.json` (Claude Code)
- `~/.codex/runcat-usage.json` (Codex)

메뉴바에 값을 표시하려면 Metrics Bar를 클릭해 해당 소스의 토글을 켜세요.

`install.sh`는 클론한 폴더의 폴러를 5분마다 실행하는 `launchd` 에이전트를 만듭니다(`RunAtLoad`로 즉시 1회 실행). 주기를 바꾸려면 `RUNCAT_POLL_INTERVAL=600 ./install.sh`처럼 실행하세요.

### 제거

```sh
./uninstall.sh
```

`launchd` 에이전트와 폴러가 쓰던 상태 파일을 제거합니다. 카드 파일(`runcat-usage.json`)은 그대로 남으니, 원하면 RunCat Neo 설정에서 소스를 직접 삭제하세요.

## 설정

환경 변수 (`install.sh`가 만든 `launchd` plist의 `EnvironmentVariables` 딕셔너리에 넣거나, 수동 실행 시 export):

| 변수 | 기본값 | 효과 |
|---|---|---|
| `RUNCAT_LANG` | 자동 (macOS 시스템 언어) | `ko` 또는 `en`으로 카드 언어 강제 지정 |
| `RUNCAT_POLL_INTERVAL` | `300` | 폴링 주기(초). 설치 시점에만 적용 |

카드 라벨과 플랜 이름은 `runcat_poll.py`(진입점 옆의 구현 파일) 상단에 있습니다:

- `STRINGS` — 언어별 표시 문자열 전체: `session` / `weekly` / `reset` 행 라벨과 카운트다운 표현. 창(window) 식별자는 들어 있지 않으므로, 언어를 바꿔도 라벨만 바뀝니다
- `CODEX_PLAN_LABELS` — Codex `plan_type`을 표시명으로 매핑 (예: `prolite → "Pro 5x"`, `pro → "Pro 20x"`)
- Claude의 플랜(`Max 20x` 등)은 rate-limit 티어에서 자동으로 유도됩니다
- 모델별 주간 한도는 해당 모델 이름(예: `Fable`)을 그대로 사용하므로, 플랜이 스코프하는 모델이 바뀌면 자동으로 따라갑니다

## 인증과 안전성

- **Claude — 읽기 전용.** 액세스 토큰을 키체인에서, 또는 Claude Code가 자격증명을 파일에 두는 환경이라면 `~/.claude/.credentials.json`에서 *읽기만* 하고 **어느 쪽에도 절대 쓰지 않습니다.** 서명되지 않은 스크립트는 서명된 앱처럼 ACL을 보존하는 `SecItemUpdate`를 할 수 없고, 거친 `security -U` 쓰기는 Claude Code가 자기 자격증명에 접근하지 못하게 잠글 위험이 있습니다. 그래서 이 도구는 Claude 토큰 갱신을 하지 않습니다. 토큰이 유효한 동안(= 최근에 Claude를 사용한 경우)에는 실측을 폴링하고, 만료되면 폴링을 멈추고 대신 마지막 성공 폴링 때 저장해 둔 usage reading(`~/.claude/runcat-reading.json`)으로 카드를 다시 만듭니다(그사이 리셋 시각이 지난 창은 0%로 처리). 다음에 Claude Code를 사용하면 Claude Code가 스스로 토큰을 갱신하므로 다시 실측으로 돌아옵니다.
- **Codex — 파일 기반.** `~/.codex/auth.json`의 토큰이 만료에 임박하면 표준 OAuth 엔드포인트로 갱신한 뒤 해당 파일에 되씁니다(Codex 자신이 쓰는 방식과 동일). Codex 토큰은 수명이 길어서 이 경우는 드뭅니다.
- 자격증명이나 토큰은 출력되거나 다른 곳에 저장되지 않으며, 해당 서비스의 usage 엔드포인트 외에는 어디로도 전송되지 않습니다.

## 주의사항

- 사용된 usage 엔드포인트는 **문서화되지 않은 내부 API**입니다(공식 클라이언트가 사용). 예고 없이 바뀔 수 있고, 그러면 이 도구와 `openusage` 모두 업데이트가 필요합니다.
- 값은 **이 맥의 토큰 기준**입니다. 다른 기기에서 쓴 사용량은 이 맥에서 다음 폴링이 성공할 때 반영됩니다.
- 리셋까지 남은 시간은 폴링마다 갱신되는 텍스트라(최대 `RUNCAT_POLL_INTERVAL`만큼 오래됨) 실시간 카운트다운이 아닙니다. RunCat 메뉴바는 한 줄만 지원하므로 두 퍼센트는 위아래가 아니라 가로로 나란히 표시됩니다(`45% · 45%`).

## 라이선스

[MIT](LICENSE)

---

*비공식 커뮤니티 통합입니다. "RunCat"은 해당 저작자의 상표이며, 이 프로젝트는 그들과 제휴하거나 승인받지 않았습니다.*
