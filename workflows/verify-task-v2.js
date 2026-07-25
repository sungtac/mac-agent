export const meta = {
  name: 'verify-task-v2',
  description: '작업 시작 전 스펙 고정 + 경량/전체 티어별 다자간 검증 (Claude/Codex/Antigravity). 설계: docs/verify-task-v2-design.md',
  phases: [
    { title: 'Preflight' },
    { title: 'Context' },
    { title: 'Light' },
    { title: 'FullSpec' },
    { title: 'FullDraft' },
    { title: 'FullExecute' },
    { title: 'FullEvaluate' },
  ],
}

// 설계 전체는 docs/verify-task-v2-design.md 참고 — 이 스크립트는 결정 기록이
// 아니라 구현이다. 결정의 "왜"를 다시 읽지 않고 이 파일만 고치지 말 것.
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

// score-dispatch.sh는 읽기전용 채점/의견용(codex 또는 agy 모두 --sandbox
// read-only 기본값). 실제 파일을 쓰는 유일한 지점(전체 트랙 3단계 실행)만
// codex-execute-dispatch.sh(-s workspace-write)를 쓴다 — 절대 섞어 쓰지 말 것.
function buildScoreDispatchInstruction(tool, prompt) {
  return `1. Bash로 \`mktemp /tmp/verify-task-v2-${tool}-XXXXXX.txt\` 실행해서 임시 파일 경로를 얻어.\n2. Write 툴로 그 경로에 아래 [프롬프트 내용]을 정확히 그대로(글자 하나 고치지 말고) 저장해.\n3. Bash로 다음을 실행해 (파일경로는 2번 경로로 치환): bash ${SCORE_DISPATCH} ${tool} <파일경로>\n4. 실행이 끝나면 Bash로 그 임시 파일을 삭제해.\n5. 3번 명령의 stdout은 이미 검증된 JSON 한 줄이야 — 그 값을 그대로 구조화된 출력으로 반환해. 내용을 고치거나, 재해석하거나, 다른 값으로 대체하지 마.\n\n[프롬프트 내용]\n${prompt}`
}

function buildExecuteDispatchInstruction(cwd, prompt) {
  return `1. Bash로 \`mktemp /tmp/verify-task-v2-exec-XXXXXX.txt\` 실행해서 임시 파일 경로를 얻어.\n2. Write 툴로 그 경로에 아래 [프롬프트 내용]을 정확히 그대로(글자 하나 고치지 말고) 저장해.\n3. Bash로 다음을 실행해 (파일경로는 2번 경로로 치환, timeout 300000ms 이상 줘): bash ${CODEX_EXECUTE_DISPATCH} ${JSON.stringify(cwd)} <파일경로>\n4. 실행이 끝나면 Bash로 그 임시 파일을 삭제해.\n5. 3번 명령의 stdout은 이미 검증된 JSON 한 줄이야({"ok":bool,"message":string}) — 그 값을 그대로 구조화된 출력으로 반환해. 내용을 고치거나, 재해석하거나, 다른 값으로 대체하지 마. 이 결과는 코덱스 자체 보고일 뿐 실제 검증이 아님을 기억해 — 실제 변경사항은 별도로 git diff로 확인할 거야.\n\n[프롬프트 내용]\n${prompt}`
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

async function gatherRealDiff(cwd) {
  const gathered = await agent(
    `Bash로 아래 명령을 그 디렉토리에서 실행하고, 나온 출력을 절대 요약하거나 고치지 말고 content 필드에 그대로 담아 반환해:\n\ncd ${JSON.stringify(cwd)} && { echo '--- git diff --stat ---'; git diff --stat HEAD; echo '--- git diff HEAD ---'; git diff HEAD; } 2>&1\n\n출력이 8000자를 넘으면 앞 8000자만 남기고 끝에 "...(잘림)"을 붙여.\n\n추가로: git diff --stat HEAD의 출력에서 실제로 변경된 파일 경로들만 뽑아 filesChanged 배열에 넣고, 그중 설정/보안/.github/workflows//공개문서(README 등)에 해당하는 게 하나라도 있으면 sensitivePath=true로 반환해.`,
    { phase: 'Light', label: 'gather-real-diff', schema: REAL_DIFF_SCHEMA }
  )
  return gathered
}

function mechanicalTierViolated(realDiff) {
  const fileCount = (realDiff?.filesChanged || []).length
  return fileCount > 3 || !!realDiff?.sensitivePath
}

// ---------- 경량 트랙 ----------

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

async function codexEvaluateLight(task, context, summary, realDiff) {
  const prompt = buildLightEvalPrompt(task, context, summary, realDiff)
  return agent(buildScoreDispatchInstruction('codex', prompt), {
    phase: 'Light',
    label: 'light-eval-codex',
    schema: LIGHT_EVAL_SCHEMA,
  })
}

// ---------- 전체 트랙: 0단계 (스펙 + 고정/동적 채점표, 안티그래비티) ----------

const FIXED_RUBRIC = `[고정 채점 항목 — 65점, 모든 작업 공통]
- 정확성 20점: 버그·논리 오류 없음
- 완전성 20점: 명시적 요청(목표) 달성 + 숨은 의도·엣지케이스 반영
- 안전성 15점: 되돌리기 가능성, 기존 기능 파손(regression) 없음
- 기존 컨벤션 준수 10점: 저장소 관례(CLAUDE.md 등)·코드 스타일 위반 없음`

const FULL_SPEC_SCHEMA = {
  type: 'object',
  properties: {
    needsClarification: { type: 'boolean' },
    clarifyingQuestions: { type: 'string' },
    spec: { type: 'string' },
    dynamicRubricItems: { type: 'string' },
    vetoConditions: { type: 'string' },
  },
  required: ['needsClarification'],
}

function buildFullSpecPrompt(task, context, objection) {
  const objectionBlock = objection
    ? `\n\n[코덱스의 반박 — 이전에 네가 쓴 스펙/채점표에 대한 이의제기, 검토해서 타당하면 반영하고 아니면 그대로 유지해도 됨]\n${objection}`
    : ''
  return `너는 이 작업의 기준을 고정하는 역할이야. 아래는 사용자의 원 지시문과 저장소 컨텍스트뿐이고, 어떤 해법·초안도 안 보여줌 — 절대 "어떻게 할지"는 스펙에 쓰지 마.

[원 지시문]
${task}

[저장소 컨텍스트]
${context.contextText}${objectionBlock}

1. 정보 충분성 판단: 목적·완료기준을 판단할 수 있어? 부족하면 needsClarification=true로 하고 clarifyingQuestions에 필수 질문 최대 3개 + 선택 질문 최대 3개를 적어(그 이상 필요해도 아는 만큼 스펙을 쓰고 "확인 필요"라고 표시). 충분하면 needsClarification=false.
2. (충분하면) spec에: 목표(한 문단) / 산출물(형식·경로) / 제약 / 종료조건(기계적으로 판정 가능한 것만)을 적어.
3. (충분하면) 아래 고정 채점 항목(65점, 이미 확정됨 — 네가 다시 쓰지 마)에 더해, 이번 작업 고유의 종료조건만 35점 배점으로 dynamicRubricItems에 항목별로 적어. 종료조건에서 직접 도출된 것만, 즉흥적으로 지어내지 마.
${FIXED_RUBRIC}
4. vetoConditions에 거부권 항목(걸리면 총점 무관 즉시 반려 — 보안 위험, 데이터 손실, 명시적 요구사항 누락 등)을 적어.

JSON으로만 답해: {"needsClarification":false,"clarifyingQuestions":"","spec":"","dynamicRubricItems":"","vetoConditions":""}`
}

async function fullSpec(task, context, objection) {
  const prompt = buildFullSpecPrompt(task, context, objection)
  return agent(buildScoreDispatchInstruction('agy', prompt), {
    phase: 'FullSpec',
    label: objection ? 'full-spec-revised' : 'full-spec',
    schema: FULL_SPEC_SCHEMA,
  })
}

const CODEX_REBUTTAL_SCHEMA = {
  type: 'object',
  properties: { hasObjection: { type: 'boolean' }, objectionText: { type: 'string' } },
  required: ['hasObjection'],
}

async function codexRebuttal(spec, context) {
  const prompt = `안티그래비티가 아래 스펙과 채점표를 작성했어(둘 다 안티그래비티 혼자 작성 — 검증 없이 그대로 쓰면 자기참조 문제가 생기니까, 네가 딱 한 번 반박할 기회야). 스펙 자체를 다시 쓰지 말고, "이거 하나 빠졌다" "이 기준 이상하다" 같은 지적만 해.

[저장소 컨텍스트]
${context.contextText}

[스펙]
${spec.spec}

[동적 채점 항목]
${spec.dynamicRubricItems}

[거부권 항목]
${spec.vetoConditions}

이의 없으면 hasObjection=false. 있으면 hasObjection=true, objectionText에 구체적으로.

JSON으로만: {"hasObjection":false,"objectionText":""}`
  return agent(buildScoreDispatchInstruction('codex', prompt), {
    phase: 'FullSpec',
    label: 'codex-rebuttal',
    schema: CODEX_REBUTTAL_SCHEMA,
  })
}

// ---------- 전체 트랙: 1단계 (초안 경쟁) ----------

const DRAFT_SCHEMA = { type: 'object', properties: { approach: { type: 'string' } }, required: ['approach'] }

function buildDraftPrompt(spec, context) {
  return `아래 스펙만 보고(다른 에이전트의 의견은 안 보여줌) 이 작업에 어떻게 접근할지 제안해줘. 실제로 실행하지 말고 접근 방식/의견만.

[저장소 컨텍스트]
${context.contextText}

[스펙]
${spec.spec}

[종료조건 관련 채점 항목]
${spec.dynamicRubricItems}

JSON으로만: {"approach":"구체적 접근 방식"}`
}

async function codexDraft(spec, context) {
  return agent(buildScoreDispatchInstruction('codex', buildDraftPrompt(spec, context)), {
    phase: 'FullDraft',
    label: 'draft-codex',
    schema: DRAFT_SCHEMA,
  })
}

async function antigravityDraft(spec, context) {
  return agent(buildScoreDispatchInstruction('agy', buildDraftPrompt(spec, context)), {
    phase: 'FullDraft',
    label: 'draft-antigravity',
    schema: DRAFT_SCHEMA,
  })
}

// ---------- 전체 트랙: 2단계 (종합, 클로드) ----------

async function synthesize(spec, draftCodex, draftAntigravity, baseline) {
  const baselineBlock = baseline
    ? `\n\n[기존 베이스라인 — 경량 트랙에서 이미 실행된 산출물, 버리지 말고 이걸 보완하는 방향으로]\n${baseline.summary}`
    : ''
  const codexBlock = draftCodex ? `\n\n[코덱스 초안]\n${draftCodex.approach}` : ''
  return agent(
    `두 초안을 비교해서, 실제 실행을 맡을 코덱스에게 줄 **짧은** 지시문만 써줘 — 전체를 다시 쓰지 말고 "이 방향으로 가되 이 부분 보완" 정도로.${baselineBlock}${codexBlock}\n\n[안티그래비티 초안]\n${draftAntigravity.approach}\n\n[스펙]\n${spec.spec}\n\n지시문 텍스트만 출력해(부가 설명 없이).`,
    { phase: 'FullDraft', label: 'synthesize', agentType: 'general-purpose' }
  )
}

// ---------- 전체 트랙: 3단계 (실행, 코덱스, 쓰기 가능) ----------

async function fullExecute(cwd, instruction, context) {
  const prompt = `아래 지시대로 실제로 파일을 수정/생성해줘. 작업 디렉토리: ${cwd}\n\n[저장소 컨텍스트]\n${context.contextText}\n\n[지시]\n${instruction}\n\n다 하고 나서 뭘 했는지 짧게 설명해.`
  return agent(buildExecuteDispatchInstruction(cwd, prompt), {
    phase: 'FullExecute',
    label: 'full-execute',
    schema: EXECUTE_ENVELOPE_SCHEMA,
  })
}

// ---------- 전체 트랙: 4단계 (평가, 안티그래비티) ----------

const FULL_EVAL_SCHEMA = {
  type: 'object',
  properties: {
    fixedScore: { type: 'number' },
    dynamicScore: { type: 'number' },
    total: { type: 'number' },
    veto: { type: 'boolean' },
    vetoReason: { type: 'string' },
    feedback: { type: 'string' },
  },
  required: ['fixedScore', 'dynamicScore', 'total', 'veto', 'feedback'],
}

function buildFullEvalPrompt(spec, realDiff) {
  return `너는 독립 채점자야. 네가 0단계에서 만든 아래 채점표로, 실제 변경사항(git diff — 이게 진실)을 채점해.

${FIXED_RUBRIC}

[동적 채점 항목 — 35점]
${spec.dynamicRubricItems}

[거부권 항목 — 걸리면 총점 무관 즉시 반려]
${spec.vetoConditions}

[스펙]
${spec.spec}

[실제 변경사항 — git diff]
${realDiff.content}

거부권 항목에 걸리면 veto=true, vetoReason에 구체적으로. 안 걸리면 veto=false로 하고 fixedScore(65점 만점)+dynamicScore(35점 만점)를 매겨 total에 합산. feedback에 구체적 감점 사유와 개선점.

JSON으로만: {"fixedScore":0,"dynamicScore":0,"total":0,"veto":false,"vetoReason":"","feedback":""}`
}

async function evaluateFull(spec, realDiff) {
  const prompt = buildFullEvalPrompt(spec, realDiff)
  return agent(buildScoreDispatchInstruction('agy', prompt), {
    phase: 'FullEvaluate',
    label: 'full-eval',
    schema: FULL_EVAL_SCHEMA,
  })
}

// ---------- 노이즈 감지 ----------
// 설계상 "구현 시 정할 파라미터"로 남겨둔 부분 — 라운드 간 점수 변동폭이
// 실제 diff 변화량에 비해 과하면 "결함"이 아니라 "측정 노이즈 가능성"으로
// 표시. 임계값은 보수적으로 시작 — 실측 데이터(verify-task-history) 쌓이면
// 조정할 것.
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
    { phase: 'FullEvaluate', label: 'history-append', agentType: 'general-purpose' }
  )
}

// ==================== 메인 흐름 ====================

const parsedArgs = typeof args === 'string' ? JSON.parse(args) : (args || {})
const MAX_ROUNDS = parsedArgs.maxRounds || 2
const task = parsedArgs.task
const persona = parsedArgs.persona || '일반 사용자'
const cwd = parsedArgs.cwd
const historyFile = parsedArgs.historyFile || '/Users/edge_ai/.claude/verify-task-v2-history.jsonl'

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
let baseline = null // 탈출구 발동 시 경량 트랙 산출물을 여기 보관, 전체 트랙 1단계에서 재사용

// ---------- 경량 트랙 ----------
if (tier === 'light') {
  let feedback = null
  for (let round = 1; round <= MAX_ROUNDS; round++) {
    log(`[경량] 라운드 ${round}: 클로드 실행 중...`)
    const execResult = await lightExecute(task, context, cwd, feedback)
    const realDiff = await gatherRealDiff(cwd)

    log(`[경량] 라운드 ${round}: 코덱스 평가 중...`)
    const evalResult = await codexEvaluateLight(task, context, execResult?.summary, realDiff)
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
if (tier === 'full' && !finalVerdict) {
  log('[전체] 0단계: 안티그래비티 스펙+채점표 작성 중...')
  let spec = await fullSpec(task, context, null)

  if (spec?.needsClarification) {
    finalVerdict = {
      passed: false,
      tier: 'full',
      error: 'needs_clarification',
      questions: spec.clarifyingQuestions,
      reason:
        'Workflow 스크립트는 AskUserQuestion을 직접 못 부름. 호출한 에이전트가 questions를 사용자에게 AskUserQuestion으로 물어보고(필수 최대 3개+선택 최대 3개, 왕복 최대 2회), 답변을 원 task 문자열 끝에 덧붙여서 이 워크플로우를 다시 호출해야 함.',
    }
    log('[전체] 0단계: 정보 부족 — 역질문 필요')
    return await finalizeAndReturn()
  }

  log('[전체] 0단계: 코덱스 반박권...')
  const rebuttal = await codexRebuttal(spec, context)
  if (rebuttal?.hasObjection) {
    log('[전체] 코덱스 반박 있음 — 안티그래비티 1회 수정...')
    spec = await fullSpec(task, context, rebuttal.objectionText)
  }

  log('[전체] 1단계: 초안 경쟁...')
  let draftCodex = null
  let draftAntigravity
  if (baseline) {
    // 탈출구로 진입한 경우: 코덱스 초안 단계 생략(실물 베이스라인이 이미
    // "의견"보다 나은 정보) — 안티그래비티만 독립적으로 접근 제시.
    draftAntigravity = await antigravityDraft(spec, context)
  } else {
    ;[draftCodex, draftAntigravity] = await parallel([
      () => codexDraft(spec, context),
      () => antigravityDraft(spec, context),
    ])
  }

  let prevTotal = null
  let prevFileCount = null

  for (let round = 1; round <= MAX_ROUNDS; round++) {
    let instruction
    if (round === 1) {
      log('[전체] 2단계: 클로드 종합 중...')
      instruction = await synthesize(spec, draftCodex, draftAntigravity, baseline)
    } else {
      // 2회차부터: 클로드는 다시 종합하지 않고 안티의 지적을 그대로 전달만.
      instruction = history[history.length - 1]?.evalResult?.feedback ?? ''
      log('[전체] 재시도: 안티그래비티 피드백을 코덱스에게 그대로 전달')
    }

    log(`[전체] 라운드 ${round}: 코덱스 실행 중...`)
    const execEnvelope = await fullExecute(cwd, instruction, context)
    const realDiff = await gatherRealDiff(cwd)

    log(`[전체] 라운드 ${round}: 안티그래비티 평가 중...`)
    const evalResult = await evaluateFull(spec, realDiff)
    history.push({ tier: 'full', round, execEnvelope, evalResult })

    const currFileCount = realDiff?.filesChanged?.length ?? 0
    const noise = prevTotal !== null ? flagNoise(prevTotal, evalResult?.total ?? 0, prevFileCount, currFileCount) : false
    prevTotal = evalResult?.total ?? 0
    prevFileCount = currFileCount

    if (evalResult?.veto) {
      log(`[전체] 라운드 ${round}: 거부권 발동 — ${evalResult.vetoReason}`)
      if (round === MAX_ROUNDS) {
        finalVerdict = {
          passed: false,
          tier: 'full',
          round,
          evalResult,
          noise,
          needsUserDecision: true,
          reason: `전체 트랙 최대 ${MAX_ROUNDS}라운드 안에 거부권을 못 벗어남. 사용자에게 물어야 함.`,
        }
        break
      }
      continue
    }

    if ((evalResult?.total ?? 0) >= 90) {
      finalVerdict = { passed: true, tier: 'full', round, evalResult, noise, wasEscapeHatch: !!baseline }
      log(`[전체] 라운드 ${round}에서 통과 (${evalResult.total}점)${noise ? ' — 단, 노이즈 가능성 있음' : ''}`)
      break
    }

    if (round === MAX_ROUNDS) {
      finalVerdict = {
        passed: false,
        tier: 'full',
        round,
        evalResult,
        noise,
        needsUserDecision: true,
        reason: `전체 트랙 최대 ${MAX_ROUNDS}라운드 안에 90점을 못 넘김. 사용자에게 물어야 함.`,
      }
      break
    }

    log(`[전체] 라운드 ${round} 미통과 (${evalResult?.total ?? '?'}점)${noise ? ' — 노이즈 가능성' : ''}`)
  }
}

async function finalizeAndReturn() {
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
      finalTotal: finalVerdict?.evalResult?.total ?? null,
    },
    historyFile
  )
  return { finalVerdict, tier, history }
}

return await finalizeAndReturn()
