const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { execFileSync } = require('node:child_process')

const runner = path.join(__dirname, '..', 'bin', 'run-nano-provider-pilot.sh')

function makeFixture(body) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'nano-provider-runner-'))
  const repo = path.join(root, 'repo')
  fs.mkdirSync(repo)
  execFileSync('/usr/bin/git', ['init', '-q', repo])
  const fakeClaude = path.join(root, 'fake-claude')
  fs.writeFileSync(fakeClaude, `#!/bin/sh\n${body}\n`, { mode: 0o755 })
  const prompt = path.join(root, 'prompt.txt')
  fs.writeFileSync(prompt, 'pilot prompt')
  const outputDir = path.join(root, 'logs')
  return { root, repo, fakeClaude, prompt, outputDir }
}

function run(fixture, extraEnv = {}) {
  return execFileSync('/bin/bash', [runner, fixture.repo, fixture.prompt], {
    env: { ...process.env, CLAUDE_BIN: fixture.fakeClaude, NANO_PILOT_LOG_DIR: fixture.outputDir, ...extraEnv },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  })
}

test('bounded pilot runner reports success and removes debug artifact', () => {
  const fixture = makeFixture('echo "wait-ceiling=$CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"; exit 0')
  try {
    const output = run(fixture)
    assert.match(output, /status=success/)
    const outputLog = fs.readdirSync(fixture.outputDir).find((name) => name.endsWith('.out.log'))
    assert.match(fs.readFileSync(path.join(fixture.outputDir, outputLog), 'utf8'), /wait-ceiling=0/)
    assert.equal(fs.readdirSync(fixture.outputDir).filter((name) => name.endsWith('.debug.log')).length, 0)
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('bounded pilot runner stops a hanging provider with a distinct exit code', () => {
  const fixture = makeFixture('sleep 10')
  try {
    assert.throws(
      () => run(fixture, { NANO_PILOT_TIMEOUT_SECONDS: '1' }),
      (error) => error.status === 124 && /status=timeout/.test(error.stdout),
    )
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('bounded pilot runner does not retry a quota failure', () => {
  const fixture = makeFixture('echo "You have hit your session limit"; exit 1')
  try {
    assert.throws(
      () => run(fixture, { NANO_PILOT_MAX_ATTEMPTS: '3' }),
      (error) => error.status === 75 && /status=provider_unavailable/.test(error.stdout),
    )
    assert.equal(fs.readdirSync(fixture.outputDir).filter((name) => name.endsWith('.out.log')).length, 1)
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})
