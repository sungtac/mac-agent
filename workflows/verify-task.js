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
  return agent(
    `Bash 툴로 아래 두 명령을 순서대로 실행해줘:\n1. codex login status\n2. env -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT /Users/edge_ai/.local/bin/agy models\n\n두 명령 다 에러 없이 성공(로그인된 상태)이면 ok=true, issues는 빈 문자열로 반환해. 하나라도 로그인 필요/에러가 나면 ok=false로 하고, 어떤 도구가 문제인지와 해결 방법(예: "터미널에서 codex login 실행" 또는 "터미널에서 agy 실행 후 로그인, 저장소의 setup.sh 참고")을 issues에 적어줘.`,
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

function buildScoringPrompt(task, result, persona) {
  return `너는 독립 채점자야. 아래 루브릭으로 AI 에이전트의 작업 결과를 채점해.

${RUBRIC}

이 작업을 요청한 사용자 수준: ${persona}

[요청받은 작업]
${task}

[제출된 결과물]
${result}

반드시 아래 JSON 형식으로만 답해 (다른 설명 텍스트 없이 JSON 객체 하나만):
{"scores":{"목표달성도":0,"정확성":0,"제약안전성":0,"완성도":0,"명확성":0,"효율성":0},"total":0,"dealbreaker":false,"dealbreaker_reason":"","feedback":"구체적인 감점 사유와 개선점"}`
}

async function scoreWithCodex(task, result, persona) {
  const prompt = buildScoringPrompt(task, result, persona)
  return agent(
    `Bash 툴로 아래 명령을 정확히(따옴표 포함 그대로) 실행해:\ncodex exec --skip-git-repo-check ${JSON.stringify(prompt)}\n\n명령 출력 안에서 JSON 객체를 찾아 그 내용 그대로 구조화된 출력으로 반환해. 출력이 JSON이 아니면 내용을 읽고 스키마에 맞게 직접 변환해서 반환해.`,
    { phase: 'Score', label: 'codex', schema: SCORE_SCHEMA }
  )
}

async function scoreWithGemini(task, result, persona) {
  const prompt = buildScoringPrompt(task, result, persona)
  return agent(
    `Bash 툴로 아래 명령을 정확히(env -u 플래그 포함 그대로) 실행해:\nenv -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT /Users/edge_ai/.local/bin/agy -p ${JSON.stringify(prompt)}\n\n명령 출력 안에서 JSON 객체를 찾아 그 내용 그대로 구조화된 출력으로 반환해. 출력이 JSON이 아니면 내용을 읽고 스키마에 맞게 직접 변환해서 반환해.`,
    { phase: 'Score', label: 'gemini', schema: SCORE_SCHEMA }
  )
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
const historyFile = parsedArgs.historyFile || '/Users/edge_ai/.claude/verify-task-history.jsonl'

let result = parsedArgs.result
const history = []
let finalVerdict = null

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
  log(`라운드 ${round}: Codex + Gemini 채점 중...`)
  const [codexScore, geminiScore] = await parallel([
    () => scoreWithCodex(task, result, persona),
    () => scoreWithGemini(task, result, persona),
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
  result = await agent(
    `아래는 작업 요청과 이전 결과물, 그리고 두 명의 독립 채점자의 피드백이야. 피드백을 반영해서 결과물을 개선해줘.\n${cwd ? `작업 디렉토리: ${cwd}\n` : ''}\n[요청받은 작업]\n${task}\n\n[이전 결과물]\n${result}\n\n[Codex 피드백] (총점 ${codexScore?.total ?? '?'}/100, 과락: ${codexScore?.dealbreaker})\n${codexScore?.feedback ?? '(응답 파싱 실패)'}\n\n[Gemini 피드백] (총점 ${geminiScore?.total ?? '?'}/100, 과락: ${geminiScore?.dealbreaker})\n${geminiScore?.feedback ?? '(응답 파싱 실패)'}\n\n개선된 최종 결과물만 출력해 (부가 설명 없이 결과물 자체만).`,
    { phase: 'Revise', label: `revise-round-${round}`, agentType: 'general-purpose' }
  )
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
