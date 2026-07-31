#!/usr/bin/env node

// Minimal local GitHub webhook receiver for the code-review event contract.
// It verifies the raw body before parsing and only emits a review decision.
// It never invokes a provider, calls GitHub, merges a PR, or logs the payload.

const http = require('node:http')
const fs = require('node:fs')
const { decideReviewTrigger } = require('../workflows/lib/code-review-trigger.js')
const { verifySignature } = require('../workflows/lib/code-review-webhook-auth.js')
const {
  DEFAULT_QUEUE_ROOT,
  SCHEMA_VERSION: REQUEST_SCHEMA_VERSION,
  recordReviewRequest,
} = require('../workflows/lib/code-review-request-queue.js')

const SCHEMA_VERSION = 'edge_agent.code_review_trigger.v1'
const DEFAULT_HOST = '127.0.0.1'
const DEFAULT_PORT = 8787
const DEFAULT_PATH = '/github/webhook'
const HEALTH_PATH = '/health'
const DEFAULT_MAX_BODY_BYTES = 1024 * 1024

function readSecretFile(secretFile) {
  if (!secretFile) return ''
  try {
    const stat = fs.statSync(secretFile)
    if (!stat.isFile() || (stat.mode & 0o077) !== 0) return ''
    return fs.readFileSync(secretFile, 'utf8').trimEnd()
  } catch {
    return ''
  }
}

function sendJson(response, statusCode, body) {
  const serialized = JSON.stringify(body) + '\n'
  response.writeHead(statusCode, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(serialized),
    'cache-control': 'no-store',
  })
  response.end(serialized)
}

function headerValue(request, name) {
  const value = request.headers[name]
  return Array.isArray(value) ? value[0] : value || ''
}

function decisionOutput(eventName, deliveryId, decision) {
  const output = {
    schema_version: SCHEMA_VERSION,
    event_name: eventName,
    delivery_id: deliveryId || null,
    decision,
  }
  if (decision.shouldReview) {
    output.review_request = {
      review_id: 'github-' + decision.idempotencyKey,
      target: decision.target,
    }
  }
  return output
}

function queueRequest(output, queueRoot) {
  if (!queueRoot || !output.review_request) return null
  return recordReviewRequest({
    schema_version: REQUEST_SCHEMA_VERSION,
    review_id: output.review_request.review_id,
    target: output.review_request.target,
    source: {
      event_name: output.event_name,
      delivery_id: output.delivery_id,
    },
  }, queueRoot)
}

function readBody(request, maxBodyBytes) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let total = 0
    let settled = false

    const fail = (error) => {
      if (settled) return
      settled = true
      reject(error)
    }

    request.on('data', (chunk) => {
      if (settled) return
      total += chunk.length
      if (total > maxBodyBytes) {
        const error = new Error('request body exceeds configured limit')
        error.code = 'BODY_TOO_LARGE'
        fail(error)
        request.resume()
        return
      }
      chunks.push(chunk)
    })
    request.on('end', () => {
      if (settled) return
      settled = true
      resolve(Buffer.concat(chunks).toString('utf8'))
    })
    request.on('error', fail)
  })
}

function createWebhookServer(options = {}) {
  const webhookPath = options.webhookPath || DEFAULT_PATH
  const healthPath = options.healthPath || HEALTH_PATH
  const secret = options.secret || readSecretFile(options.secretFile)
  const queueRoot = options.queueRoot || ''
  const maxBodyBytes = Number.isSafeInteger(options.maxBodyBytes) && options.maxBodyBytes > 0
    ? options.maxBodyBytes
    : DEFAULT_MAX_BODY_BYTES

  return http.createServer(async (request, response) => {
    if (request.method === 'GET' && request.url === healthPath) {
      sendJson(response, 200, { ok: true, service: 'code-review-webhook-server' })
      return
    }
    if (request.url !== webhookPath) {
      sendJson(response, 404, { ok: false, error: 'not_found' })
      return
    }
    if (request.method !== 'POST') {
      sendJson(response, 405, { ok: false, error: 'method_not_allowed' })
      return
    }
    if (!secret) {
      request.resume()
      sendJson(response, 503, { ok: false, error: 'webhook_secret_not_configured' })
      return
    }

    let rawBody
    try {
      rawBody = await readBody(request, maxBodyBytes)
    } catch (error) {
      if (error.code === 'BODY_TOO_LARGE') {
        sendJson(response, 413, { ok: false, error: 'payload_too_large' })
        return
      }
      sendJson(response, 400, { ok: false, error: 'invalid_request_body' })
      return
    }

    const verification = verifySignature(rawBody, headerValue(request, 'x-hub-signature-256'), secret)
    if (!verification.valid) {
      sendJson(response, 401, { ok: false, error: 'invalid_signature' })
      return
    }

    let payload
    try {
      payload = JSON.parse(rawBody)
    } catch {
      sendJson(response, 400, { ok: false, error: 'invalid_payload' })
      return
    }

    const eventName = headerValue(request, 'x-github-event')
    if (!eventName) {
      sendJson(response, 400, { ok: false, error: 'missing_event_name' })
      return
    }

    const decision = decideReviewTrigger(eventName, payload)
    const statusCode = decision.phase === 'start' ? 202 : 200
    const output = decisionOutput(eventName, headerValue(request, 'x-github-delivery'), decision)
    if (decision.shouldReview && queueRoot) {
      try {
        const handoff = queueRequest(output, queueRoot)
        output.handoff = { kind: 'local_request_queue', outcome: handoff.outcome }
      } catch {
        sendJson(response, 503, { ok: false, error: 'review_queue_unavailable' })
        return
      }
    }
    sendJson(response, statusCode, output)
  })
}

function main() {
  const secret = process.env.CODE_REVIEW_WEBHOOK_SECRET || readSecretFile(process.env.CODE_REVIEW_WEBHOOK_SECRET_FILE)
  if (!secret) {
    process.stderr.write(JSON.stringify({ ok: false, error: 'webhook_secret_not_configured' }) + '\n')
    return 2
  }
  const host = process.env.CODE_REVIEW_WEBHOOK_HOST || DEFAULT_HOST
  const port = Number.parseInt(process.env.CODE_REVIEW_WEBHOOK_PORT || String(DEFAULT_PORT), 10)
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    process.stderr.write(JSON.stringify({ ok: false, error: 'invalid_port' }) + '\n')
    return 2
  }
  const server = createWebhookServer({ secret, queueRoot: process.env.EDGE_AGENT_CODE_REVIEW_QUEUE_ROOT || DEFAULT_QUEUE_ROOT })
  server.listen(port, host, () => {
    process.stdout.write(JSON.stringify({ ok: true, host, port, path: DEFAULT_PATH }) + '\n')
  })
  server.on('error', (error) => {
    process.stderr.write(JSON.stringify({ ok: false, error: 'server_error', code: error.code || 'unknown' }) + '\n')
    process.exitCode = 1
  })
  return undefined
}

if (require.main === module) {
  const exitCode = main()
  if (exitCode !== undefined) process.exitCode = exitCode
}

module.exports = {
  DEFAULT_HOST,
  DEFAULT_MAX_BODY_BYTES,
  DEFAULT_PATH,
  DEFAULT_PORT,
  HEALTH_PATH,
  SCHEMA_VERSION,
  createWebhookServer,
  decisionOutput,
  main,
  readSecretFile,
  readBody,
}
