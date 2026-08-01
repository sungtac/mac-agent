const assert = require('node:assert/strict')
const childProcess = require('node:child_process')
const crypto = require('node:crypto')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const source = fs.readFileSync(path.resolve(__dirname, '../workflows/verify-task-v2.js'), 'utf8')

function position(text) {
  const index = source.indexOf(text)
  assert.notEqual(index, -1, `source is missing: ${text}`)
  return index
}

test('full 트랙은 조사→Codex 계획→실행→자기점검→독립 코드리뷰 순서다', () => {
  const ordered = [
    "{ title: 'FullResearch'",
    "{ title: 'FullPlan'",
    "{ title: 'FullExecute'",
    "{ title: 'FullSelfCheck'",
    "{ title: 'FullCodeReviewSkill'",
  ].map(position)

  for (let index = 1; index < ordered.length; index += 1) {
    assert.ok(ordered[index - 1] < ordered[index], `${ordered[index - 1]} must precede ${ordered[index]}`)
  }
})

test('Antigravity 조사는 병렬 호출이고 Codex 계획은 조사 결과를 받는다', () => {
  position('RESEARCH_FOCI.map((focus) =>')
  position('const planResult = await dispatchWithRetry(')
  position('const failedResearch = researchResults')
  position("error: 'research_failed'")
})

test('provider dispatch는 mktemp 템플릿이 아닌 실제 프롬프트 경로를 전달하도록 지시한다', () => {
  assert.doesNotMatch(source, /mktemp \/tmp\/verify-task-v2-(?:codex|agy|exec)-XXXXXX/)
  assert.match(source, /TMP_FILE=.*mktemp -t verify-task-v2-/)
  assert.match(source, /문자열 \\`XXXXXX\\`, \\`<파일경로>\\`, \\`<임시파일>\\`을 그대로 사용하지 말고/)
  assert.match(source, /실제 경로를 인자로 전달/)
})

test('Codex 계획 후 실행·자기점검 성공을 확인하고 독립 code-review로 넘어간다', () => {
  const plan = position('const planResult = await dispatchWithRetry(')
  const execute = position('const execution = await fullExecute(cwd, finalPlan')
  const selfCheck = position('const selfCheck = await dispatchWithRetry(')
  const review = position("log(`[전체] ${round}라운드: Claude/Antigravity 독립 차단 리뷰")
  assert.ok(plan < execute)
  assert.ok(execute < review)
  assert.ok(selfCheck < review)
  position("error: 'execution_failed'")
})

test('code-review 결과는 결정론적 검사 미실행을 승인으로 처리하지 않는다', () => {
  position("status: ['passed', 'failed', 'not_run', 'error']")
  position('const reviewerChecksPass = reviewResults.every')
  position('failedChecks.length === 0')
})

test('code-review dispatchFailed envelope는 combinedIssues와 자동수정 전에 차단된다', () => {
  const gate = position('const failedReviews = [')
  const combined = position('const combinedIssues = [')
  assert.ok(gate < combined)
  position('isDispatchFailure(result)')
  position('통과·자동수정 처리하지 않고 재시도')
})

test('score-dispatch review 실패 envelope는 CODE_REVIEW_SCHEMA의 checks를 채운다', () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'verify-task-dispatch-'))
  const promptFile = path.join(tempDir, 'prompt.txt')
  fs.writeFileSync(promptFile, 'test prompt\n')
  const result = childProcess.spawnSync(
    'bash',
    [path.resolve(__dirname, '../workflows/lib/score-dispatch.sh'), 'codex', promptFile, 'review'],
    { encoding: 'utf8', env: { ...process.env, CODEX_BIN: path.join(tempDir, 'missing-codex') } }
  )
  assert.equal(result.status, 0, result.stderr)
  const envelope = JSON.parse(result.stdout)
  assert.equal(envelope.dispatchFailed, true)
  assert.equal(envelope.checks[0].name, 'review-dispatch')
  assert.equal(envelope.checks[0].status, 'error')
})

test('score-dispatch는 JSON이 없을 때 원본 provider 출력을 실패 사유에 보존한다', () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'verify-task-raw-output-'))
  const promptFile = path.join(tempDir, 'prompt.txt')
  const fakeCodex = path.join(tempDir, 'codex')
  fs.writeFileSync(promptFile, 'test prompt\n')
  fs.writeFileSync(fakeCodex, '#!/bin/sh\nprintf "%s" "ERROR: logging before google.Init"\n')
  fs.chmodSync(fakeCodex, 0o755)
  const result = childProcess.spawnSync(
    'bash',
    [path.resolve(__dirname, '../workflows/lib/score-dispatch.sh'), 'codex', promptFile, 'review'],
    { encoding: 'utf8', env: { ...process.env, CODEX_BIN: fakeCodex } }
  )
  assert.equal(result.status, 0, result.stderr)
  const envelope = JSON.parse(result.stdout)
  assert.equal(envelope.dispatchFailed, true)
  assert.match(envelope.dispatchFailureReason, /원본 출력/)
  assert.match(envelope.dispatchFailureReason, /logging before google\.Init/)
})

test('Antigravity 리뷰 샌드박스는 저장소가 아닌 진단 로그 경로만 허용한다', () => {
  const profile = fs.readFileSync(path.resolve(__dirname, '../config/code-review-read-only.sb'), 'utf8')
  assert.match(profile, /subpath "\/Users\/edge_ai\/\.gemini\/antigravity-cli\/log"/)
  assert.match(profile, /subpath "\/Users\/edge_ai\/\.gemini\/antigravity-cli\/crashes"/)
})

test('score-dispatch는 Antigravity 로그 경로 권한 오류를 실행 전에 구조화한다', () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'verify-task-agy-preflight-'))
  const promptFile = path.join(tempDir, 'prompt.txt')
  const logPath = path.join(tempDir, 'log')
  fs.writeFileSync(promptFile, 'test prompt\n')
  fs.writeFileSync(logPath, 'not a directory\n')
  const result = childProcess.spawnSync(
    'bash',
    [path.resolve(__dirname, '../workflows/lib/score-dispatch.sh'), 'agy', promptFile, 'review'],
    {
      encoding: 'utf8',
      env: { ...process.env, AGY_BIN: '/usr/bin/true', AGY_LOG_ROOT: tempDir },
    }
  )
  assert.equal(result.status, 0, result.stderr)
  const envelope = JSON.parse(result.stdout)
  assert.equal(envelope.dispatchFailed, true)
  assert.match(envelope.dispatchFailureReason, /로그 디렉터리/)
  assert.equal(envelope.checks[0].status, 'error')
})

test('이전 자체계획·블라인드 비평 함수 경로가 제거됐다', () => {
  assert.doesNotMatch(source, /codexOwnPlan|claudeCritiquePlan|antigravityCritiquePlan|codexReconcile/)
})

test('Workflow 단계마다 영구 아이덴티티와 persona 계약이 주입된다', () => {
  position("const AGENT_PROFILE_VERSION = '1.0.0'")
  position("const COMMON_RESPONSE_STYLE =")
  position("FullResearch: 'antigravity.researcher|antigravity.red-team'")
  position("FullPlan: 'codex.architect'")
  position("FullExecute: 'codex.implementer'")
  position("FullSelfCheck: 'codex.test-engineer'")
  position("FullCodeReviewSkill: 'claude.communicator|antigravity.auditor'")
  position("workflowProfile('antigravity', persona)")
  position("workflowProfile('codex', 'implementer')")
  position("buildReviewPrompt(task, context, realDiff, testSummary, plan, 'antigravity', 'auditor')")
  position("style: 'plain-high-school-v1'")
})

test('Workflow profile snapshot은 기준 계약 파일과 동기화되어 있다', () => {
  const contract = fs.readFileSync(path.resolve(__dirname, '../config/agent-profile-contract.json'))
  const digest = crypto.createHash('sha256').update(contract).digest('hex')
  assert.match(source, new RegExp(`AGENT_PROFILE_CONTRACT_SHA256 = '${digest}'`))
})

const canRunMacSandbox = (() => {
  if (process.platform !== 'darwin' || !fs.existsSync('/usr/bin/sandbox-exec')) return false
  const smoke = childProcess.spawnSync(
    '/usr/bin/sandbox-exec',
    ['-f', path.resolve(__dirname, '../config/code-review-read-only.sb'), '--', '/usr/bin/true'],
    { encoding: 'utf8' }
  )
  return smoke.status === 0
})()

function writeFakeAgy(tempDir) {
  const fakeAgy = path.join(tempDir, 'fake-agy')
  fs.writeFileSync(fakeAgy, `#!/bin/sh
set -u
printf '%s' 'allowed' > "$AGY_LOG_ROOT/log/provider-write.txt"
if printf '%s' 'forbidden' > "$FORBIDDEN_PATH" 2>"$AGY_LOG_ROOT/log/forbidden.stderr"; then
  printf '%s' 'unexpected-success' > "$AGY_LOG_ROOT/log/forbidden-result.txt"
else
  printf '%s' 'denied' > "$AGY_LOG_ROOT/log/forbidden-result.txt"
fi
printf '%s\\n' '{"hasBlockingIssue":false,"issues":[],"notes":"sandbox probe","checks":[{"name":"sandbox-write","status":"passed","evidence":"probe"}]}'
`)
  fs.chmodSync(fakeAgy, 0o755)
  return fakeAgy
}

function runAgySandboxScenario(useOverride) {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'verify-task-agy-dynamic-profile-'))
  try {
    const homeDir = path.join(tempDir, 'home')
    const logRoot = useOverride
      ? path.join(tempDir, 'override-root')
      : path.join(homeDir, '.gemini', 'antigravity-cli')
    const profileTempDir = path.join(tempDir, 'profile-tmp')
    const promptFile = path.join(tempDir, 'prompt.txt')
    const forbiddenPath = useOverride
      ? path.join(tempDir, 'override-sibling.txt')
      : path.join(homeDir, '.gemini', 'antigravity-sibling.txt')
    fs.mkdirSync(path.join(logRoot, 'log'), { recursive: true })
    fs.mkdirSync(path.join(logRoot, 'crashes'), { recursive: true })
    fs.mkdirSync(profileTempDir, { recursive: true })
    fs.writeFileSync(promptFile, 'sandbox probe\n')
    const fakeAgy = writeFakeAgy(tempDir)
    const env = {
      ...process.env,
      AGY_BIN: fakeAgy,
      FORBIDDEN_PATH: forbiddenPath,
      HOME: homeDir,
      TMPDIR: profileTempDir,
    }
    if (useOverride) {
      env.AGY_LOG_ROOT = logRoot
    } else {
      delete env.AGY_LOG_ROOT
    }
    delete env.EDGE_AGENT_REVIEW_PROFILE

    const result = childProcess.spawnSync(
      'bash',
      [path.resolve(__dirname, '../workflows/lib/score-dispatch.sh'), 'agy', promptFile, 'review'],
      { encoding: 'utf8', env }
    )
    assert.equal(result.error, undefined, result.error && result.error.message)
    assert.equal(result.status, 0, `${result.stderr}\n${result.stdout}`)
    const envelope = JSON.parse(result.stdout)
    assert.notEqual(envelope.dispatchFailed, true, result.stdout)
    assert.equal(envelope.checks[0].status, 'passed')
    assert.equal(fs.readFileSync(path.join(logRoot, 'log', 'provider-write.txt'), 'utf8'), 'allowed')
    assert.equal(fs.readFileSync(path.join(logRoot, 'log', 'forbidden-result.txt'), 'utf8'), 'denied')
    assert.equal(fs.existsSync(forbiddenPath), false)
    assert.deepEqual(
      fs.readdirSync(profileTempDir).filter((entry) => entry.startsWith('edge-agent-review-profile.')),
      []
    )
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true })
  }
}

test('AGY_LOG_ROOT override는 동적 로그 허용과 저장소 밖 쓰기 거부를 함께 적용한다', {
  skip: !canRunMacSandbox,
}, () => {
  runAgySandboxScenario(true)
})

test('AGY_LOG_ROOT가 없으면 HOME fallback 경로에 동적 로그 허용을 적용한다', {
  skip: !canRunMacSandbox,
}, () => {
  runAgySandboxScenario(false)
})

test('동적 review 프로필 생성 실패는 provider 실행 전에 구조화된 실패로 반환된다', () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'verify-task-agy-profile-failure-'))
  try {
    const logRoot = path.join(tempDir, 'logs')
    const profileTempDir = path.join(tempDir, 'profile-tmp')
    const promptFile = path.join(tempDir, 'prompt.txt')
    const fakeAgy = path.join(tempDir, 'fake-agy')
    fs.mkdirSync(path.join(logRoot, 'log'), { recursive: true })
    fs.mkdirSync(path.join(logRoot, 'crashes'), { recursive: true })
    fs.mkdirSync(profileTempDir, { recursive: true })
    fs.writeFileSync(promptFile, 'profile failure probe\n')
    fs.writeFileSync(fakeAgy, '#!/bin/sh\nprintf x > "$AGY_LOG_ROOT/log/provider-ran.txt"\n')
    fs.chmodSync(fakeAgy, 0o755)
    const result = childProcess.spawnSync(
      'bash',
      [path.resolve(__dirname, '../workflows/lib/score-dispatch.sh'), 'agy', promptFile, 'review'],
      {
        encoding: 'utf8',
        env: {
          ...process.env,
          AGY_BIN: fakeAgy,
          AGY_LOG_ROOT: logRoot,
          EDGE_AGENT_REVIEW_PROFILE: path.join(tempDir, 'missing-profile.sb'),
          HOME: path.join(tempDir, 'home'),
          TMPDIR: profileTempDir,
        },
      }
    )
    assert.equal(result.status, 0, `${result.stderr}\n${result.stdout}`)
    const envelope = JSON.parse(result.stdout)
    assert.equal(envelope.dispatchFailed, true)
    assert.equal(envelope.checks[0].status, 'error')
    assert.match(envelope.dispatchFailureReason, /프로필/)
    assert.equal(fs.existsSync(path.join(logRoot, 'log', 'provider-ran.txt')), false)
    assert.deepEqual(fs.readdirSync(profileTempDir), [])
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true })
  }
})

test('Antigravity 로그 루트가 저장소 내부이거나 심볼릭 링크면 fail-closed 한다', () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'verify-task-agy-path-rejection-'))
  try {
    const promptFile = path.join(tempDir, 'prompt.txt')
    const targetDir = path.join(tempDir, 'target')
    const symlinkRoot = path.join(tempDir, 'symlink-root')
    fs.mkdirSync(targetDir)
    fs.writeFileSync(promptFile, 'path rejection probe\n')
    fs.symlinkSync(targetDir, symlinkRoot, 'dir')

    for (const logRoot of [path.resolve(__dirname, '..'), symlinkRoot]) {
      const result = childProcess.spawnSync(
        'bash',
        [path.resolve(__dirname, '../workflows/lib/score-dispatch.sh'), 'agy', promptFile, 'review'],
        {
          encoding: 'utf8',
          env: {
            ...process.env,
            AGY_BIN: '/usr/bin/true',
            AGY_LOG_ROOT: logRoot,
            HOME: path.join(tempDir, 'home'),
          },
        }
      )
      assert.equal(result.status, 0, `${result.stderr}\n${result.stdout}`)
      const envelope = JSON.parse(result.stdout)
      assert.equal(envelope.dispatchFailed, true)
      assert.equal(envelope.checks[0].status, 'error')
      assert.match(envelope.dispatchFailureReason, /절대 경로|제어 문자|심볼릭 링크|저장소 내부/)
    }
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true })
  }
})
