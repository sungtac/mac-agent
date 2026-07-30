const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { spawn } = require('node:child_process')

const {
  createNanoEvent,
  recordNanoEvent,
  validateEvent,
  NanoEventStoreError,
  findNanoEvent,
} = require('../workflows/lib/nano-event-store.js')

const STORE_SCRIPT = path.resolve(__dirname, '../workflows/lib/nano-event-store.js')
let tempRoot

test.beforeEach(() => {
  tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'nano-event-store-'))
})

test.afterEach(() => {
  fs.rmSync(tempRoot, { recursive: true, force: true })
})

function makeEvent(overrides = {}) {
  return createNanoEvent({
    taskId: 'task-1',
    stepId: 'step-1',
    taskType: 'test',
    changedFiles: ['src/example.js'],
    agents: ['codex'],
    verificationTier: 'light',
    status: 'passed',
    reason: 'tests passed',
    durationMs: 12,
    tokenUsage: { codex: 100 },
    preventionRules: [],
    recordedAt: '2026-07-30T00:00:00.000Z',
    ...overrides,
  })
}

function storePath() {
  return path.join(tempRoot, 'events.jsonl')
}

function runCli(event, eventFile) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [STORE_SCRIPT, eventFile], { input: undefined })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (chunk) => { stdout += chunk })
    child.stderr.on('data', (chunk) => { stderr += chunk })
    child.on('close', (code) => resolve({ code, stdout, stderr }))
    child.stdin.end(`${JSON.stringify(event)}\n`)
  })
}

test('버전 있는 이벤트를 생성하고 필수 계약을 검증한다', () => {
  const event = makeEvent()
  assert.equal(event.schemaVersion, 1)
  assert.equal(event.eventType, 'nano_step')
  assert.equal(event.idempotencyKey, 'task-1::step-1')
  assert.doesNotThrow(() => validateEvent(event))
})

test('첫 기록은 append, 동일 payload 재시도는 duplicate로 멱등 처리한다', () => {
  const event = makeEvent()
  const first = recordNanoEvent(event, storePath())
  const second = recordNanoEvent(event, storePath())
  assert.equal(first.outcome, 'appended')
  assert.equal(second.outcome, 'duplicate')
  assert.equal(fs.readFileSync(storePath(), 'utf8').trim().split('\n').length, 1)
  assert.deepEqual(findNanoEvent(storePath(), event.idempotencyKey), event)
})

test('같은 멱등키의 다른 payload는 충돌로 실패한다', () => {
  const event = makeEvent()
  recordNanoEvent(event, storePath())
  assert.throws(
    () => recordNanoEvent(makeEvent({ reason: 'different result' }), storePath()),
    (error) => error instanceof NanoEventStoreError && error.code === 'idempotency_conflict'
  )
  assert.equal(fs.readFileSync(storePath(), 'utf8').trim().split('\n').length, 1)
})

test('손상된 원장은 fail-closed로 중단한다', () => {
  fs.writeFileSync(storePath(), '{not-json}\n', 'utf8')
  assert.throws(
    () => recordNanoEvent(makeEvent(), storePath()),
    (error) => error instanceof NanoEventStoreError && error.code === 'corrupt_event_file'
  )
})

test('동시 동일 이벤트 기록은 한 줄만 남긴다', async () => {
  const event = makeEvent()
  const results = await Promise.all(Array.from({ length: 24 }, () => runCli(event, storePath())))
  assert.equal(results.every((result) => result.code === 0), true)
  const lines = fs.readFileSync(storePath(), 'utf8').trim().split('\n')
  assert.equal(lines.length, 1)
  assert.doesNotThrow(() => JSON.parse(lines[0]))
  assert.equal(results.filter((result) => JSON.parse(result.stdout).outcome === 'appended').length, 1)
  assert.equal(results.filter((result) => JSON.parse(result.stdout).outcome === 'duplicate').length, 23)
})

test('동시 서로 다른 이벤트는 모두 유효한 줄로 기록된다', async () => {
  const events = Array.from({ length: 24 }, (_, index) => makeEvent({
    taskId: `task-${index}`,
    stepId: 'step-1',
  }))
  const results = await Promise.all(events.map((event) => runCli(event, storePath())))
  assert.equal(results.every((result) => result.code === 0), true)
  const lines = fs.readFileSync(storePath(), 'utf8').trim().split('\n')
  assert.equal(lines.length, 24)
  assert.doesNotThrow(() => lines.forEach((line) => validateEvent(JSON.parse(line))))
})

test('락 획득 실패는 non-zero로 반환되어 게이트가 fail-closed 된다', () => {
  const event = makeEvent()
  const eventFile = storePath()
  fs.mkdirSync(`${eventFile}.lock`)
  assert.throws(
    () => recordNanoEvent(event, eventFile, { lockTimeoutMs: 0 }),
    (error) => error instanceof NanoEventStoreError && error.code === 'lock_timeout'
  )
})

test('CLI 기록 실패도 non-zero와 구조화된 오류를 반환한다', async () => {
  const result = await runCli({}, storePath())
  assert.equal(result.code, 1)
  assert.match(result.stderr, /"error":"invalid_event"/)
})

test('CLI check는 재시작 시 기존 이벤트를 발견할 수 있다', async () => {
  const event = makeEvent()
  recordNanoEvent(event, storePath())
  const found = await new Promise((resolve) => {
    const child = spawn(process.execPath, [STORE_SCRIPT, '--check', storePath(), event.idempotencyKey])
    let stdout = ''
    child.stdout.on('data', (chunk) => { stdout += chunk })
    child.on('close', (code) => resolve({ code, result: JSON.parse(stdout) }))
  })
  assert.equal(found.code, 0)
  assert.equal(found.result.found, true)
  assert.deepEqual(JSON.parse(found.result.eventJson), event)
})
