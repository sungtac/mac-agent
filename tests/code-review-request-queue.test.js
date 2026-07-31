const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const {
  CodeReviewQueueError,
  completeReviewRequest,
  findReviewRequest,
  listPendingRequests,
  recordReviewRequest,
  SCHEMA_VERSION,
} = require('../workflows/lib/code-review-request-queue.js')

let root

test.beforeEach(() => { root = fs.mkdtempSync(path.join(os.tmpdir(), 'code-review-queue-')) })
test.afterEach(() => { fs.rmSync(root, { recursive: true, force: true }) })

function request(overrides = {}) {
  return {
    schema_version: SCHEMA_VERSION,
    review_id: 'github-' + 'a'.repeat(64),
    target: { repository: 'acme/widget', pull_request: 7, head_sha: 'sha-queue', scope: 'diff' },
    source: { event_name: 'workflow_run', delivery_id: 'delivery-1' },
    ...overrides,
  }
}

test('records a normalized request atomically and lists it after a restart', () => {
  const result = recordReviewRequest(request(), root)
  assert.equal(result.outcome, 'appended')
  assert.equal(listPendingRequests(root).length, 1)
  assert.equal(findReviewRequest(request().review_id, root).state, 'pending')
  const files = fs.readdirSync(path.join(root, 'pending'))
  assert.equal(files.length, 1)
  assert.equal(fs.statSync(path.join(root, 'pending', files[0])).mode & 0o777, 0o600)
})

test('same review id is idempotent even when delivery metadata changes', () => {
  assert.equal(recordReviewRequest(request(), root).outcome, 'appended')
  const duplicate = recordReviewRequest(request({ source: { event_name: 'check_suite', delivery_id: 'delivery-2' } }), root)
  assert.equal(duplicate.outcome, 'duplicate')
  assert.equal(listPendingRequests(root).length, 1)
})

test('different target under the same review id fails closed', () => {
  recordReviewRequest(request(), root)
  assert.throws(
    () => recordReviewRequest(request({ target: { repository: 'acme/widget', pull_request: 7, head_sha: 'sha-other', scope: 'diff' } }), root),
    (error) => error instanceof CodeReviewQueueError && error.code === 'idempotency_conflict'
  )
})

test('completion is durable and later delivery remains a duplicate', () => {
  const review = request()
  recordReviewRequest(review, root)
  assert.equal(completeReviewRequest(review.review_id, { status: 'AI_APPROVED', report_id: 'report-1' }, root).outcome, 'completed')
  assert.equal(findReviewRequest(review.review_id, root).state, 'completed')
  assert.equal(listPendingRequests(root).length, 0)
  assert.equal(recordReviewRequest(review, root).outcome, 'duplicate')
})

test('completion rejects unsupported fields that could leak arbitrary data', () => {
  const review = request()
  recordReviewRequest(review, root)
  assert.throws(
    () => completeReviewRequest(review.review_id, { status: 'AI_APPROVED', secret: 'do-not-store' }, root),
    (error) => error instanceof CodeReviewQueueError && error.code === 'invalid_completion'
  )
})
