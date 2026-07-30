const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const { createNanoEvent } = require('../workflows/lib/nano-event-store.js')
const {
  readNanoEvents,
  summarizeNanoEvents,
  MIN_EVENTS_FOR_UPDATE,
  MIN_RISK_SIGNAL_EVENTS,
  MIN_TOKEN_SIGNAL_EVENTS,
} = require('../workflows/lib/analyze-nano-events.js')

let root

test.beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'nano-analysis-'))
})

test.afterEach(() => {
  fs.rmSync(root, { recursive: true, force: true })
})

function event(index, overrides = {}) {
  return createNanoEvent({
    taskId: `task-${index}`,
    stepId: 'step-1',
    taskType: 'analysis-test',
    changedFiles: ['src/example.js'],
    agents: ['codex'],
    verificationTier: 'light',
    status: 'passed',
    reason: 'ok',
    durationMs: index * 10,
    tokenUsage: { codex: index },
    riskInputs: { stepFileCount: 1, cumulativeFileCount: 1, providerHeadroom: { codex: 80 } },
    recordedAt: '2026-07-30T00:00:00.000Z',
    ...overrides,
  })
}

test('원장이 없으면 빈 분석과 조정 불가를 반환한다', () => {
  const result = summarizeNanoEvents([], [], path.join(root, 'missing.jsonl'))
  assert.equal(result.total, 0)
  assert.equal(result.thresholdDecision.eligibleForUpdate, false)
})

test('분석기는 tier/status/duration/token/risk 신호를 집계한다', () => {
  const events = [
    event(1),
    event(2, { verificationTier: 'mid', riskInputs: { stepFileCount: 4, cumulativeFileCount: 1, dependencyBoundaryCrossed: true, providerHeadroom: { codex: 9 } } }),
    event(3, { verificationTier: 'full', status: 'failed', sensitivePath: true, riskInputs: { stepFileCount: 1, cumulativeFileCount: 5, sensitivePath: true, providerHeadroom: { codex: 5 } } }),
  ]
  const result = summarizeNanoEvents(events, [], path.join(root, 'events.jsonl'))
  assert.deepEqual(result.byTier, { light: 1, mid: 1, full: 1 })
  assert.deepEqual(result.byStatus, { passed: 2, failed: 1 })
  assert.equal(result.durationMs.median, 20)
  assert.equal(result.durationMs.p95, 30)
  assert.equal(result.tokenTotals.codex, 6)
  assert.equal(result.signalCounts.stepFileThreshold, 1)
  assert.equal(result.signalCounts.cumulativeFileThreshold, 1)
  assert.equal(result.signalCounts.dependencyBoundaryCrossed, 1)
  assert.equal(result.signalCounts.lowTokenThreshold, 2)
})

test('표본과 위험 입력이 충분해도 자동 임계값 변경은 하지 않는다', () => {
  const events = Array.from({ length: MIN_EVENTS_FOR_UPDATE }, (_, index) => event(index))
  const result = summarizeNanoEvents(events, [], path.join(root, 'events.jsonl'))
  assert.equal(result.riskSignalEvents, MIN_EVENTS_FOR_UPDATE)
  assert.equal(result.thresholdDecision.eligibleForUpdate, true)
  assert.equal(result.thresholdDecision.current.stepFileThreshold, 3)
  assert.match(result.thresholdDecision.reason, /자동 변경하지 말고/)
})

test('손상된 줄이 있으면 충분한 표본이어도 조정 자격을 취소한다', () => {
  const events = Array.from({ length: MIN_EVENTS_FOR_UPDATE }, (_, index) => event(index))
  const result = summarizeNanoEvents(events, [{ line: 99, reason: 'invalid_event' }], path.join(root, 'events.jsonl'))
  assert.equal(result.thresholdDecision.eligibleForUpdate, false)
  assert.match(result.thresholdDecision.reason, /손상된 줄/)
})

test('파일 읽기는 유효 이벤트와 손상 줄을 분리한다', () => {
  const eventFile = path.join(root, 'events.jsonl')
  fs.writeFileSync(eventFile, `${JSON.stringify(event(1))}\nnot-json\n`, 'utf8')
  const result = readNanoEvents(eventFile)
  assert.equal(result.events.length, 1)
  assert.equal(result.invalidLines.length, 1)
  assert.equal(result.invalidLines[0].line, 2)
})

test('provider headroom 표본이 없으면 token 표본 부족으로 보류한다', () => {
  const events = Array.from({ length: MIN_EVENTS_FOR_UPDATE }, (_, index) => event(index, { riskInputs: { stepFileCount: 1, cumulativeFileCount: 1 } }))
  const result = summarizeNanoEvents(events, [], path.join(root, 'events.jsonl'))
  assert.equal(result.riskSignalEvents, MIN_EVENTS_FOR_UPDATE)
  assert.equal(result.tokenSignalEvents, 0)
  assert.equal(result.thresholdDecision.eligibleForUpdate, false)
  assert.deepEqual(result.headroomMinimums, {})
  assert.match(result.thresholdDecision.reason, /provider\/token 잔여량 표본 부족/)
  assert.equal(MIN_RISK_SIGNAL_EVENTS <= MIN_EVENTS_FOR_UPDATE, true)
  assert.equal(MIN_TOKEN_SIGNAL_EVENTS <= MIN_EVENTS_FOR_UPDATE, true)
})
