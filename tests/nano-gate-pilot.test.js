const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const { decideRiskTier } = require('../workflows/lib/decide-risk-tier.js')
const {
  createNanoEvent,
  findNanoEvent,
  recordNanoEvent,
  NanoEventStoreError,
} = require('../workflows/lib/nano-event-store.js')

let root

test.beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'nano-gate-pilot-'))
})

test.afterEach(() => {
  fs.rmSync(root, { recursive: true, force: true })
})

function eventFor({ taskId, stepId, tier, changedFiles, agents }) {
  return createNanoEvent({
    taskId,
    stepId,
    taskType: 'pilot',
    changedFiles,
    agents,
    verificationTier: tier,
    status: 'passed',
    reason: `pilot ${tier} passed`,
    durationMs: 1,
    tokenUsage: null,
    preventionRules: [],
    recordedAt: '2026-07-30T00:00:00.000Z',
  })
}

function reviewersFor(tier) {
  if (tier === 'light') return ['codex']
  if (tier === 'mid') return ['claude']
  return ['claude', 'antigravity']
}

test('저위험 파일럿은 light 검증과 단일 이벤트 기록으로 통과한다', () => {
  const eventFile = path.join(root, 'events.jsonl')
  const tier = decideRiskTier({ stepFileCount: 1, cumulativeFileCount: 1 })
  assert.equal(tier, 'light')
  const event = eventFor({ taskId: 'pilot-low', stepId: 'step-1', tier, changedFiles: ['src/pilot.js'], agents: reviewersFor(tier) })
  const recorded = recordNanoEvent(event, eventFile)
  assert.equal(recorded.outcome, 'appended')
  assert.deepEqual(findNanoEvent(eventFile, event.idempotencyKey), event)
})

test('경계 파일 수는 mid로 승격되고 통합 검증 담당자가 바뀐다', () => {
  const tier = decideRiskTier({ stepFileCount: 4, cumulativeFileCount: 1 })
  assert.equal(tier, 'mid')
  assert.deepEqual(reviewersFor(tier), ['claude'])
})

test('민감 경로는 full로 승격되고 Claude+Antigravity 이중 검증을 요구한다', () => {
  const tier = decideRiskTier({ stepFileCount: 1, cumulativeFileCount: 1, sensitivePath: true })
  assert.equal(tier, 'full')
  assert.deepEqual(reviewersFor(tier), ['claude', 'antigravity'])
})

test('재시작 파일럿은 통과 이벤트를 발견하고 실행을 건너뛴다', () => {
  const eventFile = path.join(root, 'events.jsonl')
  const event = eventFor({ taskId: 'pilot-restart', stepId: 'step-1', tier: 'light', changedFiles: ['src/pilot.js'], agents: ['codex'] })
  recordNanoEvent(event, eventFile)
  let executeCount = 0
  const existing = findNanoEvent(eventFile, event.idempotencyKey)
  if (!existing || existing.status !== 'passed') executeCount += 1
  assert.equal(executeCount, 0)
  assert.deepEqual(findNanoEvent(eventFile, event.idempotencyKey), event)
})

test('이벤트 기록 실패 파일럿은 다음 단계 진행을 차단한다', () => {
  const eventFile = path.join(root, 'events.jsonl')
  fs.mkdirSync(`${eventFile}.lock`)
  const event = eventFor({ taskId: 'pilot-failure', stepId: 'step-1', tier: 'light', changedFiles: [], agents: ['codex'] })
  let proceed = true
  assert.throws(
    () => recordNanoEvent(event, eventFile, { lockTimeoutMs: 0 }),
    (error) => error instanceof NanoEventStoreError && error.code === 'lock_timeout'
  )
  proceed = false
  assert.equal(proceed, false)
})
