const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { spawnSync } = require('node:child_process')
const test = require('node:test')

const repo = path.resolve(__dirname, '..')
const submitHook = path.join(repo, 'hooks/verify-task-intent-submit.sh')
const gateHook = path.join(repo, 'hooks/verify-task-pre-edit-gate.sh')
const postHook = path.join(repo, 'hooks/verify-task-post-tool-check.sh')

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'verify-task-auto-entry-'))
  const home = path.join(root, 'home')
  const cwd = path.join(root, 'project')
  fs.mkdirSync(home, { recursive: true })
  fs.mkdirSync(cwd, { recursive: true })
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

test('coding prompt creates a gate and injects the Workflow instruction', () => {
  const env = fixture()
  try {
    const result = run(submitHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      prompt: '이 기능을 구현하고 테스트를 추가해줘',
    }, env)
    assert.equal(result.status, 0)
    assert.match(result.stdout, /additionalContext/)
    assert.equal(state(env).status, 'gate_required')
    assert.equal(state(env).cwd, env.cwd)
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('main Edit is denied until a successful Workflow result is observed', () => {
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

    const post = run(postHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      tool_name: 'Workflow',
      tool_input: {
        scriptPath: '~/.claude/workflows/verify-task-v2.js',
        args: { task: '코드 수정 후 리뷰', cwd: env.cwd },
      },
      tool_response: { structuredContent: { finalVerdict: { passed: true } } },
    }, env)
    assert.equal(post.status, 0)
    assert.equal(state(env).status, 'workflow_completed')

    const after = run(gateHook, editInput, env)
    assert.equal(after.stdout, '')
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('failed Workflow keeps the gate closed, while subagent writes bypass it', () => {
  const env = fixture()
  try {
    run(submitHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      prompt: '버그를 수정해줘',
    }, env)
    run(postHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      tool_name: 'Workflow',
      tool_input: {
        name: 'verify-task-v2',
        args: { task: '버그 수정', cwd: env.cwd },
      },
      tool_response: { structuredContent: { finalVerdict: { passed: false } } },
    }, env)
    assert.equal(state(env).status, 'workflow_failed')
    assert.match(run(gateHook, { session_id: env.sessionId, cwd: env.cwd }, env).stdout, /permissionDecision.*deny/s)
    assert.equal(run(gateHook, { session_id: env.sessionId, cwd: env.cwd, agent_id: 'subagent-1' }, env).stdout, '')
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('Workflow for another cwd cannot open the current session gate', () => {
  const env = fixture()
  try {
    run(submitHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      prompt: '코드 수정해줘',
    }, env)
    run(postHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      tool_name: 'Workflow',
      tool_input: {
        scriptPath: '~/.claude/workflows/verify-task-v2.js',
        args: { task: '다른 저장소 작업', cwd: path.join(env.root, 'other-project') },
      },
      tool_response: { structuredContent: { finalVerdict: { passed: true } } },
    }, env)
    assert.equal(state(env).status, 'gate_required')
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
