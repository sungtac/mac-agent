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

function buildScoringPrompt(task, result, persona, cwd, verificationContent) {
  let verificationBlock
  if (verificationContent) {
    verificationBlock = `[실제 검증 자료 — 이미 수집된 git 상태/로그/diff]\n${verificationContent}\n\n아래 [에이전트가 보고한 결과 요약]은 신뢰하지 마. 위 실제 자료와 보고 내용이 일치하는지 대조해서 채점해. 너는 셸 명령을 실행할 수 없는 환경이니 절대 실행을 시도하지 말고, 위에 이미 주어진 자료만으로 판단해 — feedback에 위 자료 중 무엇을 근거로 이 결론에 도달했는지 적어.`
  } else if (cwd) {
    verificationBlock = `[실제 작업 디렉토리]\n${cwd}\n\n아래 [에이전트가 보고한 결과 요약]은 신뢰하지 마. 반드시 이 디렉토리에서 git diff / git log / 실제 파일 읽기 등을 직접 수행해서, 보고 내용이 실제 변경사항과 일치하는지 검증한 뒤 그 실제 확인 결과를 근거로 채점해. 보고서 문장만 보고 점수를 매기면 안 됨 — feedback에 어떤 파일/명령으로 확인했는지 반드시 적어.`
  } else {
    verificationBlock = `[경고] 실제 작업 디렉토리가 제공되지 않아 아래 텍스트 보고만으로 채점함 — 실제 파일/변경사항을 검증할 수 없음. 이 사실을 feedback에 명시하고, 검증 불가능한 주장(예: "테스트를 통과했다", "버그를 고쳤다")에 대해서는 액면 그대로 믿지 말고 정확성/완성도 점수를 보수적으로 낮게 잡을 것.`
  }

  return `너는 독립 채점자야. 아래 루브릭으로 AI 에이전트의 작업 결과를 채점해.

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

const FAILURE_SCORE_INSTRUCTION = `채점 도구 실행이나 출력 파싱이 실패하면(도구가 없거나, 크래시하거나, 타임아웃되거나, JSON을 못 뽑아내는 경우) 절대로 그럴듯한 점수를 지어내지 마. 대신 scores 전부 0, total 0, dealbreaker true, dealbreaker_reason에 "채점 도구 실행/파싱 실패 — 작업 내용에 대한 판단 아님"이라고 명시하고, feedback에 실제로 어떤 에러/출력이 나왔는지 적어서 반환해.`

async function scoreWithCodex(task, result, persona, cwd) {
  const prompt = buildScoringPrompt(task, result, persona, cwd)
  return agent(
    `아래 순서를 정확히 따라줘 (프롬프트 내용을 셸 명령어 문자열에 직접 이어붙이지 마 — 반드시 파일에 저장한 뒤 $(cat ...)로 전달해. 프롬프트 안에 $(...), 백틱, 따옴표가 들어있어도 안전하게 전달하기 위함이야).\n\n1. Bash로 \`mktemp /tmp/verify-task-codex-XXXXXX.txt\` 실행해서 임시 파일 경로를 얻어.\n2. Write 툴로 그 경로에 아래 [프롬프트 내용]을 정확히 그대로(글자 하나 고치지 말고) 저장해.\n3. Bash로 다음을 실행해 (파일경로는 2번 경로로 치환, ${cwd ? `먼저 그 디렉토리로 이동: cd ${JSON.stringify(cwd)} && ` : ''}): codex exec --skip-git-repo-check "$(cat <파일경로>)"\n4. 실행이 끝나면 Bash로 그 임시 파일을 삭제해.\n5. 명령 출력 안에서 JSON 객체를 찾아 그 내용 그대로 구조화된 출력으로 반환해. ${FAILURE_SCORE_INSTRUCTION}\n\n[프롬프트 내용]\n${prompt}`,
    { phase: 'Score', label: 'codex', schema: SCORE_SCHEMA }
  )
}

const CONTEXT_SCHEMA = {
  type: 'object',
  properties: {
    content: { type: 'string' },
  },
  required: ['content'],
}

async function gatherVerificationContext(cwd) {
  if (!cwd) return null
  const gathered = await agent(
    `Bash로 아래 한 명령을 그 디렉토리에서 실행하고, 나온 출력을 절대 요약하거나 고치지 말고 그대로 content 필드에 담아 반환해:\n\ncd ${JSON.stringify(cwd)} && { echo '--- git log (최근 10개) ---'; git log --oneline -10; echo '--- 커밋되지 않은 변경 (git diff HEAD) ---'; git diff HEAD; echo '--- 최근 커밋 (git show HEAD) ---'; git show HEAD; } 2>&1\n\n출력이 8000자를 넘으면 앞 8000자만 남기고 끝에 "...(잘림)"을 붙여서 반환해.`,
    { phase: 'Score', label: 'gather-context', schema: CONTEXT_SCHEMA }
  )
  return gathered?.content || null
}

async function scoreWithGemini(task, result, persona, cwd, verificationContent) {
  const prompt = buildScoringPrompt(task, result, persona, cwd, verificationContent)
  return agent(
    `아래 순서를 정확히 따라줘 (프롬프트 내용을 셸 명령어 문자열에 직접 이어붙이지 마 — 반드시 파일에 저장한 뒤 $(cat ...)로 전달해. 프롬프트 안에 $(...), 백틱, 따옴표가 들어있어도 안전하게 전달하기 위함이야). agy는 이미 필요한 검증 자료를 프롬프트 안에 텍스트로 받으므로 셸 명령을 실행할 필요가 없어 — 그래서 이 호출은 작업 디렉토리 이동 없이 진행해.\n\n1. Bash로 \`mktemp /tmp/verify-task-gemini-XXXXXX.txt\` 실행해서 임시 파일 경로를 얻어.\n2. Write 툴로 그 경로에 아래 [프롬프트 내용]을 정확히 그대로(글자 하나 고치지 말고) 저장해.\n3. Bash로 다음을 실행해 (파일경로는 2번 경로로 치환): env -u SSH_CONNECTION -u SSH_TTY -u SSH_CLIENT /Users/edge_ai/.local/bin/agy -p "$(cat <파일경로>)"\n4. 실행이 끝나면 Bash로 그 임시 파일을 삭제해.\n5. 명령 출력 안에서 JSON 객체를 찾아 그 내용 그대로 구조화된 출력으로 반환해. ${FAILURE_SCORE_INSTRUCTION}\n\n[프롬프트 내용]\n${prompt}`,
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
  log(`라운드 ${round}: 검증 자료 수집 중...`)
  const verificationContent = await gatherVerificationContext(cwd)

  log(`라운드 ${round}: Codex + Gemini 채점 중...`)
  const [codexScore, geminiScore] = await parallel([
    () => scoreWithCodex(task, result, persona, cwd),
    () => scoreWithGemini(task, result, persona, cwd, verificationContent),
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
