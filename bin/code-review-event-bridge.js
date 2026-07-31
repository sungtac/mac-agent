#!/usr/bin/env node

// Convert a GitHub-style event payload into a safe local review decision.
// This command never invokes a provider, calls GitHub, or logs the payload.

const fs = require('node:fs')
const { decideReviewTrigger } = require('../workflows/lib/code-review-trigger.js')
const { verifySignature } = require('../workflows/lib/code-review-webhook-auth.js')

const SCHEMA_VERSION = 'edge_agent.code_review_trigger.v1'

function parseArgs(argv) {
  const result = {
    eventName: process.env.GITHUB_EVENT_NAME || '',
    payloadPath: process.env.GITHUB_EVENT_PATH || '-',
    deliveryId: process.env.GITHUB_DELIVERY || '',
    signature: process.env.GITHUB_EVENT_SIGNATURE || process.env.CODE_REVIEW_WEBHOOK_SIGNATURE || '',
    secretEnv: process.env.CODE_REVIEW_WEBHOOK_SECRET_ENV || 'CODE_REVIEW_WEBHOOK_SECRET',
  }
  const positional = []
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === '--event') result.eventName = argv[++index] || ''
    else if (arg === '--payload') result.payloadPath = argv[++index] || '-'
    else if (arg === '--delivery-id') result.deliveryId = argv[++index] || ''
    else if (arg === '--signature') result.signature = argv[++index] || ''
    else if (arg === '--secret-env') result.secretEnv = argv[++index] || result.secretEnv
    else if (arg === '--require-signature') result.requireSignature = true
    else if (arg === '--help') result.help = true
    else positional.push(arg)
  }
  if (positional[0]) result.eventName = positional[0]
  if (positional[1]) result.payloadPath = positional[1]
  return result
}

function readRawPayload(payloadPath) {
  return payloadPath === '-' ? fs.readFileSync(0, 'utf8') : fs.readFileSync(payloadPath, 'utf8')
}

function readPayload(raw) {
  return JSON.parse(raw)
}

function usage() {
  return 'usage: code-review-event-bridge.js [event] [payload.json|-] [--delivery-id ID] [--signature SIG] [--secret-env ENV] [--require-signature]'
}

function main(argv) {
  const args = parseArgs(argv)
  if (args.help) {
    process.stdout.write(usage() + '\n')
    return 0
  }
  if (!args.eventName) {
    process.stderr.write(JSON.stringify({ ok: false, error: 'missing_event_name', message: usage() }) + '\n')
    return 2
  }
  let rawPayload
  try {
    rawPayload = readRawPayload(args.payloadPath)
  } catch (error) {
    process.stderr.write(JSON.stringify({ ok: false, error: 'invalid_payload', message: error.message }) + '\n')
    return 2
  }

  const secret = process.env[args.secretEnv] || ''
  if (args.requireSignature || secret || args.signature) {
    const verification = verifySignature(rawPayload, args.signature, secret)
    if (!verification.valid) {
      process.stderr.write(JSON.stringify({ ok: false, error: 'invalid_signature', reason: verification.reason }) + '\n')
      return 3
    }
  }

  let payload
  try {
    payload = readPayload(rawPayload)
  } catch (error) {
    process.stderr.write(JSON.stringify({ ok: false, error: 'invalid_payload', message: error.message }) + '\n')
    return 2
  }

  const decision = decideReviewTrigger(args.eventName, payload)
  const output = {
    schema_version: SCHEMA_VERSION,
    event_name: args.eventName,
    delivery_id: args.deliveryId || null,
    decision,
  }
  if (decision.shouldReview) {
    output.review_request = {
      review_id: 'github-' + decision.idempotencyKey,
      target: decision.target,
    }
  }
  process.stdout.write(JSON.stringify(output) + '\n')
  return 0
}

if (require.main === module) process.exitCode = main(process.argv.slice(2))

module.exports = { SCHEMA_VERSION, main, parseArgs, readPayload, readRawPayload }
