// 나노게이트 공통 위험도 판정 함수 (설계: docs/nano-gate-design.md 결정 2).
//
// 순수 함수 — LLM/에이전트 호출 없음. 절대 이 파일에 agent() 호출이나
// 비동기 I/O를 추가하지 말 것: 나노 스텝마다 매번 호출되는 게 전제라,
// 여기서 무거워지면 verify-task v1이 겪었던 "매번 무조건 풀-웨이트"
// 문제가 판정 단계로 옮겨붙을 뿐이다.
//
// 중요 — 이 파일은 지금 어디서도 import되지 않는다. 호스트 오케스트레이터는
// Python 하네스의 결정론적 판정을 사용한다. 이 파일은
// 스펙을 고정하고 단위테스트로 검증하기 위한 기준 구현이고, 실제 나노게이트
// 루프(구현계획 3단계)에 통합할 때는 이 함수 본문을 grep 가능한 형태로
// 그대로 복사해 넣어야 한다(기존 decideTier()와 동일한 패턴). 이후 이 파일을
// 고치면 인라인 사본도 반드시 같이 고칠 것 — 자동 동기화 없음.
//
// 민감 경로 패턴은 호스트 하네스의 민감 경로 규칙과 동일하게
// 유지한다(의도적 복제, 두 곳 다 손대야 함).
const SENSITIVE_PATH_PATTERNS = [
  /(^|\/)\.github\/workflows\//,
  /(^|\/)\.env(\.|$)/,
  /(^|\/)(secrets?|credentials?)(\/|\.|$)/i,
  /(^|\/)settings\.json$/,
  /(^|\/)permissions?\.json$/i,
  /(^|\/)security\//i,
  /(^|\/)README\.md$/,
]

const FULL_RISK_FILE_PATTERNS = [
  /(^|\/)(auth|authentication|authorization|routing|router|security|permissions?)(\/|\.|$)/i,
  /(^|\/)(config|configuration|deploy|deployment|infra|infrastructure|migrations?)(\/|\.|$)/i,
  /(^|\/)(Dockerfile|docker-compose(?:\..+)?|Makefile|package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$/i,
  /(^|\/)(package\.json|pyproject\.toml|Cargo\.toml|go\.mod|requirements\.txt)$/i,
]

const MID_RISK_CODE_EXTENSIONS = new Set([
  '.c', '.cc', '.cpp', '.cs', '.go', '.java', '.js', '.jsx', '.mjs', '.py',
  '.rb', '.rs', '.sh', '.swift', '.ts', '.tsx', '.vue',
])

const LIGHT_RISK_FILE_PATTERNS = [
  /(^|\/)(test|tests|__tests__|fixtures?|snapshots?)(\/|\.|$)/i,
  /\.(md|mdx|txt|rst|adoc)$/i,
]

function isSensitivePath(path) {
  return SENSITIVE_PATH_PATTERNS.some((re) => re.test(path))
}

function classifyFileRisk(filePath) {
  const normalized = String(filePath || '').replaceAll('\\', '/')
  if (!normalized) return 'light'
  if (isSensitivePath(normalized) || FULL_RISK_FILE_PATTERNS.some((re) => re.test(normalized))) return 'full'
  if (LIGHT_RISK_FILE_PATTERNS.some((re) => re.test(normalized))) return 'light'
  const extension = normalized.includes('.') ? `.${normalized.split('.').pop().toLowerCase()}` : ''
  if (MID_RISK_CODE_EXTENSIONS.has(extension)) return 'mid'
  return 'light'
}

function classifyChangedFiles(files) {
  const normalizedFiles = Array.isArray(files) ? files.filter((file) => typeof file === 'string' && file.length > 0) : []
  const classifications = normalizedFiles.map((file) => ({ file, tier: classifyFileRisk(file) }))
  const tier = classifications.some((item) => item.tier === 'full')
    ? 'full'
    : classifications.some((item) => item.tier === 'mid')
      ? 'mid'
      : 'light'
  return { tier, files: classifications }
}

function topLevelPath(filePath) {
  return String(filePath || '').replaceAll('\\', '/').split('/')[0] || ''
}

function detectDependencyBoundary(changedFiles, dependencyEdges = []) {
  const files = new Set(Array.isArray(changedFiles) ? changedFiles : [])
  if (!files.size) return false

  // Caller-provided static import edges are authoritative when available.
  if (Array.isArray(dependencyEdges) && dependencyEdges.some((edge) => {
    if (!Array.isArray(edge) || edge.length < 2) return false
    return files.has(edge[0]) && files.has(edge[1]) && topLevelPath(edge[0]) !== topLevelPath(edge[1])
  })) return true

  const hasManifest = [...files].some((file) => /(^|\/)(package\.json|pyproject\.toml|Cargo\.toml|go\.mod|requirements\.txt)$/.test(file))
  const hasSource = [...files].some((file) => classifyFileRisk(file) === 'mid')
  return hasManifest && hasSource
}

// 누적 파일 수 임계값 3은 기존 검증 게이트의 decideTier()(파일수≤3 →
// light) 임계값을 그대로 재사용한 것 — docs/nano-gate-design.md 결정 2가
// 요구한 "새 기준을 또 만들지 말고 이미 검증된 하나를 나노단위에도
// 그대로 얹자"는 원칙에 따름. 잔여토큰 10%는 이전 아이디어 회의(Discord)에서
// 합의된 "하드 세이프티 컷오프" 수치를 재사용 — 단, 여기서는 "일반 작업
// 배정 중단"이 아니라 "적어도 mid 검증을 강제"로 의미를 좁혀 씀(예산이
// 바닥날수록 검증을 낮추는 건 정확히 피해야 할 유인이므로, 이 함수는
// 잔여토큰이 낮다는 이유로 티어를 낮추는 방향으로는 절대 안 쓴다 — 항상
// 상향 조정에만 쓰인다). 두 임계값 다 파일럿(구현계획 4~5단계) 실측
// 전까지는 플레이스홀더로 취급할 것.
const CUMULATIVE_FILE_THRESHOLD = 3
const STEP_FILE_THRESHOLD = 3
const LOW_TOKEN_PCT_THRESHOLD = 10

/**
 * @param {object} input
 * @param {number} [input.stepFileCount] - 이번 나노 스텝이 건드린 파일 수
 * @param {number} [input.cumulativeFileCount] - 마지막 mid/full 검증 이후 누적 변경 파일 수
 * @param {boolean} [input.sensitivePath] - 설정/보안/.github/workflows/공개문서 포함 여부
 * @param {boolean} [input.dependencyBoundaryCrossed] - 정적 신호(import 그래프 등)로 감지된 모듈 경계 교차 여부
 * @param {number} [input.remainingTokenPct] - 기존 호출과의 호환을 위한 단일 provider 잔여 비율(0~100).
 * @param {{claude?: number, codex?: number, antigravity?: number}} [input.providerHeadroom]
 *   - provider별 잔여 사용량 창 비율(0~100). 여러 값이 있으면 가장 낮은 값을 사용한다.
 * @returns {'light'|'mid'|'full'}
 */
function validPercentage(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100
    ? value
    : undefined
}

function lowestProviderHeadroom(providerHeadroom) {
  if (!providerHeadroom || typeof providerHeadroom !== 'object') return undefined
  const values = ['claude', 'codex', 'antigravity']
    .map((provider) => validPercentage(providerHeadroom[provider]))
    .filter((value) => value !== undefined)
  return values.length ? Math.min(...values) : undefined
}

function decideRiskTier(input) {
  const {
    stepFileCount = 0,
    cumulativeFileCount = 0,
    sensitivePath = false,
    dependencyBoundaryCrossed = false,
    remainingTokenPct,
    providerHeadroom,
    changedFiles,
    dependencyEdges,
  } = input || {}

  // providerHeadroom이 있으면 양쪽 중 더 부족한 provider를 기준으로 한다.
  // 단일 숫자 remainingTokenPct는 기존 호출부를 깨지 않기 위한 호환 경로다.
  const lowestHeadroom = lowestProviderHeadroom(providerHeadroom)
  const tokenPct = lowestHeadroom ?? validPercentage(remainingTokenPct)

  const fileClassification = classifyChangedFiles(changedFiles)
  if (sensitivePath || fileClassification.tier === 'full') return 'full'
  if (fileClassification.tier === 'mid') return 'mid'
  if (dependencyBoundaryCrossed || detectDependencyBoundary(changedFiles, dependencyEdges)) return 'mid'
  if (cumulativeFileCount > CUMULATIVE_FILE_THRESHOLD) return 'mid'
  if (stepFileCount > STEP_FILE_THRESHOLD) return 'mid'
  if (tokenPct !== undefined && tokenPct <= LOW_TOKEN_PCT_THRESHOLD) return 'mid'
  return 'light'
}

module.exports = {
  decideRiskTier,
  validPercentage,
  lowestProviderHeadroom,
  isSensitivePath,
  classifyFileRisk,
  classifyChangedFiles,
  detectDependencyBoundary,
  topLevelPath,
  FULL_RISK_FILE_PATTERNS,
  MID_RISK_CODE_EXTENSIONS,
  LIGHT_RISK_FILE_PATTERNS,
  SENSITIVE_PATH_PATTERNS,
  CUMULATIVE_FILE_THRESHOLD,
  STEP_FILE_THRESHOLD,
  LOW_TOKEN_PCT_THRESHOLD,
}
