const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { execFileSync } = require('node:child_process')

const providerBin = path.join(__dirname, '..', 'workflows', 'lib', 'provider-bin.sh')

function resolveWithEnv(env) {
  const script = [
    `. "${providerBin}"`,
    'printf "%s\\n" "$(find_codex_bin)"',
    'printf "%s\\n" "$(find_agy_bin)"',
  ].join('\n')
  return execFileSync('/bin/bash', ['-c', script], {
    env: { ...env },
    encoding: 'utf8',
  }).trim().split('\n')
}

test('provider resolver honors explicit binary overrides without PATH', () => {
  assert.deepEqual(
    resolveWithEnv({ HOME: '/nonexistent', CODEX_BIN: '/bin/sh', AGY_BIN: '/bin/sh' }),
    ['/bin/sh', '/bin/sh'],
  )
})

test('provider resolver finds user-local binaries under the active HOME', () => {
  const tempHome = fs.mkdtempSync(path.join(os.tmpdir(), 'provider-bin-home-'))
  const localBin = path.join(tempHome, '.local', 'bin')
  fs.mkdirSync(localBin, { recursive: true })
  const codex = path.join(localBin, 'codex')
  const agy = path.join(localBin, 'agy')
  fs.symlinkSync('/bin/sh', codex)
  fs.symlinkSync('/bin/sh', agy)

  try {
    assert.deepEqual(resolveWithEnv({ HOME: tempHome, PATH: '/nonexistent' }), [codex, agy])
  } finally {
    fs.rmSync(tempHome, { recursive: true, force: true })
  }
})
