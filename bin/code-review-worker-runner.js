#!/usr/bin/env node

// Allowlisted repository router for the code-review request worker.
// It never derives a local path from an untrusted repository name.

const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { DEFAULT_QUEUE_ROOT, listPendingRequests } = require('../workflows/lib/code-review-request-queue.js')
const { DEFAULT_STATE_ROOT } = require('../workflows/lib/code-review-store.js')
const { runWorkerOnce } = require('./code-review-request-worker.js')

const CONFIG_SCHEMA = 'edge_agent.code_review_repositories.v1'
const DEFAULT_CONFIG = path.resolve(__dirname, '../config/code-review-repositories.json')

class CodeReviewRunnerError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'CodeReviewRunnerError'
    this.code = code
  }
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function expandPath(value) {
  if (typeof value !== 'string' || !value.trim()) throw new CodeReviewRunnerError('invalid_repository_root', 'repository_root is required')
  const home = process.env.HOME || os.homedir()
  const expanded = value.replace(/^~(?=\/|$)/, home).replaceAll('$HOME', home).replaceAll('${HOME}', home)
  const resolved = path.resolve(expanded)
  if (!path.isAbsolute(resolved)) throw new CodeReviewRunnerError('invalid_repository_root', 'repository_root must resolve to an absolute path')
  return resolved
}

function loadConfig(configPath = DEFAULT_CONFIG) {
  let config
  try {
    config = JSON.parse(fs.readFileSync(configPath, 'utf8'))
  } catch {
    throw new CodeReviewRunnerError('config_invalid', 'repository mapping config cannot be read')
  }
  if (!isPlainObject(config) || config.schema !== CONFIG_SCHEMA || !isPlainObject(config.repositories)) {
    throw new CodeReviewRunnerError('config_invalid', 'repository mapping config schema is invalid')
  }
  const repositories = {}
  for (const [name, entry] of Object.entries(config.repositories)) {
    if (!/^[^/\s]+\/[^/\s]+$/.test(name) || !isPlainObject(entry)) {
      throw new CodeReviewRunnerError('config_invalid', 'repository mapping key or entry is invalid')
    }
    repositories[name] = {
      repositoryRoot: expandPath(entry.repository_root),
      enabled: entry.enabled !== false,
      isolated: entry.isolated !== false,
    }
  }
  return { schema: config.schema, repositories }
}

async function runConfigured(options = {}, worker = runWorkerOnce) {
  const config = loadConfig(options.configPath || DEFAULT_CONFIG)
  const queueRoot = options.queueRoot || DEFAULT_QUEUE_ROOT
  const stateRoot = options.stateRoot || DEFAULT_STATE_ROOT
  const pending = listPendingRequests(queueRoot)
  const results = []
  for (const item of pending) {
    const mapping = config.repositories[item.target.repository]
    if (!mapping) {
      results.push({ review_id: item.review_id, outcome: 'unmapped_repository', repository: item.target.repository })
      continue
    }
    if (!mapping.enabled) {
      results.push({ review_id: item.review_id, outcome: 'disabled_repository', repository: item.target.repository })
      continue
    }
    try {
      const result = await worker({
        queueRoot,
        stateRoot,
        reviewId: item.review_id,
        repositoryRoot: mapping.repositoryRoot,
        repositoryName: item.target.repository,
        execute: options.execute === true,
        isolated: mapping.isolated,
      })
      results.push(result)
    } catch (error) {
      results.push({ review_id: item.review_id, outcome: 'failed', error: error.code || 'worker_failed' })
    }
  }
  const failed = results.filter((result) => ['unmapped_repository', 'disabled_repository', 'failed'].includes(result.outcome))
  return { ok: failed.length === 0, pending: pending.length, results }
}

function parseArgs(argv) {
  const args = { execute: false }
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === '--execute') args.execute = true
    else if (arg === '--config') args.configPath = argv[++index]
    else if (arg === '--queue-root') args.queueRoot = argv[++index]
    else if (arg === '--state-root') args.stateRoot = argv[++index]
    else if (arg === '--help') args.help = true
  }
  return args
}

function usage() {
  return 'usage: code-review-worker-runner.js [--config PATH] [--queue-root PATH] [--state-root PATH] [--execute]'
}

async function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv)
  if (args.help) {
    process.stdout.write(usage() + '\n')
    return 0
  }
  try {
    const result = await runConfigured(args)
    process.stdout.write(JSON.stringify(result) + '\n')
    return result.ok ? 0 : 1
  } catch (error) {
    process.stderr.write(JSON.stringify({ ok: false, error: error.code || 'runner_failed' }) + '\n')
    return 1
  }
}

if (require.main === module) main().then((code) => { process.exitCode = code })

module.exports = { CONFIG_SCHEMA, CodeReviewRunnerError, DEFAULT_CONFIG, expandPath, loadConfig, main, runConfigured }
