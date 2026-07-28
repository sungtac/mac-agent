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
  return agent(
    `Bash 툴로 아래 두 명령을 순서대로 실행해줘 (둘 다 절대경로 — 이 실행 환경 PATH에 /opt/homebrew/bin이 없을 수 있어서 bare 명령어 "codex"는 "command not found"로 실패할 수 있음):\n1. /opt/homebrew/bin/codex login status\n2. env -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT /Users/edge_ai/.local/bin/agy models\n\n두 명령 다 에러 없이 성공(로그인된 상태)이면 ok=true, issues는 빈 문자열로 반환해. 하나라도 로그인 필요/에러가 나면 ok=false로 하고, 어떤 도구가 문제인지와 해결 방법을 issues에 적어줘.`,
    { phase: 'Preflight', label: 'preflight', schema: PREFLIGHT_SCHEMA }
  )
}

const SCORE_DISPATCH = '/Users/edge_ai/mac-agent/workflows/lib/score-dispatch.sh'
const CODEX_EXECUTE_DISPATCH = '/Users/edge_ai/mac-agent/workflows/lib/codex-execute-dispatch.sh'
const HARNESS_FILE_DEFAULT = '/Users/edge_ai/mac-agent/docs/codex-harness.md'

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
    ? `[하네스 주입] 2번에서 저장할 내용은 [프롬프트 내용]을 그대로 저장하는 게 아니라, 먼저 Read 툴로 ${harnessFile}을 읽고(파일이 없으면 첫 실행이니 "해당 없음"으로 간주), "[코덱스 하네스 — 반드시 준수]\\n" + 그 내용 + "\\n\\n---\\n\\n"를 맨 앞에 붙인 뒤 [프롬프트 내용]을 이어붙인 합본이어야 해.\n\n`
    : ''
  return `${harnessNote}1. Bash로 \`mktemp /tmp/verify-task-v2-${tool}-XXXXXX.txt\` 실행해서 임시 파일 경로를 얻어.\n2. Write 툴로 그 경로에 아래 [프롬프트 내용]을 정확히 그대로(글자 하나 고치지 말고${injectHarness ? ', 단 하네스 주입 지시가 있으면 위에서 설명한 합본으로' : ''}) 저장해.\n3. Bash로 다음을 실행해 (파일경로는 2번 경로로 치환): bash ${SCORE_DISPATCH} ${tool} <파일경로> ${schemaKind}\n4. 실행이 끝나면 Bash로 그 임시 파일을 삭제해.\n5. 3번 명령의 stdout은 이미 검증된 JSON 한 줄이야 — 그 값을 그대로 구조화된 출력으로 반환해. 내용을 고치거나, 재해석하거나, 다른 값으로 대체하지 마.\n\n[프롬프트 내용]\n${prompt}`
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
    ? `[하네스 주입] 2번에서 저장할 내용은 [프롬프트 내용]을 그대로 저장하는 게 아니라, 먼저 Read 툴로 ${harnessFile}을 읽고(파일이 없으면 첫 실행이니 "해당 없음"으로 간주), "[코덱스 하네스 — 반드시 준수]\\n" + 그 내용 + "\\n\\n---\\n\\n"를 맨 앞에 붙인 뒤 [프롬프트 내용]을 이어붙인 합본이어야 해.\n\n`
    : ''
  return `${harnessNote}1. Bash로 \`mktemp /tmp/verify-task-v2-exec-XXXXXX.txt\` 실행해서 임시 파일 경로를 얻어.\n2. Write 툴로 그 경로에 아래 [프롬프트 내용]을 정확히 그대로(글자 하나 고치지 말고${harnessFile ? ', 단 위에서 설명한 하네스 합본으로' : ''}) 저장해.\n3. Bash로 다음을 실행해 (파일경로는 2번 경로로 치환, timeout 300000ms 이상 줘): bash ${CODEX_EXECUTE_DISPATCH} ${JSON.stringify(cwd)} <파일경로>\n4. 실행이 끝나면 Bash로 그 임시 파일을 삭제해.\n5. 3번 명령의 stdout은 이미 검증된 JSON 한 줄이야({"ok":bool,"message":string}) — 그 값을 그대로 구조화된 출력으로 반환해. 내용을 고치거나, 재해석하거나, 다른 값으로 대체하지 마. 이 결과는 코덱스 자체 보고일 뿐 실제 검증이 아님을 기억해 — 실제 변경사항은 별도로 git diff로 확인할 거야.\n\n[프롬프트 내용]\n${prompt}`
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
    contextText: { type: 'string' },
    intendedFiles: { type: 'array', items: { type: 'string' } },
    sensitivePath: { type: 'boolean' },
  },
  required: ['contextText', 'intendedFiles', 'sensitivePath'],
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
    `아래는 곧 시작할 작업이고, 아직 아무 실행도 안 한 상태야. 순서대로 해줘:\n\n[작업]\n${task}\n\n1. Bash로 이 디렉토리에서 아래를 실행: cd ${JSON.stringify(cwd)} && { echo '--- git status ---'; git status; echo '--- 최근 커밋 5개 ---'; git log --oneline -5; } 2>&1\n2. 작업과 관련 있어 보이는 파일들을 Glob/Grep/Read로 가볍게 훑어봐(전체 저장소를 다 읽지 말고, 작업 키워드로 관련 있는 것만).\n3. 이 디렉토리(또는 상위)에 CLAUDE.md/AGENTS.md 같은 컨벤션 문서가 있으면 Read로 읽어서 관련 부분을 요약해.\n4. package.json의 scripts, Makefile, README의 테스트 관련 섹션 등에서 테스트 실행 명령을 찾아봐(있으면).\n5. 위 1~4에서 얻은 사실을 contextText 하나의 텍스트로 정리해(요약하지 말고 사실 위주로, 다음 단계 에이전트들이 저장소를 직접 못 보고 이 텍스트만 볼 거야).\n6. 이 작업이 **실제로 건드릴 것으로 예상되는 파일 경로 목록**을 intendedFiles에 넣어줘 — 아직 실행 전이니 예측이야, 최대한 구체적으로. 새로 만들 파일도 포함.\n7. intendedFiles 중 설정/보안/.github/workflows//공개문서(README 등)에 해당하는 게 하나라도 있으면 sensitivePath=true, 아니면 false.`,
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
    `Bash로 아래 명령을 그 디렉토리에서 실행하고, 나온 출력을 절대 요약하거나 고치지 말고 content 필드에 그대로 담아 반환해:\n\ncd ${JSON.stringify(cwd)} && { echo '--- git status --porcelain ---'; git status --porcelain; echo '--- git diff --stat HEAD (tracked 변경만) ---'; git diff --stat HEAD; echo '--- git diff HEAD (tracked 변경만) ---'; git diff HEAD; echo '--- untracked 신규 파일 전체 내용 (git diff에는 안 잡힘) ---'; git status --porcelain | awk '$1 == "??" {print $2}' | while IFS= read -r f; do echo "=== NEW FILE: $f ==="; cat "$f"; done; } 2>&1\n\n출력이 8000자를 넘으면 앞 8000자만 남기고 끝에 "...(잘림)"을 붙여.\n\n추가로: git status --porcelain 출력 전체(수정된 tracked 파일 + untracked 신규 파일 둘 다)에서 실제로 변경/추가된 파일 경로를 전부 뽑아 filesChanged 배열에 넣고(신규 파일도 반드시 포함), 그중 설정/보안/.github/workflows//공개문서(README 등)에 해당하는 게 하나라도 있으면 sensitivePath=true로 반환해.`,
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

function formatFixInstruction(combinedIssues) {
  return combinedIssues.map((it, i) => `${i + 1}. [${it.source}] ${it.description}`).join('\n')
}

// ---------- 노이즈 감지 (경량 트랙 전용 — 전체 트랙은 무점수라 해당 없음) ----------
// 설계상 "구현 시 정할 파라미터"로 남겨둔 부분 — 라운드 간 점수 변동폭이
// 실제 diff 변화량에 비해 과하면 "결함"이 아니라 "측정 노이즈 가능성"으로
// 표시. 임계값은 보수적으로 시작 — 실측 데이터(verify-task-history) 쌓이면
// 조정할 것. 2026-07-27 개정으로 전체 트랙은 무점수가 되어 이 함수를 더 이상
// 호출하지 않지만, 경량 트랙은 그대로 이 함수를 쓴다 — 삭제하지 말 것.
function flagNoise(prevTotal, currTotal, prevFileCount, currFileCount) {
  const scoreDelta = Math.abs(currTotal - prevTotal)
  const fileDelta = Math.abs(currFileCount - prevFileCount)
  if (fileDelta <= 1 && scoreDelta >= 20) return true
  if (fileDelta <= 3 && scoreDelta >= 40) return true
  return false
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
const historyFile = parsedArgs.historyFile || '/Users/edge_ai/.claude/verify-task-v2-history.jsonl'
const HARNESS_FILE = parsedArgs.harnessFile || HARNESS_FILE_DEFAULT

if (!cwd) {
  return {
    finalVerdict: { passed: false, error: 'missing_cwd' },
    reason: 'verify-task-v2는 컨텍스트 수집(git status/diff)이 반드시 필요해서 cwd가 필수야. verify-task(v1)와 다름 — v1은 cwd 선택, v2는 필수.',
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

let tier = decideTier(context)
log(`티어 판정: ${tier} (예상 파일 ${context.intendedFiles?.length ?? '?'}개, 민감경로: ${context.sensitivePath})`)

const history = []
let finalVerdict = null
let baseline = null // 탈출구 발동 시 경량 트랙 산출물을 여기 보관 — 전체 트랙에서 1~8단계(계획/비평/실행)를 생략하고 바로 코드리뷰로 직행하는 데 씀
let harnessRulesAddedCount = 0

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

    const combinedIssues = [
      ...(claudeReview?.issues || []).map((i) => ({ ...i, source: 'claude' })),
      ...(antigravityReview?.issues || []).map((i) => ({ ...i, source: 'antigravity' })),
    ]
    history.push({ tier: 'full', round, claudeReview, antigravityReview })

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

async function notifyDiscordEscalation(message) {
  // Best-effort, one-way (Mac → Discord) — same discord-notify.sh used by
  // weekly-report.sh/work-log-stop-check.sh. Failure here must never affect
  // finalVerdict; this is purely a side-channel nudge.
  try {
    await agent(
      `Bash로 정확히 아래 명령을 실행해줘 (실패해도 무시하고 결과만 알려줘):\nbash "$HOME/mac-agent/bin/discord-notify.sh" ${JSON.stringify(message)}`,
      { phase: 'FullReview', label: 'discord-notify', agentType: 'general-purpose' }
    )
  } catch (e) {
    log(`디스코드 알림 실패(무시): ${e}`)
  }
}

async function finalizeAndReturn() {
  if (finalVerdict?.needsUserDecision || finalVerdict?.error === 'needs_clarification') {
    const shortTask = typeof task === 'string' ? task.slice(0, 200) : String(task)
    const reason = finalVerdict?.reason || finalVerdict?.questions || '(사유 없음)'
    await notifyDiscordEscalation(
      `⚠️ verify-task-v2 에스컬레이션 (${tier} 트랙) — "${shortTask}"\n${reason}`
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
    },
    historyFile
  )
  return { finalVerdict, tier, history }
}

return await finalizeAndReturn()
