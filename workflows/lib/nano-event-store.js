#!/usr/bin/env node

// 나노 게이트 이벤트 원장.
//
// Workflow의 내부 journal.jsonl은 외부 이벤트를 주입하거나 멱등키로
// 갱신하는 공개 계약이 없으므로 이 원장은 별도 파일로 둔다. 기록은
// "락 획득 -> 기존 키 확인 -> 한 줄 append -> fsync -> 락 해제" 순서다.
// 같은 taskId+stepId를 다른 내용으로 재기록하려는 경우에는 조용히
// 성공시키지 않고 충돌로 실패한다. 호출자는 이 실패를 게이트 실패로
// 취급해서 다음 나노 스텝으로 진행하면 안 된다.

const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const SCHEMA_VERSION = 1
const EVENT_TYPE = 'nano_step'
const DEFAULT_EVENT_FILE = path.join(os.homedir(), '.claude', 'nano-gate-events.jsonl')
const DEFAULT_LOCK_TIMEOUT_MS = 5000
const LOCK_RETRY_MS = 25
const MAX_STRING_LENGTH = 10000

class NanoEventStoreError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'NanoEventStoreError'
    this.code = code
  }
}

function sleepSync(milliseconds) {
  const signal = new Int32Array(new SharedArrayBuffer(4))
  Atomics.wait(signal, 0, 0, milliseconds)
}

function assertText(value, field) {
  if (typeof value !== 'string' || value.length === 0 || value.length > MAX_STRING_LENGTH || /[\r\n]/.test(value)) {
    throw new NanoEventStoreError('invalid_event', `${field} must be a non-empty single-line string`)
  }
}

function assertStringArray(value, field) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string' || /[\r\n]/.test(item))) {
    throw new NanoEventStoreError('invalid_event', `${field} must be an array of strings`)
  }
}

function expectedIdempotencyKey(taskId, stepId) {
  return `${taskId}::${stepId}`
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function validateRiskInputs(value) {
  if (value === null || value === undefined) return
  if (!isPlainObject(value)) {
    throw new NanoEventStoreError('invalid_event', 'riskInputs must be an object or null')
  }
  for (const field of ['stepFileCount', 'cumulativeFileCount']) {
    if (value[field] !== undefined && (!Number.isInteger(value[field]) || value[field] < 0)) {
      throw new NanoEventStoreError('invalid_event', `${field} in riskInputs must be a non-negative integer`)
    }
  }
  for (const field of ['remainingTokenPct']) {
    if (value[field] !== undefined && (typeof value[field] !== 'number' || !Number.isFinite(value[field]) || value[field] < 0 || value[field] > 100)) {
      throw new NanoEventStoreError('invalid_event', `${field} in riskInputs must be between 0 and 100`)
    }
  }
  if (value.providerHeadroom !== undefined && value.providerHeadroom !== null) {
    if (!isPlainObject(value.providerHeadroom)) {
      throw new NanoEventStoreError('invalid_event', 'providerHeadroom in riskInputs must be an object or null')
    }
    for (const provider of ['claude', 'codex', 'antigravity']) {
      if (value.providerHeadroom[provider] !== undefined && (typeof value.providerHeadroom[provider] !== 'number' || !Number.isFinite(value.providerHeadroom[provider]) || value.providerHeadroom[provider] < 0 || value.providerHeadroom[provider] > 100)) {
        throw new NanoEventStoreError('invalid_event', `${provider} headroom in riskInputs must be between 0 and 100`)
      }
    }
  }
}

function validateEvent(event) {
  if (!isPlainObject(event)) {
    throw new NanoEventStoreError('invalid_event', 'event must be an object')
  }
  if (event.schemaVersion !== SCHEMA_VERSION) {
    throw new NanoEventStoreError('invalid_event', `schemaVersion must be ${SCHEMA_VERSION}`)
  }
  if (event.eventType !== EVENT_TYPE) {
    throw new NanoEventStoreError('invalid_event', `eventType must be ${EVENT_TYPE}`)
  }

  for (const field of ['taskId', 'stepId', 'taskType', 'verificationTier', 'status', 'reason', 'recordedAt']) {
    assertText(event[field], field)
  }
  if (!['light', 'mid', 'full'].includes(event.verificationTier)) {
    throw new NanoEventStoreError('invalid_event', 'verificationTier must be light, mid, or full')
  }
  if (!['passed', 'failed'].includes(event.status)) {
    throw new NanoEventStoreError('invalid_event', 'status must be passed or failed')
  }
  assertStringArray(event.changedFiles, 'changedFiles')
  assertStringArray(event.agents, 'agents')
  assertStringArray(event.preventionRules, 'preventionRules')

  if (typeof event.durationMs !== 'number' || !Number.isFinite(event.durationMs) || event.durationMs < 0) {
    throw new NanoEventStoreError('invalid_event', 'durationMs must be a non-negative finite number')
  }
  if (event.tokenUsage !== null && !isPlainObject(event.tokenUsage)) {
    throw new NanoEventStoreError('invalid_event', 'tokenUsage must be an object or null')
  }
  validateRiskInputs(event.riskInputs)

  const expectedKey = expectedIdempotencyKey(event.taskId, event.stepId)
  if (event.idempotencyKey !== expectedKey) {
    throw new NanoEventStoreError('invalid_event', 'idempotencyKey must equal taskId::stepId')
  }
  return event
}

function createNanoEvent(input = {}) {
  const taskId = input.taskId
  const stepId = input.stepId
  assertText(taskId, 'taskId')
  assertText(stepId, 'stepId')

  const event = {
    schemaVersion: SCHEMA_VERSION,
    eventType: EVENT_TYPE,
    taskId,
    stepId,
    idempotencyKey: expectedIdempotencyKey(taskId, stepId),
    taskType: input.taskType,
    changedFiles: input.changedFiles || [],
    agents: input.agents || [],
    verificationTier: input.verificationTier,
    status: input.status,
    reason: input.reason,
    durationMs: input.durationMs,
    tokenUsage: input.tokenUsage ?? null,
    riskInputs: input.riskInputs ?? null,
    preventionRules: input.preventionRules || [],
    recordedAt: input.recordedAt || new Date().toISOString(),
  }
  return validateEvent(event)
}

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (isPlainObject(value)) {
    return Object.keys(value).sort().reduce((result, key) => {
      result[key] = canonicalize(value[key])
      return result
    }, {})
  }
  return value
}

function canonicalJson(value) {
  return JSON.stringify(canonicalize(value))
}

function readExistingEvents(eventFile) {
  if (!fs.existsSync(eventFile)) return new Map()
  const content = fs.readFileSync(eventFile, 'utf8')
  const entries = new Map()
  for (const [index, line] of content.split('\n').entries()) {
    if (!line.trim()) continue
    let existing
    try {
      existing = JSON.parse(line)
    } catch (error) {
      throw new NanoEventStoreError('corrupt_event_file', `invalid JSON at ${eventFile}:${index + 1}`)
    }
    validateEvent(existing)
    if (entries.has(existing.idempotencyKey) && canonicalJson(entries.get(existing.idempotencyKey)) !== canonicalJson(existing)) {
      throw new NanoEventStoreError('corrupt_event_file', `duplicate conflicting key at ${eventFile}:${index + 1}`)
    }
    entries.set(existing.idempotencyKey, existing)
  }
  return entries
}

function acquireLock(lockPath, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  while (true) {
    try {
      fs.mkdirSync(lockPath)
      fs.writeFileSync(path.join(lockPath, 'owner'), `${process.pid}\n${new Date().toISOString()}\n`, 'utf8')
      return
    } catch (error) {
      if (error.code !== 'EEXIST') throw error
      if (Date.now() >= deadline) {
        throw new NanoEventStoreError('lock_timeout', `could not acquire event lock: ${lockPath}`)
      }
      sleepSync(LOCK_RETRY_MS)
    }
  }
}

function releaseLock(lockPath) {
  fs.rmSync(lockPath, { recursive: true, force: true })
}

function recordNanoEvent(event, eventFile = DEFAULT_EVENT_FILE, options = {}) {
  validateEvent(event)
  const resolvedFile = path.resolve(eventFile)
  const lockPath = `${resolvedFile}.lock`
  const timeoutMs = options.lockTimeoutMs ?? DEFAULT_LOCK_TIMEOUT_MS
  if (!Number.isInteger(timeoutMs) || timeoutMs < 0) {
    throw new NanoEventStoreError('invalid_options', 'lockTimeoutMs must be a non-negative integer')
  }

  fs.mkdirSync(path.dirname(resolvedFile), { recursive: true })
  acquireLock(lockPath, timeoutMs)
  try {
    const existingEvents = readExistingEvents(resolvedFile)
    const eventJson = canonicalJson(event)
    const existing = existingEvents.get(event.idempotencyKey)
    if (existing !== undefined) {
      if (canonicalJson(existing) !== eventJson) {
        throw new NanoEventStoreError('idempotency_conflict', `event key already exists with different content: ${event.idempotencyKey}`)
      }
      return { ok: true, outcome: 'duplicate', idempotencyKey: event.idempotencyKey, eventFile: resolvedFile }
    }

    const descriptor = fs.openSync(resolvedFile, 'a', 0o600)
    try {
      fs.writeSync(descriptor, `${eventJson}\n`, null, 'utf8')
      fs.fsyncSync(descriptor)
    } finally {
      fs.closeSync(descriptor)
    }
    return { ok: true, outcome: 'appended', idempotencyKey: event.idempotencyKey, eventFile: resolvedFile }
  } finally {
    releaseLock(lockPath)
  }
}

function findNanoEvent(eventFile = DEFAULT_EVENT_FILE, idempotencyKey, options = {}) {
  assertText(idempotencyKey, 'idempotencyKey')
  const resolvedFile = path.resolve(eventFile)
  if (!fs.existsSync(resolvedFile)) return null
  const lockPath = `${resolvedFile}.lock`
  const timeoutMs = options.lockTimeoutMs ?? DEFAULT_LOCK_TIMEOUT_MS
  if (!Number.isInteger(timeoutMs) || timeoutMs < 0) {
    throw new NanoEventStoreError('invalid_options', 'lockTimeoutMs must be a non-negative integer')
  }
  acquireLock(lockPath, timeoutMs)
  try {
    return readExistingEvents(resolvedFile).get(idempotencyKey) || null
  } finally {
    releaseLock(lockPath)
  }
}

function main() {
  if (process.argv[2] === '--check') {
    const eventFile = process.argv[3] || process.env.NANO_EVENT_FILE || DEFAULT_EVENT_FILE
    const event = findNanoEvent(eventFile, process.argv[4])
    process.stdout.write(`${JSON.stringify({ ok: true, found: !!event, eventJson: event ? JSON.stringify(event) : '' })}\n`)
    return
  }
  const eventFile = process.argv[2] || process.env.NANO_EVENT_FILE || DEFAULT_EVENT_FILE
  const input = fs.readFileSync(0, 'utf8')
  let event
  try {
    event = JSON.parse(input)
  } catch (error) {
    throw new NanoEventStoreError('invalid_event', 'stdin must contain one JSON event object')
  }
  const result = recordNanoEvent(event, eventFile)
  process.stdout.write(`${JSON.stringify(result)}\n`)
}

if (require.main === module) {
  try {
    main()
  } catch (error) {
    const code = error instanceof NanoEventStoreError ? error.code : 'storage_error'
    process.stderr.write(`${JSON.stringify({ ok: false, error: code, message: error.message })}\n`)
    process.exitCode = 1
  }
}

module.exports = {
  SCHEMA_VERSION,
  EVENT_TYPE,
  DEFAULT_EVENT_FILE,
  NanoEventStoreError,
  createNanoEvent,
  validateEvent,
  recordNanoEvent,
  findNanoEvent,
  expectedIdempotencyKey,
}
