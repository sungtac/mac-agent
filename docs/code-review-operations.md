# Code Review 운영 절차

## 1. 설치 전 점검

시스템 상태를 변경하지 않고 설정만 검사한다.

```bash
cd /Users/edge_ai/mac-agent
python3 bin/code-review-ops-preflight.py
```

이 점검은 worker와 Webhook server의 launchd 템플릿도 검사하며, 시스템에
LaunchAgent를 등록하지 않는다.

provider 실행까지 준비됐는지 확인하려면 다음을 추가한다.

```bash
python3 bin/code-review-ops-preflight.py --require-providers --allow-execute
```

`--require-clean`은 현재 매핑된 모든 worktree가 깨끗해야 통과한다. worker는
실제로 실행할 때에도 같은 clean-worktree 조건을 다시 검사한다.

## 2. Webhook 수신기

수신기는 반드시 외부 TLS endpoint 또는 검증된 reverse proxy 뒤에 두고,
GitHub secret은 프로세스 환경이나 별도 secret manager에서 주입한다. GitHub
Webhook을 등록하지 않았거나 공개 endpoint가 없다면 launchd worker만
설치해도 pending 요청은 생기지 않는다.

```bash
CODE_REVIEW_WEBHOOK_SECRET='주입된 secret' \
node bin/code-review-webhook-server.js
```

수신기는 `POST /github/webhook`의 raw body를 HMAC 검증한 뒤, 원본 payload가
아닌 SHA 귀속 요청만 로컬 큐에 저장한다.

## 3. worker 수동 dry-run

```bash
node bin/code-review-worker-runner.js \
  --config config/code-review-repositories.json
```

dry-run에서는 provider를 호출하지 않고 저장소·SHA·worktree 조건만 검사한다.
기본 allowlist는 isolated 모드를 사용하므로 source worktree가 dirty여도
원본을 건드리지 않는 임시 detached worktree에서 대상 SHA를 검사한다.

## 4. provider 실행

비용이 발생하므로 운영자가 명시적으로 실행한다.

```bash
node bin/code-review-worker-runner.js \
  --config config/code-review-repositories.json \
  --execute
```

Codex와 Antigravity 중 하나라도 실패하면 큐 요청은 pending으로 남고,
SHA 귀속 보고서가 저장된 뒤에만 완료된다.

## 5. launchd

`config/com.macagent.code-review-worker.plist.template`은 60초 주기의
설치 템플릿이고, `config/com.macagent.code-review-webhook-server.plist.template`
은 HMAC Webhook server용 상시 실행 템플릿이다. worker 템플릿은 provider
호출을 활성화한다. 설치 전 점검에서 `--allow-execute`를 명시해야
이 운영 모드를 승인한 것으로 간주한다. 설치 전 dry-run은 다음과 같다.

```bash
bash bin/install-code-review-launchd.sh --dry-run
```

`--install`은 secret 파일(mode 600)을 확인한 뒤에만 두 LaunchAgent를
복사하고 bootstrap한다. provider 실행이 포함된 템플릿을 설치하기 전에는
반드시 `--require-providers --allow-execute` 점검을 먼저 통과시킨다.

worker는 StartInterval=60 방식의 단발 실행 작업이므로 launchctl print에서
주기 사이에 state = not running으로 보이는 것은 정상이다. 운영 확인은
runs가 증가하는지, last exit code = 0인지, pending queue가 줄어드는지,
Webhook /health가 응답하는지를 함께 확인한다. 상시 프로세스 복구를 위해
worker를 직접 kickstart하지 않는다.

GitHub 등록, TLS/reverse proxy, secret manager, launchd load는 이 로컬
코드의 자동 변경 범위를 벗어난 운영 권한 작업이다.
