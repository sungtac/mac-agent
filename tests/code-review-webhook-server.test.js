const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const http = require('node:http')
const os = require('node:os')
const path = require('node:path')
const { createWebhookServer, readSecretFile } = require('../bin/code-review-webhook-server.js')
const { signPayload } = require('../workflows/lib/code-review-webhook-auth.js')

function request(server, { method = 'POST', path = '/github/webhook', body = '', headers = {} } = {}) {
  const address = server.address()
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: address.address,
      port: address.port,
      method,
      path,
      headers: { ...headers, 'content-length': Buffer.byteLength(body) },
    }, (response) => {
      const chunks = []
      response.on('data', (chunk) => chunks.push(chunk))
      response.on('end', () => resolve({
        statusCode: response.statusCode,
        body: Buffer.concat(chunks).toString('utf8'),
      }))
    })
    req.on('error', reject)
    req.end(body)
  })
}

async function withServer(options, callback) {
  const server = createWebhookServer(options)
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  try {
    return await callback(server)
  } finally {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
  }
}

function workflowBody() {
  return JSON.stringify({
    action: 'completed',
    repository: { full_name: 'acme/widget' },
    workflow_run: { conclusion: 'success', head_sha: 'sha-http', pull_requests: [{ number: 7 }] },
  })
}

test('webhook server exposes health and rejects unsigned webhook requests', async () => {
  await withServer({ secret: 'http-secret' }, async (server) => {
    const health = await request(server, { method: 'GET', path: '/health' })
    assert.equal(health.statusCode, 200)
    assert.deepEqual(JSON.parse(health.body), { ok: true, service: 'code-review-webhook-server' })

    const unsigned = await request(server, { body: workflowBody(), headers: { 'x-github-event': 'workflow_run' } })
    assert.equal(unsigned.statusCode, 401)
    assert.deepEqual(JSON.parse(unsigned.body), { ok: false, error: 'invalid_signature' })
  })
})

test('webhook secret-file loading requires restrictive permissions and never exposes content', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'code-review-secret-'))
  const secretPath = path.join(directory, 'webhook.secret')
  try {
    fs.writeFileSync(secretPath, 'file-secret\n', { mode: 0o600 })
    assert.equal(readSecretFile(secretPath), 'file-secret')
    fs.chmodSync(secretPath, 0o644)
    assert.equal(readSecretFile(secretPath), '')
  } finally {
    fs.rmSync(directory, { recursive: true, force: true })
  }
})

test('webhook server verifies raw body and returns a SHA-bound review request', async () => {
  await withServer({ secret: 'http-secret' }, async (server) => {
    const body = workflowBody()
    const result = await request(server, {
      body,
      headers: {
        'x-github-event': 'workflow_run',
        'x-github-delivery': 'delivery-http-1',
        'x-hub-signature-256': signPayload(body, 'http-secret'),
      },
    })
    assert.equal(result.statusCode, 202)
    const output = JSON.parse(result.body)
    assert.equal(output.delivery_id, 'delivery-http-1')
    assert.equal(output.decision.phase, 'start')
    assert.equal(output.review_request.target.head_sha, 'sha-http')
  })
})

test('webhook server durably queues a normalized start request before acknowledging', async () => {
  const queueRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'code-review-webhook-queue-'))
  try {
    await withServer({ secret: 'http-secret', queueRoot }, async (server) => {
      const body = workflowBody()
      const headers = {
        'x-github-event': 'workflow_run',
        'x-github-delivery': 'delivery-http-queue',
        'x-hub-signature-256': signPayload(body, 'http-secret'),
      }
      const first = await request(server, { body, headers })
      const second = await request(server, { body, headers: { ...headers, 'x-github-delivery': 'delivery-http-queue-duplicate' } })
      assert.equal(JSON.parse(first.body).handoff.outcome, 'appended')
      assert.equal(JSON.parse(second.body).handoff.outcome, 'duplicate')
      const files = fs.readdirSync(path.join(queueRoot, 'pending'))
      assert.equal(files.length, 1)
      const queued = JSON.parse(fs.readFileSync(path.join(queueRoot, 'pending', files[0]), 'utf8'))
      assert.equal(queued.target.head_sha, 'sha-http')
      assert.equal(queued.source.delivery_id, 'delivery-http-queue')
      assert.equal(Object.hasOwn(queued, 'payload'), false)
    })
  } finally {
    fs.rmSync(queueRoot, { recursive: true, force: true })
  }
})

test('webhook server fails closed for route, method, and body-size violations', async () => {
  await withServer({ secret: 'http-secret', maxBodyBytes: 32 }, async (server) => {
    const route = await request(server, { path: '/other', body: 'x' })
    assert.equal(route.statusCode, 404)

    const method = await request(server, { method: 'GET', body: '' })
    assert.equal(method.statusCode, 405)

    const large = await request(server, { body: 'x'.repeat(33), headers: { 'x-github-event': 'workflow_run' } })
    assert.equal(large.statusCode, 413)
  })
})

test('webhook server applies bounded transport settings', async () => {
  await withServer({
    secret: 'http-secret',
    requestTimeout: 3333,
    headersTimeout: 2222,
    socketTimeout: 1111,
    maxConcurrentBodies: 2,
  }, async (server) => {
    assert.equal(server.requestTimeout, 3333)
    assert.equal(server.headersTimeout, 2222)
    assert.equal(server.timeout, 1111)
    assert.equal(server.maxRequestsPerSocket, 100)
  })
})

test('webhook server clamps headers timeout to the request timeout', async () => {
  await withServer({
    secret: 'http-secret',
    requestTimeout: 1111,
    headersTimeout: 2222,
  }, async (server) => {
    assert.equal(server.requestTimeout, 1111)
    assert.equal(server.headersTimeout, 1111)
  })
})
