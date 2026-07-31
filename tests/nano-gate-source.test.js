const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const source = fs.readFileSync(path.resolve(__dirname, '../workflows/verify-task-v2.js'), 'utf8')

test('나노 모드는 명시적 nanoMode/nanoSteps일 때만 기존 트랙 앞에서 선택된다', () => {
  assert.match(source, /const NANO_MODE = parsedArgs\.nanoMode === true \|\| Array\.isArray\(parsedArgs\.nanoSteps\)/)
  const branch = source.indexOf('if (NANO_MODE) {')
  const lightTrack = source.indexOf('// ---------- 경량 트랙 ----------')
  assert.ok(branch >= 0 && branch < lightTrack)
  assert.match(source.slice(branch, lightTrack), /runNanoGate\(/)
})

test('나노 스텝 순서는 조회→실행→light→위험도/통합→기록이다', () => {
  const body = source.slice(source.indexOf('async function runNanoGate'))
  const positions = [
    body.indexOf('const existing = await checkNanoEvent'),
    body.search(/(?:const|let) execution = await fullExecute/),
    body.indexOf('const lightReview = await nanoLightValidate'),
    body.indexOf('const tier = decideNanoRiskTier'),
    body.indexOf('const integration = await nanoIntegrationValidate'),
    body.lastIndexOf('const recorded = await recordNanoOutcome'),
  ]
  assert.equal(positions.every((position) => position >= 0), true)
  assert.deepEqual([...positions].sort((a, b) => a - b), positions)
})

test('기록 실패는 nano_event_record_failed로 중단되고 다음 스텝으로 진행하지 않는다', () => {
  assert.match(source, /error: 'nano_event_record_failed'/)
  assert.match(source, /검증은 통과했지만 이벤트 기록에 실패하여 다음 스텝으로 진행하지 않음/)
  assert.match(source, /실패 이벤트 기록도 실패하여 중단함/)
})

test('재시작 시 이미 통과한 step은 재실행하지 않고 재사용한다', () => {
  const body = source.slice(source.indexOf('async function runNanoGate'))
  assert.match(body, /if \(existing\.found\)/)
  assert.match(body, /previous\.status !== 'passed'/)
  assert.match(body, /history\.push\(\{ stepId: step\.stepId, reused: true/)
  assert.match(body, /continue/)
})

test('위험도 결과에 따라 light/mid/full 통합검증이 선택된다', () => {
  assert.match(source, /if \(tier === 'light'\) return \{ ok: true/)
  assert.match(source, /if \(tier === 'mid'\)/)
  assert.match(source, /return \{ ok: !claudeReview\.hasBlockingIssue && !antigravityReview\.hasBlockingIssue/)
})

test('headless Write 계약을 지키도록 임시 dispatch 파일을 먼저 Read한다', () => {
  assert.match(source, /반드시 Read 툴로 방금 얻은 임시 파일을 한 번 읽어\(빈 파일이어도 괜찮아/)
  assert.match(source, /Claude의 Write 계약상 먼저 읽은 파일만 Write할 수 있음/)
  assert.match(source, /파일경로는 3번 경로로 치환/)
})

test('컨텍스트/실제 diff 수집은 모든 StructuredOutput 필드를 명시한다', () => {
  assert.match(source, /\{"cwdExists":true,"contextText":"수집한 사실","intendedFiles":\["예상 경로"\],"sensitivePath":false\}/)
  assert.match(source, /\{"content":"위 명령의 원문 출력","filesChanged":\["실제 변경 경로"\],"sensitivePath":false\}/)
})

test('나노 이벤트 기록도 임시 파일 Read 후 Write하고 light 검토 필드를 고정한다', () => {
  assert.match(source, /반드시 Read 툴로 그 빈 임시 파일을 한 번 읽은 뒤 Write/)
  assert.match(source, /마지막 응답은 반드시 다른 설명 없이 아래 세 키를 모두 포함한 JSON 객체 하나여야 해\(필드 누락 금지\)/)
})

test('일반 트랙도 코딩 파일은 경량으로 우회하지 않는다', () => {
  assert.match(source, /function standardFileTier\(filePath\)/)
  assert.match(source, /const hasCodeFile = fileTiers\.includes\('mid'\)/)
  assert.match(source, /!hasFullFile && !hasCodeFile/)
})
