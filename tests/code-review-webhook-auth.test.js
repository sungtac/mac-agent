const test = require('node:test')
const assert = require('node:assert/strict')

const { signPayload, verifySignature } = require('../workflows/lib/code-review-webhook-auth.js')

test('validates the exact raw request body with a constant-time comparison', () => {
  const body = '{"action":"completed","value":1}'
  const signature = signPayload(body, 'test-secret')
  assert.deepEqual(verifySignature(body, signature, 'test-secret'), { valid: true, reason: 'verified' })
})

test('rejects changed body, missing signature, malformed signature, and missing secret', () => {
  const signature = signPayload('{"value":1}', 'test-secret')
  assert.equal(verifySignature('{"value":2}', signature, 'test-secret').valid, false)
  assert.equal(verifySignature('{"value":1}', '', 'test-secret').reason, 'missing_signature')
  assert.equal(verifySignature('{"value":1}', 'sha256=bad', 'test-secret').reason, 'malformed_signature')
  assert.equal(verifySignature('{"value":1}', signature, '').reason, 'missing_secret')
})
