const test = require('node:test')
const assert = require('node:assert/strict')

const { decideReviewTrigger } = require('../workflows/lib/code-review-trigger.js')

function pullRequestPayload(overrides = {}) {
  return {
    action: 'ready_for_review',
    repository: { full_name: 'acme/widget' },
    pull_request: { number: 12, draft: false, head: { sha: 'sha-1' } },
    ...overrides,
  }
}

test('PR readiness waits for CI instead of reviewing every push immediately', () => {
  const result = decideReviewTrigger('pull_request', pullRequestPayload())
  assert.equal(result.shouldReview, false)
  assert.equal(result.phase, 'await_ci')
  assert.equal(result.target.head_sha, 'sha-1')
  assert.match(result.reason, /waiting for successful CI/)
})

test('successful workflow completion starts one SHA-bound review', () => {
  const result = decideReviewTrigger('workflow_run', {
    action: 'completed',
    repository: { full_name: 'acme/widget' },
    workflow_run: { conclusion: 'success', head_sha: 'sha-2', pull_requests: [{ number: 12 }] },
  })
  assert.equal(result.shouldReview, true)
  assert.equal(result.phase, 'start')
  assert.equal(result.target.pull_request, 12)
  assert.ok(result.idempotencyKey)
})

test('failed CI, draft PRs, and unsupported events do not start review', () => {
  assert.equal(decideReviewTrigger('workflow_run', {
    action: 'completed',
    repository: { full_name: 'acme/widget' },
    workflow_run: { conclusion: 'failure', head_sha: 'sha-3', pull_requests: [{ number: 12 }] },
  }).shouldReview, false)
  assert.equal(decideReviewTrigger('pull_request', pullRequestPayload({
    pull_request: { number: 12, draft: true, head: { sha: 'sha-1' } },
  })).phase, 'ignore')
  assert.equal(decideReviewTrigger('push', pullRequestPayload()).phase, 'ignore')
})

test('missing target identity fails closed', () => {
  const result = decideReviewTrigger('workflow_run', { action: 'completed', workflow_run: { conclusion: 'success' } })
  assert.equal(result.phase, 'ignore')
  assert.equal(result.idempotencyKey, null)
})
