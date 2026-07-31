const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { recordReviewRequest, SCHEMA_VERSION } = require('../workflows/lib/code-review-request-queue.js')
const { CONFIG_SCHEMA, loadConfig, runConfigured } = require('../bin/code-review-worker-runner.js')

let root
let queueRoot
let stateRoot
let configPath

test.beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'code-review-runner-'))
  queueRoot = path.join(root, 'queue')
  stateRoot = path.join(root, 'state')
  configPath = path.join(root, 'repositories.json')
  fs.writeFileSync(configPath, JSON.stringify({
    schema: CONFIG_SCHEMA,
    repositories: { 'acme/widget': { repository_root: root, enabled: true } },
  }))
})

test.afterEach(() => fs.rmSync(root, { recursive: true, force: true }))

function request(repository, id) {
  return {
    schema_version: SCHEMA_VERSION,
    review_id: 'github-' + id.repeat(64),
    target: { repository, pull_request: 1, head_sha: 'sha-' + id, scope: 'diff' },
    source: { event_name: 'workflow_run', delivery_id: null },
  }
}

test('runner routes only allowlisted repositories and stays dry-run by default', async () => {
  recordReviewRequest(request('acme/widget', 'a'), queueRoot)
  recordReviewRequest(request('unknown/other', 'b'), queueRoot)
  const calls = []
  const result = await runConfigured({ configPath, queueRoot, stateRoot }, async (options) => {
    calls.push(options)
    return { ok: true, outcome: 'dry_run', review_id: options.reviewId }
  })
  assert.equal(result.ok, false)
  assert.equal(result.pending, 2)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].execute, false)
  assert.equal(calls[0].isolated, true)
  assert.equal(result.results.find((item) => item.repository === 'unknown/other').outcome, 'unmapped_repository')
})

test('runner passes explicit execute only to enabled mappings', async () => {
  recordReviewRequest(request('acme/widget', 'c'), queueRoot)
  let call
  const result = await runConfigured({ configPath, queueRoot, stateRoot, execute: true }, async (options) => {
    call = options
    return { ok: true, outcome: 'completed', review_id: options.reviewId }
  })
  assert.equal(result.ok, true)
  assert.equal(call.execute, true)
  assert.equal(call.repositoryName, 'acme/widget')
})

test('config expands home paths and rejects malformed mappings', () => {
  const config = loadConfig(configPath)
  assert.equal(config.repositories['acme/widget'].repositoryRoot, root)
  fs.writeFileSync(configPath, JSON.stringify({ schema: 'wrong', repositories: {} }))
  assert.throws(() => loadConfig(configPath), /schema is invalid/)
})
