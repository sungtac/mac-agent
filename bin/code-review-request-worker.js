#!/usr/bin/env node

// Provider-neutral consumer for normalized code-review requests.
// Default mode is dry-run. Provider execution requires --execute and a clean
// local repository whose current HEAD exactly matches the queued SHA.

const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { execFileSync, spawn } = require('node:child_process')
const {
  DEFAULT_QUEUE_ROOT,
  completeReviewRequest,
  findReviewRequest,
  listPendingRequests,
} = require('../workflows/lib/code-review-request-queue.js')
const {
  DEFAULT_STATE_ROOT,
  recordReviewReport,
} = require('../workflows/lib/code-review-store.js')

const SCORE_DISPATCH = path.resolve(__dirname, '../workflows/lib/score-dispatch.sh')
const ALLOWED_SEVERITIES = new Set(['blocker', 'high', 'medium', 'low', 'nit'])
const ALLOWED_CATEGORIES = new Set(['correctness', 'security', 'performance', 'robustness', 'maintainability', 'tooling', 'scope'])

class CodeReviewWorkerError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'CodeReviewWorkerError'
    this.code = code
  }
}

function git(repositoryRoot, args) {
  try {
    return execFileSync('/usr/bin/git', ['-C', repositoryRoot, ...args], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    }).trim()
  } catch (error) {
    throw new CodeReviewWorkerError('git_check_failed', 'git check failed: ' + args.join(' '))
  }
}

function verifyTarget(repositoryRoot, request) {
  const root = path.resolve(repositoryRoot || '')
  if (!root || root === path.parse(root).root || !fs.existsSync(root)) {
    throw new CodeReviewWorkerError('repository_root_invalid', 'repository root is missing')
  }
  const canonicalRoot = fs.realpathSync(root)
  const top = fs.realpathSync(git(root, ['rev-parse', '--show-toplevel']))
  if (top !== canonicalRoot) throw new CodeReviewWorkerError('repository_root_invalid', 'repository root is not the git worktree root')
  const head = git(root, ['rev-parse', 'HEAD'])
  if (head !== request.target.head_sha) {
    throw new CodeReviewWorkerError('head_sha_mismatch', 'local HEAD does not match queued head_sha')
  }
  if (git(root, ['status', '--porcelain', '--untracked-files=all'])) {
    throw new CodeReviewWorkerError('worktree_dirty', 'review worker requires a clean worktree')
  }
  try {
    execFileSync('/usr/bin/git', ['-C', root, 'diff-tree', '--no-commit-id', '--check', '-r', head], {
      stdio: ['ignore', 'pipe', 'pipe'],
    })
  } catch {
    throw new CodeReviewWorkerError('diff_check_failed', 'target commit failed git diff --check')
  }
  // macOS exposes /tmp through /private/tmp. The short spelling is accepted
  // by the nested provider sandbox used by the existing dispatch scripts.
  const executionRoot = canonicalRoot.replace(/^\/private\/tmp\//, '/tmp/')
  return { root: executionRoot, head }
}

function prepareIsolatedWorktree(repositoryRoot, request) {
  const sourceRoot = path.resolve(repositoryRoot || '')
  if (!sourceRoot || !fs.existsSync(sourceRoot)) throw new CodeReviewWorkerError('repository_root_invalid', 'repository root is missing')
  const top = fs.realpathSync(git(sourceRoot, ['rev-parse', '--show-toplevel']))
  if (top !== fs.realpathSync(sourceRoot)) throw new CodeReviewWorkerError('repository_root_invalid', 'repository root is not the git worktree root')
  const remote = git(sourceRoot, ['remote', 'get-url', 'origin'])
  if (!remote) throw new CodeReviewWorkerError('repository_remote_missing', 'isolated review requires origin remote')
  const remoteMatch = remote.match(/github\.com[/:]([^/]+\/[^/]+?)(?:\.git)?$/)
  if (remoteMatch && remoteMatch[1] !== request.target.repository) {
    throw new CodeReviewWorkerError('repository_remote_mismatch', 'origin remote does not match queued repository')
  }
  try {
    execFileSync('/usr/bin/git', ['-C', sourceRoot, 'cat-file', '-e', request.target.head_sha + '^{commit}'], { stdio: 'ignore' })
  } catch {
    try {
      execFileSync('/usr/bin/git', ['-C', sourceRoot, 'fetch', '--no-tags', 'origin', request.target.head_sha], { stdio: ['ignore', 'ignore', 'ignore'] })
    } catch {
      throw new CodeReviewWorkerError('head_sha_unavailable', 'target head_sha is not available locally and fetch failed')
    }
  }
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'code-review-worktree-'))
  const worktree = path.join(parent, 'repo')
  try {
    execFileSync('/usr/bin/git', ['-C', sourceRoot, 'worktree', 'add', '--detach', worktree, request.target.head_sha], { stdio: ['ignore', 'ignore', 'ignore'] })
    const target = verifyTarget(worktree, request)
    return {
      ...target,
      cleanup() {
        try { execFileSync('/usr/bin/git', ['-C', sourceRoot, 'worktree', 'remove', '--force', worktree], { stdio: 'ignore' }) } catch {}
        try { fs.rmSync(parent, { recursive: true, force: true }) } catch {}
      },
    }
  } catch (error) {
    try { execFileSync('/usr/bin/git', ['-C', sourceRoot, 'worktree', 'remove', '--force', worktree], { stdio: 'ignore' }) } catch {}
    try { fs.rmSync(parent, { recursive: true, force: true }) } catch {}
    if (error instanceof CodeReviewWorkerError) throw error
    throw new CodeReviewWorkerError('isolated_worktree_failed', 'isolated worktree creation failed')
  }
}

function createPrompt(request, repositoryRoot, provider) {
  const role = provider === 'codex' ? '1차 코드 리뷰어' : '독립 승인 검증자'
  return `너는 ${role}다. 실제 파일을 수정하지 말고 읽기 전용으로만 검토해.

[검토 대상]
- repository: ${request.target.repository}
- pull request: #${request.target.pull_request}
- head_sha: ${request.target.head_sha}
- local repository root: ${repositoryRoot}

[필수 절차]
1. git -C ${repositoryRoot} rev-parse HEAD가 head_sha와 같은지 확인한다.
2. git show --stat --oneline ${request.target.head_sha}와 해당 커밋의 diff를 읽는다.
3. 정확성, 보안, 성능, 견고성, 유지보수성을 검토한다.
4. 실제 근거가 있는 문제만 issues에 기록한다. 테스트를 실행하지 않았으면 통과했다고 말하지 않는다.
5. 파일을 수정하거나 commit, merge, 외부 전송을 하지 않는다.

응답은 반드시 JSON 객체 하나만 반환한다:
{
  "hasBlockingIssue": boolean,
  "issues": [{"description": "...", "severity": "blocker|high|medium|low|nit", "category": "correctness|security|performance|robustness|maintainability|tooling|scope", "location": "file:line 또는 commit 범위", "evidence": "재현·검증 근거", "remediation": "수정 방향", "blocking": boolean}],
  "notes": "검토 범위와 실행하지 못한 검사의 사실"
}`
}

function writePrompt(content) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'code-review-worker-'))
  const filePath = path.join(directory, 'prompt.txt')
  fs.writeFileSync(filePath, content, { encoding: 'utf8', mode: 0o600 })
  return { directory, filePath }
}

function dispatchProvider(tool, promptFile, repositoryRoot, options = {}) {
  if (typeof options.dispatch === 'function') return Promise.resolve(options.dispatch(tool, promptFile, repositoryRoot))
  return new Promise((resolve, reject) => {
    const child = spawn('/bin/bash', [SCORE_DISPATCH, tool, promptFile, 'review'], {
      cwd: repositoryRoot,
      env: { ...process.env },
      stdio: ['ignore', 'pipe', 'ignore'],
    })
    const chunks = []
    child.stdout.on('data', (chunk) => chunks.push(chunk))
    child.on('error', reject)
    child.on('close', (code) => {
      if (code !== 0) return reject(new CodeReviewWorkerError('provider_process_failed', tool + ' dispatcher exited non-zero'))
      resolve(Buffer.concat(chunks).toString('utf8'))
    })
  })
}

function parseProviderResult(value, tool) {
  let result = value
  if (typeof value === 'string') {
    try { result = JSON.parse(value.trim()) } catch { throw new CodeReviewWorkerError('provider_output_invalid', tool + ' returned invalid JSON') }
  }
  if (!result || result.dispatchFailed === true) throw new CodeReviewWorkerError('provider_failed', tool + ' review did not complete')
  if (typeof result.hasBlockingIssue !== 'boolean' || !Array.isArray(result.issues)) {
    throw new CodeReviewWorkerError('provider_output_invalid', tool + ' review schema is invalid')
  }
  return result
}

function normalizeFinding(source, issue, index, headSha) {
  const description = typeof issue?.description === 'string' && issue.description.trim()
    ? issue.description.trim()
    : 'Provider reported a review issue without a description.'
  const severity = ALLOWED_SEVERITIES.has(issue?.severity) ? issue.severity : (issue?.blocking ? 'blocker' : 'medium')
  const category = ALLOWED_CATEGORIES.has(issue?.category) ? issue.category : 'correctness'
  return {
    id: `${source}-${index + 1}`,
    severity,
    category,
    location: typeof issue?.location === 'string' && issue.location.trim() ? issue.location : `commit:${headSha}`,
    title: description.slice(0, 180),
    evidence: typeof issue?.evidence === 'string' && issue.evidence.trim() ? issue.evidence : description,
    remediation: typeof issue?.remediation === 'string' && issue.remediation.trim() ? issue.remediation : 'Review and correct the reported behavior before merge.',
    confidence: 'medium',
    verified: false,
  }
}

function buildReport(request, target, codex, antigravity) {
  const findings = [
    ...(codex.issues || []).map((issue, index) => normalizeFinding('codex', issue, index, target.head)),
    ...(antigravity.issues || []).map((issue, index) => normalizeFinding('antigravity', issue, index, target.head)),
  ]
  const hasBlockingIssue = codex.hasBlockingIssue || antigravity.hasBlockingIssue || findings.some((finding) => finding.severity === 'blocker')
  const report = {
    schema_version: 'edge_agent.code_review_report.v1',
    review_id: request.review_id,
    status: hasBlockingIssue ? 'CHANGES_REQUIRED' : 'AI_APPROVED',
    target: {
      scope: request.target.scope,
      head_sha: target.head,
      paths: [],
    },
    findings,
    checks: [
      { name: 'target-head-sha', status: 'passed' },
      { name: 'codex-review', status: 'passed' },
      { name: 'antigravity-review', status: 'passed' },
    ],
  }
  if (!hasBlockingIssue) {
    report.approval = {
      provider: 'antigravity',
      reviewed_head_sha: target.head,
      decision_reason: 'Codex review and independent Antigravity verification completed on the same SHA.',
    }
  }
  return report
}

async function runWorkerOnce(options = {}) {
  const queueRoot = options.queueRoot || DEFAULT_QUEUE_ROOT
  const stateRoot = options.stateRoot || DEFAULT_STATE_ROOT
  const pending = listPendingRequests(queueRoot)
  if (pending.length === 0 && !options.reviewId) return { ok: true, outcome: 'empty' }
  const reviewId = options.reviewId || pending[0]?.review_id
  const selected = reviewId ? findReviewRequest(reviewId, queueRoot) : null
  if (!selected || selected.state !== 'pending') {
    throw new CodeReviewWorkerError('queue_request_missing', 'pending request disappeared before processing')
  }
  const request = selected.request
  if (!request) throw new CodeReviewWorkerError('queue_request_missing', 'pending request disappeared before processing')
  if (!options.repositoryRoot) throw new CodeReviewWorkerError('repository_root_required', 'repository root is required')
  if (options.repositoryName && options.repositoryName !== request.target.repository) {
    throw new CodeReviewWorkerError('repository_mismatch', 'configured repository name does not match queued request')
  }
  const target = options.isolated
    ? prepareIsolatedWorktree(options.repositoryRoot, request)
    : verifyTarget(options.repositoryRoot, request)
  if (!options.execute) {
    if (target.cleanup) target.cleanup()
    return { ok: true, outcome: 'dry_run', review_id: request.review_id, head_sha: target.head }
  }

  const prompts = {
    codex: writePrompt(createPrompt(request, target.root, 'codex')),
    agy: writePrompt(createPrompt(request, target.root, 'agy')),
  }
  try {
    const [codexRaw, antigravityRaw] = await Promise.all([
      dispatchProvider('codex', prompts.codex.filePath, target.root, options),
      dispatchProvider('agy', prompts.agy.filePath, target.root, options),
    ])
    const codex = parseProviderResult(codexRaw, 'codex')
    const antigravity = parseProviderResult(antigravityRaw, 'antigravity')
    const report = buildReport(request, target, codex, antigravity)
    const persisted = recordReviewReport(report, stateRoot)
    completeReviewRequest(request.review_id, {
      status: report.status,
      report_id: report.review_id,
    }, queueRoot)
    return { ok: true, outcome: 'completed', review_id: request.review_id, status: report.status, persistence: persisted.outcome }
  } finally {
    for (const prompt of Object.values(prompts)) {
      try { fs.rmSync(prompt.directory, { recursive: true, force: true }) } catch {}
    }
    if (target.cleanup) target.cleanup()
  }
}

function parseArgs(argv) {
  const args = { execute: false, once: true }
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === '--execute') args.execute = true
    else if (arg === '--once') args.once = true
    else if (arg === '--queue-root') args.queueRoot = argv[++index]
    else if (arg === '--state-root') args.stateRoot = argv[++index]
    else if (arg === '--repository-root') args.repositoryRoot = argv[++index]
    else if (arg === '--repository-name') args.repositoryName = argv[++index]
    else if (arg === '--isolated') args.isolated = true
    else if (arg === '--help') args.help = true
  }
  return args
}

function usage() {
  return 'usage: code-review-request-worker.js --repository-root PATH [--repository-name OWNER/REPO] [--execute] [--queue-root PATH] [--state-root PATH]'
}

async function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv)
  if (args.help) {
    process.stdout.write(usage() + '\n')
    return 0
  }
  if (!args.repositoryRoot) {
    process.stderr.write(JSON.stringify({ ok: false, error: 'repository_root_required', message: usage() }) + '\n')
    return 2
  }
  try {
    const result = await runWorkerOnce(args)
    process.stdout.write(JSON.stringify(result) + '\n')
    return 0
  } catch (error) {
    process.stderr.write(JSON.stringify({ ok: false, error: error.code || 'worker_failed' }) + '\n')
    return 1
  }
}

if (require.main === module) main().then((code) => { process.exitCode = code })

module.exports = {
  CodeReviewWorkerError,
  buildReport,
  createPrompt,
  main,
  parseProviderResult,
  prepareIsolatedWorktree,
  runWorkerOnce,
  verifyTarget,
}
