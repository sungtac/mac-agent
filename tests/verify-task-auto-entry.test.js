const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { spawnSync } = require('node:child_process')
const test = require('node:test')

const repo = path.resolve(__dirname, '..')
const submitHook = path.join(repo, 'hooks/verify-task-intent-submit.sh')
const gateHook = path.join(repo, 'hooks/verify-task-pre-edit-gate.sh')
const hostOrchestrator = path.join(repo, 'bin/verify-task-orchestrator.py')

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'verify-task-auto-entry-'))
  const home = path.join(root, 'home')
  const cwd = path.join(root, 'project')
  fs.mkdirSync(home, { recursive: true })
  fs.mkdirSync(cwd, { recursive: true })
  const initialized = spawnSync('git', ['init', '-q'], { cwd, encoding: 'utf8' })
  assert.equal(initialized.status, 0, initialized.stderr)
  return { root, home, cwd, sessionId: 'session-test-123' }
}

function run(script, input, env) {
  return spawnSync('bash', [script], {
    input: JSON.stringify(input),
    encoding: 'utf8',
    env: { ...process.env, HOME: env.home },
  })
}

function state(env) {
  return JSON.parse(fs.readFileSync(
    path.join(env.home, '.claude/hooks-state/verify-task-v2', `${env.sessionId}.json`),
    'utf8',
  ))
}

function runHost(env, cwd = env.cwd, task = '코드 수정') {
  return spawnSync('python3', [
    hostOrchestrator,
    '--task', task,
    '--cwd', cwd,
    '--run-dir', path.join(env.root, 'verify-run'),
    '--session-id', env.sessionId,
    '--dry-run',
  ], {
    encoding: 'utf8',
    env: { ...process.env, HOME: env.home },
  })
}

test('coding prompt creates a gate and injects the host orchestrator instruction', () => {
  const env = fixture()
  try {
    const result = run(submitHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      prompt: '이 기능을 구현하고 테스트를 추가해줘',
    }, env)
    assert.equal(result.status, 0)
    assert.match(result.stdout, /verify-task-orchestrator\.py/)
    assert.equal(state(env).status, 'gate_required')
    assert.equal(state(env).cwd, env.cwd)
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('main Edit is denied until the host orchestrator records success', () => {
  const env = fixture()
  try {
    run(submitHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      prompt: '코드 수정 후 리뷰해줘',
    }, env)
    const editInput = { session_id: env.sessionId, cwd: env.cwd }
    const before = run(gateHook, editInput, env)
    assert.match(before.stdout, /permissionDecision.*deny/s)

    const post = runHost(env, env.cwd, '코드 수정 후 리뷰')
    assert.equal(post.status, 0, post.stdout || post.stderr)
    assert.equal(state(env).status, 'workflow_completed')

    const after = run(gateHook, editInput, env)
    assert.equal(after.stdout, '')
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('failed host orchestration keeps the gate closed, while subagent writes bypass it', () => {
  const env = fixture()
  try {
    run(submitHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      prompt: '버그를 수정해줘',
    }, env)
    const failedCwd = path.join(env.root, 'not-a-git-project')
    const result = runHost(env, failedCwd, '버그 수정')
    assert.notEqual(result.status, 0)
    assert.equal(state(env).status, 'workflow_failed')
    assert.match(run(gateHook, { session_id: env.sessionId, cwd: env.cwd }, env).stdout, /permissionDecision.*deny/s)
    assert.equal(run(gateHook, { session_id: env.sessionId, cwd: env.cwd, agent_id: 'subagent-1' }, env).stdout, '')
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('host orchestration for another cwd cannot open the current session gate', () => {
  const env = fixture()
  try {
    run(submitHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      prompt: '코드 수정해줘',
    }, env)
    const otherCwd = path.join(env.root, 'other-project')
    fs.mkdirSync(otherCwd)
    const initialized = spawnSync('git', ['init', '-q'], { cwd: otherCwd, encoding: 'utf8' })
    assert.equal(initialized.status, 0, initialized.stderr)
    const result = runHost(env, otherCwd, '다른 저장소 작업')
    assert.equal(result.status, 0, result.stdout || result.stderr)
    assert.equal(state(env).status, 'gate_required')
    assert.equal(state(env).cwd, env.cwd)
    assert.match(run(gateHook, { session_id: env.sessionId, cwd: env.cwd }, env).stdout, /permissionDecision.*deny/s)
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('ordinary prompt does not create a coding gate', () => {
  const env = fixture()
  try {
    const result = run(submitHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      prompt: '오늘 서울 날씨 알려줘',
    }, env)
    assert.equal(result.stdout, '')
    assert.equal(fs.existsSync(path.join(env.home, '.claude/hooks-state/verify-task-v2')), false)
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('missing main-session context fails closed at the edit gate', () => {
  const env = fixture()
  try {
    const result = run(gateHook, {}, env)
    assert.match(result.stdout, /permissionDecision.*deny/s)
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})
