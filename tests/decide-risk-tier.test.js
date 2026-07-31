const test = require('node:test')
const assert = require('node:assert/strict')
const {
  decideRiskTier,
  isSensitivePath,
  validPercentage,
  lowestProviderHeadroom,
  classifyFileRisk,
  classifyChangedFiles,
  detectDependencyBoundary,
} = require('../workflows/lib/decide-risk-tier.js')

test('빈 입력/기본값은 light', () => {
  assert.equal(decideRiskTier({}), 'light')
  assert.equal(decideRiskTier(undefined), 'light')
})

test('민감 경로는 다른 신호와 무관하게 항상 full', () => {
  assert.equal(
    decideRiskTier({ sensitivePath: true, cumulativeFileCount: 0, stepFileCount: 0, remainingTokenPct: 100 }),
    'full'
  )
})

test('의존성 경계 교차는 mid (파일수가 적어도)', () => {
  assert.equal(
    decideRiskTier({ dependencyBoundaryCrossed: true, cumulativeFileCount: 1, stepFileCount: 1 }),
    'mid'
  )
})

test('누적 변경 파일이 임계값(3) 초과하면 mid', () => {
  assert.equal(decideRiskTier({ cumulativeFileCount: 3 }), 'light')
  assert.equal(decideRiskTier({ cumulativeFileCount: 4 }), 'mid')
})

test('단일 스텝이 큰 경우도 mid (누적이 아직 안 넘었어도)', () => {
  assert.equal(decideRiskTier({ stepFileCount: 3, cumulativeFileCount: 3 }), 'light')
  assert.equal(decideRiskTier({ stepFileCount: 4, cumulativeFileCount: 1 }), 'mid')
})

test('잔여 토큰이 낮으면(<=10%) mid로 상향, light를 더 낮추지는 않음', () => {
  assert.equal(decideRiskTier({ remainingTokenPct: 10 }), 'mid')
  assert.equal(decideRiskTier({ remainingTokenPct: 11 }), 'light')
  assert.equal(decideRiskTier({ remainingTokenPct: 9 }), 'mid')
})

test('remainingTokenPct 미지정(undefined)은 판정에 영향 없음', () => {
  assert.equal(decideRiskTier({ cumulativeFileCount: 1 }), 'light')
})

test('providerHeadroom은 가장 부족한 provider를 기준으로 mid로 상향', () => {
  assert.equal(decideRiskTier({ providerHeadroom: { claude: 40, codex: 10 } }), 'mid')
  assert.equal(decideRiskTier({ providerHeadroom: { claude: 40, codex: 11 } }), 'light')
  assert.equal(decideRiskTier({ providerHeadroom: { claude: 40 } }), 'light')
})

test('providerHeadroom이 있으면 호환용 단일 값보다 우선', () => {
  assert.equal(
    decideRiskTier({ remainingTokenPct: 100, providerHeadroom: { claude: 9, codex: 90 } }),
    'mid'
  )
})

test('잘못된 토큰 비율은 안전하게 무시하고 light를 낮추지 않음', () => {
  assert.equal(decideRiskTier({ remainingTokenPct: -1 }), 'light')
  assert.equal(decideRiskTier({ remainingTokenPct: 101 }), 'light')
  assert.equal(decideRiskTier({ providerHeadroom: { claude: '9', codex: NaN } }), 'light')
})

test('provider headroom 보조 함수는 유효한 0도 보존', () => {
  assert.equal(validPercentage(0), 0)
  assert.equal(validPercentage(101), undefined)
  assert.equal(lowestProviderHeadroom({ claude: 0, codex: 80 }), 0)
  assert.equal(lowestProviderHeadroom({ claude: '0' }), undefined)
})

test('sensitivePath가 다른 모든 mid 신호보다 우선(full)', () => {
  assert.equal(
    decideRiskTier({
      sensitivePath: true,
      dependencyBoundaryCrossed: true,
      cumulativeFileCount: 100,
      remainingTokenPct: 1,
    }),
    'full'
  )
})

test('isSensitivePath: .github/workflows, .env, secrets, README 등 매칭', () => {
  assert.equal(isSensitivePath('.github/workflows/ci.yml'), true)
  assert.equal(isSensitivePath('.env.production'), true)
  assert.equal(isSensitivePath('config/secrets.yml'), true)
  assert.equal(isSensitivePath('README.md'), true)
  assert.equal(isSensitivePath('src/utils/helpers.js'), false)
})

test('파일 위험도 분류: 일반 코드 mid, 테스트·문서 light, 민감·운영 파일 full', () => {
  assert.equal(classifyFileRisk('src/service.ts'), 'mid')
  assert.equal(classifyFileRisk('tests/service.test.ts'), 'light')
  assert.equal(classifyFileRisk('docs/guide.md'), 'light')
  assert.equal(classifyFileRisk('src/auth/login.py'), 'full')
  assert.equal(classifyFileRisk('config/production.yaml'), 'full')
  assert.equal(classifyFileRisk('.github/workflows/ci.yml'), 'full')
})

test('파일 목록은 가장 높은 위험도를 선택한다', () => {
  assert.deepEqual(classifyChangedFiles(['tests/a.test.ts', 'src/a.ts']), {
    tier: 'mid',
    files: [
      { file: 'tests/a.test.ts', tier: 'light' },
      { file: 'src/a.ts', tier: 'mid' },
    ],
  })
  assert.equal(classifyChangedFiles(['docs/a.md', 'config/settings.json']).tier, 'full')
  assert.equal(classifyChangedFiles([]).tier, 'light')
})

test('변경 파일 분류는 기존 수치·토큰 규칙보다 낮추지 않는다', () => {
  assert.equal(decideRiskTier({ changedFiles: ['src/a.ts'], stepFileCount: 1 }), 'mid')
  assert.equal(decideRiskTier({ changedFiles: ['config/a.yml'], remainingTokenPct: 100 }), 'full')
  assert.equal(decideRiskTier({ changedFiles: ['tests/a.test.ts'], remainingTokenPct: 100 }), 'light')
})

test('명시적 의존성 edge가 서로 다른 경계를 가로지르면 mid로 승격', () => {
  assert.equal(
    detectDependencyBoundary(['src/a.ts', 'workflows/a.js'], [['src/a.ts', 'workflows/a.js']]),
    true
  )
  assert.equal(
    decideRiskTier({
      changedFiles: ['src/a.ts', 'workflows/a.js'],
      dependencyEdges: [['src/a.ts', 'workflows/a.js']],
    }),
    'mid'
  )
})

test('manifest와 소스 코드 동시 변경은 정적 의존성 경계로 mid', () => {
  assert.equal(detectDependencyBoundary(['package.json', 'src/index.js']), true)
  assert.equal(detectDependencyBoundary(['src/a.js', 'src/b.js']), false)
})
