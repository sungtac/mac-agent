#!/usr/bin/env node

// Immutable, local code-review report store.
// Reports are keyed by review_id + target.head_sha. New SHAs get new reports.

const crypto = require('node:crypto')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const SCHEMA_VERSION = 'edge_agent.code_review_report.v1'
const FINDING_SEVERITIES = new Set(['blocker', 'high', 'medium', 'low', 'nit'])
const FINDING_CATEGORIES = new Set([
  'correctness',
  'security',
  'performance',
  'robustness',
  'maintainability',
  'tooling',
  'scope',
])
const FINDING_STATUSES = new Set(['open', 'fixed', 'regression', 'deferred', 'superseded'])
const REQUIRED_FINDING_FIELDS = [
  'id',
  'severity',
  'category',
  'location',
  'title',
  'evidence',
  'remediation',
]
const DEFAULT_STATE_ROOT = process.env.EDGE_AGENT_CODE_REVIEW_STATE_ROOT ||
  (process.env.EDGE_AGENT_STATE_ROOT && path.join(process.env.EDGE_AGENT_STATE_ROOT, 'code-review')) ||
  (process.env.EDGE_AGENT_RUNTIME_ROOT && path.join(process.env.EDGE_AGENT_RUNTIME_ROOT, 'state', 'code-review')) ||
  path.join(os.homedir(), '.edge-agent', 'state', 'code-review')

class CodeReviewStoreError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'CodeReviewStoreError'
    this.code = code
  }
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (isPlainObject(value)) {
    return Object.keys(value).sort().reduce((result, key) => {
      result[key] = canonicalize(value[key])
      return result
    }, {})
  }
  return value
}

function canonicalJson(value) {
  return JSON.stringify(canonicalize(value))
}

function assertText(value, field) {
  if (typeof value !== 'string' || value.trim() === '' || /[\r\n]/.test(value)) {
    throw new CodeReviewStoreError('invalid_report', field + ' must be a non-empty single-line string')
  }
}

function assertNonEmptyText(value, field) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new CodeReviewStoreError('invalid_report', field + ' must be a non-empty string')
  }
}

function reportKey(report) {
  return crypto.createHash('sha256')
    .update(report.review_id + '::' + report.target.head_sha)
    .digest('hex')
}

function reviewIndexKey(reviewId) {
  return crypto.createHash('sha256').update(reviewId).digest('hex')
}

function validateReport(report) {
  if (!isPlainObject(report)) throw new CodeReviewStoreError('invalid_report', 'report must be an object')
  if (report.schema_version !== SCHEMA_VERSION) {
    throw new CodeReviewStoreError('invalid_report', 'schema_version must be ' + SCHEMA_VERSION)
  }
  assertText(report.review_id, 'review_id')
  if (report.round !== undefined && (!Number.isInteger(report.round) || report.round < 1)) throw new CodeReviewStoreError('invalid_report', 'round must be a positive integer')
  if (report.parent_report_key !== undefined && report.parent_report_key !== null) assertNonEmptyText(report.parent_report_key, 'parent_report_key')
  if (report.pr_number !== undefined && report.pr_number !== null && (!Number.isInteger(report.pr_number) || report.pr_number < 1)) throw new CodeReviewStoreError('invalid_report', 'pr_number must be a positive integer or null')
  if (!['REVIEWED', 'AI_APPROVED', 'CHANGES_REQUIRED', 'ESCALATED', 'SUPERSEDED'].includes(report.status)) {
    throw new CodeReviewStoreError('invalid_report', 'status is invalid')
  }
  if (!isPlainObject(report.target)) throw new CodeReviewStoreError('invalid_report', 'target must be an object')
  if (!['diff', 'files', 'module', 'repo', 'snippet'].includes(report.target.scope)) {
    throw new CodeReviewStoreError('invalid_report', 'target.scope is invalid')
  }
  assertText(report.target.head_sha, 'target.head_sha')
  if (!Array.isArray(report.findings) || !Array.isArray(report.checks)) {
    throw new CodeReviewStoreError('invalid_report', 'findings and checks must be arrays')
  }
  for (const [index, finding] of report.findings.entries()) {
    if (!isPlainObject(finding)) {
      throw new CodeReviewStoreError('invalid_report', `findings[${index}] must be an object`)
    }
    for (const field of REQUIRED_FINDING_FIELDS) {
      assertNonEmptyText(finding[field], `findings[${index}].${field}`)
    }
    if (!FINDING_SEVERITIES.has(finding.severity)) {
      throw new CodeReviewStoreError('invalid_report', `findings[${index}].severity is invalid`)
    }
    if (!FINDING_CATEGORIES.has(finding.category)) {
      throw new CodeReviewStoreError('invalid_report', `findings[${index}].category is invalid`)
    }
    if (finding.status !== undefined && !FINDING_STATUSES.has(finding.status)) throw new CodeReviewStoreError('invalid_report', `findings[${index}].status is invalid`)
  }
  for (const [index, check] of report.checks.entries()) {
    if (!isPlainObject(check) || typeof check.name !== 'string' || !check.name) {
      throw new CodeReviewStoreError('invalid_report', 'checks[' + index + '] is invalid')
    }
    if (!['passed', 'failed', 'not_run', 'error'].includes(check.status)) {
      throw new CodeReviewStoreError('invalid_report', 'checks[' + index + '].status is invalid')
    }
  }
  if (report.status === 'AI_APPROVED') {
    if (!isPlainObject(report.approval)) {
      throw new CodeReviewStoreError('invalid_report', 'AI_APPROVED requires approval')
    }
    if (report.approval.reviewed_head_sha !== report.target.head_sha) {
      throw new CodeReviewStoreError('invalid_report', 'approval SHA does not match target SHA')
    }
    if (!report.approval.provider || !report.approval.decision_reason) {
      throw new CodeReviewStoreError('invalid_report', 'approval provider and reason are required')
    }
    if (report.checks.length === 0 || report.checks.some((check) => check.status !== 'passed')) {
      throw new CodeReviewStoreError('invalid_report', 'AI_APPROVED requires passed checks')
    }
    if (report.findings.some((finding) => finding && finding.severity === 'blocker')) {
      throw new CodeReviewStoreError('invalid_report', 'AI_APPROVED cannot contain blocker findings')
    }
  }
  return report
}

function pathsFor(stateRoot) {
  const root = path.resolve(stateRoot || DEFAULT_STATE_ROOT)
  return { root, reports: path.join(root, 'reports'), latest: path.join(root, 'latest') }
}

function writeAtomic(filePath, value) {
  const directory = path.dirname(filePath)
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 })
  const temporary = path.join(directory, '.tmp-' + process.pid + '-' + crypto.randomBytes(6).toString('hex'))
  const descriptor = fs.openSync(temporary, 'wx', 0o600)
  try {
    fs.writeFileSync(descriptor, JSON.stringify(value, null, 2) + '\n', 'utf8')
    fs.fsyncSync(descriptor)
  } finally {
    fs.closeSync(descriptor)
  }
  try {
    fs.renameSync(temporary, filePath)
  } catch (error) {
    try { fs.unlinkSync(temporary) } catch {}
    throw error
  }
}

function readJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'))
  } catch (error) {
    if (error.code === 'ENOENT') return null
    throw new CodeReviewStoreError('corrupt_store', 'invalid JSON at ' + filePath)
  }
}

function recordReviewReport(report, stateRoot = DEFAULT_STATE_ROOT) {
  validateReport(report)
  const paths = pathsFor(stateRoot)
  const reportPath = path.join(paths.reports, reportKey(report) + '.json')
  const existing = readJson(reportPath)
  if (existing) {
    if (canonicalJson(existing) !== canonicalJson(report)) {
      throw new CodeReviewStoreError('idempotency_conflict', 'report key contains different content: ' + report.review_id)
    }
    return { ok: true, outcome: 'duplicate', reportPath, reviewId: report.review_id, headSha: report.target.head_sha }
  }
  writeAtomic(reportPath, report)
  writeAtomic(path.join(paths.latest, reviewIndexKey(report.review_id) + '.json'), {
    review_id: report.review_id,
    head_sha: report.target.head_sha,
    report_file: path.basename(reportPath),
  })
  return { ok: true, outcome: 'appended', reportPath, reviewId: report.review_id, headSha: report.target.head_sha }
}

function findLatestReview(reviewId, stateRoot = DEFAULT_STATE_ROOT) {
  assertText(reviewId, 'review_id')
  const paths = pathsFor(stateRoot)
  const index = readJson(path.join(paths.latest, reviewIndexKey(reviewId) + '.json'))
  if (!index) return null
  const report = readJson(path.join(paths.reports, index.report_file))
  if (!report) throw new CodeReviewStoreError('corrupt_store', 'latest index points to missing report')
  validateReport(report)
  return report
}

function findLatestReviewByPr(prNumber, repository, stateRoot = DEFAULT_STATE_ROOT) {
  if (!Number.isInteger(prNumber) || prNumber < 1) throw new CodeReviewStoreError('invalid_query', 'pr_number must be a positive integer')
  const paths = pathsFor(stateRoot)
  if (!fs.existsSync(paths.reports)) return null
  let latest = null
  for (const filename of fs.readdirSync(paths.reports).filter((value) => value.endsWith('.json'))) {
    const report = readJson(path.join(paths.reports, filename))
    if (!report) continue
    validateReport(report)
    if (report.pr_number !== prNumber || (repository && report.target.repository !== repository)) continue
    if (!latest || (report.round || 1) > (latest.round || 1)) latest = report
  }
  return latest
}

function main() {
  const mode = process.argv[2]
  const stateRoot = process.env.EDGE_AGENT_CODE_REVIEW_STATE_ROOT || process.env.EDGE_AGENT_STATE_ROOT || DEFAULT_STATE_ROOT
  if (mode === '--record') {
    const report = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'))
    process.stdout.write(JSON.stringify(recordReviewReport(report, stateRoot)) + '\n')
    return
  }
  if (mode === '--find') {
    process.stdout.write(JSON.stringify({ ok: true, report: findLatestReview(process.argv[3], stateRoot) }) + '\n')
    return
  }
  throw new CodeReviewStoreError('usage', 'usage: code-review-store.js --record <report.json> | --find <review_id>')
}

if (require.main === module) {
  try {
    main()
  } catch (error) {
    const code = error instanceof CodeReviewStoreError ? error.code : 'storage_error'
    process.stderr.write(JSON.stringify({ ok: false, error: code, message: error.message }) + '\n')
    process.exitCode = 1
  }
}

module.exports = {
  SCHEMA_VERSION,
  DEFAULT_STATE_ROOT,
  CodeReviewStoreError,
  validateReport,
  recordReviewReport,
  findLatestReview,
  findLatestReviewByPr,
  FINDING_STATUSES,
}
