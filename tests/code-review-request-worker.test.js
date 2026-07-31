const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { execFileSync } = require('node:child_process')
const { recordReviewRequest, findReviewRequest, SCHEMA_VERSION: REQUEST_SCHEMA } = require('../workflows/lib/code-review-request-queue.js')
const { findLatestReview } = require('../workflows/lib/code-review-store.js')
const { runWorkerOnce } = require('../bin/code-review-request-worker.js')

let root
let repo
let queueRoot
let stateRoot
let headSha

test.beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'code-review-worker-'))
  repo = path.join(root, 'repo')
  queueRoot = path.join(root, 'queue')
  stateRoot = path.join(root, 'state')
  fs.mkdirSync(repo)
  execFileSync('/usr/bin/git', ['-C', repo, 'init', '-q'])
  execFileSync('/usr/bin/git', ['-C', repo, 'config', 'user.email', 'test@example.com'])
  execFileSync('/usr/bin/git', ['-C', repo, 'config', 'user.name', 'Test'])
  execFileSync('/usr/bin/git', ['-C', repo, 'remote', 'add', 'origin', 'https://github.com/acme/widget.git'])
  fs.writeFileSync(path.join(repo, 'app.js'), 'module.exports = 1\n')
  execFileSync('/usr/bin/git', ['-C', repo, 'add', 'app.js'])
  execFileSync('/usr/bin/git', ['-C', repo, 'commit', '-q', '-m', 'initial'])
  headSha = execFileSync('/usr/bin/git', ['-C', repo, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim()
})

test.afterEach(() => fs.rmSync(root, { recursive: true, force: true }))

function queueRequest(overrides = {}) {
  return {
    schema_version: REQUEST_SCHEMA,
    review_id: 'github-' + 'b'.repeat(64),
    target: { repository: 'acme/widget', pull_request: 9, head_sha: headSha, scope: 'diff' },
    source: { event_name: 'workflow_run', delivery_id: 'worker-test' },
    ...overrides,
  }
}

function fakeDispatch(tool, promptFile) {
  const prompt = fs.readFileSync(promptFile, 'utf8')
  assert.match(prompt, new RegExp(tool === 'codex' ? '1차 코드 리뷰어' : '독립 승인 검증자'))
  return {
    hasBlockingIssue: false,
    issues: [],
    notes: 'fake evidence',
  }
}

test('dry-run verifies the exact clean SHA without invoking providers', async () => {
  const request = queueRequest()
  recordReviewRequest(request, queueRoot)
  let invoked = false
  const result = await runWorkerOnce({ queueRoot, stateRoot, repositoryRoot: repo, repositoryName: 'acme/widget', dispatch: () => { invoked = true } })
  assert.equal(result.outcome, 'dry_run')
  assert.equal(invoked, false)
  assert.equal(findReviewRequest(request.review_id, queueRoot).state, 'pending')
})

test('execute runs Codex and Antigravity, persists the report, and completes the queue item', async () => {
  const request = queueRequest()
  recordReviewRequest(request, queueRoot)
  const result = await runWorkerOnce({ queueRoot, stateRoot, repositoryRoot: repo, repositoryName: 'acme/widget', execute: true, dispatch: fakeDispatch })
  assert.equal(result.status, 'AI_APPROVED')
  assert.equal(findReviewRequest(request.review_id, queueRoot).state, 'completed')
  const report = findLatestReview(request.review_id, stateRoot)
  assert.equal(report.status, 'AI_APPROVED')
  assert.equal(report.approval.provider, 'antigravity')
  assert.equal(report.approval.reviewed_head_sha, headSha)
})

test('head SHA mismatch fails closed and leaves the request pending', async () => {
  const request = queueRequest({ target: { repository: 'acme/widget', pull_request: 9, head_sha: 'wrong-sha', scope: 'diff' } })
  recordReviewRequest(request, queueRoot)
  await assert.rejects(
    () => runWorkerOnce({ queueRoot, stateRoot, repositoryRoot: repo, repositoryName: 'acme/widget', execute: true, dispatch: fakeDispatch }),
    (error) => error.code === 'head_sha_mismatch'
  )
  assert.equal(findReviewRequest(request.review_id, queueRoot).state, 'pending')
})

test('isolated mode reviews the exact SHA without requiring the source worktree to be clean', async () => {
  const request = queueRequest()
  recordReviewRequest(request, queueRoot)
  fs.writeFileSync(path.join(repo, 'uncommitted.js'), 'preserve me\n')
  const result = await runWorkerOnce({ queueRoot, stateRoot, repositoryRoot: repo, repositoryName: 'acme/widget', isolated: true, execute: true, dispatch: fakeDispatch })
  assert.equal(result.status, 'AI_APPROVED')
  assert.equal(fs.readFileSync(path.join(repo, 'uncommitted.js'), 'utf8'), 'preserve me\n')
  const worktrees = execFileSync('/usr/bin/git', ['-C', repo, 'worktree', 'list'], { encoding: 'utf8' }).trim().split('\n')
  assert.equal(worktrees.length, 1)
})

test('a blocking finding produces changes required without AI approval', async () => {
  const request = queueRequest()
  recordReviewRequest(request, queueRoot)
  const result = await runWorkerOnce({
    queueRoot,
    stateRoot,
    repositoryRoot: repo,
    repositoryName: 'acme/widget',
    execute: true,
    dispatch: async (tool) => ({ hasBlockingIssue: tool === 'codex', issues: tool === 'codex' ? [{ description: 'unsafe behavior', blocking: true, evidence: 'fake evidence', remediation: 'fix it' }] : [], notes: '' }),
  })
  assert.equal(result.status, 'CHANGES_REQUIRED')
  assert.equal(findLatestReview(request.review_id, stateRoot).approval, undefined)
})
