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

test('full 트랙은 프롬프트화→조사→계획→계획검토→수정→실행→코드리뷰 순서다', () => {
  const ordered = [
    "{ title: 'FullPromptify'",
    "{ title: 'FullResearch'",
    "{ title: 'FullPlan'",
    "{ title: 'FullPlanReview'",
    "{ title: 'FullPlanRevise'",
    "{ title: 'FullExecute'",
    "{ title: 'FullCodeReviewSkill'",
  ].map(position)

  for (let index = 1; index < ordered.length; index += 1) {
    assert.ok(ordered[index - 1] < ordered[index], `${ordered[index - 1]} must precede ${ordered[index]}`)
  }
})

test('Antigravity 조사와 Codex 계획 검토는 병렬 호출이고 실패 결과를 빈 결과로 대체하지 않는다', () => {
  position('RESEARCH_FOCI.map((focus) =>')
  position('const planReviews = await parallel([')
  position('const failedResearch = researchResults')
  position('const failedPlanReviews = planReviews.filter')
  position("error: 'research_failed'")
  position("error: 'plan_review_failed'")
})

test('Codex 최종 계획 수정 후 실행 성공을 확인하고 code-review 스킬로 넘어간다', () => {
  const revise = position('const reconciled = await dispatchWithRetry(')
  const execute = position('const execution = await fullExecute(')
  const review = position("log(`[전체] ${round}라운드: code-review 스킬 발동")
  assert.ok(revise < execute)
  assert.ok(execute < review)
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
  position("FullPromptify: 'claude.planner'")
  position("FullExecute: 'codex.implementer'")
  position("FullCodeReviewSkill: 'codex.code-reviewer|antigravity.auditor'")
  position("workflowProfile('claude', 'planner')")
  position("workflowProfile('antigravity', persona)")
  position("workflowProfile('codex', 'plan-reviewer')")
  position("workflowProfile('codex', 'implementer')")
  position("buildReviewPrompt(task, context, realDiff, 'antigravity', 'auditor')")
  position("style: 'plain-high-school-v1'")
})

test('Workflow profile snapshot은 기준 계약 파일과 동기화되어 있다', () => {
  const contract = fs.readFileSync(path.resolve(__dirname, '../config/agent-profile-contract.json'))
  const digest = crypto.createHash('sha256').update(contract).digest('hex')
  assert.match(source, new RegExp(`AGENT_PROFILE_CONTRACT_SHA256 = '${digest}'`))
})
