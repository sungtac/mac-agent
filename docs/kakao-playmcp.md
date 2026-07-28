# kakao-playmcp (카카오톡 모닝 브리핑 — Play MCP 연동)

`cron/kakao-morning-briefing.sh` — 매일 09:00 `launchd`(`~/Library/LaunchAgents/com.macagent.kakao-morning-briefing.plist`, 레이블 `com.macagent.kakao-morning-briefing`, `Hour: 9, Minute: 0`, `Weekday` 키 없음=매일)로 실행. 오늘 구글캘린더 일정 + 오늘 날씨(기상청 단기예보, 광주광역시 격자좌표) + 뉴스 브리핑(종합/IT·AI/경제)을 카카오톡 "나에게 보내기"로 발송한다.

## 배경 — 왜 필요했나

사용자가 본 유튜브 영상(Claude Desktop의 Cowork 커넥터+예약작업 UI로 매일 아침 카톡 브리핑을 자동 발송하는 데모)을 보고, Cowork가 아닌 구독형 Claude Code(CLI)로도 같은 걸 할 수 있는지 물어서 검토 후 실제로 연결했다. Cowork의 "예약 작업" UI에 해당하는 부분은 이미 이 저장소의 `weekly-report.sh`가 launchd+headless `claude -p` 패턴으로 구현해둔 게 있어서 그대로 재사용했다.

## 연결 방식 — Plan A(네이티브 OAuth) 실패, Plan B(mcporter 브릿지) 성공

**Plan A (실패, 2026-07-28):** Claude Code 자체에 이미 검증된 네이티브 원격 MCP OAuth(`claude mcp login <name>`, Notion/Slack/Google Drive/Gmail/Calendar 5개에서 이미 동작 확인됨)를 먼저 시도했다.
```
claude mcp add --transport http kakao-playmcp https://playmcp.kakao.com/mcp --scope user
claude mcp login kakao-playmcp
```
결과: `HTTP 403 ... "허용되지 않은 IP 대역입니다" (ERR-PLAYAUTH-90403)`. PlayMCP의 OAuth 엔드포인트는 사전 등록된 외부 에이전트 클라이언트(mcporter 등)만 허용하고, Claude Code 자신이 즉석에서 만드는 OAuth 클라이언트는 거부한다 — 이건 설계 단계에서 "실제로 돌려봐야 아는 미확인 지점"으로 표시해뒀던 부분이고, 실제로 그렇게 확인됐다.

**Plan B (성공):** [mcporter](https://github.com/openclaw/mcporter) — 카카오 전용이 아닌 범용 MCP 클라이언트/브릿지 도구 — 를 경유.

1. 사용자가 `playmcp.kakao.com`에서 도구함에 원하는 도구(카카오톡 나에게 보내기, 미세먼지, 기상청 단기예보, 뉴스 브리핑, 청약 등)를 등록하고 **원타임 토큰(OTT, 10분 유효)** 발급.
2. 에이전트가 즉시:
   ```bash
   npm i -g mcporter
   mcporter config add mcp-gateway https://playmcp.kakao.com/mcp --auth oauth --scope home
   curl -s -X POST https://playmcp.kakao.com/api/v1/auths/otts:exchange \
     -H 'Content-Type: application/json' -d '{"tokenValue":"<OTT>"}'
   # 응답의 accessToken/refreshToken을 mcporter vault set으로 저장 (credentials.json 스키마를
   # 직접 손으로 만들지 말 것 — mcporter가 vault set/--stdin으로 정식 지원함)
   echo '{"tokens":{"access_token":"...","token_type":"Bearer","refresh_token":"..."},"clientInfo":{"client_id":"..."}}' \
     | mcporter vault set mcp-gateway --stdin
   ```
3. **Claude Code와의 최종 연결 — 두 가지 함정이 있었음:**
   - mcporter를 곧바로 `claude mcp add kakao-playmcp -- mcporter serve --servers mcp-gateway --stdio`로 등록하면 `Connection closed`로 실패한다: `mcporter serve`는 **keep-alive로 등록된 서버만** 브릿지할 수 있는데, `mcporter config add`로 만든 기본 서버 정의엔 keep-alive가 안 붙어있다. `~/.mcporter/mcporter.json`의 해당 서버 항목에 `"lifecycle": "keep-alive"`를 직접 추가하고, `mcporter daemon start`로 데몬을 띄워야 한다.
   - `claude mcp add <name> -- <command> [args...] --scope user`처럼 `--scope`를 `--` 뒤(서브프로세스 인자 자리)에 쓰면 `mcporter`의 인자로 잘못 들어가서 연결이 깨진다. **`--scope user`는 반드시 `--` 앞, `claude mcp add` 자신의 옵션 자리에 와야 한다**: `claude mcp add --scope user kakao-playmcp -- /opt/homebrew/bin/mcporter serve --servers mcp-gateway --stdio`. 절대경로 사용은 이 저장소 전체의 PATH 축소 관행(launchd 환경에서 `/usr/bin:/bin:/usr/sbin:/sbin`으로 축소됨) 그대로 적용.
   - `--scope user`가 아니라 기본값(`local`)로 등록하면 그 순간의 cwd(프로젝트)에서만 보인다 — `weekly-report.sh` 같은 headless `claude -p` 스크립트가 어디서 실행되든 이 서버를 봐야 하므로 반드시 `user` 스코프여야 함.

최종 검증: `claude mcp list`에 `kakao-playmcp ... ✔ Connected`. headless `claude -p`로 카카오톡 나에게 보내기 도구를 직접 호출해 실제 폰 카톡 나챗방 도착까지 육안 확인함(2026-07-28).

## 데몬 생존성 — 재부팅에 취약, 스크립트가 매번 방어

`mcporter daemon start`로 띄운 keep-alive 데몬은 그냥 백그라운드 프로세스일 뿐 launchd 잡이 아니다 — **재부팅하면 죽고, 아무도 자동으로 다시 안 띄운다.** `cron/kakao-morning-briefing.sh`는 이걸 신뢰하지 않고 매 실행마다 `mcporter daemon start`를 방어적으로 호출한다(멱등성 확인됨 — 2026-07-28: 이미 떠있으면 "Daemon already running"만 찍고 exit 0). 별도 launchd 데몬 plist를 새로 만들지 않은 이유: 하루 한 번 실행되는 스크립트 안에서 매번 확인하는 게, 상시 떠있는 별도 launchd 잡을 하나 더 관리하는 것보다 단순함.

## 실측으로만 확인 가능했던 것들 (문서만으로는 알 수 없었음)

- **카톡 발송 응답("메시지를 성공적으로 보냈습니다")은 즉시 도착을 보장하지 않는다.** 최초 두 번의 테스트 발송 모두 응답은 즉시 성공이었지만, 실제 폰 도착까지 수 분 지연이 있었다(비동기 처리로 추정, 근본 원인 미상). 발송 성공 응답만 보고 "완료"로 판단하지 말고, 자동화 검증 시엔 반드시 실제 수신 확인까지 기다릴 것.
- **"메시지 최대 200자" 문서 제약은 실제로 강제되지 않는다.** 250자 테스트(`가`×250)를 그대로 보내봤더니 잘림 없이 전체가 도착함(2026-07-28 확인) — 자동화 스크립트에서 인위적으로 200자로 자르지 않음. 다만 가독성을 위해 뉴스 요약은 원 도구가 권장하는 500~800자/주제가 아니라 2~3문장으로 압축하도록 프롬프트에 명시함(기술적 제약이 아니라 UX 판단).
- **날씨 도구(`20-get_short_term_forcast`)의 `nx`/`ny`(58, 74) — 검색으로는 광주 격자표를 못 찾아 처음엔 기온 그럴듯함으로만 간접 확인했으나, 이후 기상청 공식 변환식(LCC 투영법)으로 직접 계산해 재검증함(2026-07-28).** 공식·파라미터 출처: `RE=6371.00877, GRID=5.0, SLAT1=30, SLAT2=60, OLON=126, OLAT=38, XO=43, YO=136`(기상청 격자-위경도 변환 표준 공식). 이 공식을 서울 종로구 기준값(37.579871, 126.989352 → nx=60, ny=127, 널리 알려진 검증값)으로 먼저 교차검증한 뒤, 광주광역시청 좌표(35.1595, 126.8526)를 대입하면 정확히 nx=58, ny=74가 나옴 — 스크립트에 쓰인 값과 정확히 일치. 다른 지역으로 포팅 시 이 공식에 해당 지역 위경도만 대입하면 재계산 가능.

## 자동 브리핑에서 의도적으로 제외한 것

도구함에 청약(부동산, 8개 도구) 관련 도구가 등록돼 있지만 자동 일일 브리핑엔 포함하지 않음 — 청약 공고는 매일 갱신되는 성격이 아니라 반복 발송 실익이 적음(사용자 확인, 2026-07-28). 필요할 때 대화로 직접 호출하는 용도로 남겨둠. 주식/사주 도구는 사용자가 도구함 자체에서 제거함.

## 알려진 제약 / 후속 과제

- 뉴스 요약 3주제(종합/IT·AI/경제) 각각 별도 도구 호출 — `KakaoPNB-summarize_news`는 최종 요약이 아니라 "이렇게 요약하라"는 지시문+원본 기사 목록을 반환하는 도구라, 실제 요약 작성은 그 지시문을 받은 Claude가 매번 수행함 — 도구 자체의 결정적 출력이 아니므로 매일 문체/분량이 미세하게 달라질 수 있음.
- `weekly-report.sh`와 동일한 launchd-트리거 headless `claude -p` 간헐적 행 이슈(원인: Bun 기반 CLI의 HTTP 커넥션 풀 내부 스톨, `docs/weekly-report.md` 참고)에 이 스크립트도 동일하게 노출됨 — 동일한 재시도+워치독(240초, 3회) 그대로 적용.
- To port to another agent/machine: `cron/kakao-morning-briefing.sh`와 plist를 복사하고, mcporter를 새로 설치해 Play MCP 게이트웨이를 위 Plan B 절차로 재연결해야 함(계정마다 별도 OTT 필요) — nx/ny 좌표와 뉴스 주제는 사용자 지역/관심사에 맞게 조정.

## 사용량 사전 게이트 (2026-07-28, P4)

`mcporter daemon start`를 부르기 전에 `workflows/lib/usage-preflight-gate.sh claude`부터 확인한다 — 이 스크립트도 결국 headless `claude -p` 실행이라, 계정 사용량이 바닥인 채로 시작해봐야 위 "간헐적 행 이슈"만 반복될 뿐이라서. `SKIP:`이면 로그만 남기고 `discord-notify.sh`로 일방향 알림(이 스크립트는 애초에 discord-bot.py의 답장-재시도 시스템에 안 물려 있어 pending-job은 안 씀 — 실패 3회 소진 시 알림과 동일한 성격), exit 4. 게이트 자체가 실패하면 fail-open으로 그냥 진행. 실제 계정 사용량(클로드 5시간창 0%)으로 SKIP 분기까지 샌드박스 검증 완료 — 실제 09:00 launchd 트리거로 라이브 검증은 아직 안 함.
