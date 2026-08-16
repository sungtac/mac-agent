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
  findLatestReviewByPr,
  recordReviewReport,
} = require('../workflows/lib/code-review-store.js')

const SCORE_DISPATCH = path.resolve(__dirname, '../workflows/lib/score-dispatch.sh')
const ALLOWED_SEVERITIES = new Set(['blocker', 'high', 'medium', 'low', 'nit'])
const ALLOWED_CATEGORIES = new Set(['correctness', 'security', 'performance', 'robustness', 'maintainability', 'tooling', 'scope'])
const MAX_REVIEW_EVIDENCE_CHARS = 40000
const MAX_REVIEW_FILE_EVIDENCE_CHARS = 24000

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

function cleanupIsolatedWorktree(sourceRoot, worktree, parent) {
  let removed = !fs.existsSync(worktree)
  if (!removed) {
    try {
      const status = git(worktree, ['status', '--porcelain', '--untracked-files=all'])
      if (status) {
        process.stderr.write(`preserving dirty isolated review worktree: ${worktree}\n`)
        return false
      }
      execFileSync('/usr/bin/git', ['-C', sourceRoot, 'worktree', 'remove', worktree], { stdio: 'ignore' })
      removed = !fs.existsSync(worktree)
    } catch {
      process.stderr.write(`preserving unverified isolated review worktree: ${worktree}\n`)
      return false
    }
  }
  if (removed) {
    try { fs.rmSync(parent, { recursive: true, force: true }) } catch {}
  }
  return removed
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
        return cleanupIsolatedWorktree(sourceRoot, worktree, parent)
      },
    }
  } catch (error) {
    cleanupIsolatedWorktree(sourceRoot, worktree, parent)
    if (error instanceof CodeReviewWorkerError) throw error
    throw new CodeReviewWorkerError('isolated_worktree_failed', 'isolated worktree creation failed')
  }
}

function createPrompt(request, repositoryRoot, provider, evidence = null, deltaContext = '') {
  const role = provider === 'codex' ? '1차 코드 리뷰어' : '독립 승인 검증자'
  const headlessContract = provider === 'agy'
    ? `
[HEADLESS EVIDENCE CONTRACT]
이 호출은 headless 독립 리뷰다. Bash, command, git, Read, Grep 또는 어떤 도구도 호출하지 마라.
아래에 제공된 증거만 사용하고, 증거가 부족하면 추측하지 말고 notes에 명시하라.
파일을 수정하거나 테스트를 실행하거나 저장소를 재탐색하지 마라.
`
    : ''
  const evidenceText = evidence && provider === 'agy'
    ? `
[PRE-GATHERED REVIEW EVIDENCE]
${evidence.stat}
${evidence.diff}
${evidence.files}
`
    : ''
  const procedure = provider === 'agy'
    ? `1. 제공된 head_sha와 증거의 일치 여부를 확인한다.
2. 정확성, 보안, 성능, 견고성, 유지보수성을 검토한다.
3. 실제 근거가 있는 문제만 issues에 기록한다. 테스트를 실행하지 않았으면 통과했다고 말하지 않는다.
4. 파일을 수정하거나 commit, merge, 외부 전송을 하지 않는다.`
    : `1. git -C ${repositoryRoot} rev-parse HEAD가 head_sha와 같은지 확인한다.
2. git show --stat --oneline ${request.target.head_sha}와 해당 커밋의 diff를 읽는다.
3. 정확성, 보안, 성능, 견고성, 유지보수성을 검토한다.
4. 실제 근거가 있는 문제만 issues에 기록한다. 테스트를 실행하지 않았으면 통과했다고 말하지 않는다.
5. 파일을 수정하거나 commit, merge, 외부 전송을 하지 않는다.`
  const deltaText = deltaContext ? `
[DELTA TRACKING 참고자료]
이전 라운드 참고자료이며 신규 이슈를 억제하지 않는다.
${deltaContext}
` : ''
  return `너는 ${role}다. 실제 파일을 수정하지 말고 읽기 전용으로만 검토해.
${headlessContract}

[검토 대상]
- repository: ${request.target.repository}
- pull request: #${request.target.pull_request}
- head_sha: ${request.target.head_sha}
- local repository root: ${repositoryRoot}

[검토 절차]
${procedure}

${evidenceText}
${deltaText}

응답은 반드시 JSON 객체 하나만 반환한다:
{
  "hasBlockingIssue": boolean,
  "issues": [{"description": "...", "severity": "blocker|high|medium|low|nit", "category": "correctness|security|performance|robustness|maintainability|tooling|scope", "location": "file:line 또는 commit 범위", "evidence": "재현·검증 근거", "remediation": "수정 방향", "blocking": boolean}],
  "notes": "검토 범위와 실행하지 못한 검사의 사실"
}`
}

function buildReviewEvidence(repositoryRoot, headSha) {
  const stat = git(repositoryRoot, ['show', '--stat', '--oneline', '--no-renames', headSha])
  let diff = git(repositoryRoot, ['show', '--format=fuller', '--binary', '--no-ext-diff', '--no-renames', headSha])
  if (diff.length > MAX_REVIEW_EVIDENCE_CHARS) diff = diff.slice(0, MAX_REVIEW_EVIDENCE_CHARS) + '\n...(diff truncated by host)'
  const names = git(repositoryRoot, ['diff-tree', '--no-commit-id', '--name-only', '-r', '--root', headSha])
    .split('\n').map((value) => value.trim()).filter(Boolean)
  let remainingFiles = MAX_REVIEW_FILE_EVIDENCE_CHARS
  const files = names.map((name) => {
    if (remainingFiles <= 0) return `--- ${name} ---\n(omitted: host evidence budget exhausted)`
    if (/(^|\/)(\.env|secrets?|credentials?|.*\.(pem|key))($|\/)/i.test(name)) {
      return `--- ${name} ---\n(omitted by host policy)`
    }
    try {
      const content = git(repositoryRoot, ['show', `${headSha}:${name}`]).slice(0, Math.min(12000, remainingFiles))
      remainingFiles -= content.length
      return `--- ${name} ---\n${content}`
    } catch {
      return `--- ${name} ---\n(unavailable in target commit)`
    }
  }).join('\n\n')
  return { stat, diff, files }
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

function buildReport(request, target, codex, antigravity, delta = {}) {
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
      repository: request.target.repository,
      pull_request: request.target.pull_request,
      paths: [],
    },
    round: delta.round || 1,
    parent_report_key: delta.parent_report_key || null,
    pr_number: request.target.pull_request,
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

function formatDeltaSummaryComment(report, previous) {
  const statusBadge = report.status === 'AI_APPROVED' ? '✅ AI_APPROVED' : '❌ CHANGES_REQUIRED'
  const titleFor = (finding) => finding.title || finding.description || finding.id || '제목 없는 이슈'
  const formatList = (title, findings) => {
    const items = findings.map((finding) => `- ${titleFor(finding)}`).join('\n') || '- 없음'
    if (findings.length <= 5) return `### ${title}\n${items}`
    return `### ${title}\n<details><summary>${title} (${findings.length}개)</summary>\n\n${items}\n\n</details>`
  }

  const lines = [
    '<!-- mac-agent-code-review-summary -->',
    `## Round ${report.round} 리뷰 요약`,
    `판정: ${statusBadge}`,
  ]

  if (!previous) {
    lines.push('', `이번 라운드 findings: ${(report.findings || []).length}개`, formatList('이번 라운드 이슈', report.findings || []))
    return lines.join('\n')
  }

  const previousFindings = previous.findings || []
  const currentFindings = report.findings || []
  const currentIds = new Set(currentFindings.map((finding) => finding.id))
  const previousIds = new Set(previousFindings.map((finding) => finding.id))
  const fixed = previousFindings.filter((finding) => !currentIds.has(finding.id))
  const open = previousFindings.filter((finding) => currentIds.has(finding.id))
  const added = currentFindings.filter((finding) => !previousIds.has(finding.id))

  lines.push(
    '',
    `✅ Fixed: ${fixed.length}개`,
    `⚠️ Remaining Open: ${open.length}개`,
    `🆕 New: ${added.length}개`,
    '',
    formatList('해결된 이슈', fixed),
    '',
    formatList('새로 생긴 이슈', added),
    '',
    formatList('여전히 open인 이슈', open),
  )
  return lines.join('\n')
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

  const previous = findLatestReviewByPr(request.target.pull_request, request.target.repository, stateRoot)
  const reviewEvidence = buildReviewEvidence(target.root, target.head)
  const deltaContext = previous ? JSON.stringify({ round: previous.round || 1, findings: (previous.findings || []).map((finding) => ({ id: finding.id, location: finding.location, title: finding.title, status: reviewEvidence.diff.includes(finding.location) ? 'open' : 'fixed' })) }, null, 2) : ''

  const prompts = {
    codex: writePrompt(createPrompt(request, target.root, 'codex', null, deltaContext)),
    agy: writePrompt(createPrompt(request, target.root, 'agy', reviewEvidence, deltaContext)),
  }
  try {
    const [codexRaw, antigravityRaw] = await Promise.all([
      dispatchProvider('codex', prompts.codex.filePath, target.root, options),
      dispatchProvider('agy', prompts.agy.filePath, target.root, options),
    ])
    const codex = parseProviderResult(codexRaw, 'codex')
    const antigravity = parseProviderResult(antigravityRaw, 'antigravity')
    const report = buildReport(request, target, codex, antigravity, { round: previous ? (previous.round || 1) + 1 : 1, parent_report_key: previous ? `${previous.review_id}::${previous.target.head_sha}` : null })
    const persisted = recordReviewReport(report, stateRoot)
    if (request.target.pull_request && request.target.repository) {
      let commentDirectory
      try {
        commentDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'code-review-comment-'))
        const commentFile = path.join(commentDirectory, 'comment.md')
        fs.writeFileSync(commentFile, formatDeltaSummaryComment(report, previous), { encoding: 'utf8', mode: 0o600 })
        execFileSync('gh', ['pr', 'comment', String(request.target.pull_request), '--repo', request.target.repository, '--body-file', commentFile, '--edit-last', '--create-if-none'], { stdio: 'ignore' })
      } catch (error) {
        console.warn('failed to publish code review summary comment:', error.message)
      } finally {
        if (commentDirectory) {
          try { fs.rmSync(commentDirectory, { recursive: true, force: true }) } catch {}
        }
      }
    }
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
  formatDeltaSummaryComment,
  createPrompt,
  buildReviewEvidence,
  main,
  parseProviderResult,
  prepareIsolatedWorktree,
  runWorkerOnce,
  verifyTarget,
}
