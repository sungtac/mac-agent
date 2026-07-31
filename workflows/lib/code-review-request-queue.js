#!/usr/bin/env node

// Durable, provider-neutral handoff for normalized code-review requests.
// Raw webhook payloads, secrets, and diffs are never written here.

const crypto = require('node:crypto')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const SCHEMA_VERSION = 'edge_agent.code_review_request.v1'
const COMPLETION_SCHEMA_VERSION = 'edge_agent.code_review_request_completion.v1'
const DEFAULT_QUEUE_ROOT = process.env.EDGE_AGENT_CODE_REVIEW_QUEUE_ROOT ||
  (process.env.EDGE_AGENT_CODE_REVIEW_STATE_ROOT && path.join(process.env.EDGE_AGENT_CODE_REVIEW_STATE_ROOT, 'requests')) ||
  (process.env.EDGE_AGENT_STATE_ROOT && path.join(process.env.EDGE_AGENT_STATE_ROOT, 'code-review', 'requests')) ||
  path.join(os.homedir(), '.edge-agent', 'state', 'code-review', 'requests')

class CodeReviewQueueError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'CodeReviewQueueError'
    this.code = code
  }
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
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

function assertText(value, field) {
  if (typeof value !== 'string' || value.trim() === '' || /[\r\n]/.test(value)) {
    throw new CodeReviewQueueError('invalid_request', field + ' must be a non-empty single-line string')
  }
}

function requestKey(reviewId) {
  return crypto.createHash('sha256').update(reviewId).digest('hex')
}

function pathsFor(stateRoot) {
  const root = path.resolve(stateRoot || DEFAULT_QUEUE_ROOT)
  return {
    root,
    pending: path.join(root, 'pending'),
    completed: path.join(root, 'completed'),
  }
}

function fileFor(directory, reviewId) {
  return path.join(directory, requestKey(reviewId) + '.json')
}

function readJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'))
  } catch (error) {
    if (error.code === 'ENOENT') return null
    throw new CodeReviewQueueError('corrupt_queue', 'invalid JSON at ' + filePath)
  }
}

function writeAtomic(filePath, value, exclusive = false) {
  const directory = path.dirname(filePath)
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 })
  const temporary = path.join(directory, '.tmp-' + process.pid + '-' + crypto.randomBytes(6).toString('hex'))
  const descriptor = fs.openSync(temporary, 'wx', 0o600)
  try {
    fs.writeFileSync(descriptor, JSON.stringify(value, null, 2) + '\n', 'utf8')
    fs.fsyncSync(descriptor)
  } finally {
    fs.closeSync(descriptor)
  }
  try {
    if (exclusive) {
      fs.linkSync(temporary, filePath)
      fs.unlinkSync(temporary)
      return true
    }
    fs.renameSync(temporary, filePath)
  } catch (error) {
    try { fs.unlinkSync(temporary) } catch {}
    if (exclusive && error.code === 'EEXIST') return false
    throw error
  }
  return true
}

function validateRequest(request) {
  if (!isPlainObject(request) || request.schema_version !== SCHEMA_VERSION) {
    throw new CodeReviewQueueError('invalid_request', 'schema_version is invalid')
  }
  assertText(request.review_id, 'review_id')
  if (!isPlainObject(request.target)) throw new CodeReviewQueueError('invalid_request', 'target must be an object')
  assertText(request.target.repository, 'target.repository')
  if (!Number.isInteger(request.target.pull_request) || request.target.pull_request <= 0) {
    throw new CodeReviewQueueError('invalid_request', 'target.pull_request must be a positive integer')
  }
  assertText(request.target.head_sha, 'target.head_sha')
  if (request.target.scope !== 'diff') throw new CodeReviewQueueError('invalid_request', 'target.scope must be diff')
  if (!isPlainObject(request.source)) throw new CodeReviewQueueError('invalid_request', 'source must be an object')
  assertText(request.source.event_name, 'source.event_name')
  if (request.source.delivery_id !== null && request.source.delivery_id !== undefined) {
    assertText(request.source.delivery_id, 'source.delivery_id')
  }
  return request
}

function identity(request) {
  return {
    schema_version: request.schema_version,
    review_id: request.review_id,
    target: request.target,
  }
}

function existingOutcome(existing, request, filePath, state) {
  if (canonicalJson(identity(existing.request || existing)) !== canonicalJson(identity(request))) {
    throw new CodeReviewQueueError('idempotency_conflict', 'review id contains a different target: ' + request.review_id)
  }
  return { ok: true, outcome: 'duplicate', state, requestPath: filePath, reviewId: request.review_id }
}

function recordReviewRequest(request, stateRoot = DEFAULT_QUEUE_ROOT) {
  validateRequest(request)
  const paths = pathsFor(stateRoot)
  const pendingPath = fileFor(paths.pending, request.review_id)
  const completedPath = fileFor(paths.completed, request.review_id)
  const pending = readJson(pendingPath)
  if (pending) return existingOutcome(pending, request, pendingPath, 'pending')
  const completed = readJson(completedPath)
  if (completed) return existingOutcome(completed, request, completedPath, 'completed')
  const appended = writeAtomic(pendingPath, request, true)
  if (!appended) {
    const existingPending = readJson(pendingPath)
    if (existingPending) return existingOutcome(existingPending, request, pendingPath, 'pending')
    const existingCompleted = readJson(completedPath)
    if (existingCompleted) return existingOutcome(existingCompleted, request, completedPath, 'completed')
    throw new CodeReviewQueueError('queue_storage', 'request was concurrently created but could not be read')
  }
  return { ok: true, outcome: 'appended', state: 'pending', requestPath: pendingPath, reviewId: request.review_id }
}

function findReviewRequest(reviewId, stateRoot = DEFAULT_QUEUE_ROOT) {
  assertText(reviewId, 'review_id')
  const paths = pathsFor(stateRoot)
  const pendingPath = fileFor(paths.pending, reviewId)
  const pending = readJson(pendingPath)
  if (pending) return { state: 'pending', request: validateRequest(pending), requestPath: pendingPath }
  const completedPath = fileFor(paths.completed, reviewId)
  const completed = readJson(completedPath)
  if (!completed) return null
  return { state: 'completed', completion: completed, requestPath: completedPath }
}

function listPendingRequests(stateRoot = DEFAULT_QUEUE_ROOT) {
  const paths = pathsFor(stateRoot)
  if (!fs.existsSync(paths.pending)) return []
  return fs.readdirSync(paths.pending).filter((name) => name.endsWith('.json')).sort().map((name) => {
    const request = validateRequest(readJson(path.join(paths.pending, name)))
    return { review_id: request.review_id, target: request.target, requestPath: path.join(paths.pending, name) }
  })
}

function completeReviewRequest(reviewId, result = {}, stateRoot = DEFAULT_QUEUE_ROOT) {
  assertText(reviewId, 'review_id')
  if (!isPlainObject(result)) throw new CodeReviewQueueError('invalid_completion', 'result must be an object')
  const allowedKeys = new Set(['status', 'report_id', 'error'])
  if (Object.keys(result).some((key) => !allowedKeys.has(key))) {
    throw new CodeReviewQueueError('invalid_completion', 'result contains unsupported fields')
  }
  if (!['AI_APPROVED', 'CHANGES_REQUIRED', 'ESCALATED', 'FAILED'].includes(result.status)) {
    throw new CodeReviewQueueError('invalid_completion', 'result.status is invalid')
  }
  if (result.report_id !== undefined) assertText(result.report_id, 'result.report_id')
  if (result.error !== undefined) assertText(result.error, 'result.error')
  const current = findReviewRequest(reviewId, stateRoot)
  if (!current) throw new CodeReviewQueueError('not_found', 'review request not found: ' + reviewId)
  if (current.state === 'completed') return { ok: true, outcome: 'duplicate', state: 'completed', requestPath: current.requestPath }
  const paths = pathsFor(stateRoot)
  const completion = {
    schema_version: COMPLETION_SCHEMA_VERSION,
    review_id: reviewId,
    request: current.request,
    result: { ...result },
  }
  const completedPath = fileFor(paths.completed, reviewId)
  writeAtomic(completedPath, completion)
  try { fs.unlinkSync(current.requestPath) } catch (error) {
    if (error.code !== 'ENOENT') throw new CodeReviewQueueError('queue_storage', error.message)
  }
  return { ok: true, outcome: 'completed', state: 'completed', requestPath: completedPath }
}

module.exports = {
  COMPLETION_SCHEMA_VERSION,
  CodeReviewQueueError,
  DEFAULT_QUEUE_ROOT,
  SCHEMA_VERSION,
  completeReviewRequest,
  findReviewRequest,
  listPendingRequests,
  recordReviewRequest,
  validateRequest,
}
