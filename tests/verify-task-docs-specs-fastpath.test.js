const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { spawnSync } = require('node:child_process')
const test = require('node:test')

const repo = path.resolve(__dirname, '..')
const gateHook = path.join(repo, 'hooks/verify-task-pre-edit-gate.sh')
const checkHook = path.join(repo, 'hooks/verify-task-dotfile-fastpath-check.sh')

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'docs-specs-fastpath-'))
  const home = path.join(root, 'home')
  const cwd = path.join(root, 'project')
  fs.mkdirSync(home, { recursive: true })
  fs.mkdirSync(path.join(cwd, 'docs/specs'), { recursive: true })
  fs.mkdirSync(path.join(cwd, 'src'), { recursive: true })
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

function backupDir(env) {
  return path.join(env.home, '.claude/hooks-state/dotfile-fastpath-backups')
}

test('new docs/specs file passes the gate without any workflow state', () => {
  const env = fixture()
  try {
    const filePath = path.join(env.cwd, 'docs/specs/2026-01-01-example-design.md')
    const result = run(gateHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      tool_input: { file_path: filePath },
    }, env)
    assert.equal(result.stdout, '')
    // mkdir -p runs unconditionally in the fast-path block, so the backup
    // directory exists but must stay empty when there was no prior file to back up.
    assert.deepEqual(fs.readdirSync(backupDir(env)), [])
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('editing an existing docs/specs file backs up the old content before passing', () => {
  const env = fixture()
  try {
    const filePath = path.join(env.cwd, 'docs/specs/2026-01-01-example-design.md')
    fs.writeFileSync(filePath, '# Example\n\nOriginal body\n')
    const result = run(gateHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      tool_input: { file_path: filePath },
    }, env)
    assert.equal(result.stdout, '')
    const backups = fs.readdirSync(backupDir(env))
    assert.equal(backups.length, 1)
    assert.match(backups[0], /^2026-01-01-example-design\.md\.\d{14}\.bak$/)
    assert.equal(
      fs.readFileSync(path.join(backupDir(env), backups[0]), 'utf8'),
      '# Example\n\nOriginal body\n',
    )
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('a non-docs/specs code file at the same cwd is still denied without workflow state', () => {
  const env = fixture()
  try {
    const filePath = path.join(env.cwd, 'src/foo.py')
    const result = run(gateHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      tool_input: { file_path: filePath },
    }, env)
    assert.match(result.stdout, /permissionDecision.*deny/s)
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('valid docs/specs content passes the PostToolUse check silently', () => {
  const env = fixture()
  try {
    const filePath = path.join(env.cwd, 'docs/specs/2026-01-01-example-design.md')
    fs.writeFileSync(filePath, '# Example\n\nBody\n')
    const result = run(checkHook, {
      tool_input: { file_path: filePath },
    }, env)
    assert.equal(result.stdout, '')
    assert.equal(fs.readFileSync(filePath, 'utf8'), '# Example\n\nBody\n')
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('empty docs/specs content fails the PostToolUse check and restores the backup', () => {
  const env = fixture()
  try {
    const filePath = path.join(env.cwd, 'docs/specs/2026-01-01-example-design.md')
    fs.writeFileSync(filePath, '# Example\n\nOriginal body\n')
    run(gateHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      tool_input: { file_path: filePath },
    }, env)
    fs.writeFileSync(filePath, '')
    const result = run(checkHook, {
      tool_input: { file_path: filePath },
    }, env)
    assert.match(result.stdout, /비어 있습니다/)
    assert.equal(fs.readFileSync(filePath, 'utf8'), '# Example\n\nOriginal body\n')
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('docs/specs content without a leading heading fails the check and restores the backup', () => {
  const env = fixture()
  try {
    const filePath = path.join(env.cwd, 'docs/specs/2026-01-01-example-design.md')
    fs.writeFileSync(filePath, '# Example\n\nOriginal body\n')
    run(gateHook, {
      session_id: env.sessionId,
      cwd: env.cwd,
      tool_input: { file_path: filePath },
    }, env)
    fs.writeFileSync(filePath, '이것은 제목 없이 시작합니다.\n')
    const result = run(checkHook, {
      tool_input: { file_path: filePath },
    }, env)
    assert.match(result.stdout, /제목으로 시작하지 않습니다/)
    assert.equal(fs.readFileSync(filePath, 'utf8'), '# Example\n\nOriginal body\n')
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})

test('invalid content on a brand-new docs/specs file fails the check with no backup to restore', () => {
  const env = fixture()
  try {
    const filePath = path.join(env.cwd, 'docs/specs/2026-01-02-new-design.md')
    fs.writeFileSync(filePath, '')
    const result = run(checkHook, {
      tool_input: { file_path: filePath },
    }, env)
    assert.match(result.stdout, /복구하지 못했습니다/)
    assert.equal(fs.readFileSync(filePath, 'utf8'), '')
  } finally {
    fs.rmSync(env.root, { recursive: true, force: true })
  }
})
