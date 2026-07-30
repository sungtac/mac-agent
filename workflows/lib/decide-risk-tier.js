// 나노게이트 공통 위험도 판정 함수 (설계: docs/nano-gate-design.md 결정 2).
//
// 순수 함수 — LLM/에이전트 호출 없음. 절대 이 파일에 agent() 호출이나
// 비동기 I/O를 추가하지 말 것: 나노 스텝마다 매번 호출되는 게 전제라,
// 여기서 무거워지면 verify-task v1이 겪었던 "매번 무조건 풀-웨이트"
// 문제가 판정 단계로 옮겨붙을 뿐이다.
//
// 중요 — 이 파일은 지금 어디서도 import되지 않는다: Workflow 툴 스크립트
// (workflows/verify-task-v2.js 등)는 파일시스템/require 접근이 없는
// 샌드박스에서 실행되므로 외부 JS 모듈을 못 불러온다(verify-task-v2.js
// 상단 주석에 이미 명시됨 — "의도적 중복이지, 재사용이 아니다"). 이 파일은
// 스펙을 고정하고 단위테스트로 검증하기 위한 기준 구현이고, 실제 나노게이트
// 루프(구현계획 3단계)에 통합할 때는 이 함수 본문을 grep 가능한 형태로
// 그대로 복사해 넣어야 한다(기존 decideTier()와 동일한 패턴). 이후 이 파일을
// 고치면 인라인 사본도 반드시 같이 고칠 것 — 자동 동기화 없음.
//
// 민감 경로 패턴은 verify-task-v2.js의 SENSITIVE_PATH_PATTERNS와 동일하게
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

function isSensitivePath(path) {
  return SENSITIVE_PATH_PATTERNS.some((re) => re.test(path))
}

// 누적 파일 수 임계값 3은 기존 verify-task-v2.js의 decideTier()(파일수≤3 →
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
  } = input || {}

  // providerHeadroom이 있으면 양쪽 중 더 부족한 provider를 기준으로 한다.
  // 단일 숫자 remainingTokenPct는 기존 호출부를 깨지 않기 위한 호환 경로다.
  const lowestHeadroom = lowestProviderHeadroom(providerHeadroom)
  const tokenPct = lowestHeadroom ?? validPercentage(remainingTokenPct)

  if (sensitivePath) return 'full'
  if (dependencyBoundaryCrossed) return 'mid'
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
  SENSITIVE_PATH_PATTERNS,
  CUMULATIVE_FILE_THRESHOLD,
  STEP_FILE_THRESHOLD,
  LOW_TOKEN_PCT_THRESHOLD,
}
