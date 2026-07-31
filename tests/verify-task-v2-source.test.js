const assert = require('node:assert/strict')
const fs = require('node:fs')
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

test('이전 자체계획·블라인드 비평 함수 경로가 제거됐다', () => {
  assert.doesNotMatch(source, /codexOwnPlan|claudeCritiquePlan|antigravityCritiquePlan|codexReconcile/)
})
