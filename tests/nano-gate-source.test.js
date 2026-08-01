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
  assert.match(source, /return \{ ok: !codexReview\.hasBlockingIssue && !antigravityReview\.hasBlockingIssue/)
})

test('전체 코드 리뷰 역할은 Claude와 Antigravity 독립 검증으로 고정된다', () => {
  assert.match(source, /async function claudeReviewDiff\(/)
  assert.match(source, /async function antigravityReviewDiff\(/)
  assert.match(source, /FullCodeReviewSkill: 'claude\.communicator\|antigravity\.auditor'/)
})

test('headless Write 계약을 지키도록 임시 dispatch 파일을 먼저 Read한다', () => {
  assert.match(source, /반드시 Read 툴로 그 실제 경로의 임시 파일을 한 번 읽어\(빈 파일이어도 괜찮아/)
  assert.match(source, /Claude의 Write 계약상 먼저 읽은 파일만 Write할 수 있음/)
  assert.match(source, /실제 경로를 인자로 전달/)
})

test('컨텍스트/실제 diff 수집은 모든 StructuredOutput 필드를 명시한다', () => {
  assert.match(source, /relevant_files: \{ type: 'array'/)
  assert.match(source, /files_changed: \{ type: 'array'/)
})

test('full 리뷰는 SHA 귀속 보고서를 저장하고 저장 실패 시 승인하지 않는다', () => {
  assert.match(source, /const CODE_REVIEW_STORE = MAC_AGENT_ROOT/)
  assert.match(source, /async function persistReviewReport\(report\)/)
  assert.match(source, /error: 'review_persistence_failed'/)
})

test('나노 이벤트 기록도 임시 파일 Read 후 Write하고 light 검토 필드를 고정한다', () => {
  assert.match(source, /반드시 Read 툴로 그 빈 임시 파일을 한 번 읽은 뒤 Write/)
  assert.match(source, /마지막 응답은 반드시 다른 설명 없이 아래 세 키를 모두 포함한 JSON 객체 하나여야 해\(필드 누락 금지\)/)
})

test('일반 코드 파일은 결정론적 민감도 조건이 없으면 경량으로 처리할 수 있다', () => {
  assert.match(source, /return context\?\.policy\?\.track === 'light' \? 'light' : 'full'/)
})
