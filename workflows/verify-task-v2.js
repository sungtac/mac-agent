export const meta = {
  name: 'verify-task-v2',
  description: '작업 시작 전 스펙 고정 + 경량/전체 티어별 다자간 검증 (Claude/Codex/Antigravity). 설계: docs/verify-task-v2-design.md',
  phases: [
    { title: 'Preflight' },
    { title: 'Context' },
    { title: 'Light' },
    { title: 'FullPromptify' },
    { title: 'FullResearch' },
    { title: 'FullPlan' },
    { title: 'FullPlanReview' },
    { title: 'FullPlanRevise' },
    { title: 'FullExecute' },
    { title: 'FullCodeReviewSkill' },
  ],
}

// 설계 전체는 docs/verify-task-v2-design.md 참고 — 이 스크립트는 결정 기록이
// 아니라 구현이다. 결정의 "왜"를 다시 읽지 않고 이 파일만 고치지 말 것.
//
// 2026-08-01 개정: 전체(full) 트랙을 Claude 프롬프트화→Antigravity 병렬
// 조사→Claude 병렬화 계획→Codex 병렬 계획 검토→Codex 계획 수정/실행→
// code-review 스킬 발동 흐름으로 변경함. 경량(light)·나노(nano) 트랙은
// 비용과 호환성을 위해 유지한다.
//
// Workflow 스크립트는 다른 로컬 파일을 import 할 수 없어(자기완결적이어야
// 함), verify-task.js와 겹치는 헬퍼(대시패치 지시문 생성, 검증용 diff 수집,
// 히스토리 기록, preflight)를 이 파일 안에 중복 구현한다 — 의도적 중복이지,
// 실수가 아니다.

const PREFLIGHT_SCHEMA = {
  type: 'object',
  properties: { ok: { type: 'boolean' }, issues: { type: 'string' } },
  required: ['ok'],
}

async function preflightCheck() {
  // 2026-07-30 fix (Codex 코드리뷰로 발견) — verify-task.js와 동일한 이유로
  // CODEX_BIN/AGY_BIN 환경변수 override를 존중하도록 통일.
  return agent(
    `Bash 툴로 아래 두 명령을 순서대로 실행해줘 (CODEX_BIN/AGY_BIN 환경변수가 설정돼 있으면 그 경로를 쓰고, 없으면 Homebrew/사용자 bin 후보를 찾아 써 — 이 실행 환경 PATH가 축소돼 있을 수 있어서 bare 명령어만 믿지 말 것):\n1. "\${CODEX_BIN:-/opt/homebrew/bin/codex}" login status\n2. env -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT "\${AGY_BIN:-\$HOME/.local/bin/agy}" models\n\n두 명령 다 에러 없이 성공(로그인된 상태)이면 ok=true, issues는 빈 문자열로 반환해. 하나라도 로그인 필요/에러가 나면 ok=false로 하고, 어떤 도구가 문제인지와 해결 방법을 issues에 적어줘.`,
    { phase: 'Preflight', label: 'preflight', schema: PREFLIGHT_SCHEMA }
  )
}

// Workflow 스크립트는 Node.js API에 접근할 수 없다(process 미정의) — 여기서
// env override를 시도하면 스크립트 자체가 로드 시점에 죽는다. 경로가 다른
// 환경에서 필요하면 args로 받아서 써야지 process.env로 받으면 안 된다.
const MAC_AGENT_ROOT = '/Users/edge_ai/mac-agent'
const CODE_REVIEW_STORE = MAC_AGENT_ROOT + '/workflows/lib/code-review-store.js'
const CLAUDE_HOME = '/Users/edge_ai'
const SCORE_DISPATCH = `${MAC_AGENT_ROOT}/workflows/lib/score-dispatch.sh`
const CODEX_EXECUTE_DISPATCH = `${MAC_AGENT_ROOT}/workflows/lib/codex-execute-dispatch.sh`
const NANO_EVENT_RECORDER = `${MAC_AGENT_ROOT}/workflows/lib/nano-event-store.js`
const HARNESS_FILE_DEFAULT = `${MAC_AGENT_ROOT}/docs/codex-harness.md`
const NANO_EVENT_FILE_DEFAULT = `${CLAUDE_HOME}/.claude/nano-gate-events.jsonl`

// score-dispatch.sh는 읽기전용 채점/의견용(codex 또는 agy 모두 --sandbox
// read-only 기본값). 실제 파일을 쓰는 유일한 지점(전체 트랙 실행 단계)만
// codex-execute-dispatch.sh(-s workspace-write)를 쓴다 — 절대 섞어 쓰지 말 것.
//
// 하네스 주입: score-dispatch.sh의 코덱스 호출엔 -C(대상 저장소 경로)가
// 없고, codex-execute-dispatch.sh의 -C는 "대상 프로젝트"지 이 하네스 파일이
// 있는 mac-agent 저장소가 아니다. 그래서 코덱스 자신에게 "하네스 파일을
// 읽어라"라고 시킬 수 없다 — 이 지시문들을 실행하는 Claude 서브에이전트가
// (Read 툴로) 대신 읽어서 프롬프트 텍스트 안에 직접 박아 넣는다. harnessFile
// 인자가 있을 때만(그리고 tool==='codex'일 때만 — agy는 하네스 대상 아님)
// 이 prepend가 붙는다.
// schemaKind는 score-dispatch.sh의 FAILURE_ENVELOPE가 어떤 단계 스키마에
// 맞는 실패 봉투를 만들지 결정하는 3번째 인자다 — 이걸 안 넘기면 스크립트는
// v1(rubric) 모양만 반환하는데, v2 각 단계 스키마(plan/critique/reconcile/
// review/light-eval)는 그거랑 구조가 전혀 달라서 실제 실패가 나면 스키마
// 검증 자체가 깨졌다(2026-07-27/28 실측, docs/verify-task-v2-design.md
// "손 안 댄 것" 기록). 호출부마다 자기 스키마에 맞는 kind를 명시해야 함.
function buildScoreDispatchInstruction(tool, prompt, harnessFile, schemaKind) {
  const injectHarness = tool === 'codex' && !!harnessFile
  const harnessNote = injectHarness
    ? `[하네스 주입] 3번에서 저장할 내용은 [프롬프트 내용]을 그대로 저장하는 게 아니라, 먼저 Read 툴로 ${harnessFile}을 읽고(파일이 없으면 첫 실행이니 "해당 없음"으로 간주), "[코덱스 하네스 — 반드시 준수]\\n" + 그 내용 + "\\n\\n---\\n\\n"를 맨 앞에 붙인 뒤 [프롬프트 내용]을 이어붙인 합본이어야 해.\n\n`
    : ''
  return `${harnessNote}1. Bash로 \`mktemp /tmp/verify-task-v2-${tool}-XXXXXX.txt\` 실행해서 임시 파일 경로를 얻어.\n2. 반드시 Read 툴로 방금 얻은 임시 파일을 한 번 읽어(빈 파일이어도 괜찮아 — Claude의 Write 계약상 먼저 읽은 파일만 Write할 수 있음).\n3. Write 툴로 그 경로에 아래 [프롬프트 내용]을 정확히 그대로(글자 하나 고치지 말고${injectHarness ? ', 단 하네스 주입 지시가 있으면 위에서 설명한 합본으로' : ''}) 저장해.\n4. Bash로 다음을 실행해 (파일경로는 3번 경로로 치환): bash ${SCORE_DISPATCH} ${tool} <파일경로> ${schemaKind}\n5. 실행이 끝나면 Bash로 그 임시 파일을 삭제해.\n6. 4번 명령의 stdout은 이미 검증된 JSON 한 줄이야 — 그 값을 그대로 구조화된 출력으로 반환해. 내용을 고치거나, 재해석하거나, 다른 값으로 대체하지 마.\n\n[프롬프트 내용]\n${prompt}`
}

// v1(verify-task.js)은 문자열(dealbreaker_reason) 동기화로 도구 실패를
// 판별하지만, v2는 스키마마다 필드가 달라 문자열 위치가 스키마별로 다를 수
// 있어 그 방식이 안 맞는다. 대신 모든 v2 실패 봉투가 공통으로 갖는
// dispatchFailed 불리언 마커 하나로 스키마 무관하게 판별한다(score-dispatch.sh
// FAILURE_ENVELOPE와 짝 — 필드명이 동기화 지점).
function isDispatchFailure(result) {
  return !!result && result.dispatchFailed === true
}

// v1의 scoreWithDispatchRetry와 동일한 목적(도구 실행/파싱 실패와 진짜 낮은
// 평가를 구분해 1회만 자동 재시도) — v2는 호출부가 여러 종류라 제네릭하게
// 구현. dispatchFn은 (isRetry) => Promise 형태를 받아 label 표시에만 쓴다.
async function dispatchWithRetry(dispatchFn, label) {
  let result = await dispatchFn(false)
  if (isDispatchFailure(result)) {
    log(`${label}: 도구 실행/파싱 실패로 보여 — 1회 재시도 중...`)
    result = await dispatchFn(true)
  }
  return result
}

function buildExecuteDispatchInstruction(cwd, prompt, harnessFile) {
  const harnessNote = harnessFile
    ? `[하네스 주입] 3번에서 저장할 내용은 [프롬프트 내용]을 그대로 저장하는 게 아니라, 먼저 Read 툴로 ${harnessFile}을 읽고(파일이 없으면 첫 실행이니 "해당 없음"으로 간주), "[코덱스 하네스 — 반드시 준수]\\n" + 그 내용 + "\\n\\n---\\n\\n"를 맨 앞에 붙인 뒤 [프롬프트 내용]을 이어붙인 합본이어야 해.\n\n`
    : ''
  return `${harnessNote}1. Bash로 \`mktemp /tmp/verify-task-v2-exec-XXXXXX.txt\` 실행해서 임시 파일 경로를 얻어.\n2. 반드시 Read 툴로 방금 얻은 임시 파일을 한 번 읽어(빈 파일이어도 괜찮아 — Claude의 Write 계약상 먼저 읽은 파일만 Write할 수 있음).\n3. Write 툴로 그 경로에 아래 [프롬프트 내용]을 정확히 그대로(글자 하나 고치지 말고${harnessFile ? ', 단 하네스 주입 지시가 있으면 위에서 설명한 합본으로' : ''}) 저장해.\n4. Bash로 다음을 실행해 (파일경로는 3번 경로로 치환, timeout 300000ms 이상 줘): bash ${CODEX_EXECUTE_DISPATCH} ${JSON.stringify(cwd)} <파일경로>\n5. 실행이 끝나면 Bash로 그 임시 파일을 삭제해.\n6. 4번 명령의 stdout은 이미 검증된 JSON 한 줄이야({"ok":bool,"message":string}) — 그 값을 그대로 구조화된 출력으로 반환해. 내용을 고치거나, 재해석하거나, 다른 값으로 대체하지 마. 이 결과는 코덱스 자체 보고일 뿐 실제 검증이 아님을 기억해 — 실제 변경사항은 별도로 git diff로 확인할 거야.\n\n[프롬프트 내용]\n${prompt}`
}

const EXECUTE_ENVELOPE_SCHEMA = {
  type: 'object',
  properties: { ok: { type: 'boolean' }, message: { type: 'string' } },
  required: ['ok', 'message'],
}

// ---------- 컨텍스트 수집 + 티어 판정 (기계적) ----------

const CONTEXT_SCHEMA = {
  type: 'object',
  properties: {
    cwdExists: { type: 'boolean' },
    contextText: { type: 'string' },
    intendedFiles: { type: 'array', items: { type: 'string' } },
    sensitivePath: { type: 'boolean' },
  },
  required: ['cwdExists', 'contextText', 'intendedFiles', 'sensitivePath'],
}

// 민감 경로 근사 규칙 — docs/verify-task-v2-design.md의 "설정/보안/
// .github/workflows/공개문서"를 정규식으로 근사한 것. 완벽하지 않음 —
// false positive/negative 관찰되면 이 목록을 조정할 것 (work-log-stop-check.sh의
// grep 휴리스틱과 같은 성격: 의도적으로 단순한 기계적 근사).
const SENSITIVE_PATH_PATTERNS = [
  /(^|\/)\.github\/workflows\//,
  /(^|\/)\.env(\.|$)/,
  /(^|\/)(secrets?|credentials?)(\/|\.|$)/i,
  /(^|\/)settings\.json$/,
  /(^|\/)permissions?\.json$/i,
  /(^|\/)security\//i,
  /(^|\/)README\.md$/,
]

function isSensitivePath(path) {
  return SENSITIVE_PATH_PATTERNS.some((re) => re.test(path))
}

const STANDARD_FULL_FILE_PATTERNS = [
  /(^|\/)(auth|authentication|authorization|routing|router|security|permissions?)(\/|\.|$)/i,
  /(^|\/)(config|configuration|deploy|deployment|infra|infrastructure|migrations?)(\/|\.|$)/i,
  /(^|\/)(Dockerfile|docker-compose(?:\..+)?|Makefile|package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$/i,
  /(^|\/)(package\.json|pyproject\.toml|Cargo\.toml|go\.mod|requirements\.txt)$/i,
]
const STANDARD_CODE_EXTENSIONS = new Set(['.c', '.cc', '.cpp', '.cs', '.go', '.java', '.js', '.jsx', '.mjs', '.py', '.rb', '.rs', '.sh', '.swift', '.ts', '.tsx', '.vue'])
const STANDARD_LIGHT_FILE_PATTERNS = [
  /(^|\/)(test|tests|__tests__|fixtures?|snapshots?)(\/|\.|$)/i,
  /\.(md|mdx|txt|rst|adoc)$/i,
]

function standardFileTier(filePath) {
  const normalized = String(filePath || '').replaceAll('\\', '/')
  if (!normalized) return 'light'
  if (isSensitivePath(normalized) || STANDARD_FULL_FILE_PATTERNS.some((pattern) => pattern.test(normalized))) return 'full'
  if (STANDARD_LIGHT_FILE_PATTERNS.some((pattern) => pattern.test(normalized))) return 'light'
  const dot = normalized.lastIndexOf('.')
  const extension = dot >= 0 ? normalized.slice(dot).toLowerCase() : ''
  return STANDARD_CODE_EXTENSIONS.has(extension) ? 'mid' : 'light'
}

async function gatherContext(cwd, task) {
  const gathered = await agent(
    `아래는 곧 시작할 작업이고, 아직 아무 실행도 안 한 상태야. 순서대로 해줘:\n\n[작업]\n${task}\n\n0. 먼저 Bash로 \`[ -d ${JSON.stringify(cwd)} ] && echo EXISTS || echo MISSING\`을 실행해. "MISSING"이면 cwdExists=false로 하고, contextText에는 그 사실만 짧게 적고, intendedFiles는 빈 배열, sensitivePath는 false로 채워서 즉시 끝내 — 존재하지 않는 디렉토리에서 아래 1~7번을 시도하지 마(git 명령이 엉뚱한 디렉토리에서 실행되거나 에러 텍스트가 진짜 컨텍스트인 것처럼 섞여 들어감).\n1. cwdExists=true로 하고, Bash로 이 디렉토리에서 아래를 실행: cd ${JSON.stringify(cwd)} && { echo '--- git status ---'; git status; echo '--- 최근 커밋 5개 ---'; git log --oneline -5; } 2>&1\n2. 작업과 관련 있어 보이는 파일들을 Glob/Grep/Read로 가볍게 훑어봐(전체 저장소를 다 읽지 말고, 작업 키워드로 관련 있는 것만).\n3. 이 디렉토리(또는 상위)에 CLAUDE.md/AGENTS.md 같은 컨벤션 문서가 있으면 Read로 읽어서 관련 부분을 요약해.\n4. package.json의 scripts, Makefile, README의 테스트 관련 섹션 등에서 테스트 실행 명령을 찾아봐(있으면).\n5. 위 1~4에서 얻은 사실을 contextText 하나의 텍스트로 정리해(요약하지 말고 사실 위주로, 다음 단계 에이전트들이 저장소를 직접 못 보고 이 텍스트만 볼 거야).\n6. 이 작업이 **실제로 건드릴 것으로 예상되는 파일 경로 목록**을 intendedFiles에 넣어줘 — 아직 실행 전이니 예측이야, 최대한 구체적으로. 새로 만들 파일도 포함.\n7. intendedFiles 중 설정/보안/.github/workflows//공개문서(README 등)에 해당하는 게 하나라도 있으면 sensitivePath=true, 아니면 false.\n\n마지막 응답은 반드시 다른 설명 없이 아래 네 키를 모두 포함한 JSON 객체 하나여야 해(필드 누락 금지):\n{"cwdExists":true,"contextText":"수집한 사실","intendedFiles":["예상 경로"],"sensitivePath":false}`,
    { phase: 'Context', label: 'gather-context', schema: CONTEXT_SCHEMA }
  )
  return gathered
}

function decideTier(context) {
  const fileCount = (context?.intendedFiles || []).length
  const sensitive = !!context?.sensitivePath
  const fileTiers = (context?.intendedFiles || []).map(standardFileTier)
  const hasFullFile = fileTiers.includes('full')
  const hasCodeFile = fileTiers.includes('mid')
  return fileCount <= 3 && !sensitive && !hasFullFile && !hasCodeFile ? 'light' : 'full'
}

// ---------- 사후 검증용 실제 diff 수집 (verify-task.js와 동일 패턴) ----------

const REAL_DIFF_SCHEMA = {
  type: 'object',
  properties: {
    content: { type: 'string' },
    filesChanged: { type: 'array', items: { type: 'string' } },
    sensitivePath: { type: 'boolean' },
    headSha: { type: 'string' },
  },
  required: ['content', 'filesChanged', 'sensitivePath', 'headSha'],
}

// 주의: `git diff HEAD`는 아직 add된 적 없는 untracked 신규 파일을 절대 보여주지
// 않는다(코덱스/에이전트가 새 파일을 만든 경우 흔함) — 실측(2026-07-27 종단
// 테스트)으로 이 누락 때문에 안티그래비티(파일시스템 직접 접근 불가, 이 함수가
// 만든 텍스트만 봄)가 이미 정확히 생성된 파일을 "누락됐다"고 거짓 반려한 사례가
// 실제로 발생함. 그래서 git status --porcelain으로 신규(untracked) 파일 목록을
// 뽑아 전체 내용을 별도 섹션으로 반드시 덧붙인다 — git add 등으로 실제 git
// 상태를 건드리지 않고 읽기만 한다.
async function gatherRealDiff(cwd) {
  const gathered = await agent(
    `Bash로 아래 명령을 그 디렉토리에서 실행하고, 나온 출력을 절대 요약하거나 고치지 말고 content 필드에 그대로 담아 반환해:\n\ncd ${JSON.stringify(cwd)} && { echo '--- git rev-parse HEAD ---'; git rev-parse HEAD; echo '--- git status --porcelain ---'; git status --porcelain; echo '--- git diff --stat HEAD (tracked 변경만) ---'; git diff --stat HEAD; echo '--- git diff HEAD (tracked 변경만) ---'; git diff HEAD; echo '--- untracked 신규 파일 전체 내용 (git diff에는 안 잡힘) ---'; git status --porcelain | awk '$1 == "??" {print $2}' | while IFS= read -r f; do echo "=== NEW FILE: $f ==="; cat "$f"; done; } 2>&1\n\n출력이 8000자를 넘으면 앞 8000자만 남기고 끝에 "...(잘림)"을 붙여.\n\n추가로: git rev-parse HEAD의 결과를 headSha 문자열에 넣어. git status --porcelain 출력 전체(수정된 tracked 파일 + untracked 신규 파일 둘 다)에서 실제로 변경/추가된 파일 경로를 전부 뽑아 filesChanged 배열에 넣고(신규 파일도 반드시 포함), 그중 설정/보안/.github/workflows//공개문서(README 등)에 해당하는 게 하나라도 있으면 sensitivePath=true로 반환해.\n\n마지막 응답은 반드시 다른 설명 없이 아래 네 키를 모두 포함한 JSON 객체 하나여야 해(필드 누락 금지):\n{"content":"위 명령의 원문 출력","filesChanged":["실제 변경 경로"],"sensitivePath":false,"headSha":"현재 HEAD SHA"}`,
    { phase: 'Light', label: 'gather-real-diff', schema: REAL_DIFF_SCHEMA }
  )
  return gathered
}

function mechanicalTierViolated(realDiff) {
  const fileCount = (realDiff?.filesChanged || []).length
  return fileCount > 3 || !!realDiff?.sensitivePath
}

const REVIEW_PERSIST_SCHEMA = {
  type: 'object',
  properties: {
    persisted: { type: 'boolean' },
    outcome: { type: 'string' },
    reportPath: { type: 'string' },
    error: { type: 'string' },
  },
  required: ['persisted'],
}

function buildReviewReport(task, cwd, tier, realDiff, verdict, history) {
  if (!realDiff?.headSha) return null
  const reviewRounds = history.filter((entry) => entry.codexReview || entry.antigravityReview)
  const lastRound = reviewRounds[reviewRounds.length - 1] || {}
  const reviewResults = [
    ['codex', lastRound.codexReview],
    ['antigravity', lastRound.antigravityReview],
  ]
  const findings = []
  for (const [source, result] of reviewResults) {
    for (const [index, issue] of (result?.issues || []).entries()) {
      findings.push({
        id: source + '-' + (index + 1),
        severity: issue.blocking ? 'blocker' : 'medium',
        category: 'correctness',
        location: (realDiff.filesChanged || []).join(', ') || 'diff',
        title: source + ' review finding',
        evidence: String(issue.description || 'No description provided'),
        remediation: 'Review and resolve this finding before approval.',
        verified: source === 'antigravity',
      })
    }
  }
  const checks = reviewResults.map(([source, result]) => ({
    name: source + '-review',
    status: !result ? 'error' : result.hasBlockingIssue ? 'failed' : 'passed',
  }))
  for (const [source, result] of reviewResults) {
    for (const check of result?.checks || []) {
      checks.push({
        name: source + '-' + String(check.name || 'unnamed-check'),
        status: ['passed', 'failed', 'not_run', 'error'].includes(check.status) ? check.status : 'error',
        ...(check.evidence ? { evidence_ref: String(check.evidence) } : {}),
      })
    }
  }
  checks.push({ name: 'verify-task-v2-verdict', status: verdict?.passed ? 'passed' : 'failed' })
  const reviewerChecksPass = reviewResults.every(([, result]) =>
    !!result && Array.isArray(result.checks) && result.checks.length > 0 && result.checks.every((check) => check.status === 'passed')
  )
  const approvalEligible = tier === 'full' &&
    !!verdict?.passed &&
    !!lastRound.codexReview &&
    !!lastRound.antigravityReview &&
    !lastRound.codexReview.hasBlockingIssue &&
    !lastRound.antigravityReview.hasBlockingIssue &&
    reviewerChecksPass
  const report = {
    schema_version: 'edge_agent.code_review_report.v1',
    review_id: 'verify-task-v2-' + stableNanoTaskId(task, cwd),
    status: approvalEligible ? 'AI_APPROVED' : verdict?.needsUserDecision ? 'ESCALATED' : 'CHANGES_REQUIRED',
    target: {
      scope: 'diff',
      head_sha: realDiff.headSha,
      paths: realDiff.filesChanged || [],
    },
    findings,
    checks,
  }
  if (approvalEligible) {
    report.approval = {
      provider: 'antigravity',
      reviewed_head_sha: realDiff.headSha,
      decision_reason: 'Codex review and independent Antigravity verification passed on the same SHA.',
    }
  }
  return report
}

async function persistReviewReport(report) {
  if (!report) return { persisted: false, error: 'review report target SHA is missing' }
  const reportJson = JSON.stringify(report)
  return agent(
    [
      'Bash로 mktemp /tmp/code-review-report-XXXXXX.json 을 실행해 임시 파일 경로를 얻어.',
      '반드시 Read 툴로 그 임시 파일을 한 번 읽은 뒤 Write 툴로 아래 JSON을 글자 하나 바꾸지 않고 저장해.',
      'Bash로 node ' + CODE_REVIEW_STORE + ' --record <임시 파일 경로> 를 실행해.',
      'stdout JSON의 outcome과 reportPath를 그대로 반환하고, 저장이 성공했으면 persisted=true로 답해.',
      '임시 파일은 마지막에 삭제해. 외부 전송이나 merge는 하지 마.',
      '',
      '[리뷰 보고서 JSON]',
      reportJson,
    ].join('\n'),
    { phase: 'FullCodeReviewSkill', label: 'persist-code-review-report', schema: REVIEW_PERSIST_SCHEMA, agentType: 'general-purpose' }
  )
}

// ---------- 경량 트랙 (2026-07-27 개정에서 손 안 댐 — 그대로 유지) ----------

const LIGHT_EXEC_SCHEMA = {
  type: 'object',
  properties: { summary: { type: 'string' } },
  required: ['summary'],
}

async function lightExecute(task, context, cwd, feedback) {
  const feedbackBlock = feedback ? `\n\n[이전 라운드 코덱스 피드백 — 반영해서 수정해]\n${feedback}` : ''
  return agent(
    `아래 작업을 실제로 수행해(Edit/Write/Bash 등 필요한 도구 다 써도 됨). 작업 디렉토리: ${cwd}\n\n[작업]\n${task}\n\n[저장소 컨텍스트]\n${context.contextText}${feedbackBlock}\n\n다 하고 나서 한 일을 summary에 간결하게 적어(파일별로 뭘 했는지).`,
    { phase: 'Light', label: 'light-execute', schema: LIGHT_EXEC_SCHEMA, agentType: 'general-purpose' }
  )
}

const LIGHT_EVAL_SCHEMA = {
  type: 'object',
  properties: {
    completionCriteria: { type: 'string' },
    total: { type: 'number' },
    escapeHatch: { type: 'boolean' },
    escapeHatchReason: { type: 'string' },
    feedback: { type: 'string' },
  },
  required: ['completionCriteria', 'total', 'escapeHatch', 'feedback'],
}

function buildLightEvalPrompt(task, context, summary, realDiff) {
  return `너는 독립 채점자야. 이건 경량 트랙 작업(파일 5개 이하, 되돌리기 쉬움, 구조·보안·외부공개 영향 없는 작업으로 사전 분류됨)이라 정식 사전 스펙이 없어. 아래 절차로 해:

1. [원 작업]과 [저장소 컨텍스트]만 보고, 채점 전에 네가 판단하는 완료조건을 completionCriteria에 먼저 명시해(채점표를 사후에 유리하게 짜맞추지 말고, 원 작업 자체에서 합리적으로 도출되는 기준).
2. [실제 변경사항](git diff — 이게 진실이고, [에이전트 보고]는 참고만)을 그 completionCriteria에 대조해서 100점 만점으로 채점(total).
3. **탈출구**: 실제로 건드린 범위가 "가볍게"라는 전제를 벗어났다고 판단되면(파일이 예상보다 많다, 구조적으로 크다 등), 관대하게 채점하지 말고 escapeHatch=true로 반려해. 단, "느낌상 크다"는 안 되고, escapeHatchReason에 구체적 근거(실제 파일 수, 어떤 파일이 왜 문제인지)를 반드시 명시해야만 escapeHatch=true를 쓸 수 있어. 근거 없이는 escapeHatch=false로 두고 정상 채점해.

[원 작업]
${task}

[저장소 컨텍스트]
${context.contextText}

[에이전트 보고]
${summary}

[실제 변경사항 — git diff]
${realDiff.content}

반드시 JSON으로만 답해: {"completionCriteria":"","total":0,"escapeHatch":false,"escapeHatchReason":"","feedback":"구체적 감점 사유와 개선점"}`
}

async function codexEvaluateLight(task, context, summary, realDiff, isRetry) {
  const prompt = buildLightEvalPrompt(task, context, summary, realDiff)
  return agent(buildScoreDispatchInstruction('codex', prompt, null, 'light-eval'), {
    phase: 'Light',
    label: isRetry ? 'light-eval-codex-retry' : 'light-eval-codex',
    schema: LIGHT_EVAL_SCHEMA,
  })
}

// ---------- 전체 트랙: Claude 프롬프트화 + Antigravity 병렬 조사 ----------

const PROMPTIFY_SCHEMA = {
  type: 'object',
  properties: {
    normalizedPrompt: { type: 'string' },
    researchBrief: { type: 'string' },
    acceptanceCriteria: { type: 'array', items: { type: 'string' } },
    constraints: { type: 'array', items: { type: 'string' } },
  },
  required: ['normalizedPrompt', 'researchBrief', 'acceptanceCriteria', 'constraints'],
}

function buildPromptifyPrompt(task, context) {
  return `너는 작업 오케스트레이터인 클로드야. 사용자의 원 지시문을 조사·계획·구현 에이전트가 오해하지 않도록 실행 가능한 프롬프트로 정규화해. 아직 파일을 수정하지 말고, 사실을 새로 지어내지 마.

[사용자 원 지시문]
${task}

[저장소 컨텍스트]
${context.contextText}

normalizedPrompt에는 목표·범위·완료 조건을 포함한 정규화된 지시문을 적어.
researchBrief에는 Antigravity가 조사해야 할 핵심 질문을 적어.
acceptanceCriteria에는 구현 완료를 판정할 수 있는 조건을 적어.
constraints에는 기존 동작·호환성·안전 제약을 적어. 정보가 부족하면 추측하지 말고 "확인 필요"로 표시해.

JSON으로만 답해: {"normalizedPrompt":"","researchBrief":"","acceptanceCriteria":[],"constraints":[]}`
}

async function claudePromptifyTask(task, context) {
  return agent(buildPromptifyPrompt(task, context), {
    phase: 'FullPromptify',
    label: 'claude-promptify',
    schema: PROMPTIFY_SCHEMA,
    agentType: 'general-purpose',
  })
}

const RESEARCH_FOCI = [
  { id: 'repository', label: '저장소 구조·기존 구현·컨벤션', instruction: '관련 파일, 기존 추상화, 호출 흐름, 컨벤션과 재사용 가능한 구현을 조사해.' },
  { id: 'dependencies', label: '의존성·공식 자료·외부 정보', instruction: '사용 중인 라이브러리/프로토콜/API의 현재 제약과 공식 자료를 조사해. 불확실한 사실은 출처와 함께 표시해.' },
  { id: 'risks-tests', label: '위험·엣지케이스·테스트', instruction: '실패 경로, 보안·호환성 위험, 동시성 문제와 필요한 테스트를 조사해.' },
]

const RESEARCH_SCHEMA = {
  type: 'object',
  properties: {
    focus: { type: 'string' },
    findings: { type: 'string' },
    evidence: {
      type: 'array',
      items: {
        type: 'object',
        properties: { source: { type: 'string' }, fact: { type: 'string' }, relevance: { type: 'string' } },
        required: ['source', 'fact', 'relevance'],
      },
    },
    risks: { type: 'array', items: { type: 'string' } },
    testImplications: { type: 'array', items: { type: 'string' } },
  },
  required: ['focus', 'findings', 'evidence', 'risks', 'testImplications'],
}

function buildResearchPrompt(promptified, context, focus) {
  return `너는 Antigravity 조사 에이전트야. 코드를 수정하거나 계획을 확정하지 말고, 아래 작업을 ${focus.label} 관점에서 독립적으로 조사해. 다른 조사 에이전트의 결과는 보지 않아. 저장소 컨텍스트에 없는 사실은 추측하지 말고, 외부 자료를 확인했다면 evidence.source에 출처를 적어.

[정규화된 작업 프롬프트]
${promptified.normalizedPrompt}

[조사 브리프]
${promptified.researchBrief}

[저장소 컨텍스트]
${context.contextText}

[이번 조사 초점]
${focus.instruction}

findings에는 조사한 사실과 구현에 미치는 영향을 적고, evidence에는 근거를, risks에는 이 초점의 위험을, testImplications에는 검증에 필요한 테스트를 적어.

JSON으로만 답해: {"focus":"${focus.id}","findings":"","evidence":[{"source":"","fact":"","relevance":""}],"risks":[],"testImplications":[]}`
}

async function antigravityResearch(promptified, context, focus, isRetry) {
  return agent(buildScoreDispatchInstruction('agy', buildResearchPrompt(promptified, context, focus), null, 'research'), {
    phase: 'FullResearch',
    label: isRetry ? `antigravity-research-${focus.id}-retry` : `antigravity-research-${focus.id}`,
    schema: RESEARCH_SCHEMA,
  })
}

// ---------- 전체 트랙: Claude 병렬화 계획 ----------

const CLAUDE_PLAN_SCHEMA = {
  type: 'object',
  properties: {
    needsClarification: { type: 'boolean' },
    clarifyingQuestions: { type: 'string' },
    planSummary: { type: 'string' },
    parallelTasks: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          taskId: { type: 'string' },
          objective: { type: 'string' },
          files: { type: 'array', items: { type: 'string' } },
          dependsOn: { type: 'array', items: { type: 'string' } },
          instruction: { type: 'string' },
          doneCriteria: { type: 'string' },
          conflictNotes: { type: 'string' },
        },
        required: ['taskId', 'objective', 'files', 'dependsOn', 'instruction', 'doneCriteria', 'conflictNotes'],
      },
    },
    integrationSteps: { type: 'string' },
    testPlan: { type: 'string' },
  },
  required: ['needsClarification', 'planSummary', 'parallelTasks', 'integrationSteps', 'testPlan'],
}

function buildClaudeParallelPlanPrompt(task, context, promptified, research) {
  return `너는 클로드 계획 담당자야. Antigravity의 병렬 조사 결과를 통합해 Codex가 구현할 코딩 계획을 작성해. 계획은 실제 파일 충돌을 피하면서 독립적으로 수행할 수 있는 작업과 선행 의존성을 명확히 해야 해. 아직 파일을 수정하지 마.

[사용자 원 지시문]
${task}

[정규화된 프롬프트]
${promptified.normalizedPrompt}

[완료 조건]
${JSON.stringify(promptified.acceptanceCriteria)}

[제약]
${JSON.stringify(promptified.constraints)}

[Antigravity 병렬 조사 결과]
${JSON.stringify(research)}

정보가 부족해 안전하게 계획할 수 없으면 needsClarification=true와 질문을 최대 3개 적어. 충분하면 needsClarification=false로 하고, parallelTasks를 파일 소유권·dependsOn·충돌 가능성까지 포함해 작성해. 실제 병렬 구현이 안전하지 않은 작업은 억지로 병렬화하지 말고 dependsOn에 표시해. integrationSteps와 testPlan은 Codex가 마지막에 실행할 수 있을 정도로 구체적으로 적어.

JSON으로만 답해: {"needsClarification":false,"clarifyingQuestions":"","planSummary":"","parallelTasks":[{"taskId":"","objective":"","files":[],"dependsOn":[],"instruction":"","doneCriteria":"","conflictNotes":""}],"integrationSteps":"","testPlan":""}`
}

async function claudeBuildParallelPlan(task, context, promptified, research) {
  return agent(buildClaudeParallelPlanPrompt(task, context, promptified, research), {
    phase: 'FullPlan',
    label: 'claude-parallel-plan',
    schema: CLAUDE_PLAN_SCHEMA,
    agentType: 'general-purpose',
  })
}

// ---------- 전체 트랙: Codex 병렬 계획 검토 + 최종 계획 수정 ----------

const PLAN_REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    reviewerFocus: { type: 'string' },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: { description: { type: 'string' }, severity: { type: 'string' } },
        required: ['description'],
      },
    },
    approvedParts: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
  required: ['reviewerFocus', 'issues', 'approvedParts', 'notes'],
}

function buildCodexPlanReviewPrompt(task, context, promptified, claudePlan, focus) {
  return `너는 Codex의 독립 계획 검토자야. 아직 코드를 수정하지 말고, 클로드가 만든 병렬 코딩 계획을 ${focus} 관점에서 검토해. 다른 Codex 검토자의 결과는 보지 않아.

[원 지시문]
${task}

[정규화된 프롬프트]
${promptified.normalizedPrompt}

[저장소 컨텍스트]
${context.contextText}

[클로드 계획]
${JSON.stringify(claudePlan)}

실제 구현에서 발생할 누락·모순·파일 충돌·잘못된 의존성·테스트 공백만 issues에 적어. 문제가 없으면 빈 배열. approvedParts에는 타당한 부분을 적고, notes에는 검토 근거를 적어.

JSON으로만 답해: {"reviewerFocus":"${focus}","issues":[{"description":"","severity":""}],"approvedParts":[],"notes":""}`
}

async function codexReviewParallelPlan(task, context, promptified, claudePlan, focus, isRetry) {
  return agent(buildScoreDispatchInstruction('codex', buildCodexPlanReviewPrompt(task, context, promptified, claudePlan, focus), null, 'plan-review'), {
    phase: 'FullPlanReview',
    label: isRetry ? `codex-plan-review-${focus}-retry` : `codex-plan-review-${focus}`,
    schema: PLAN_REVIEW_SCHEMA,
  })
}

const REVISED_PLAN_SCHEMA = {
  type: 'object',
  properties: {
    compiledIssues: {
      type: 'array',
      items: {
        type: 'object',
        properties: { description: { type: 'string' }, source: { type: 'string' } },
        required: ['description', 'source'],
      },
    },
    disagreements: { type: 'string' },
    revisedPlan: { type: 'string' },
  },
  required: ['compiledIssues', 'revisedPlan'],
}

function buildCodexRevisedPlanPrompt(task, context, promptified, claudePlan, planReviews) {
  return `너는 실제 코딩을 담당할 Codex야. 클로드의 병렬 코딩 계획과 두 개의 독립적인 Codex 계획 검토 결과를 객관적으로 검토해 최종 구현 계획을 수정해. 아직 이 호출에서는 코드를 수정하지 말고 계획만 반환해.

[원 지시문]
${task}

[정규화된 프롬프트]
${promptified.normalizedPrompt}

[저장소 컨텍스트]
${context.contextText}

[클로드 계획]
${JSON.stringify(claudePlan)}

[Codex 병렬 계획 검토]
${JSON.stringify(planReviews)}

compiledIssues에는 두 검토에서 발견된 이슈를 source와 함께 전부 기록해. 타당하지 않은 지적은 disagreements에 이유를 적어. revisedPlan에는 실제 코딩 지시, 병렬 작업 단위, 작업 간 순서, 통합 단계, 테스트 명령과 완료 조건을 빠짐없이 포함해. 검토 결과를 무조건 따르지 말되, 반박 근거를 남겨야 해.

JSON으로만 답해: {"compiledIssues":[{"description":"","source":"codex-plan-review"}],"disagreements":"","revisedPlan":""}`
}

async function codexRevisePlan(task, context, promptified, claudePlan, planReviews, harnessFile, isRetry) {
  return agent(buildScoreDispatchInstruction('codex', buildCodexRevisedPlanPrompt(task, context, promptified, claudePlan, planReviews), harnessFile, 'reconcile'), {
    phase: 'FullPlanRevise',
    label: isRetry ? 'codex-revise-plan-retry' : 'codex-revise-plan',
    schema: REVISED_PLAN_SCHEMA,
  })
}

// ---------- 하네스 파일 기록 (클로드, 영구 누적) ----------

const HARNESS_APPEND_SCHEMA = {
  type: 'object',
  properties: {
    appended: { type: 'boolean' },
    rulesAdded: { type: 'array', items: { type: 'string' } },
  },
  required: ['appended'],
}

async function appendHarnessRules(issues, stageLabel, harnessFile) {
  if (!issues || issues.length === 0) return { appended: false, rulesAdded: [] }
  const issuesJson = JSON.stringify(issues)
  return agent(
    `아래는 verify-task-v2 워크플로우의 "${stageLabel}" 단계에서 발견된 결함/버그/오류 목록이야. 코덱스가 앞으로 같은 실수를 반복하지 않도록 이 내용을 영구 누적 규칙 문서에 추가해줘.

1. Read 툴로 ${harnessFile}을 읽어봐. 파일이 없으면 첫 실행이니 아래 헤더로 새로 시작해(Write 툴로 먼저 저장):
"# Codex Harness — 누적 규칙\\n\\n이 문서는 verify-task-v2 워크플로우가 발견한 결함/버그/오류를 코덱스가 앞으로 참고할 규칙으로 영구 누적한 것이다. append-only — 기존 규칙은 절대 삭제·수정하지 말 것.\\n\\n## 규칙 목록\\n"

2. 아래 [발견된 이슈]를 하나씩 검토해서, 이번 작업에만 해당하는 구체적 서술이 아니라 앞으로도 유효한 범용적 규칙 문장으로 일반화해(예: "X 파일의 Y 함수가 틀림" 같은 1회성 서술이 아니라 "~할 때는 반드시 ~해야 한다"는 규칙 형태로). 파일에 이미 사실상 동일한 규칙이 있으면 중복 추가하지 말고 건너뛰어.
3. Bash로 \`cat >> ${harnessFile} << 'HARNESSEOF'\n(일반화한 규칙들을 "- "로 시작하는 마크다운 리스트 각 줄로, 줄 끝에 "(출처: ${stageLabel})" 표기)\nHARNESSEOF\` 형태로 안전하게 append만 해 — 기존 내용은 절대 건드리지 마.
4. 실제로 새로 추가한 규칙 문장들을 rulesAdded 배열에 담아 반환해(중복이라 건너뛴 건 포함하지 마). 하나도 못 추가했으면 appended=false에 빈 배열, 하나라도 추가했으면 appended=true.

[발견된 이슈]
${issuesJson}

JSON으로만: {"appended":true,"rulesAdded":[""]}`,
    {
      phase: stageLabel.startsWith('pre-execution') ? 'FullPlanRevise' : 'FullCodeReviewSkill',
      label: `harness-append-${stageLabel}`,
      schema: HARNESS_APPEND_SCHEMA,
      agentType: 'general-purpose',
    }
  )
}

// ---------- 전체 트랙: 실행 (코덱스, 쓰기 가능) ----------

async function fullExecute(cwd, instruction, context, harnessFile) {
  const prompt = `아래 지시대로 실제로 파일을 수정/생성해줘. 작업 디렉토리: ${cwd}\n\n[저장소 컨텍스트]\n${context.contextText}\n\n[지시]\n${instruction}\n\n다 하고 나서 뭘 했는지 짧게 설명해.`
  return dispatchWithRetry(
    () => agent(buildExecuteDispatchInstruction(cwd, prompt, harnessFile), {
      phase: 'FullExecute',
      label: 'full-execute',
      schema: EXECUTE_ENVELOPE_SCHEMA,
    }),
    'full-execute'
  )
}

// ---------- 전체 트랙: 코드 리뷰 (코덱스+안티그래비티 블라인드, 무점수) ----------

const CODE_REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    hasBlockingIssue: { type: 'boolean' },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: { description: { type: 'string' }, blocking: { type: 'boolean' } },
        required: ['description', 'blocking'],
      },
    },
    notes: { type: 'string' },
    checks: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          status: { type: 'string', enum: ['passed', 'failed', 'not_run', 'error'] },
          evidence: { type: 'string' },
        },
        required: ['name', 'status'],
      },
    },
  },
  required: ['hasBlockingIssue', 'issues', 'checks'],
}

function buildReviewPrompt(task, context, realDiff) {
  return `너는 code-review 스킬 계약을 수행하는 독립 코드 리뷰어야. 점수는 매기지 않는다. Codex는 1차 리뷰어이고 Antigravity는 독립 승인 검증자다. 실제 변경사항(git diff)에서 correctness/security/robustness/performance/maintainability 문제를 찾고, 다른 리뷰어의 의견은 보지 않는다(블라인드).

[원 작업]
${task}

[저장소 컨텍스트]
${context.contextText}

[실제 변경사항 — git diff]
${realDiff.content}

1. 가능하면 저장소에서 발견한 결정론적 검사(테스트, 린터, 타입 검사)를 실행하고 checks에 실제 결과와 evidence를 적어. 실행할 명령을 찾지 못하거나 실행하지 못했으면 status=not_run/error로 적고 통과로 가장하지 마.
2. 실제로 문제가 되는 지점을 issues 배열에 담아(각 항목: description 필수, blocking — 반드시 고쳐야 할 정도면 true, 사소하면 false). 하나라도 blocking=true인 이슈가 있으면 hasBlockingIssue=true, 없으면 false. 문제가 없으면 issues는 빈 배열이고 hasBlockingIssue=false. notes에 그 외 참고할 점.

JSON으로만: {"hasBlockingIssue":false,"issues":[{"description":"","blocking":false}],"notes":"","checks":[{"name":"","status":"passed","evidence":""}]}`
}

async function claudeReviewDiff(task, context, realDiff) {
  return agent(buildReviewPrompt(task, context, realDiff), {
    phase: 'FullCodeReviewSkill',
    label: 'code-review-skill-claude',
    schema: CODE_REVIEW_SCHEMA,
    agentType: 'general-purpose',
  })
}

async function codexReviewDiff(task, context, realDiff, isRetry) {
  return agent(buildScoreDispatchInstruction('codex', buildReviewPrompt(task, context, realDiff), null, 'review'), {
    phase: 'FullCodeReviewSkill',
    label: isRetry ? 'code-review-skill-codex-retry' : 'code-review-skill-codex',
    schema: CODE_REVIEW_SCHEMA,
  })
}

async function antigravityReviewDiff(task, context, realDiff, isRetry) {
  return agent(buildScoreDispatchInstruction('agy', buildReviewPrompt(task, context, realDiff), null, 'review'), {
    phase: 'FullCodeReviewSkill',
    label: isRetry ? 'code-review-skill-antigravity-retry' : 'code-review-skill-antigravity',
    schema: CODE_REVIEW_SCHEMA,
  })
}

// ---------- 선택적 나노 게이트 트랙 (2026-07-30) ----------
const NANO_PLAN_SCHEMA = {
  type: 'object',
  properties: {
    steps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          stepId: { type: 'string' },
          taskType: { type: 'string' },
          files: { type: 'array', items: { type: 'string' } },
          instruction: { type: 'string' },
          doneCriteria: { type: 'string' },
          dependencyBoundaryCrossed: { type: 'boolean' },
        },
        required: ['stepId', 'instruction'],
      },
    },
  },
  required: ['steps'],
}

const NANO_REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    hasBlockingIssue: { type: 'boolean' },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: { description: { type: 'string' }, blocking: { type: 'boolean' } },
        required: ['description', 'blocking'],
      },
    },
    notes: { type: 'string' },
  },
  required: ['hasBlockingIssue', 'issues'],
}

const NANO_CHECK_SCHEMA = {
  type: 'object',
  properties: { ok: { type: 'boolean' }, found: { type: 'boolean' }, eventJson: { type: 'string' } },
  required: ['ok', 'found', 'eventJson'],
}

const NANO_RECORD_SCHEMA = {
  type: 'object',
  properties: { ok: { type: 'boolean' }, outcome: { type: 'string' }, idempotencyKey: { type: 'string' } },
  required: ['ok', 'outcome', 'idempotencyKey'],
}

function buildNanoPlanPrompt(task, context) {
  return [
    '아래 작업을 서로 독립적으로 검증·롤백할 수 있는 나노 스텝 목록으로 쪼개줘. 각 스텝은 앞 스텝이 통과·기록된 뒤에만 실행할 수 있어야 하고, 한 스텝의 계약이 끝난 지점에서 기존 테스트/문법검사를 실행할 수 있어야 해.',
    '',
    '[원 작업]',
    task,
    '',
    '[저장소 컨텍스트]',
    context.contextText,
    '',
    '최대 32개 steps 배열만 JSON으로 반환해. 각 항목에는 stepId(재시작해도 변하지 않는 짧은 영문/숫자 id), taskType, files 배열, instruction, doneCriteria, dependencyBoundaryCrossed(boolean)를 넣어.',
    '예시: {"steps":[{"stepId":"step-1","taskType":"code","files":["path"],"instruction":"","doneCriteria":"","dependencyBoundaryCrossed":false}]}',
  ].join('\n')
}

async function codexNanoPlan(task, context, harnessFile) {
  return agent(buildScoreDispatchInstruction('codex', buildNanoPlanPrompt(task, context), harnessFile, 'nano-plan'), {
    phase: 'FullPlan',
    label: 'nano-plan',
    schema: NANO_PLAN_SCHEMA,
  })
}

function normalizeNanoSteps(rawSteps) {
  if (!Array.isArray(rawSteps) || rawSteps.length === 0 || rawSteps.length > 32) return null
  const seen = new Set()
  const steps = []
  for (const raw of rawSteps) {
    if (!raw || typeof raw !== 'object') return null
    const stepId = typeof raw.stepId === 'string' ? raw.stepId.trim() : ''
    const instruction = typeof raw.instruction === 'string' ? raw.instruction.trim() : ''
    if (!stepId || !instruction || seen.has(stepId)) return null
    seen.add(stepId)
    steps.push({
      stepId,
      taskType: typeof raw.taskType === 'string' && raw.taskType.trim() ? raw.taskType.trim() : 'code-change',
      files: Array.isArray(raw.files) ? raw.files.filter((file) => typeof file === 'string') : [],
      instruction,
      doneCriteria: typeof raw.doneCriteria === 'string' ? raw.doneCriteria : '',
      dependencyBoundaryCrossed: raw.dependencyBoundaryCrossed === true,
    })
  }
  return steps
}

function stableNanoTaskId(task, cwd) {
  let hash = 2166136261
  for (const char of `${cwd}\\0${task}`) {
    hash ^= char.charCodeAt(0)
    hash = Math.imul(hash, 16777619)
  }
  return `nano-${(hash >>> 0).toString(16)}`
}

function nanoText(value, fallback) {
  const textValue = String(value || fallback || '').replace(/[\r\n]+/g, ' ').trim()
  return textValue.slice(0, 9000) || String(fallback || 'unspecified')
}

function lowestNanoHeadroom(providerHeadroom) {
  if (!providerHeadroom || typeof providerHeadroom !== 'object') return undefined
  const values = ['claude', 'codex', 'antigravity']
    .map((provider) => providerHeadroom[provider])
    .filter((value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100)
  return values.length ? Math.min(...values) : undefined
}

// decide-risk-tier.js의 파일 분류 기준과 동기화한다(Workflow 샌드박스는
// 외부 모듈을 import할 수 없어 의도적으로 인라인 복제).
const NANO_FULL_FILE_PATTERNS = [
  /(^|\/)(auth|authentication|authorization|routing|router|security|permissions?)(\/|\.|$)/i,
  /(^|\/)(config|configuration|deploy|deployment|infra|infrastructure|migrations?)(\/|\.|$)/i,
  /(^|\/)(Dockerfile|docker-compose(?:\..+)?|Makefile|package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$/i,
  /(^|\/)(package\.json|pyproject\.toml|Cargo\.toml|go\.mod|requirements\.txt)$/i,
]
const NANO_MID_CODE_EXTENSIONS = new Set(['.c', '.cc', '.cpp', '.cs', '.go', '.java', '.js', '.jsx', '.mjs', '.py', '.rb', '.rs', '.sh', '.swift', '.ts', '.tsx', '.vue'])
const NANO_LIGHT_FILE_PATTERNS = [
  /(^|\/)(test|tests|__tests__|fixtures?|snapshots?)(\/|\.|$)/i,
  /\.(md|mdx|txt|rst|adoc)$/i,
]
function classifyNanoFile(filePath) {
  const normalized = String(filePath || '').replaceAll('\\', '/')
  if (!normalized) return 'light'
  if (isSensitivePath(normalized) || NANO_FULL_FILE_PATTERNS.some((pattern) => pattern.test(normalized))) return 'full'
  if (NANO_LIGHT_FILE_PATTERNS.some((pattern) => pattern.test(normalized))) return 'light'
  const dot = normalized.lastIndexOf('.')
  const extension = dot >= 0 ? normalized.slice(dot).toLowerCase() : ''
  return NANO_MID_CODE_EXTENSIONS.has(extension) ? 'mid' : 'light'
}

function detectNanoDependencyBoundary(changedFiles, dependencyEdges = []) {
  const files = new Set(Array.isArray(changedFiles) ? changedFiles : [])
  const topLevel = (file) => String(file || '').replaceAll('\\', '/').split('/')[0] || ''
  if (Array.isArray(dependencyEdges) && dependencyEdges.some((edge) => Array.isArray(edge) && edge.length >= 2 && files.has(edge[0]) && files.has(edge[1]) && topLevel(edge[0]) !== topLevel(edge[1]))) return true
  const hasManifest = [...files].some((file) => /(^|\/)(package\.json|pyproject\.toml|Cargo\.toml|go\.mod|requirements\.txt)$/.test(file))
  return hasManifest && [...files].some((file) => classifyNanoFile(file) === 'mid')
}

// Workflow 샌드박스에서는 외부 모듈을 import할 수 없으므로 위험도 함수의
// 우선순위/임계값을 workflows/lib/decide-risk-tier.js와 의도적으로 복제한다.
function decideNanoRiskTier(input) {
  const { stepFileCount = 0, cumulativeFileCount = 0, sensitivePath = false, dependencyBoundaryCrossed = false, dependencyEdges, remainingTokenPct, providerHeadroom, changedFiles = [] } = input || {}
  const providerHeadroomPct = lowestNanoHeadroom(providerHeadroom)
  const singlePct = typeof remainingTokenPct === 'number' && Number.isFinite(remainingTokenPct) && remainingTokenPct >= 0 && remainingTokenPct <= 100 ? remainingTokenPct : undefined
  const tokenPct = providerHeadroomPct ?? singlePct
  const fileTiers = (Array.isArray(changedFiles) ? changedFiles : []).map(classifyNanoFile)
  if (sensitivePath || fileTiers.includes('full')) return 'full'
  if (fileTiers.includes('mid')) return 'mid'
  if (dependencyBoundaryCrossed || detectNanoDependencyBoundary(changedFiles, dependencyEdges)) return 'mid'
  if (cumulativeFileCount > 3) return 'mid'
  if (stepFileCount > 3) return 'mid'
  if (tokenPct !== undefined && tokenPct <= 10) return 'mid'
  return 'light'
}

async function checkNanoEvent(eventFile, idempotencyKey) {
  return agent(
    `Bash로 node ${NANO_EVENT_RECORDER} --check ${JSON.stringify(eventFile)} ${JSON.stringify(idempotencyKey)} 를 실행하고 stdout JSON을 그대로 반환해. 파일은 수정하지 마.`,
    { phase: 'Light', label: 'nano-event-check', schema: NANO_CHECK_SCHEMA, agentType: 'general-purpose' }
  )
}

async function recordNanoEvent(eventFile, event) {
  const eventJson = JSON.stringify(event)
  return agent(
    `mktemp로 임시 JSON 파일을 만들고, 반드시 Read 툴로 그 빈 임시 파일을 한 번 읽은 뒤 Write로 아래 이벤트를 그대로 저장해(Claude의 Write 계약상 먼저 읽은 파일만 Write할 수 있음). 그 다음 node ${NANO_EVENT_RECORDER} ${JSON.stringify(eventFile)} <임시파일> 를 실행해. stdout JSON을 그대로 반환하고 임시 파일은 삭제해.\n\n${eventJson}`,
    { phase: 'Light', label: 'nano-event-record', schema: NANO_RECORD_SCHEMA, agentType: 'general-purpose' }
  )
}

async function nanoLightValidate(task, step, context, realDiff) {
  const prompt = [
    '너는 나노 스텝의 독립 light 검증자야. 파일을 수정하지 말고, 아래 실제 diff와 완료조건만 검토해. 점수는 매기지 않는다. 변경이 완료조건을 충족하고 명백한 문법/연동 결함이 없으면 hasBlockingIssue=false, 그렇지 않으면 실제 근거와 함께 blocking=true 이슈를 적어. 제공된 diff 밖의 사실은 추측하지 마.',
    '',
    '[원 작업]', task,
    '',
    '[현재 나노 스텝]', JSON.stringify(step),
    '',
    '[저장소 컨텍스트]', context.contextText,
    '',
    '[실제 변경사항]', realDiff?.content || '(diff 없음)',
    '',
    '마지막 응답은 반드시 다른 설명 없이 아래 세 키를 모두 포함한 JSON 객체 하나여야 해(필드 누락 금지): {"hasBlockingIssue":false,"issues":[],"notes":""}',
  ].join('\n')
  return agent(buildScoreDispatchInstruction('codex', prompt, null, 'review'), {
    phase: 'Light',
    label: 'nano-light-validate',
    schema: NANO_REVIEW_SCHEMA,
  })
}

async function nanoIntegrationValidate(task, context, realDiff, tier) {
  if (tier === 'light') return { ok: true, issues: [], reviewers: ['codex'] }
  if (tier === 'mid') {
    const review = await codexReviewDiff(task, context, realDiff, false)
    if (!review) return { ok: false, reason: 'mid 통합 리뷰어가 결과를 반환하지 않음' }
    return { ok: !review.hasBlockingIssue, issues: (review.issues || []).map((issue) => ({ ...issue, source: 'codex' })), reviewers: ['codex'] }
  }
  const [codexReview, antigravityReview] = await parallel([
    () => codexReviewDiff(task, context, realDiff, false),
    () => antigravityReviewDiff(task, context, realDiff, false),
  ])
  if (!codexReview || !antigravityReview) return { ok: false, reason: 'full 통합 리뷰어 중 하나 이상이 결과를 반환하지 않음' }
  const issues = [
    ...(codexReview.issues || []).map((issue) => ({ ...issue, source: 'codex' })),
    ...(antigravityReview.issues || []).map((issue) => ({ ...issue, source: 'antigravity' })),
  ]
  return { ok: !codexReview.hasBlockingIssue && !antigravityReview.hasBlockingIssue, issues, reviewers: ['codex', 'antigravity'] }
}

// Workflow 스크립트는 Date.now()/argless new Date()를 못 쓴다(resume 재현성이
// 깨짐) — 그래서 시각은 항상 `date -u +%s` Bash 호출(nanoEpochSeconds)로
// 받아온 정수 초를 넘겨받는다. new Date(epochMs)처럼 인자가 있는 형태는
// 결정적이라 허용되므로 recordedAt 변환에는 그대로 쓴다.
async function nanoEpochSeconds() {
  const result = await agent(
    'Bash로 정확히 `date -u +%s`만 실행해서 나온 정수(초 단위 UTC epoch)를 다른 텍스트 없이 seconds 필드(숫자)에 그대로 반환해.',
    {
      phase: 'Light',
      label: 'nano-epoch-seconds',
      agentType: 'general-purpose',
      schema: { type: 'object', properties: { seconds: { type: 'number' } }, required: ['seconds'] },
    }
  )
  return typeof result?.seconds === 'number' && Number.isFinite(result.seconds) ? result.seconds : null
}

function makeNanoEvent(taskId, step, status, tier, changedFiles, agents, reason, startedAtSeconds, nowSeconds, tokenUsage, riskInputs = null) {
  const durationMs = Number.isFinite(startedAtSeconds) && Number.isFinite(nowSeconds)
    ? Math.max(0, Math.round((nowSeconds - startedAtSeconds) * 1000))
    : 0
  return {
    schemaVersion: 1,
    eventType: 'nano_step',
    taskId,
    stepId: step.stepId,
    idempotencyKey: `${taskId}::${step.stepId}`,
    taskType: nanoText(step.taskType, 'code-change'),
    changedFiles: [...new Set(changedFiles.filter((file) => typeof file === 'string'))],
    agents: [...new Set(agents)],
    verificationTier: tier,
    status,
    reason: nanoText(reason, status === 'passed' ? 'nano step passed' : 'nano step failed'),
    durationMs,
    tokenUsage: tokenUsage && typeof tokenUsage === 'object' && !Array.isArray(tokenUsage) ? tokenUsage : null,
    riskInputs,
    preventionRules: [],
    recordedAt: Number.isFinite(nowSeconds) ? new Date(nowSeconds * 1000).toISOString() : null,
  }
}

async function recordNanoOutcome(eventFile, event) {
  const recorded = await recordNanoEvent(eventFile, event)
  if (!recorded || recorded.ok !== true) return { ok: false, reason: '나노 이벤트 기록기에서 성공 응답을 받지 못함' }
  return { ok: true, recorded }
}

async function runNanoGate(task, context, cwd, harnessFile, eventFile, options = {}) {
  let steps = normalizeNanoSteps(options.nanoSteps)
  if (!steps) {
    const plan = await codexNanoPlan(task, context, harnessFile)
    if (!plan || isDispatchFailure(plan)) {
      return {
        history: [],
        finalVerdict: { passed: false, tier: 'nano', error: 'nano_plan_failed', reason: isDispatchFailure(plan) ? `나노 계획 도구 실패: ${plan.dispatchFailureReason}` : '나노 계획을 받지 못함', needsUserDecision: true },
      }
    }
    steps = normalizeNanoSteps(plan.steps)
  }
  if (!steps) {
    return {
      history: [],
      finalVerdict: { passed: false, tier: 'nano', error: 'nano_plan_invalid', reason: '나노 계획에 중복/빈 stepId 또는 instruction이 있음', needsUserDecision: true },
    }
  }

  const taskId = options.taskId || stableNanoTaskId(task, cwd)
  const tokenUsage = options.tokenUsage || null
  const providerHeadroom = options.providerHeadroom
  const remainingTokenPct = options.remainingTokenPct
  const history = []
  const cumulativeFiles = new Set()
  const baseline = await gatherRealDiff(cwd)
  const baselineFiles = new Set(baseline?.filesChanged || [])

  for (const step of steps) {
    const startedAtSeconds = await nanoEpochSeconds()
    const idempotencyKey = `${taskId}::${step.stepId}`
    const existing = await checkNanoEvent(eventFile, idempotencyKey)
    if (!existing || existing.ok !== true) {
      return { history, finalVerdict: { passed: false, tier: 'nano', error: 'nano_event_check_failed', stepId: step.stepId, reason: '기존 나노 이벤트 조회 실패 — 안전을 위해 실행을 중단함', needsUserDecision: true } }
    }
    if (existing.found) {
      let previous
      try { previous = JSON.parse(existing.eventJson) } catch (error) { previous = null }
      if (!previous || previous.status !== 'passed') {
        return { history, finalVerdict: { passed: false, tier: 'nano', error: 'nano_step_previously_failed', stepId: step.stepId, reason: '동일 나노 스텝의 실패 이벤트가 이미 존재함 — 자동 재실행하지 않음', needsUserDecision: true } }
      }
      history.push({ stepId: step.stepId, reused: true, event: previous })
      for (const file of previous.changedFiles || []) cumulativeFiles.add(file)
      if (previous.verificationTier !== 'light') cumulativeFiles.clear()
      continue
    }

    log(`[나노] ${step.stepId}: 계약 단위 실행`)
    const executionInstruction = `${step.instruction}\n\n[완료조건]\n${step.doneCriteria}`
    let execution = await fullExecute(cwd, executionInstruction, context, harnessFile)
    let realDiff = await gatherRealDiff(cwd)
    // A successful dispatcher response is not proof that Codex wrote files.
    // If the declared target is still absent from the real diff, issue one
    // explicit corrective execution before invoking the reviewer. This turns
    // a silent no-op into a bounded recovery attempt while preserving the
    // independent diff-based safety check.
    const declaredFilesForRetry = Array.isArray(step.files) ? step.files : []
    const actualFilesAfterFirstExecution = Array.isArray(realDiff?.filesChanged) ? realDiff.filesChanged : []
    const missingDeclaredFiles = declaredFilesForRetry.filter((file) => !actualFilesAfterFirstExecution.includes(file))
    if (execution?.ok !== false && missingDeclaredFiles.length > 0) {
      log(`[나노] ${step.stepId}: 실행 응답은 왔지만 실제 diff 없음 — 1회 교정 실행`)
      execution = await fullExecute(
        cwd,
        `${executionInstruction}\n\n[교정 실행]\n앞선 실행 후 실제 git diff에 변경이 없습니다. 이번에는 반드시 위 작업을 실제 파일에 적용하고, 완료 전에 git diff -- ${missingDeclaredFiles.join(' ')} 로 변경을 확인해.`,
        context,
        harnessFile
      )
      realDiff = await gatherRealDiff(cwd)
    }
    const actualFiles = Array.isArray(realDiff?.filesChanged) ? realDiff.filesChanged : []
    const declaredFiles = Array.isArray(step.files) ? step.files : []
    const changedFiles = [...new Set([
      ...declaredFiles.filter((file) => actualFiles.includes(file)),
      ...actualFiles.filter((file) => !baselineFiles.has(file)),
      ...declaredFiles,
    ])]
    for (const file of changedFiles) cumulativeFiles.add(file)

    const riskInputs = {
      stepFileCount: changedFiles.length,
      cumulativeFileCount: cumulativeFiles.size,
      sensitivePath: !!realDiff?.sensitivePath,
      dependencyBoundaryCrossed: step.dependencyBoundaryCrossed,
      dependencyEdges: step.dependencyEdges,
      remainingTokenPct,
      providerHeadroom,
    }

    const finishFailure = async (errorCode, reason, tier = 'light', issues = []) => {
      const failedAtSeconds = await nanoEpochSeconds()
      const event = makeNanoEvent(taskId, step, 'failed', tier, changedFiles, ['codex'], reason, startedAtSeconds, failedAtSeconds, tokenUsage, riskInputs)
      event.preventionRules = issues.map((issue) => nanoText(issue.description || issue, 'review issue')).slice(0, 20)
      const recorded = await recordNanoOutcome(eventFile, event)
      if (!recorded.ok) {
        return { history, finalVerdict: { passed: false, tier: 'nano', error: 'nano_event_record_failed', stepId: step.stepId, reason: `${errorCode}: ${reason}; 실패 이벤트 기록도 실패하여 중단함`, needsUserDecision: true } }
      }
      history.push({ stepId: step.stepId, event, error: errorCode, issues })
      return { history, finalVerdict: { passed: false, tier: 'nano', error: errorCode, stepId: step.stepId, reason, issues, needsUserDecision: true } }
    }

    if (!execution || execution.ok === false) return await finishFailure('nano_execute_failed', '계약 단위 실행이 성공 응답을 반환하지 않음')

    const lightReview = await nanoLightValidate(task, step, context, realDiff)
    if (!lightReview || isDispatchFailure(lightReview)) return await finishFailure('nano_light_validation_failed', 'light 검증 도구가 결과를 반환하지 않음')
    if (lightReview.hasBlockingIssue) return await finishFailure('nano_light_blocked', 'light 검증에서 블로킹 이슈가 발견됨', 'light', lightReview.issues || [])

    const tier = decideNanoRiskTier(riskInputs)
    const integration = await nanoIntegrationValidate(task, context, realDiff, tier)
    if (!integration.ok) return await finishFailure('nano_integration_blocked', integration.reason || '통합 검증에서 블로킹 이슈가 발견됨', tier, integration.issues || [])

    const passedAtSeconds = await nanoEpochSeconds()
    const event = makeNanoEvent(taskId, step, 'passed', tier, changedFiles, ['codex', ...integration.reviewers], 'light 및 필요한 통합 검증 통과', startedAtSeconds, passedAtSeconds, tokenUsage, riskInputs)
    const recorded = await recordNanoOutcome(eventFile, event)
    if (!recorded.ok) {
      return { history, finalVerdict: { passed: false, tier: 'nano', error: 'nano_event_record_failed', stepId: step.stepId, reason: '검증은 통과했지만 이벤트 기록에 실패하여 다음 스텝으로 진행하지 않음', needsUserDecision: true } }
    }
    history.push({ stepId: step.stepId, event, verification: { lightReview, integration } })
    if (tier !== 'light') cumulativeFiles.clear()
  }

  return { history, finalVerdict: { passed: true, tier: 'nano', steps: history.length, taskId } }
}

function formatFixInstruction(combinedIssues) {
  return combinedIssues.map((it, i) => `${i + 1}. [${it.source}] ${it.description}`).join('\n')
}

// ---------- 히스토리 로깅 ----------

async function appendHistory(record, historyFile) {
  const recordJson = JSON.stringify(record)
  await agent(
    `아래 JSON 레코드 한 건을 히스토리 로그 파일에 한 줄(JSONL)로 추가(append)해줘. 기존 파일 내용은 절대 건드리지 말고 끝에 한 줄만 추가해.\n\n1. Bash로 \`mkdir -p $(dirname ${historyFile})\` 실행.\n2. Bash로 \`date -u +%Y-%m-%dT%H:%M:%SZ\` 실행해서 현재 UTC 시각을 얻어.\n3. 아래 JSON에 "timestamp" 필드로 그 시각을 추가한 뒤, 한 줄짜리 JSON 문자열로 만들어서 Bash \`cat >> ${historyFile} << 'HISTEOF'\n(그 JSON 한 줄)\nHISTEOF\` 형태로 안전하게 append 해.\n4. 성공하면 "ok"만 반환해.\n\n원본 JSON (timestamp 필드만 추가하고 나머지는 그대로 유지):\n${recordJson}`,
    { phase: 'FullCodeReviewSkill', label: 'history-append', agentType: 'general-purpose' }
  )
}

// ==================== 메인 흐름 ====================

const parsedArgs = typeof args === 'string' ? JSON.parse(args) : (args || {})
const MAX_ROUNDS = parsedArgs.maxRounds || 2
const task = parsedArgs.task
const persona = parsedArgs.persona || '일반 사용자'
const cwd = parsedArgs.cwd
const historyFile = parsedArgs.historyFile || `${CLAUDE_HOME}/.claude/verify-task-v2-history.jsonl`
const HARNESS_FILE = parsedArgs.harnessFile || HARNESS_FILE_DEFAULT
const NANO_MODE = parsedArgs.nanoMode === true || Array.isArray(parsedArgs.nanoSteps)
const NANO_EVENT_FILE = parsedArgs.nanoEventFile || NANO_EVENT_FILE_DEFAULT

if (!cwd) {
  return {
    finalVerdict: { passed: false, error: 'missing_cwd' },
    reason: 'verify-task-v2는 컨텍스트 수집(git status/diff)이 반드시 필요해서 cwd가 필수야. verify-task(v1)와 다름 — v1은 cwd 선택, v2는 필수.',
  }
}

// v1(verify-task.js)에서 실측 확인된 것과 같은 버그 클래스에 대한 대칭 방어:
// Workflow({scriptPath, resumeFromRunId})로 재개할 때 args를 다시 안 넘기면
// parsedArgs가 {}로 무너진다. v2는 cwd 필수라 위 가드가 대부분 이 경로를 우연히
// 막아주지만, task만 비고 cwd는 어쩌다 남아있는 경로까지 커버하려면 별도 확인이
// 필요하다 — task 없이 진행하면 코덱스 계획 작성 단계 프롬프트에 "undefined"가
// 실제로 들어간 채 외부 호출이 나간다.
if (!task) {
  return {
    finalVerdict: { passed: false, error: 'missing_task' },
    reason: `task가 비어있음(cwd=${JSON.stringify(cwd)}) — args가 실제로 전달되지 않았을 가능성이 높음. Workflow({scriptPath, resumeFromRunId})로 재개하는 경우에도 args는 매번 다시 넘겨야 함.`,
  }
}

log('사전 점검: Codex/Antigravity 로그인 상태 확인 중...')
const preflight = await preflightCheck()
if (!preflight || preflight.ok === false) {
  const issues = preflight?.issues || '사전 점검 응답을 받지 못함'
  log(`사전 점검 실패: ${issues}`)
  return { finalVerdict: { passed: false, error: 'preflight_failed', issues }, history: [] }
}
log('사전 점검 통과')

log('컨텍스트 수집 중 (git status/diff, 관련 파일, 컨벤션, 테스트 명령)...')
const context = await gatherContext(cwd, task)
if (!context) {
  return { finalVerdict: { passed: false, error: 'context_gathering_failed' } }
}
// 실측 감사로 발견한 잠재 버그(2026-07-30, 아직 실제로는 안 터짐): cwd
// 존재 여부를 한 번도 확인 안 했다 — 특히 Discord 답장 재시도 경로에서
// pending-job에 저장된 cwd(예: /private/tmp/.../scratchpad/...)가 세션
// 종료 후 정리돼서 사라진 채로 답장이 오면, cd가 조용히 실패하고 이후
// 전부 빈 컨텍스트로 계속 진행돼서 "완료" 판정이 날 수 있었다. 이제
// gatherContext 스스로 존재 여부를 확인해서 보고하므로, 여기서 즉시
// 막는다.
if (context.cwdExists === false) {
  return {
    finalVerdict: {
      passed: false,
      error: 'cwd_not_found',
      reason: `cwd가 존재하지 않음: ${cwd}. Discord 답장 재시도라면 원본 pending-job이 가리키던 디렉토리(예: 임시 스크래치패드)가 이미 정리됐을 수 있음 — 유효한 cwd로 다시 시도할 것.`,
    },
    history: [],
  }
}

let tier = decideTier(context)
log(`티어 판정: ${tier} (예상 파일 ${context.intendedFiles?.length ?? '?'}개, 민감경로: ${context.sensitivePath})`)

const history = []
let finalVerdict = null
let baseline = null // 탈출구 발동 시 경량 트랙 산출물을 여기 보관 — 전체 트랙에서 사전 계획/조사를 생략하고 바로 코드리뷰로 직행하는 데 씀
let realDiff = null
let harnessRulesAddedCount = 0

if (NANO_MODE) {
  log('[나노] 나노 게이트 트랙 시작')
  const nanoResult = await runNanoGate(task, context, cwd, HARNESS_FILE, NANO_EVENT_FILE, {
    nanoSteps: parsedArgs.nanoSteps,
    taskId: parsedArgs.taskId,
    tokenUsage: parsedArgs.tokenUsage,
    providerHeadroom: parsedArgs.providerHeadroom,
    remainingTokenPct: parsedArgs.remainingTokenPct,
  })
  tier = 'nano'
  history.push(...(nanoResult.history || []))
  finalVerdict = nanoResult.finalVerdict
  return await finalizeAndReturn()
}

// ---------- 경량 트랙 ----------
if (tier === 'light') {
  let feedback = null
  for (let round = 1; round <= MAX_ROUNDS; round++) {
    log(`[경량] 라운드 ${round}: 클로드 실행 중...`)
    const execResult = await lightExecute(task, context, cwd, feedback)
    const realDiff = await gatherRealDiff(cwd)

    log(`[경량] 라운드 ${round}: 코덱스 평가 중...`)
    const evalResult = await dispatchWithRetry(
      (isRetry) => codexEvaluateLight(task, context, execResult?.summary, realDiff, isRetry),
      '코덱스 경량 평가'
    )
    history.push({ tier: 'light', round, execResult, evalResult })

    // 2026-07-29 fix: `dispatchWithRetry`가 재시도까지 다 쓰고도 채점
    // dispatch 자체가 실패하면(도구 실행/파싱 실패) evalResult는
    // dispatchFailed:true 봉투를 그대로 갖고 있는데, 아래 원래 로직은
    // `evalResult?.total ?? 0`으로 이를 조용히 "진짜 0점"으로 취급했다 —
    // MAX_ROUNDS 소진 후 "90점을 못 넘김"이라는 사실과 다른 사유로
    // needsUserDecision이 뜨는 오분류. FullPlan/FullPlanRevise/FullResearch
    // 단계는 이미 isDispatchFailure()로 이 구분을 명시적으로 하는데(같은
    // 파일 위쪽 참고), 경량 트랙만 이 처리가 빠져 있었다 — 여기서도 동일하게
    // dispatch 실패와 진짜 낮은 점수를 구분한다.
    if (isDispatchFailure(evalResult)) {
      if (round === MAX_ROUNDS) {
        finalVerdict = {
          passed: false,
          tier: 'light',
          round,
          execResult,
          evalResult,
          error: 'light_eval_dispatch_failed',
          needsUserDecision: true,
          reason: `경량 트랙 최대 ${MAX_ROUNDS}라운드 안에 코덱스 채점 도구 실행/파싱이 계속 실패함(${evalResult.dispatchFailureReason}) — 실제로 낮은 점수를 받은 게 아니라 채점 자체가 안 된 상태이니 통과/실패 어느 쪽으로도 간주하면 안 됨. 같은 task로 재시도할 것.`,
        }
        break
      }
      log(`[경량] 라운드 ${round}: 코덱스 채점 도구 실행/파싱 실패(${evalResult.dispatchFailureReason}) — 채점 결과 없이 재시도`)
      feedback = null
      continue
    }

    const mechViolated = mechanicalTierViolated(realDiff)
    const codexEscapeHatch = !!evalResult?.escapeHatch && !!evalResult?.escapeHatchReason
    if (mechViolated || codexEscapeHatch) {
      log(
        `[경량] 탈출구 발동 — ${mechViolated ? '기계적 규칙 위반(실제 파일 ' + realDiff.filesChanged.length + '개/민감경로 ' + realDiff.sensitivePath + ')' : '코덱스 판단: ' + evalResult.escapeHatchReason}. 전체 트랙으로 재분류, 기존 산출물은 베이스라인으로 재사용.`
      )
      baseline = execResult
      tier = 'full'
      break
    }

    if ((evalResult?.total ?? 0) >= 90) {
      finalVerdict = { passed: true, tier: 'light', round, execResult, evalResult }
      log(`[경량] 라운드 ${round}에서 통과 (${evalResult.total}점)`)
      break
    }

    if (round === MAX_ROUNDS) {
      finalVerdict = {
        passed: false,
        tier: 'light',
        round,
        execResult,
        evalResult,
        needsUserDecision: true,
        reason: `경량 트랙 최대 ${MAX_ROUNDS}라운드 안에 90점을 못 넘김. 호출한 에이전트는 사용자에게 물어야 함: (a) 현재 결과물 수용 (b) maxRounds 늘려 재시도 (c) 수동 개입.`,
      }
      break
    }

    log(`[경량] 라운드 ${round} 미통과 (${evalResult?.total ?? '?'}점) — 피드백 반영해서 재시도`)
    feedback = evalResult?.feedback ?? ''
  }
}

// ---------- 전체 트랙 (직접 진입 또는 경량→전체 탈출구 재분류) ----------
// 2026-08-01 개정: 프롬프트화→Antigravity 병렬 조사→Claude 병렬화 계획→
// Codex 병렬 계획 검토→Codex 최종 계획 수정/구현→code-review 스킬 순서.
// 탈출구로 들어온 경우 이미 실제 코드가 있으므로 사전 계획/조사를 생략하고
// code-review 스킬로 직행한다.
if (tier === 'full' && !finalVerdict) {
  if (baseline) {
    log('[전체] 탈출구 경로 — 사전 계획/조사 생략, code-review 스킬로 직행')
    realDiff = await gatherRealDiff(cwd)
  } else {
    log('[전체] 1단계: 클로드가 사용자 지시문을 실행 프롬프트로 정규화...')
    const promptified = await claudePromptifyTask(task, context)
    if (!promptified) {
      finalVerdict = {
        passed: false,
        tier: 'full',
        error: 'promptify_failed',
        reason: '클로드 프롬프트화 단계가 실패함 — 정규화되지 않은 지시문으로 조사를 시작하지 않도록 중단함. 같은 task로 워크플로우를 재시도할 것.',
      }
      log('[전체] 1단계: 프롬프트화 실패 — 재시도 필요')
      return await finalizeAndReturn()
    }

    log('[전체] 2단계: Antigravity가 개선 자료를 병렬 조사...')
    const researchResults = await parallel(
      RESEARCH_FOCI.map((focus) =>
        () => dispatchWithRetry(
          (isRetry) => antigravityResearch(promptified, context, focus, isRetry),
          `Antigravity 조사(${focus.id})`
        )
      )
    )
    const failedResearch = researchResults
      .map((result, index) => (!result || isDispatchFailure(result) ? RESEARCH_FOCI[index].id : null))
      .filter(Boolean)
    if (failedResearch.length) {
      finalVerdict = {
        passed: false,
        tier: 'full',
        error: 'research_failed',
        reason: `Antigravity 병렬 조사 실패(${failedResearch.join(', ')}) — 조사 결과 없이 계획을 만들지 않도록 중단함. 같은 task로 워크플로우를 재시도할 것.`,
      }
      log(`[전체] 2단계: 병렬 조사 실패(${failedResearch.join(', ')}) — 재시도 필요`)
      return await finalizeAndReturn()
    }

    log('[전체] 3단계: 클로드가 병렬 실행 가능한 코딩 계획 수립...')
    const claudePlan = await claudeBuildParallelPlan(task, context, promptified, researchResults)
    if (!claudePlan) {
      finalVerdict = {
        passed: false,
        tier: 'full',
        error: 'plan_failed',
        reason: '클로드 병렬 코딩 계획 단계가 실패함 — 계획 없이 Codex를 실행하지 않도록 중단함. 같은 task로 워크플로우를 재시도할 것.',
      }
      log('[전체] 3단계: 병렬 계획 실패 — 재시도 필요')
      return await finalizeAndReturn()
    }

    if (claudePlan?.needsClarification) {
      finalVerdict = {
        passed: false,
        tier: 'full',
        error: 'needs_clarification',
        questions: claudePlan.clarifyingQuestions,
        reason: '클로드가 계획 작성에 필요한 정보를 부족하다고 판단함. 호출한 에이전트가 questions를 사용자에게 물어보고 답변을 원 task에 덧붙여 이 워크플로우를 다시 호출해야 함.',
      }
      log('[전체] 3단계: 정보 부족 — 역질문 필요')
      return await finalizeAndReturn()
    }

    log('[전체] 4단계: Codex가 계획을 병렬 검토...')
    const planReviews = await parallel([
      () => dispatchWithRetry(
        (isRetry) => codexReviewParallelPlan(task, context, promptified, claudePlan, '아키텍처·의존성·파일 충돌', isRetry),
        'Codex 계획 검토(아키텍처)'
      ),
      () => dispatchWithRetry(
        (isRetry) => codexReviewParallelPlan(task, context, promptified, claudePlan, '구현 가능성·엣지케이스·테스트', isRetry),
        'Codex 계획 검토(구현/테스트)'
      ),
    ])
    const failedPlanReviews = planReviews.filter((result) => !result || isDispatchFailure(result)).length
    if (failedPlanReviews) {
      finalVerdict = {
        passed: false,
        tier: 'full',
        error: 'plan_review_failed',
        reason: `Codex 병렬 계획 검토 ${failedPlanReviews}건이 실패함 — 검토되지 않은 계획으로 실행하지 않도록 중단함. 같은 task로 워크플로우를 재시도할 것.`,
      }
      log(`[전체] 4단계: Codex 계획 검토 실패(${failedPlanReviews}건) — 재시도 필요`)
      return await finalizeAndReturn()
    }

    log('[전체] 5단계: Codex가 검토 결과를 반영해 최종 계획 수정...')
    const reconciled = await dispatchWithRetry(
      (isRetry) => codexRevisePlan(task, context, promptified, claudePlan, planReviews, HARNESS_FILE, isRetry),
      'Codex 최종 계획 수정'
    )

    if (!reconciled || isDispatchFailure(reconciled)) {
      finalVerdict = {
        passed: false,
        tier: 'full',
        error: 'codex_plan_revision_failed',
        reason: isDispatchFailure(reconciled)
          ? `Codex 최종 계획 수정 단계가 도구 실행/파싱 실패로 1회 재시도 후에도 실패함: ${reconciled.dispatchFailureReason}. 같은 task로 워크플로우를 재시도할 것.`
          : 'Codex 최종 계획 수정 단계가 실패함(일시적 오류일 가능성 높음) — 같은 task로 워크플로우를 재시도할 것.',
      }
      log('[전체] 5단계: Codex 최종 계획 수정 실패 — 재시도 필요')
      return await finalizeAndReturn()
    }

    log('[전체] 하네스 규칙 추가 (계획 검토 단계)...')
    const harnessResultPre = await appendHarnessRules(reconciled?.compiledIssues, 'pre-execution-plan-review', HARNESS_FILE)
    harnessRulesAddedCount += harnessResultPre?.rulesAdded?.length || 0

    log('[전체] 6단계: Codex가 개선된 계획대로 코딩...')
    const execution = await fullExecute(cwd, reconciled?.revisedPlan, context, HARNESS_FILE)
    if (!execution || execution.ok === false) {
      finalVerdict = {
        passed: false,
        tier: 'full',
        error: 'execution_failed',
        reason: execution?.message || 'Codex 코딩 실행이 성공 응답을 반환하지 않음 — 코드 리뷰로 성공을 가장하지 않도록 중단함.',
        needsUserDecision: true,
      }
      log('[전체] 6단계: Codex 실행 실패 — 리뷰 단계로 진행하지 않음')
      return await finalizeAndReturn()
    }
    realDiff = await gatherRealDiff(cwd)
  }

  for (let round = 1; round <= MAX_ROUNDS; round++) {
    log(`[전체] ${round}라운드: code-review 스킬 발동 — Codex/Antigravity 독립 코드리뷰...`)
    const [codexReview, antigravityReview] = await parallel([
      () => codexReviewDiff(task, context, realDiff, false),
      () => dispatchWithRetry((isRetry) => antigravityReviewDiff(task, context, realDiff, isRetry), '안티그래비티 코드리뷰'),
    ])

    history.push({ tier: 'full', round, codexReview, antigravityReview })

    // 실측(2026-07-28, discord-bot의 verify-task-v2-retry 테스트 중 계정
    // 세션 한도 초과로 재현됨): codexReview/antigravityReview 둘 다 agent()가
    // null을 반환할 수 있는데(터미널 API 오류 등), 아래 `!x?.hasBlockingIssue`
    // 패턴은 null도 undefined도 false로 평가돼 "리뷰어가 이슈 없다고 답했다"와
    // "리뷰어 호출 자체가 실패했다"를 구분 못하고 후자를 조용히 통과시켜버렸다
    // — 리뷰가 아예 안 됐는데 통과 판정이 나가는 fail-open 버그. 둘 중
    // 하나라도 null이면 정상 판정으로 진행하지 말고 즉시 실패로 처리해 재시도.
    if (!codexReview || !antigravityReview) {
      const whichFailed = [!codexReview && 'codex', !antigravityReview && 'antigravity'].filter(Boolean).join(', ')
      if (round === MAX_ROUNDS) {
        finalVerdict = {
          passed: false,
          tier: 'full',
          round,
          error: 'code_review_failed',
          reason: `전체 트랙 최대 ${MAX_ROUNDS}라운드 안에 코드리뷰(${whichFailed})가 계속 실패함(세션 한도 초과 등 일시적 오류일 가능성 높음) — 리뷰가 실제로 수행되지 못한 상태이니 통과로 간주하면 안 됨. 사용자에게 물어야 함.`,
          needsUserDecision: true,
        }
        break
      }
      log(`[전체] ${round}라운드: 코드리뷰 실패(${whichFailed}) — 통과 처리하지 않고 재시도`)
      continue
    }

    const combinedIssues = [
      ...(codexReview?.issues || []).map((i) => ({ ...i, source: 'codex' })),
      ...(antigravityReview?.issues || []).map((i) => ({ ...i, source: 'antigravity' })),
    ]
    const failedChecks = [
      ...(codexReview.checks || []).map((check) => ({ ...check, source: 'codex' })),
      ...(antigravityReview.checks || []).map((check) => ({ ...check, source: 'antigravity' })),
    ].filter((check) => check.status !== 'passed')
    for (const check of failedChecks) {
      combinedIssues.push({
        description: `결정론적 검사 미통과 또는 미실행: ${check.source}/${check.name} (${check.status})${check.evidence ? ` — ${check.evidence}` : ''}`,
        blocking: true,
        source: check.source,
      })
    }

    if (combinedIssues.length) {
      // 탈출구 경로의 1라운드는 클로드(lightExecute)가 쓴 코드에서 나온
      // 발견이라 저자 표기를 남겨, 클로드 툴 사용 특유의 문제까지
      // "코덱스가 반복하는 실수"로 잘못 일반화되지 않게 한다.
      const stageLabel = baseline && round === 1 ? 'code-review-of-claude-baseline' : 'code-review'
      const harnessResultPost = await appendHarnessRules(combinedIssues, stageLabel, HARNESS_FILE)
      harnessRulesAddedCount += harnessResultPost?.rulesAdded?.length || 0
    }

    const passed = !codexReview?.hasBlockingIssue && !antigravityReview?.hasBlockingIssue && failedChecks.length === 0
    if (passed) {
      finalVerdict = { passed: true, tier: 'full', round, wasEscapeHatch: !!baseline }
      log(`[전체] ${round}라운드에서 통과 (블로킹 이슈 없음)`)
      break
    }

    if (round === MAX_ROUNDS) {
      finalVerdict = {
        passed: false,
        tier: 'full',
        round,
        combinedIssues,
        needsUserDecision: true,
        reason: `전체 트랙 최대 ${MAX_ROUNDS}라운드 안에 블로킹 이슈를 해소 못함. 사용자에게 물어야 함.`,
      }
      break
    }

    log(`[전체] ${round}라운드 불통과 — 코덱스에게 수정 지시`)
    await fullExecute(cwd, formatFixInstruction(combinedIssues), context, HARNESS_FILE)
    realDiff = await gatherRealDiff(cwd)
  }
}

// pendingJobParams가 있으면 discord-notify.sh의 반환 메시지 id를 캡처해서
// weekly-report.sh/work-log-stop-check.sh와 같은 스키마로
// ~/.claude/discord-bot/pending/<id>.json을 쓴다 — 이 스크립트는 JS 샌드박스라
// 파일시스템에 직접 못 쓰므로, appendHistory/appendHarnessRules와 같은
// 방식으로 agent()에게 Bash/Write로 대신 시킨다. jobType으로 pending-job의
// `type` 필드를 결정한다 — needs_clarification은 "verify-task-v2-retry"
// (답장 전체를 답변으로 붙여 재실행), needsUserDecision은
// "verify-task-v2-decision-retry"(2026-07-28 추가: 답장에서 재시도 의도
// 키워드만 감지해 라운드를 늘려 같은 task로 재실행 — discord-bot.py 쪽에서
// 판단). pendingJobParams가 없으면 기존 그대로 완전 일방향.
// 실측 감사로 발견한 버그(2026-07-30): 이 pending-job 작성 경로는 weekly-report.sh/
// work-log-stop-check.sh(둘 다 결정적 inline python3로 필드를 직접 씀)와 달리,
// 서브에이전트에게 자연어로 지시만 하고 반환값을 전혀 확인하지 않았다(schema
// 없이 호출, 결과 버림) — 서브에이전트가 지시를 잘못 따르거나, id 파싱을
// 실수하거나, Write가 실패해도 이 함수도 호출자도 절대 알 방법이 없었다.
// schema를 줘서 서브에이전트가 실제로 뭘 했는지(썼는지/안 썼는지/왜) 구조화된
// 값으로 답하게 하고, 최소한 log()에 남겨서 워크플로우 진행 로그/journal에서는
// 보이게 한다 — 이게 두 bash 생산자 수준의 신뢰성을 주진 않지만("여전히 LLM의
// 지시 이행에 의존"이라는 근본 한계는 남음), 최소한 "완전히 조용한 블랙박스"는
// 벗어난다.
const NOTIFY_ESCALATION_SCHEMA = {
  type: 'object',
  properties: {
    written: { type: 'boolean' },
    messageId: { type: 'string' },
    reason: { type: 'string' },
  },
  required: ['written'],
}

async function notifyDiscordEscalation(message, jobType, pendingJobParams) {
  try {
    if (!pendingJobParams) {
      await agent(
        `Bash로 정확히 아래 명령을 실행해줘 (실패해도 무시하고 결과만 알려줘):\nbash "${MAC_AGENT_ROOT}/bin/discord-notify.sh" ${JSON.stringify(message)}`,
        { phase: 'FullCodeReviewSkill', label: 'discord-notify', agentType: 'general-purpose' }
      )
      return
    }
    const paramsJson = JSON.stringify(pendingJobParams)
    const result = await agent(
      `1. Bash로 정확히 아래 명령을 실행해서 메시지 id를 얻어(실패하면 빈 문자열일 수 있어):\nbash "${MAC_AGENT_ROOT}/bin/discord-notify.sh" ${JSON.stringify(message)}\n\n2. 1번 출력(메시지 id)이 비어있으면 written=false, reason에 "no message id"라고 채워서 끝내 — pending-job을 쓸 필요 없어.\n3. id가 있으면:\n   a. Bash로 \`mkdir -p "$HOME/.claude/discord-bot/pending"\` 실행.\n   b. Bash로 \`python3 -c "import datetime; print(datetime.datetime.now().isoformat())"\`을 실행해서 현재 로컬시각(naive isoformat)을 얻어 — date -u나 다른 형식 절대 쓰지 마, weekly-report.sh의 pending-job과 형식이 정확히 같아야 discord-bot.py가 파싱한다.\n   c. 아래 [원본 params JSON]을 그대로 \`params\` 필드로 쓰고, \`type\`은 ${JSON.stringify(jobType)}, \`created_at\`은 방금 얻은 시각으로 채운 JSON 객체 하나를 만들어서, Write 툴로 \`$HOME/.claude/discord-bot/pending/<1번에서 얻은 id>.json\`에 저장해(파일 내용은 그 JSON 객체 하나, 다른 텍스트 없이).\n   d. Write가 성공했으면 written=true, messageId에 그 id를 채워서 답해. Write가 실패했으면 written=false, reason에 무엇이 실패했는지 적어.\n\n[원본 params JSON]\n${paramsJson}`,
      { phase: 'FullCodeReviewSkill', label: 'discord-notify', agentType: 'general-purpose', schema: NOTIFY_ESCALATION_SCHEMA }
    )
    if (!result) {
      log('디스코드 에스컬레이션 알림: 서브에이전트 호출 자체가 실패함(세션/사용 한도 등) — pending-job이 안 만들어졌을 수 있음')
    } else if (!result.written) {
      log(`디스코드 에스컬레이션 알림: pending-job 작성 안 됨 (${result.reason || '사유 미상'})`)
    }
  } catch (e) {
    log(`디스코드 알림 실패(무시): ${e}`)
  }
}

async function finalizeAndReturn() {
  if (finalVerdict?.needsUserDecision || finalVerdict?.error === 'needs_clarification') {
    const shortTask = typeof task === 'string' ? task.slice(0, 200) : String(task)
    const isClarification = finalVerdict?.error === 'needs_clarification'
    // 실측 감사로 발견한 버그(2026-07-30): needs_clarification일 때
    // finalVerdict.reason은 항상 "Workflow 스크립트는 AskUserQuestion을
    // 직접 못 부름..." 같은 고정 상투문구이고(항상 truthy), 실제 질문은
    // finalVerdict.questions에 따로 담긴다. 예전 `reason || questions`
    // 순서는 reason이 항상 이겨서, Discord 알림에 실제로 답해야 할 질문이
    // 아니라 상투문구만 보여줬다 — 답장 재시도 메커니즘 자체는 그대로
    // 작동했지만(사용자 답변을 그대로 이어붙임), "질문을 보고 답한다"는
    // 이 흐름의 전제가 알림 시점에 깨져 있었다. needs_clarification일 때는
    // questions를 우선하고, 비어 있을 때만 reason으로 폴백한다.
    const reason = isClarification
      ? (finalVerdict?.questions || finalVerdict?.reason || '(사유 없음)')
      : (finalVerdict?.reason || '(사유 없음)')
    const jobType = isClarification ? 'verify-task-v2-retry' : 'verify-task-v2-decision-retry'
    const replyHint = isClarification
      ? '\n\n이 메시지에 답장하면 그 내용을 답변으로 붙여서 재시도합니다.'
      : '\n\n이 메시지에 "재시도"/"retry"/"다시" 중 하나를 포함해서 답장하면 라운드를 늘려 같은 작업을 재시도합니다. 그 외 답장이나 무응답은 자동 조치 없이 종료됩니다.'
    await notifyDiscordEscalation(
      `⚠️ verify-task-v2 에스컬레이션 (${tier} 트랙) — "${shortTask}"\n${reason}${replyHint}`,
      jobType,
      {
        task,
        cwd,
        persona,
        maxRounds: MAX_ROUNDS,
        historyFile,
        harnessFile: HARNESS_FILE,
        ...(NANO_MODE ? {
          nanoMode: true,
          nanoSteps: parsedArgs.nanoSteps,
          taskId: parsedArgs.taskId,
          nanoEventFile: NANO_EVENT_FILE,
          tokenUsage: parsedArgs.tokenUsage,
          providerHeadroom: parsedArgs.providerHeadroom,
          remainingTokenPct: parsedArgs.remainingTokenPct,
        } : {}),
        ...(isClarification ? { questions: finalVerdict?.questions } : {}),
      }
    )
  }

  let reviewPersistence = null
  if (tier === 'full') {
    const reviewReport = buildReviewReport(task, cwd, tier, realDiff, finalVerdict, history)
    reviewPersistence = await persistReviewReport(reviewReport)
    if (finalVerdict?.passed && !reviewPersistence?.persisted) {
      finalVerdict = {
        ...finalVerdict,
        passed: false,
        error: 'review_persistence_failed',
        needsUserDecision: true,
        reason: '코드 리뷰 결과 저장에 실패하여 AI_APPROVED를 발행하지 않음.',
      }
    }
  }

  log('히스토리 저장 중...')
  await appendHistory(
    {
      task: typeof task === 'string' ? task.slice(0, 300) : task,
      persona,
      tier,
      wasEscapeHatch: !!baseline,
      rounds: history.length,
      passed: !!finalVerdict?.passed,
      needsUserDecision: !!finalVerdict?.needsUserDecision,
      needsClarification: finalVerdict?.error === 'needs_clarification',
      finalTotal: tier === 'light' ? (finalVerdict?.evalResult?.total ?? null) : null,
      finalHasBlockingIssue: tier === 'full' && finalVerdict && !finalVerdict.error ? !finalVerdict.passed : null,
      rulesAddedToHarness: tier === 'full' ? harnessRulesAddedCount : null,
      nanoMode: NANO_MODE,
      nanoStepsCompleted: NANO_MODE ? history.length : null,
      reviewReportPersisted: reviewPersistence?.persisted ?? null,
    },
    historyFile
  )
  return { finalVerdict, tier, history, reviewPersistence }
}

return await finalizeAndReturn()
