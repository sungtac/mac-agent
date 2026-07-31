export const meta = {
  name: 'verify-task',
  description: 'Score a completed task with Codex + Antigravity(Gemini) against a shared rubric, revise, and loop until it passes',
  phases: [
    { title: 'Preflight' },
    { title: 'Score' },
    { title: 'Revise' },
  ],
}

const PREFLIGHT_SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    issues: { type: 'string' },
  },
  required: ['ok'],
}

async function preflightCheck() {
  // 2026-07-30 fix (Codex 코드리뷰로 발견, 낮은 우선순위로 보류했다가
  // 처리): score-dispatch.sh는 CODEX_BIN/AGY_BIN 환경변수 override를
  // 지원하는데, 이 preflight 프롬프트는 그거랑 무관하게 항상 이 머신의
  // 절대경로를 하드코딩해서 실제로는 "다른 머신으로 포팅 가능"이라는
  // 주장이 이 지점에서 깨져 있었다. score-dispatch.sh와 동일한
  // `${VAR:-기본값}` bash 파라미터 확장 관례를 그대로 프롬프트 텍스트에
  // 심어서, 실행 시점에 그 Bash 호출 환경에 CODEX_BIN/AGY_BIN이 설정돼
  // 있으면 그걸 쓰고 없으면 기존 기본값으로 폴백하도록 통일.
  return agent(
    `Bash 툴로 아래 두 명령을 순서대로 실행해줘 (CODEX_BIN/AGY_BIN 환경변수가 설정돼 있으면 그 경로를 쓰고, 없으면 Homebrew/사용자 bin 후보를 찾아 써 — 축소된 launchd PATH에서 bare 명령어만 믿지 말 것):\n1. "\${CODEX_BIN:-/opt/homebrew/bin/codex}" login status\n2. env -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT "\${AGY_BIN:-\$HOME/.local/bin/agy}" models\n\n두 명령 다 에러 없이 성공(로그인된 상태)이면 ok=true, issues는 빈 문자열로 반환해. 하나라도 로그인 필요/에러가 나면 ok=false로 하고, 어떤 도구가 문제인지와 해결 방법(예: "터미널에서 codex login 실행(CODEX_BIN 설정돼 있으면 그 경로로)" 또는 "터미널에서 agy 실행 후 로그인, 저장소의 setup.sh 참고")을 issues에 적어줘.`,
    { phase: 'Preflight', label: 'preflight', schema: PREFLIGHT_SCHEMA }
  )
}

const RUBRIC = `[100점 만점 루브릭]
- 목표 달성도 30점: 명시적 요청 완료(15) / 숨은 의도·맥락 반영(10) / 스코프 이탈 없음(5)
- 정확성 25점: 버그·논리 오류 없음(15) / 사실·데이터 정확(10)
- 제약·안전성 준수 15점: 지정 형식/범위/도구 제약 준수(5) / 보안·개인정보 위험 없음(5) / 기존 기능 파손(regression) 없음(5)
- 완성도 15점: 예외·엣지케이스 처리(8) / 검증·테스트까지 완료(7)
- 명확성 10점: 이해하기 쉬운 설명(5) / 사용자 수준에 맞는 표현(5)
- 효율성 5점: 불필요한 과설계 없음(3) / 간결한 접근(2)

총 100점, 85점 이상 통과.
과락 규칙: 목표 달성도·정확성·제약·안전성 중 한 항목이라도 해당 영역 배점의 50% 미만이면 총점과 무관하게 자동 불합격.`

const SCORE_SCHEMA = {
  type: 'object',
  properties: {
    scores: {
      type: 'object',
      properties: {
        목표달성도: { type: 'number' },
        정확성: { type: 'number' },
        제약안전성: { type: 'number' },
        완성도: { type: 'number' },
        명확성: { type: 'number' },
        효율성: { type: 'number' },
      },
      required: ['목표달성도', '정확성', '제약안전성', '완성도', '명확성', '효율성'],
    },
    total: { type: 'number' },
    dealbreaker: { type: 'boolean' },
    dealbreaker_reason: { type: 'string' },
    feedback: { type: 'string' },
  },
  required: ['scores', 'total', 'dealbreaker', 'feedback'],
}

const SHARED_PROFILE_RULES = `공통 답변 규칙: 일반 사용자가 이해할 수 있는 고등학생 수준으로 설명하고 결론을 먼저 말해. 어려운 용어는 처음 나올 때 풀어 쓰고, 실제로 하지 않은 일을 완료했다고 말하지 마. 사용자에게 보이는 답변에는 장식용 ###와 ** 문법을 사용하지 마.`
const LEGACY_AGENT_PROFILES = {
  codex: 'Codex는 정밀 구현 및 검증 엔지니어이며 실제 diff와 테스트를 근거로 판단한다.',
  antigravity: 'Antigravity는 독립 조사관이자 레드팀 검증자이며 계획의 허점과 반례를 찾는다.',
}

function buildScoringPrompt(task, result, persona, cwd, verificationContent) {
  let verificationBlock
  if (verificationContent) {
    verificationBlock = `[실제 검증 자료 — 이미 수집된 git 상태/로그/diff]\n${verificationContent}\n\n아래 [에이전트가 보고한 결과 요약]은 신뢰하지 마. 위 실제 자료와 보고 내용이 일치하는지 대조해서 채점해. 너는 셸 명령을 실행할 수 없는 환경이니 절대 실행을 시도하지 말고, 위에 이미 주어진 자료만으로 판단해 — feedback에 위 자료 중 무엇을 근거로 이 결론에 도달했는지 적어.`
  } else if (cwd) {
    verificationBlock = `[실제 작업 디렉토리]\n${cwd}\n\n아래 [에이전트가 보고한 결과 요약]은 신뢰하지 마. 반드시 이 디렉토리에서 git diff / git log / 실제 파일 읽기 등을 직접 수행해서, 보고 내용이 실제 변경사항과 일치하는지 검증한 뒤 그 실제 확인 결과를 근거로 채점해. 보고서 문장만 보고 점수를 매기면 안 됨 — feedback에 어떤 파일/명령으로 확인했는지 반드시 적어.`
  } else {
    verificationBlock = `[경고] 실제 작업 디렉토리가 제공되지 않아 아래 텍스트 보고만으로 채점함 — 실제 파일/변경사항을 검증할 수 없음. 이 사실을 feedback에 명시하고, 검증 불가능한 주장(예: "테스트를 통과했다", "버그를 고쳤다")에 대해서는 액면 그대로 믿지 말고 정확성/완성도 점수를 보수적으로 낮게 잡을 것.`
  }

  return `${SHARED_PROFILE_RULES}
${LEGACY_AGENT_PROFILES.codex}
${LEGACY_AGENT_PROFILES.antigravity}

너는 독립 채점자야. 아래 루브릭으로 AI 에이전트의 작업 결과를 채점해.

${RUBRIC}

이 작업을 요청한 사용자 수준: ${persona}

[요청받은 작업]
${task}

[에이전트가 보고한 결과 요약]
${result}

${verificationBlock}

반드시 아래 JSON 형식으로만 답해 (다른 설명 텍스트 없이 JSON 객체 하나만):
{"scores":{"목표달성도":0,"정확성":0,"제약안전성":0,"완성도":0,"명확성":0,"효율성":0},"total":0,"dealbreaker":false,"dealbreaker_reason":"","feedback":"구체적인 감점 사유와 개선점, 그리고 실제로 무엇을 확인해서 이 결론에 도달했는지"}`
}

const MAC_AGENT_ROOT = process.env.MAC_AGENT_ROOT || '/Users/edge_ai/mac-agent'
const DISPATCH_SCRIPT = process.env.SCORE_DISPATCH_SCRIPT || `${MAC_AGENT_ROOT}/workflows/lib/score-dispatch.sh`

function buildDispatchInstruction(tool, prompt) {
  return `1. Bash로 \`mktemp /tmp/verify-task-${tool}-XXXXXX.txt\` 실행해서 임시 파일 경로를 얻어.\n2. 반드시 Read 툴로 방금 얻은 임시 파일을 한 번 읽어(빈 파일이어도 괜찮아 — Claude의 Write 계약상 먼저 읽은 파일만 Write할 수 있음).\n3. Write 툴로 그 경로에 아래 [프롬프트 내용]을 정확히 그대로(글자 하나 고치지 말고) 저장해.\n4. Bash로 다음을 실행해 (파일경로는 3번 경로로 치환): bash ${DISPATCH_SCRIPT} ${tool} <파일경로>\n5. 실행이 끝나면 Bash로 그 임시 파일을 삭제해.\n6. 4번 명령의 stdout은 이미 검증된 JSON 한 줄이야(성공이든 실패든 스크립트가 결정적으로 만든 값) — 그 값을 그대로 구조화된 출력으로 반환해. 내용을 고치거나, 재해석하거나, 다른 값으로 대체하지 마.\n\n[프롬프트 내용]\n${prompt}`
}

async function scoreWithCodex(task, result, persona, cwd, verificationContent, isRetry) {
  const prompt = buildScoringPrompt(task, result, persona, cwd, verificationContent)
  return agent(buildDispatchInstruction('codex', prompt), { phase: 'Score', label: isRetry ? 'codex-retry' : 'codex', schema: SCORE_SCHEMA })
}

const CONTEXT_SCHEMA = {
  type: 'object',
  properties: {
    content: { type: 'string' },
  },
  required: ['content'],
}

// 주의: `git diff HEAD`는 아직 add된 적 없는 untracked 신규 파일을 절대 보여주지
// 않는다 — 실측(verify-task-v2 종단 테스트, 2026-07-27~28)으로 이 누락 때문에
// 셸 실행 불가 환경인 코덱스/제미나이 채점자(score-dispatch.sh는 -C 없이
// 호출돼 둘 다 저장소를 직접 못 봄, 이 함수가 만든 텍스트만 봄)가 이미 정확히
// 생성된 파일을 "누락됐다"고 거짓 채점한 사례가 실제로 발생함. 그래서
// git status --porcelain으로 신규(untracked) 파일 목록을 뽑아 전체 내용을
// 별도 섹션으로 반드시 덧붙인다 — git add 등으로 실제 git 상태를 건드리지 않고
// 읽기만 한다.
async function gatherVerificationContext(cwd) {
  if (!cwd) return null
  const gathered = await agent(
    `Bash로 아래 한 명령을 그 디렉토리에서 실행하고, 나온 출력을 절대 요약하거나 고치지 말고 그대로 content 필드에 담아 반환해:\n\ncd ${JSON.stringify(cwd)} && { echo '--- git log (최근 10개) ---'; git log --oneline -10; echo '--- git status --porcelain ---'; git status --porcelain; echo '--- 커밋되지 않은 tracked 변경 (git diff HEAD) ---'; git diff HEAD; echo '--- untracked 신규 파일 전체 내용 (git diff에는 안 잡힘) ---'; git status --porcelain | awk '$1 == "??" {print $2}' | while IFS= read -r f; do echo "=== NEW FILE: $f ==="; cat "$f"; done; echo '--- 최근 커밋 (git show HEAD) ---'; git show HEAD; } 2>&1\n\n출력이 8000자를 넘으면 앞 8000자만 남기고 끝에 "...(잘림)"을 붙여서 반환해.`,
    { phase: 'Score', label: 'gather-context', schema: CONTEXT_SCHEMA }
  )
  return gathered?.content || null
}

async function scoreWithGemini(task, result, persona, cwd, verificationContent, isRetry) {
  const prompt = buildScoringPrompt(task, result, persona, cwd, verificationContent)
  return agent(buildDispatchInstruction('agy', prompt), { phase: 'Score', label: isRetry ? 'gemini-retry' : 'gemini', schema: SCORE_SCHEMA })
}

// score-dispatch.sh는 codex/agy 실행 또는 JSON 파싱이 실패하면 항상 이 정확한
// 문구를 dealbreaker_reason에 담아 고정 실패 봉투(scores 전부 0, total 0,
// dealbreaker true)를 반환한다 — 실제 작업 품질에 대한 판단이 아니라 도구
// 실행 자체의 실패 신호. 이 문구가 바뀌면 score-dispatch.sh의 FAILURE_ENVELOPE()
// 도 같이 바꿔야 함(둘이 반드시 동기화돼야 하는 상수).
const DISPATCH_FAILURE_REASON = '채점 도구 실행/파싱 실패 — 작업 내용에 대한 판단 아님'

function isDispatchFailure(score) {
  return !!score && score.dealbreaker_reason === DISPATCH_FAILURE_REASON
}

// 실측(2026-07-24, factorial 2차 시도): Codex 94점 vs Gemini 0점처럼 한쪽이
// 도구 실행/파싱 실패로 극단적 이상치를 내는 사례가 실제로 있었음. 통과 기준
// 자체(85점, 둘 다 만족)는 그대로 두고, 이런 이상치만 1회 자동 재채점해서
// 진짜 낮은 점수와 도구 실패를 구분한다.
async function scoreWithDispatchRetry(scoreFn, graderName) {
  let score = await scoreFn(false)
  if (isDispatchFailure(score)) {
    log(`${graderName} 채점이 도구 실행/파싱 실패로 보여 — 1회 재채점 중...`)
    score = await scoreFn(true)
  }
  return score
}

function passes(score) {
  if (!score) return false
  if (score.dealbreaker) return false
  return score.total >= 85
}

async function appendHistory(record, historyFile) {
  const recordJson = JSON.stringify(record)
  await agent(
    `아래 JSON 레코드 한 건을 히스토리 로그 파일에 한 줄(JSONL)로 추가(append)해줘. 기존 파일 내용은 절대 건드리지 말고 끝에 한 줄만 추가해.\n\n1. Bash로 \`mkdir -p $(dirname ${historyFile})\` 실행.\n2. Bash로 \`date -u +%Y-%m-%dT%H:%M:%SZ\` 실행해서 현재 UTC 시각을 얻어.\n3. 아래 JSON에 "timestamp" 필드로 그 시각을 추가한 뒤, 한 줄짜리 JSON 문자열로 만들어서 Bash \`cat >> ${historyFile} << 'HISTEOF'\n(그 JSON 한 줄)\nHISTEOF\` 형태로 안전하게 append 해 (JSON 안에 작은따옴표나 특수문자가 있어도 깨지지 않게 heredoc 사용).\n4. 성공하면 "ok"만 반환해.\n\n원본 JSON (timestamp 필드만 추가하고 나머지는 그대로 유지):\n${recordJson}`,
    { phase: 'Score', label: 'history-append', agentType: 'general-purpose' }
  )
}

const parsedArgs = typeof args === 'string' ? JSON.parse(args) : (args || {})
const MAX_ROUNDS = parsedArgs.maxRounds || 3
const task = parsedArgs.task
const persona = parsedArgs.persona || '일반 사용자'
const cwd = parsedArgs.cwd
const historyFile = parsedArgs.historyFile || `${process.env.HOME || '/Users/edge_ai'}/.claude/verify-task-history.jsonl`

let result = parsedArgs.result
const history = []
let finalVerdict = null

// 실측 버그 (2026-07-29, 세션 925808ac): Workflow({scriptPath, resumeFromRunId})로
// 재개할 때 args를 다시 안 넘기면(문서에 명시 안 된 함정 — resume이 이전 run의
// args를 자동으로 이어받지 않음) 이 스크립트가 매 실행 top-level에서 args를 새로
// 읽으므로 parsedArgs가 조용히 {}로 무너진다. 그 상태로도 여기까지는 아무 에러
// 없이 통과해서, task/result가 실제로 codex/gemini에게 보내는 채점 프롬프트에
// 문자 그대로 "undefined"로 들어간 채 진짜 외부 호출을 몇 차례나 낭비한 사례를
// 실제로 재현·확인함(agent-a94b8aa9...jsonl). preflight보다도 먼저, 외부 도구를
// 한 번도 부르기 전에 여기서 즉시 막는다.
if (!task || result === undefined) {
  return {
    finalVerdict: {
      passed: false,
      error: 'missing_task_or_result',
      reason: `task/result가 비어있음(task=${JSON.stringify(task)}, result=${JSON.stringify(result)}) — args가 실제로 전달되지 않았을 가능성이 높음. Workflow({scriptPath, resumeFromRunId})로 재개하는 경우에도 args는 매번 다시 넘겨야 함(resume이 이전 run의 args를 자동으로 이어받지 않음).`,
    },
    history: [],
  }
}

log('사전 점검: Codex/Antigravity 로그인 상태 확인 중...')
const preflight = await preflightCheck()
if (!preflight || preflight.ok === false) {
  const issues = preflight?.issues || '사전 점검 응답을 받지 못함'
  log(`사전 점검 실패: ${issues}`)
  return {
    finalVerdict: { passed: false, error: 'preflight_failed', issues },
    history: [],
  }
}
log('사전 점검 통과 — 채점 시작')

for (let round = 1; round <= MAX_ROUNDS; round++) {
  log(`라운드 ${round}: 검증 자료 수집 중...`)
  const verificationContent = await gatherVerificationContext(cwd)

  log(`라운드 ${round}: Codex + Gemini 채점 중...`)
  const [codexScore, geminiScore] = await parallel([
    () => scoreWithDispatchRetry((isRetry) => scoreWithCodex(task, result, persona, cwd, verificationContent, isRetry), 'Codex'),
    () => scoreWithDispatchRetry((isRetry) => scoreWithGemini(task, result, persona, cwd, verificationContent, isRetry), 'Gemini'),
  ])
  history.push({ round, result, codexScore, geminiScore })

  const bothPass = passes(codexScore) && passes(geminiScore)
  if (bothPass) {
    finalVerdict = { passed: true, round, result, codexScore, geminiScore }
    log(`라운드 ${round}에서 통과 (Codex ${codexScore?.total}점 / Gemini ${geminiScore?.total}점)`)
    break
  }

  log(`라운드 ${round} 미통과 (Codex ${codexScore?.total ?? '?'}점 / Gemini ${geminiScore?.total ?? '?'}점)`)

  if (round === MAX_ROUNDS) {
    finalVerdict = {
      passed: false,
      round,
      result,
      codexScore,
      geminiScore,
      needsUserDecision: true,
      reason: `최대 ${MAX_ROUNDS}라운드 안에 통과 기준(85점, 과락 없음)을 충족하지 못함. 호출한 에이전트는 반드시 사용자에게 물어봐야 함: (a) 현재 결과물을 그대로 수용, (b) maxRounds를 늘려 재시도, (c) 수동 개입.`,
    }
    break
  }

  log(`라운드 ${round}: 피드백 반영해서 수정 중...`)
  const revised = await agent(
    `아래는 작업 요청과 이전 결과물, 그리고 두 명의 독립 채점자의 피드백이야. 피드백을 반영해서 결과물을 개선해줘.\n${cwd ? `작업 디렉토리: ${cwd}\n` : ''}\n[요청받은 작업]\n${task}\n\n[이전 결과물]\n${result}\n\n[Codex 피드백] (총점 ${codexScore?.total ?? '?'}/100, 과락: ${codexScore?.dealbreaker})\n${codexScore?.feedback ?? '(응답 파싱 실패)'}\n\n[Gemini 피드백] (총점 ${geminiScore?.total ?? '?'}/100, 과락: ${geminiScore?.dealbreaker})\n${geminiScore?.feedback ?? '(응답 파싱 실패)'}\n\n개선된 최종 결과물만 출력해 (부가 설명 없이 결과물 자체만).`,
    { phase: 'Revise', label: `revise-round-${round}`, agentType: 'general-purpose' }
  )
  // 실측 버그 (2026-07-29, 같은 세션): 이 revise 호출이 세션/사용 한도 등
  // 일시적 오류로 죽으면 agent()는 null을 반환한다(Workflow 툴 자체 문서: "터미널
  // API 오류로 죽으면 null"). 예전 코드는 `result = await agent(...)`로 이 null을
  // 그대로 덮어써서, 멀쩡했던 result가 다음 라운드부터 계속 null로 채점자에게
  // 전달됐다(실측: journal.jsonl에 이 라운드의 revise 호출이 started만 있고
  // result가 없음, 이후 라운드 finalVerdict.result가 null). null이면 이전
  // result를 그대로 유지해 다음 라운드에서 같은 채점을 다시 시도한다 — 조용히
  // 덮어써서 데이터를 잃는 대신, 최소한 라운드 하나를 "공짜 재시도"로 쓴다.
  if (revised === null) {
    log(`라운드 ${round}: 수정 에이전트 호출이 실패함(세션/사용 한도 등 일시적 오류 가능성) — 결과물을 덮어쓰지 않고 이전 결과물 그대로 다음 라운드 재시도`)
  } else {
    result = revised
  }
}

log('채점 히스토리 저장 중...')
await appendHistory(
  {
    task: typeof task === 'string' ? task.slice(0, 300) : task,
    persona,
    rounds: history.length,
    passed: !!finalVerdict?.passed,
    needsUserDecision: !!finalVerdict?.needsUserDecision,
    finalCodexTotal: finalVerdict?.codexScore?.total ?? null,
    finalGeminiTotal: finalVerdict?.geminiScore?.total ?? null,
  },
  historyFile
)

return { finalVerdict, history }
