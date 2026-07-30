export const meta = {
  name: 'verify-task-v2',
  description: '작업 시작 전 스펙 고정 + 경량/전체 티어별 다자간 검증 (Claude/Codex/Antigravity). 설계: docs/verify-task-v2-design.md',
  phases: [
    { title: 'Preflight' },
    { title: 'Context' },
    { title: 'Light' },
    { title: 'FullPlan' },
    { title: 'FullCritique' },
    { title: 'FullReconcile' },
    { title: 'FullExecute' },
    { title: 'FullReview' },
  ],
}

// 설계 전체는 docs/verify-task-v2-design.md 참고 — 이 스크립트는 결정 기록이
// 아니라 구현이다. 결정의 "왜"를 다시 읽지 않고 이 파일만 고치지 말 것.
//
// 2026-07-27 개정: 전체(full) 트랙이 채점표 기반(안티 스펙+고정/동적 rubric+
// 90점)에서 하네스 기반 정성 검토(코덱스 자체계획→클로드+안티 블라인드
// 비평→코덱스 취합/개선→실행→클로드+안티 무점수 듀얼 코드리뷰)로 재설계됨.
// 경량(light) 트랙은 전혀 안 건드림. docs/verify-task-v2-design.md의
// "## 개정" 섹션에 왜 바뀌었는지 기록돼 있음 — 여기서 다시 설명 안 함.
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
  return fileCount <= 3 && !sensitive ? 'light' : 'full'
}

// ---------- 사후 검증용 실제 diff 수집 (verify-task.js와 동일 패턴) ----------

const REAL_DIFF_SCHEMA = {
  type: 'object',
  properties: {
    content: { type: 'string' },
    filesChanged: { type: 'array', items: { type: 'string' } },
    sensitivePath: { type: 'boolean' },
  },
  required: ['content', 'filesChanged', 'sensitivePath'],
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
    `Bash로 아래 명령을 그 디렉토리에서 실행하고, 나온 출력을 절대 요약하거나 고치지 말고 content 필드에 그대로 담아 반환해:\n\ncd ${JSON.stringify(cwd)} && { echo '--- git status --porcelain ---'; git status --porcelain; echo '--- git diff --stat HEAD (tracked 변경만) ---'; git diff --stat HEAD; echo '--- git diff HEAD (tracked 변경만) ---'; git diff HEAD; echo '--- untracked 신규 파일 전체 내용 (git diff에는 안 잡힘) ---'; git status --porcelain | awk '$1 == "??" {print $2}' | while IFS= read -r f; do echo "=== NEW FILE: $f ==="; cat "$f"; done; } 2>&1\n\n출력이 8000자를 넘으면 앞 8000자만 남기고 끝에 "...(잘림)"을 붙여.\n\n추가로: git status --porcelain 출력 전체(수정된 tracked 파일 + untracked 신규 파일 둘 다)에서 실제로 변경/추가된 파일 경로를 전부 뽑아 filesChanged 배열에 넣고(신규 파일도 반드시 포함), 그중 설정/보안/.github/workflows//공개문서(README 등)에 해당하는 게 하나라도 있으면 sensitivePath=true로 반환해.\n\n마지막 응답은 반드시 다른 설명 없이 아래 세 키를 모두 포함한 JSON 객체 하나여야 해(필드 누락 금지):\n{"content":"위 명령의 원문 출력","filesChanged":["실제 변경 경로"],"sensitivePath":false}`,
    { phase: 'Light', label: 'gather-real-diff', schema: REAL_DIFF_SCHEMA }
  )
  return gathered
}

function mechanicalTierViolated(realDiff) {
  const fileCount = (realDiff?.filesChanged || []).length
  return fileCount > 3 || !!realDiff?.sensitivePath
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

// ---------- 전체 트랙: 1단계 (코덱스 자체 계획 작성) ----------

const CODEX_PLAN_SCHEMA = {
  type: 'object',
  properties: {
    needsClarification: { type: 'boolean' },
    clarifyingQuestions: { type: 'string' },
    plan: { type: 'string' },
  },
  required: ['needsClarification'],
}

function buildCodexPlanPrompt(task, context) {
  return `너는 이 작업을 실제로 코딩할 담당자야. 아래는 사용자의 원 지시문과 저장소 컨텍스트뿐이고, 다른 에이전트의 의견은 아직 없어 — 네가 이 작업에 대한 실행 계획을 처음으로 세우는 거야.

[원 지시문]
${task}

[저장소 컨텍스트]
${context.contextText}

1. 정보 충분성 판단: 이 계획을 세우고 실행하기에 정보가 충분해? 부족하면 needsClarification=true로 하고 clarifyingQuestions에 필수 질문 최대 3개 + 선택 질문 최대 3개를 적어(그 이상 필요해도 아는 만큼 계획을 쓰고 "확인 필요"라고 표시). 충분하면 needsClarification=false.
2. (충분하면) plan에 네가 실제로 코딩할 구체적 실행 계획을 적어: 건드릴 파일, 각 파일에서 할 일, 예상되는 엣지케이스와 처리 방법, 완료 조건. "어떻게 할지"를 상세히 적어 — 이후 다른 에이전트들이 이 계획만 보고 비평할 거야.

JSON으로만 답해: {"needsClarification":false,"clarifyingQuestions":"","plan":""}`
}

async function codexOwnPlan(task, context, harnessFile, isRetry) {
  const prompt = buildCodexPlanPrompt(task, context)
  return agent(buildScoreDispatchInstruction('codex', prompt, harnessFile, 'plan'), {
    phase: 'FullPlan',
    label: isRetry ? 'codex-own-plan-retry' : 'codex-own-plan',
    schema: CODEX_PLAN_SCHEMA,
  })
}

// ---------- 전체 트랙: 2단계 (클로드+안티그래비티 블라인드 비평) ----------

const CRITIQUE_SCHEMA = {
  type: 'object',
  properties: {
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: { description: { type: 'string' }, severity: { type: 'string' } },
        required: ['description'],
      },
    },
    notes: { type: 'string' },
  },
  required: ['issues'],
}

function buildCritiquePrompt(task, context, codexPlan) {
  return `너는 독립 비평자야. 채점표나 점수는 없어 — 이 계획에서 실제로 문제가 될 만한 버그/공백/결함/오류만 찾아. 다른 비평자의 의견은 안 보여줌(블라인드 — 서로 결과를 보면 앵커링 편향이 생기니까).

[원 지시문]
${task}

[저장소 컨텍스트]
${context.contextText}

[코덱스가 작성한 실행 계획]
${codexPlan.plan}

이 계획을 실행했을 때 실제로 문제가 생길 만한 지점을 issues 배열에 담아(각 항목: description 필수, severity는 자유 텍스트, 예: "critical"/"minor"). 문제가 없으면 빈 배열. notes에 그 외 참고할 점을 자유롭게.

JSON으로만: {"issues":[{"description":"","severity":""}],"notes":""}`
}

async function claudeCritiquePlan(task, context, codexPlan) {
  return agent(buildCritiquePrompt(task, context, codexPlan), {
    phase: 'FullCritique',
    label: 'critique-claude',
    schema: CRITIQUE_SCHEMA,
    agentType: 'general-purpose',
  })
}

async function antigravityCritiquePlan(task, context, codexPlan, isRetry) {
  return agent(buildScoreDispatchInstruction('agy', buildCritiquePrompt(task, context, codexPlan), null, 'critique'), {
    phase: 'FullCritique',
    label: isRetry ? 'critique-antigravity-retry' : 'critique-antigravity',
    schema: CRITIQUE_SCHEMA,
  })
}

// ---------- 전체 트랙: 3단계 (코덱스 취합+판단+계획 개선, 5+7단계 병합) ----------
// 사용자가 설명한 흐름에서 "코덱스가 버그/결함을 정리해 클로드에게 전달"(5)과
// "코덱스가 클로드+안티의 분석을 객관적으로 평가해 개선 후 코딩 시작"(7)은
// 원래 별개 턴이지만, 클로드의 하네스 반영(6)은 "다음 실행부터" 의미가
// 있는 것이지 이번 실행 중 코덱스가 자기가 방금 만든 규칙을 다시 읽어야 할
// 이유는 없다(이미 원본 비평 텍스트를 그대로 받으므로 정보 손실 없음).
// 그래서 5+7을 한 호출로 병합해 왕복을 줄인다.

const RECONCILE_SCHEMA = {
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

function buildReconcilePrompt(task, context, codexPlan, claudeCritique, antigravityCritique) {
  return `아래는 네(코덱스)가 작성한 실행 계획과, 클로드+안티그래비티가 각각 독립적으로(서로 안 보고) 비평한 내용이야. 두 비평을 취합하고, 네가 보기에 타당한 지적은 반영해서 계획을 개선해. 타당하지 않다고 판단되는 지적은 disagreements에 왜 받아들이지 않는지 적고 무시해도 돼 — 비평자 말을 무조건 다 따를 필요는 없어, 네가 객관적으로 판단해.

[원 지시문]
${task}

[저장소 컨텍스트]
${context.contextText}

[네가 작성한 원래 계획]
${codexPlan.plan}

[클로드의 비평]
${JSON.stringify(claudeCritique?.issues || [])}
${claudeCritique?.notes || ''}

[안티그래비티의 비평]
${JSON.stringify(antigravityCritique?.issues || [])}
${antigravityCritique?.notes || ''}

1. compiledIssues에 두 비평에서 나온 이슈들을 (description, source: "claude" 또는 "antigravity") 형태로 전부 합쳐 적어 — 네가 타당하다고 본 것만이 아니라 나온 것 전부 기록해(사용자가 이후 이 기록으로 재발방지 문서를 만들 거야).
2. disagreements에 네가 반영하지 않기로 한 지적과 그 이유를 적어(없으면 빈 문자열).
3. revisedPlan에 비평을 반영한 최종 실행 계획을 적어 — 실제로 이 계획대로 코딩할 거야.

JSON으로만: {"compiledIssues":[{"description":"","source":""}],"disagreements":"","revisedPlan":""}`
}

async function codexReconcile(task, context, codexPlan, claudeCritique, antigravityCritique, harnessFile, isRetry) {
  const prompt = buildReconcilePrompt(task, context, codexPlan, claudeCritique, antigravityCritique)
  return agent(buildScoreDispatchInstruction('codex', prompt, harnessFile, 'reconcile'), {
    phase: 'FullReconcile',
    label: isRetry ? 'codex-reconcile-retry' : 'codex-reconcile',
    schema: RECONCILE_SCHEMA,
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
      phase: stageLabel === 'pre-execution' ? 'FullReconcile' : 'FullReview',
      label: `harness-append-${stageLabel}`,
      schema: HARNESS_APPEND_SCHEMA,
      agentType: 'general-purpose',
    }
  )
}

// ---------- 전체 트랙: 실행 (코덱스, 쓰기 가능) ----------

async function fullExecute(cwd, instruction, context, harnessFile) {
  const prompt = `아래 지시대로 실제로 파일을 수정/생성해줘. 작업 디렉토리: ${cwd}\n\n[저장소 컨텍스트]\n${context.contextText}\n\n[지시]\n${instruction}\n\n다 하고 나서 뭘 했는지 짧게 설명해.`
  return agent(buildExecuteDispatchInstruction(cwd, prompt, harnessFile), {
    phase: 'FullExecute',
    label: 'full-execute',
    schema: EXECUTE_ENVELOPE_SCHEMA,
  })
}

// ---------- 전체 트랙: 코드 리뷰 (클로드+안티그래비티 블라인드, 무점수) ----------

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
  },
  required: ['hasBlockingIssue', 'issues'],
}

function buildReviewPrompt(task, context, realDiff) {
  return `너는 독립 코드 리뷰어야. 점수나 채점표는 없어 — 실제 변경사항(git diff)에서 버그/결함/오류만 찾아. 다른 리뷰어의 의견은 안 보여줌(블라인드).

[원 작업]
${task}

[저장소 컨텍스트]
${context.contextText}

[실제 변경사항 — git diff]
${realDiff.content}

이 변경에서 실제로 문제가 되는 지점을 issues 배열에 담아(각 항목: description 필수, blocking — 반드시 고쳐야 할 정도면 true, 사소하면 false). 하나라도 blocking=true인 이슈가 있으면 hasBlockingIssue=true, 없으면 false. 문제가 없으면 issues는 빈 배열이고 hasBlockingIssue=false. notes에 그 외 참고할 점.

JSON으로만: {"hasBlockingIssue":false,"issues":[{"description":"","blocking":false}],"notes":""}`
}

async function claudeReviewDiff(task, context, realDiff) {
  return agent(buildReviewPrompt(task, context, realDiff), {
    phase: 'FullReview',
    label: 'review-claude',
    schema: CODE_REVIEW_SCHEMA,
    agentType: 'general-purpose',
  })
}

async function antigravityReviewDiff(task, context, realDiff, isRetry) {
  return agent(buildScoreDispatchInstruction('agy', buildReviewPrompt(task, context, realDiff), null, 'review'), {
    phase: 'FullReview',
    label: isRetry ? 'review-antigravity-retry' : 'review-antigravity',
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

// Workflow 샌드박스에서는 외부 모듈을 import할 수 없으므로 위험도 함수의
// 우선순위/임계값을 workflows/lib/decide-risk-tier.js와 의도적으로 복제한다.
function decideNanoRiskTier(input) {
  const { stepFileCount = 0, cumulativeFileCount = 0, sensitivePath = false, dependencyBoundaryCrossed = false, remainingTokenPct, providerHeadroom } = input || {}
  const providerHeadroomPct = lowestNanoHeadroom(providerHeadroom)
  const singlePct = typeof remainingTokenPct === 'number' && Number.isFinite(remainingTokenPct) && remainingTokenPct >= 0 && remainingTokenPct <= 100 ? remainingTokenPct : undefined
  const tokenPct = providerHeadroomPct ?? singlePct
  if (sensitivePath) return 'full'
  if (dependencyBoundaryCrossed) return 'mid'
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
    const review = await claudeReviewDiff(task, context, realDiff)
    if (!review) return { ok: false, reason: 'mid 통합 리뷰어가 결과를 반환하지 않음' }
    return { ok: !review.hasBlockingIssue, issues: (review.issues || []).map((issue) => ({ ...issue, source: 'claude' })), reviewers: ['claude'] }
  }
  const [claudeReview, antigravityReview] = await parallel([
    () => claudeReviewDiff(task, context, realDiff),
    () => antigravityReviewDiff(task, context, realDiff, false),
  ])
  if (!claudeReview || !antigravityReview) return { ok: false, reason: 'full 통합 리뷰어 중 하나 이상이 결과를 반환하지 않음' }
  const issues = [
    ...(claudeReview.issues || []).map((issue) => ({ ...issue, source: 'claude' })),
    ...(antigravityReview.issues || []).map((issue) => ({ ...issue, source: 'antigravity' })),
  ]
  return { ok: !claudeReview.hasBlockingIssue && !antigravityReview.hasBlockingIssue, issues, reviewers: ['claude', 'antigravity'] }
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
    const execution = await fullExecute(cwd, `${step.instruction}\n\n[완료조건]\n${step.doneCriteria}`, context, harnessFile)
    const realDiff = await gatherRealDiff(cwd)
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
    { phase: 'FullReview', label: 'history-append', agentType: 'general-purpose' }
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
let baseline = null // 탈출구 발동 시 경량 트랙 산출물을 여기 보관 — 전체 트랙에서 1~8단계(계획/비평/실행)를 생략하고 바로 코드리뷰로 직행하는 데 씀
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
    // needsUserDecision이 뜨는 오분류. FullPlan/FullReconcile/FullCritique
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
// 2026-07-27 개정: 채점표 없음. 1~3단계(코덱스 계획→클로드+안티 블라인드
// 비평→코덱스 취합/개선)를 거쳐 실행하고, 실행 결과를 클로드+안티가 무점수로
// 듀얼 리뷰한다. 탈출구로 들어온 경우 이미 실제 코드가 있으므로 1~3단계를
// 생략하고 바로 리뷰로 직행한다.
if (tier === 'full' && !finalVerdict) {
  let realDiff

  if (baseline) {
    log('[전체] 탈출구 경로 — 1~3단계(코덱스 계획/비평/취합) 생략, 코드리뷰로 직행')
    realDiff = await gatherRealDiff(cwd)
  } else {
    log('[전체] 1단계: 코덱스 자체 계획 작성...')
    const codexPlan = await dispatchWithRetry(
      (isRetry) => codexOwnPlan(task, context, HARNESS_FILE, isRetry),
      '코덱스 계획 작성'
    )

    if (!codexPlan || isDispatchFailure(codexPlan)) {
      finalVerdict = {
        passed: false,
        tier: 'full',
        error: 'codex_plan_failed',
        reason: isDispatchFailure(codexPlan)
          ? `코덱스 자체 계획 작성 단계가 도구 실행/파싱 실패로 1회 재시도 후에도 실패함: ${codexPlan.dispatchFailureReason}. 같은 task로 워크플로우를 재시도할 것.`
          : '코덱스 자체 계획 작성 단계가 실패함(세이프티 분류기 오류 등 일시적 오류일 가능성 높음) — 같은 task로 워크플로우를 재시도할 것.',
      }
      log('[전체] 1단계: 코덱스 계획 작성 실패 — 재시도 필요')
      return await finalizeAndReturn()
    }

    if (codexPlan?.needsClarification) {
      finalVerdict = {
        passed: false,
        tier: 'full',
        error: 'needs_clarification',
        questions: codexPlan.clarifyingQuestions,
        reason:
          'Workflow 스크립트는 AskUserQuestion을 직접 못 부름. 호출한 에이전트가 questions를 사용자에게 AskUserQuestion으로 물어보고(필수 최대 3개+선택 최대 3개, 왕복 최대 2회), 답변을 원 task 문자열 끝에 덧붙여서 이 워크플로우를 다시 호출해야 함.',
      }
      log('[전체] 1단계: 정보 부족 — 역질문 필요')
      return await finalizeAndReturn()
    }

    log('[전체] 2단계: 클로드/안티그래비티 블라인드 비평...')
    const [claudeCritique, antigravityCritique] = await parallel([
      () => claudeCritiquePlan(task, context, codexPlan),
      () => dispatchWithRetry((isRetry) => antigravityCritiquePlan(task, context, codexPlan, isRetry), '안티그래비티 비평'),
    ])

    // 2026-07-29 수정: 아래 3단계(codexReconcile)로 넘어가는 buildReconcilePrompt는
    // `claudeCritique?.issues || []` / `antigravityCritique?.issues || []`로 조용히
    // 빈 배열 폴백한다 — agent()가 실패해 null을 반환하면(세션 한도 초과 등 일시적
    // 오류) 코덱스는 "클로드/안티그래비티 둘 다 이슈를 못 찾았다"고 오인하고 자기
    // 계획을 검증 없이 그대로 밀어붙이게 된다. 정확히 같은 버그 클래스가 아래 FullReview
    // 라운드 루프(claudeReview/antigravityReview)에서는 2026-07-28 실측 후 이미 고쳐져
    // 있었는데, 이 앞단(Critique)에는 그 수정이 전이되지 않았었다 — 여기서도 동일하게
    // null을 "이슈 없음"이 아니라 "비평 자체가 실패함"으로 명시 처리한다.
    if (!claudeCritique || !antigravityCritique) {
      const whichFailed = [!claudeCritique && 'claude', !antigravityCritique && 'antigravity'].filter(Boolean).join(', ')
      finalVerdict = {
        passed: false,
        tier: 'full',
        error: 'critique_failed',
        reason: `클로드/안티그래비티 블라인드 비평 단계(${whichFailed})가 실패함(세션 한도 초과 등 일시적 오류일 가능성 높음) — 비평이 실제로 수행되지 못한 상태이니 코덱스가 "이슈 없음"으로 오인하고 계획을 그대로 진행하면 안 됨. 같은 task로 워크플로우를 재시도할 것.`,
      }
      log('[전체] 2단계: 블라인드 비평 실패 — 재시도 필요')
      return await finalizeAndReturn()
    }

    log('[전체] 3단계: 코덱스 취합+판단+계획 개선...')
    const reconciled = await dispatchWithRetry(
      (isRetry) => codexReconcile(task, context, codexPlan, claudeCritique, antigravityCritique, HARNESS_FILE, isRetry),
      '코덱스 취합'
    )

    if (!reconciled || isDispatchFailure(reconciled)) {
      finalVerdict = {
        passed: false,
        tier: 'full',
        error: 'codex_reconcile_failed',
        reason: isDispatchFailure(reconciled)
          ? `코덱스 취합/계획개선 단계가 도구 실행/파싱 실패로 1회 재시도 후에도 실패함: ${reconciled.dispatchFailureReason}. 같은 task로 워크플로우를 재시도할 것.`
          : '코덱스 취합/계획개선 단계가 실패함(일시적 오류일 가능성 높음) — 같은 task로 워크플로우를 재시도할 것.',
      }
      log('[전체] 3단계: 코덱스 취합 실패 — 재시도 필요')
      return await finalizeAndReturn()
    }

    log('[전체] 하네스 규칙 추가 (사전비평 단계)...')
    const harnessResultPre = await appendHarnessRules(reconciled?.compiledIssues, 'pre-execution', HARNESS_FILE)
    harnessRulesAddedCount += harnessResultPre?.rulesAdded?.length || 0

    log('[전체] 실행: 코덱스가 개선된 계획대로 코딩...')
    await fullExecute(cwd, reconciled?.revisedPlan, context, HARNESS_FILE)
    realDiff = await gatherRealDiff(cwd)
  }

  for (let round = 1; round <= MAX_ROUNDS; round++) {
    log(`[전체] ${round}라운드: 클로드/안티그래비티 블라인드 코드리뷰(무점수)...`)
    const [claudeReview, antigravityReview] = await parallel([
      () => claudeReviewDiff(task, context, realDiff),
      () => dispatchWithRetry((isRetry) => antigravityReviewDiff(task, context, realDiff, isRetry), '안티그래비티 코드리뷰'),
    ])

    history.push({ tier: 'full', round, claudeReview, antigravityReview })

    // 실측(2026-07-28, discord-bot의 verify-task-v2-retry 테스트 중 계정
    // 세션 한도 초과로 재현됨): claudeReview/antigravityReview 둘 다 agent()가
    // null을 반환할 수 있는데(터미널 API 오류 등), 아래 `!x?.hasBlockingIssue`
    // 패턴은 null도 undefined도 false로 평가돼 "리뷰어가 이슈 없다고 답했다"와
    // "리뷰어 호출 자체가 실패했다"를 구분 못하고 후자를 조용히 통과시켜버렸다
    // — 리뷰가 아예 안 됐는데 통과 판정이 나가는 fail-open 버그. 둘 중
    // 하나라도 null이면 정상 판정으로 진행하지 말고 즉시 실패로 처리해 재시도.
    if (!claudeReview || !antigravityReview) {
      const whichFailed = [!claudeReview && 'claude', !antigravityReview && 'antigravity'].filter(Boolean).join(', ')
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
      ...(claudeReview?.issues || []).map((i) => ({ ...i, source: 'claude' })),
      ...(antigravityReview?.issues || []).map((i) => ({ ...i, source: 'antigravity' })),
    ]

    if (combinedIssues.length) {
      // 탈출구 경로의 1라운드는 클로드(lightExecute)가 쓴 코드에서 나온
      // 발견이라 저자 표기를 남겨, 클로드 툴 사용 특유의 문제까지
      // "코덱스가 반복하는 실수"로 잘못 일반화되지 않게 한다.
      const stageLabel = baseline && round === 1 ? 'code-review-of-claude-baseline' : 'code-review'
      const harnessResultPost = await appendHarnessRules(combinedIssues, stageLabel, HARNESS_FILE)
      harnessRulesAddedCount += harnessResultPost?.rulesAdded?.length || 0
    }

    const passed = !claudeReview?.hasBlockingIssue && !antigravityReview?.hasBlockingIssue
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
        { phase: 'FullReview', label: 'discord-notify', agentType: 'general-purpose' }
      )
      return
    }
    const paramsJson = JSON.stringify(pendingJobParams)
    const result = await agent(
      `1. Bash로 정확히 아래 명령을 실행해서 메시지 id를 얻어(실패하면 빈 문자열일 수 있어):\nbash "${MAC_AGENT_ROOT}/bin/discord-notify.sh" ${JSON.stringify(message)}\n\n2. 1번 출력(메시지 id)이 비어있으면 written=false, reason에 "no message id"라고 채워서 끝내 — pending-job을 쓸 필요 없어.\n3. id가 있으면:\n   a. Bash로 \`mkdir -p "$HOME/.claude/discord-bot/pending"\` 실행.\n   b. Bash로 \`python3 -c "import datetime; print(datetime.datetime.now().isoformat())"\`을 실행해서 현재 로컬시각(naive isoformat)을 얻어 — date -u나 다른 형식 절대 쓰지 마, weekly-report.sh의 pending-job과 형식이 정확히 같아야 discord-bot.py가 파싱한다.\n   c. 아래 [원본 params JSON]을 그대로 \`params\` 필드로 쓰고, \`type\`은 ${JSON.stringify(jobType)}, \`created_at\`은 방금 얻은 시각으로 채운 JSON 객체 하나를 만들어서, Write 툴로 \`$HOME/.claude/discord-bot/pending/<1번에서 얻은 id>.json\`에 저장해(파일 내용은 그 JSON 객체 하나, 다른 텍스트 없이).\n   d. Write가 성공했으면 written=true, messageId에 그 id를 채워서 답해. Write가 실패했으면 written=false, reason에 무엇이 실패했는지 적어.\n\n[원본 params JSON]\n${paramsJson}`,
      { phase: 'FullReview', label: 'discord-notify', agentType: 'general-purpose', schema: NOTIFY_ESCALATION_SCHEMA }
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
    },
    historyFile
  )
  return { finalVerdict, tier, history }
}

return await finalizeAndReturn()
