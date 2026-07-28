# 새 에이전트 설치 매뉴얼

이 fleet(현재 macmini 하나, 향후 확장 가능)에 새 머신/에이전트를 편입시키거나, 기존
에이전트에 스킬을 재설치할 때 따르는 절차. 세 갈래로 나뉜다:

1. **mac-agent 자산** (훅·워크플로우·cron) — 이 저장소 자체
2. **Claude Skills** (hwpx, pptx, 이식형 패턴 5종, 외부 도입 5종) — `skill-catalog`로 설치
3. **hwpx/pptx 개발환경** — 스킬을 쓰기만 할 게 아니라 고칠 때만 필요

전체 목록·상태 요약은 스킬 카탈로그 아티팩트(맥 바탕화면 `구현스킬카탈로그.html`)를 참고.

## 1. mac-agent 자산 설치

```bash
git clone https://github.com/sungtac/mac-agent.git ~/mac-agent
cd ~/mac-agent && ./setup.sh   # Codex/Antigravity CLI 확인·설치
```

- `setup.sh`는 OAuth 로그인까지는 못 한다 — 브라우저 승인은 사람이 직접.
- 훅·워크플로우를 `~/.claude/`에 편입하는 법(심볼릭 링크 또는 플러그인 마켓플레이스)과
  각 자산 설명은 [README.md](../README.md) / [CLAUDE.md](../CLAUDE.md) 참조.
- `cron/weekly-report.sh`는 macOS `launchd`로 별도 등록 필요 — plist 예시는
  [docs/weekly-report.md](weekly-report.md).

## 2. Claude Skills 설치 (skill-catalog)

전제: 이 머신에 Google Drive(`sungtac@gmail.com`)가 마운트돼 있고
`내 드라이브/portable-skills/`가 보여야 한다.

```bash
python3 ~/.claude/skills/skill-catalog/generator/catalog.py --list
python3 ~/.claude/skills/skill-catalog/generator/catalog.py --install \
  hwpx,pptx,grounded-rigor,agent-model-board,independent-critique-loop,rnd-consortium-rnr-strategy,workflow-follow-through,\
diagnosing-bugs,tdd,handoff,design-taste-frontend,last30days \
  --yes
python3 ~/.claude/skills/skill-catalog/generator/catalog.py --doctor
```

- `three-role-work-verification`은 내장 `verify-task-v2`와 개념이 겹쳐 설치 시점에
  의도적으로 거부된다(정상 동작).
- 재설치(덮어쓰기)는 항상 멱등 — 안전하게 반복 실행 가능.
- `diagnosing-bugs`/`tdd`/`handoff`(mattpocock/skills), `design-taste-frontend`(Leonxlnx/taste-skill),
  `last30days`(mvanhorn/last30days-skill)는 2026-07-28에 검토 후 편입한 외부(MIT) 스킬 —
  각 폴더의 `SOURCE.md`에 원본 저장소·커밋·라이선스 기록. `last30days`는 `node`, `python3`
  바이너리가 있어야 동작(대부분 소스는 API 키 불필요, X/TikTok 등 일부만 선택적 키 필요).

## 3. skill-catalog 자신 + `_shared/adapters` 편입 (신규 머신 최초 1회 한정)

`skill-catalog`는 스스로를 카탈로그 품목으로 다루지 않는다(`SELF_EXCLUDE`) — Drive에도
없고 GitHub화도 안 돼 있어, 2번 절차로는 안 들어온다. 완전히 새 머신이면 기존 머신에서
직접 rsync로 가져온다:

```bash
rsync -a <기존머신>:~/.claude/skills/skill-catalog/ ~/.claude/skills/skill-catalog/
rsync -a <기존머신>:~/.claude/skills/_shared/        ~/.claude/skills/_shared/
```

(현재 유일한 배포 경로 — 자동화 없음. 이 경로 자체를 GitHub이나 Drive로 옮기는 건
별도 결정 필요.)

## 4. hwpx/pptx 개발환경 (선택)

스킬을 실행만 하면 2번으로 충분하다. 스킬 코드를 직접 고치려면 git 저장소로 받는다:

```bash
git clone https://github.com/sungtac/hwpx-skill.git ~/document-writing-project/hwpx-skill
git clone https://github.com/sungtac/pptx-skill.git ~/document-writing-project/pptx-skill
ln -sfn ~/document-writing-project/hwpx-skill ~/.claude/skills/hwpx
ln -sfn ~/document-writing-project/pptx-skill ~/.claude/skills/pptx
```

두 저장소 모두 **private**(사용자 GitHub 개인 계정). 의존성:

- hwpx: `pip install python-hwpx lxml --break-system-packages`
- pptx: `python3.11 -m pip install python-pptx --break-system-packages` +
  `libreoffice-impress` 설치 + 맑은 고딕 fontconfig 등록
  (상세: `pptx-skill/SKILL.md` "다음에 실제로 만들 때 해야 할 것")

## 5. 갱신 체크리스트 (스킬을 추가·수정할 때마다)

1. git 저장소가 있는 것(hwpx-skill, pptx-skill, mac-agent)은 로컬에서 커밋 → GitHub push
2. Drive portable-skills/ 배포 대상이면 재동기화:
   ```bash
   rsync -a --exclude=".git" ~/document-writing-project/hwpx-skill/ \
     "$HOME/Library/CloudStorage/GoogleDrive-sungtac@gmail.com/내 드라이브/portable-skills/hwpx/"
   ```
   (pptx도 동일하게. **폴더명은 저장소명이 아니라 SKILL.md의 `name:` 값과 일치시킬 것** —
   안 맞으면 catalog.py가 "이미 설치됨"을 인식 못 하고 별도 품목으로 취급한다.)
3. 각 에이전트에서 `catalog.py --doctor`로 stale 여부 확인 → 필요하면 `--install` 재실행
4. 스킬 카탈로그 아티팩트(요약 웹페이지)와 맥 바탕화면 사본도 새 항목 반영해 갱신

## 부록: 소스오브트루스 지도

| 자산군 | 소스오브트루스 | 설치 경로 |
|---|---|---|
| mac-agent 훅·워크플로우·cron | GitHub `sungtac/mac-agent` | git clone + symlink |
| hwpx, pptx 스킬 | GitHub `sungtac/hwpx-skill`, `sungtac/pptx-skill` (개발) | Drive `portable-skills/` (설치, `catalog.py`) |
| 이식형 패턴 5종 + skill-catalog + `_shared` | Drive `portable-skills/` (GitHub 없음) | `catalog.py --install` (skill-catalog·`_shared`는 수동 rsync) |
| 외부 도입 스킬 5종(diagnosing-bugs·tdd·handoff·design-taste-frontend·last30days) | 각 원저자 GitHub(MIT, `SOURCE.md` 참조) → Drive `portable-skills/`로 편입 | `catalog.py --install` |
