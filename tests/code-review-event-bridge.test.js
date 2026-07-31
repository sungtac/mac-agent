const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { spawnSync } = require('node:child_process')
const { signPayload } = require('../workflows/lib/code-review-webhook-auth.js')

const BRIDGE = path.resolve(__dirname, '../bin/code-review-event-bridge.js')

function runBridge(args, input = '', env = {}) {
  return spawnSync(process.execPath, [BRIDGE, ...args], {
    input,
    encoding: 'utf8',
    env: { ...process.env, ...env },
  })
}

function writePayload(payload) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'code-review-bridge-'))
  const file = path.join(directory, 'event.json')
  fs.writeFileSync(file, JSON.stringify(payload), 'utf8')
  return { directory, file }
}

test('bridge emits await_ci for a PR lifecycle event without invoking a provider', () => {
  const payload = {
    action: 'ready_for_review',
    repository: { full_name: 'acme/widget' },
    pull_request: { number: 4, draft: false, head: { sha: 'sha-1' } },
  }
  const result = runBridge(['pull_request', '-'], JSON.stringify(payload), { GITHUB_DELIVERY: 'delivery-1' })
  assert.equal(result.status, 0)
  const output = JSON.parse(result.stdout)
  assert.equal(output.schema_version, 'edge_agent.code_review_trigger.v1')
  assert.equal(output.delivery_id, 'delivery-1')
  assert.equal(output.decision.phase, 'await_ci')
  assert.equal(output.review_request, undefined)
})

test('bridge emits a SHA-bound review request after successful CI', () => {
  const { directory, file } = writePayload({
    action: 'completed',
    repository: { full_name: 'acme/widget' },
    workflow_run: { conclusion: 'success', head_sha: 'sha-2', pull_requests: [{ number: 4 }] },
  })
  try {
    const result = runBridge(['workflow_run', file, '--delivery-id', 'delivery-2'])
    assert.equal(result.status, 0)
    const output = JSON.parse(result.stdout)
    assert.equal(output.decision.phase, 'start')
    assert.equal(output.review_request.target.head_sha, 'sha-2')
    assert.match(output.review_request.review_id, /^github-[a-f0-9]{64}$/)
  } finally {
    fs.rmSync(directory, { recursive: true, force: true })
  }
})

test('bridge fails closed for malformed input and never prints payload content', () => {
  const result = runBridge(['workflow_run', '-'], '{"secret":"do-not-print"')
  assert.equal(result.status, 2)
  assert.match(result.stderr, /invalid_payload/)
  assert.doesNotMatch(result.stderr, /do-not-print/)
})

test('bridge verifies a configured webhook signature before deciding', () => {
  const body = JSON.stringify({
    action: 'completed',
    repository: { full_name: 'acme/widget' },
    workflow_run: { conclusion: 'success', head_sha: 'sha-secure', pull_requests: [{ number: 4 }] },
  })
  const signature = signPayload(body, 'bridge-secret')
  const valid = runBridge(['workflow_run', '-', '--require-signature', '--signature', signature], body, {
    CODE_REVIEW_WEBHOOK_SECRET: 'bridge-secret',
  })
  assert.equal(valid.status, 0)
  assert.equal(JSON.parse(valid.stdout).decision.phase, 'start')

  const invalid = runBridge(['workflow_run', '-', '--require-signature', '--signature', 'sha256=bad'], body, {
    CODE_REVIEW_WEBHOOK_SECRET: 'bridge-secret',
  })
  assert.equal(invalid.status, 3)
  assert.match(invalid.stderr, /invalid_signature/)
  assert.doesNotMatch(invalid.stderr, /bridge-secret|sha256=bad/)
})
