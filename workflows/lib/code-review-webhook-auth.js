#!/usr/bin/env node

// GitHub X-Hub-Signature-256 verification. This module never logs secrets,
// signatures, or request bodies.

const crypto = require('node:crypto')

function signPayload(rawBody, secret) {
  if (!secret) throw new TypeError('secret is required')
  return 'sha256=' + crypto.createHmac('sha256', secret).update(rawBody).digest('hex')
}

function verifySignature(rawBody, signature, secret) {
  if (!secret) return { valid: false, reason: 'missing_secret' }
  if (typeof signature !== 'string' || !signature.startsWith('sha256=')) {
    return { valid: false, reason: 'missing_signature' }
  }
  const provided = signature.slice('sha256='.length)
  if (!/^[a-f0-9]{64}$/i.test(provided)) {
    return { valid: false, reason: 'malformed_signature' }
  }
  const expected = signPayload(rawBody, secret).slice('sha256='.length)
  const valid = crypto.timingSafeEqual(
    Buffer.from(expected, 'hex'),
    Buffer.from(provided, 'hex')
  )
  return { valid, reason: valid ? 'verified' : 'signature_mismatch' }
}

module.exports = { signPayload, verifySignature }
