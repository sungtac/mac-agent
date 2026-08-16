const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const {
  CodeReviewStoreError,
  findLatestReview,
  findLatestReviewByPr,
  recordReviewReport,
} = require('../workflows/lib/code-review-store.js')

let root

test.beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'code-review-store-'))
})

test.afterEach(() => {
  fs.rmSync(root, { recursive: true, force: true })
})

function report(overrides = {}) {
  return {
    schema_version: 'edge_agent.code_review_report.v1',
    review_id: 'repo-1-pr-7',
    status: 'AI_APPROVED',
    target: { scope: 'diff', head_sha: 'sha-1' },
    findings: [],
    checks: [{ name: 'unit', status: 'passed' }],
    approval: {
      provider: 'antigravity',
      reviewed_head_sha: 'sha-1',
      decision_reason: 'independently verified',
    },
    ...overrides,
  }
}

test('stores an immutable SHA-bound report and resolves latest pointer', () => {
  const first = recordReviewReport(report(), root)
  assert.equal(first.outcome, 'appended')
  assert.deepEqual(findLatestReview('repo-1-pr-7', root), report())

  const second = recordReviewReport(report({
    target: { scope: 'diff', head_sha: 'sha-2' },
    approval: { provider: 'antigravity', reviewed_head_sha: 'sha-2', decision_reason: 'verified again' },
  }), root)
  assert.equal(second.outcome, 'appended')
  assert.equal(findLatestReview('repo-1-pr-7', root).target.head_sha, 'sha-2')
  assert.equal(fs.readdirSync(path.join(root, 'reports')).length, 2)
})

test('replaying the same report is idempotent and changing content conflicts', () => {
  assert.equal(recordReviewReport(report(), root).outcome, 'appended')
  assert.equal(recordReviewReport(report(), root).outcome, 'duplicate')
  assert.throws(
    () => recordReviewReport(report({ notes: 'tampered' }), root),
    (error) => error instanceof CodeReviewStoreError && error.code === 'idempotency_conflict'
  )
})

test('approval cannot be persisted for different SHA or failed checks', () => {
  assert.throws(
    () => recordReviewReport(report({
      approval: { provider: 'antigravity', reviewed_head_sha: 'old', decision_reason: 'bad' },
    }), root),
    (error) => error instanceof CodeReviewStoreError && error.code === 'invalid_report'
  )
  assert.throws(
    () => recordReviewReport(report({ checks: [{ name: 'unit', status: 'failed' }] }), root),
    (error) => error instanceof CodeReviewStoreError && error.code === 'invalid_report'
  )
})

test('rejects findings outside the report schema', () => {
  assert.throws(
    () => recordReviewReport(report({
      findings: [{
        id: 'f-1',
        severity: 'medium',
        category: 'not-a-category',
        location: 'app.js:1',
        title: 'Invalid category',
        evidence: 'evidence',
        remediation: 'fix it',
      }],
    }), root),
    (error) => error instanceof CodeReviewStoreError && error.code === 'invalid_report'
  )
})

test('accepts multiline evidence and remediation in findings', () => {
  const result = recordReviewReport(report({
    findings: [{
      id: 'f-1',
      severity: 'medium',
      category: 'correctness',
      location: 'app.js:1',
      title: 'Behavior mismatch',
      evidence: 'line one\nline two',
      remediation: 'step one\nstep two',
    }],
  }), root)
  assert.equal(result.outcome, 'appended')
})

test('supports delta fields, finding statuses, PR lookup, and legacy reports', () => {
  const first = report({
    round: 1,
    pr_number: 7,
    target: { scope: 'diff', head_sha: 'sha-1', repository: 'acme/widget' },
    findings: [{ id: 'f-1', severity: 'medium', category: 'correctness', status: 'open', location: 'app.js#run', title: 'Issue', evidence: 'evidence', remediation: 'fix' }],
  })
  const second = report({
    round: 2, parent_report_key: 'parent-key', pr_number: 7,
    target: { scope: 'diff', head_sha: 'sha-2', repository: 'acme/widget' },
    approval: { provider: 'antigravity', reviewed_head_sha: 'sha-2', decision_reason: 'verified again' },
  })
  recordReviewReport(first, root)
  recordReviewReport(second, root)
  assert.equal(findLatestReviewByPr(7, 'acme/widget', root).round, 2)
  assert.throws(() => recordReviewReport(report({ round: 0 }), root), /round must be/)
  assert.throws(() => recordReviewReport(report({ findings: [{ id: 'f', severity: 'low', category: 'correctness', status: 'unknown', location: 'x', title: 'x', evidence: 'x', remediation: 'x' }] }), root), /status is invalid/)
  assert.equal(findLatestReview('repo-1-pr-7', root).round, 2)
})
