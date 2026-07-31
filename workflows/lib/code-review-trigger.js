#!/usr/bin/env node

// Pure GitHub-style event adapter. It never calls GitHub or starts a provider.

const crypto = require('node:crypto')
const REVIEWABLE_PR_ACTIONS = new Set(['opened', 'reopened', 'ready_for_review', 'synchronize'])

function text(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : ''
}

function repositoryName(payload) {
  return text(payload?.repository?.full_name) || text(payload?.repository?.name)
}

function pullRequestNumber(payload) {
  const value = payload?.pull_request?.number ?? payload?.number
  return Number.isInteger(value) && value > 0 ? value : null
}

function pullRequestHeadSha(payload) {
  return text(payload?.pull_request?.head?.sha) ||
    text(payload?.workflow_run?.head_sha) ||
    text(payload?.check_suite?.head_sha)
}

function targetFrom(payload, eventName) {
  const repository = repositoryName(payload)
  const number = pullRequestNumber(payload) ?? payload?.workflow_run?.pull_requests?.[0]?.number ?? null
  const headSha = pullRequestHeadSha(payload)
  if (!repository || !Number.isInteger(number) || number <= 0 || !headSha) return null
  return { repository, pull_request: number, head_sha: headSha, scope: 'diff', event: eventName }
}

function idempotencyKey(target) {
  return crypto.createHash('sha256')
    .update(target.repository + '#' + target.pull_request + '@' + target.head_sha)
    .digest('hex')
}

function decision(phase, reason, target = null) {
  return {
    shouldReview: phase === 'start',
    phase,
    reason,
    target,
    idempotencyKey: target ? idempotencyKey(target) : null,
  }
}

function decideReviewTrigger(eventName, payload) {
  const event = text(eventName).toLowerCase()
  const target = targetFrom(payload, event)
  if (!target) return decision('ignore', 'repository, pull request number, or head SHA is missing')
  if (payload?.pull_request?.draft === true) return decision('ignore', 'draft pull requests are not reviewable', target)

  if (event === 'pull_request') {
    const action = text(payload.action).toLowerCase()
    if (!REVIEWABLE_PR_ACTIONS.has(action)) {
      return decision('ignore', 'pull_request action is not reviewable: ' + (action || 'missing'), target)
    }
    return decision('await_ci', 'waiting for successful CI on head SHA ' + target.head_sha, target)
  }

  if (event === 'workflow_run') {
    const action = text(payload.action).toLowerCase()
    const conclusion = text(payload.workflow_run?.conclusion).toLowerCase()
    if (action !== 'completed' || conclusion !== 'success') {
      return decision('await_ci', 'workflow run is not successful: ' + (action || 'missing') + '/' + (conclusion || 'missing'), target)
    }
    return decision('start', 'CI completed successfully on head SHA ' + target.head_sha, target)
  }

  if (event === 'check_suite') {
    const action = text(payload.action).toLowerCase()
    const conclusion = text(payload.check_suite?.conclusion).toLowerCase()
    if (action !== 'completed' || conclusion !== 'success') {
      return decision('await_ci', 'check suite is not successful: ' + (action || 'missing') + '/' + (conclusion || 'missing'), target)
    }
    return decision('start', 'check suite completed successfully on head SHA ' + target.head_sha, target)
  }

  return decision('ignore', 'unsupported event: ' + (event || 'missing'), target)
}

module.exports = { decideReviewTrigger, idempotencyKey, targetFrom }
