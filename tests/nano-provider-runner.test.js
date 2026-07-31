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
    env: { ...process.env, CLAUDE_BIN: fixture.fakeClaude, NANO_PILOT_LOG_DIR: fixture.outputDir, NANO_PILOT_REQUIRE_USAGE_DATA: '0', ...extraEnv },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  })
}

function runWithEvent(fixture, eventFile, extraEnv = {}) {
  return execFileSync('/bin/bash', [runner, fixture.repo, fixture.prompt, eventFile], {
    env: { ...process.env, CLAUDE_BIN: fixture.fakeClaude, NANO_PILOT_LOG_DIR: fixture.outputDir, NANO_PILOT_REQUIRE_USAGE_DATA: '0', ...extraEnv },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  })
}

test('real pilot refuses to start when usage data is unavailable', () => {
  const fixture = makeFixture('echo should-not-run > "$NANO_PILOT_LOG_DIR/provider-ran"; exit 0')
  const gate = path.join(fixture.root, 'fake-usage-gate')
  fs.writeFileSync(gate, '#!/bin/sh\necho "PROCEED (coach returned no data — gate skipped, not enforced)"\n', { mode: 0o755 })
  try {
    assert.throws(
      () => run(fixture, { NANO_PILOT_REQUIRE_USAGE_DATA: '1', NANO_PILOT_USAGE_GATE: gate }),
      (error) => error.status === 76 && /status=usage_data_unavailable/.test(error.stdout),
    )
    assert.equal(fs.existsSync(path.join(fixture.outputDir, 'provider-ran')), false)
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('pilot records a post-task usage snapshot without changing task result', () => {
  const fixture = makeFixture('exit 0')
  const snapshot = path.join(fixture.root, 'snapshot.jsonl')
  const capture = path.join(fixture.root, 'capture-usage')
  fs.writeFileSync(capture, `#!/bin/sh\nprintf 'snapshot\\n' >> "$2"\n`, { mode: 0o755 })
  try {
    const output = run(fixture, {
      NANO_USAGE_SNAPSHOT_SCRIPT: capture,
      NANO_USAGE_SNAPSHOT_FILE: snapshot,
    })
    assert.match(output, /status=success/)
    assert.match(output, /usage_snapshot=recorded/)
    assert.equal(fs.readFileSync(snapshot, 'utf8'), 'snapshot\n')
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

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

test('pilot never treats a detached Workflow as success', () => {
  const fixture = makeFixture('echo "Workflow wf_test (task ID task_test) is running in the background against the repo"; exit 0')
  try {
    assert.throws(
      () => run(fixture),
      (error) => error.status === 125 && /status=workflow_detached/.test(error.stdout),
    )
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('pilot refuses mismatched Workflow event file before provider start', () => {
  const fixture = makeFixture('echo should-not-run > "$NANO_PILOT_LOG_DIR/provider-ran"; exit 0')
  const prompt = path.join(fixture.root, 'prompt.txt')
  fs.writeFileSync(prompt, 'Workflow({args:{cwd:"' + fixture.repo + '", nanoEventFile:"/tmp/other-events.jsonl"}})')
  const eventFile = path.join(fixture.root, 'events.jsonl')
  try {
    assert.throws(
      () => runWithEvent(fixture, eventFile),
      (error) => error.status === 67 && /status=configuration_error/.test(error.stdout),
    )
    assert.equal(fs.existsSync(path.join(fixture.outputDir, 'provider-ran')), false)
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

test('pilot does not call a provider-process success a workflow success', () => {
  const fixture = makeFixture('echo \'{"finalVerdict":{"passed":false,"error":"nano_light_blocked"}}\'; exit 0')
  try {
    assert.throws(
      () => run(fixture),
      (error) => error.status === 1 && /status=workflow_failed/.test(error.stdout),
    )
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})
