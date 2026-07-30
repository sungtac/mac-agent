#!/usr/bin/env node

// 나노 게이트 임계값 조정 전용 분석기.
//
// 이 도구는 임계값을 자동으로 바꾸지 않는다. 실제 provider 표본과 위험도
// 입력이 충분하지 않은 상태에서 숫자를 바꾸면 파일럿 한두 건을 일반화하는
// 오류가 생긴다. 분석 결과의 thresholdDecision.eligibleForUpdate가 true인
// 경우에만 사람이 결과를 검토해 decide-risk-tier.js의 상수를 조정한다.

const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { validateEvent } = require('./nano-event-store.js')
const {
  CUMULATIVE_FILE_THRESHOLD,
  STEP_FILE_THRESHOLD,
  LOW_TOKEN_PCT_THRESHOLD,
} = require('./decide-risk-tier.js')

const DEFAULT_EVENT_FILE = path.join(os.homedir(), '.claude', 'nano-gate-events.jsonl')
const MIN_EVENTS_FOR_UPDATE = 20
const MIN_RISK_SIGNAL_EVENTS = 10
const MIN_TOKEN_SIGNAL_EVENTS = 10

function percentile(values, fraction) {
  if (!values.length) return null
  const sorted = [...values].sort((a, b) => a - b)
  const index = Math.min(sorted.length - 1, Math.ceil(sorted.length * fraction) - 1)
  return sorted[Math.max(0, index)]
}

function readNanoEvents(eventFile = DEFAULT_EVENT_FILE) {
  if (!fs.existsSync(eventFile)) return { events: [], invalidLines: [], eventFile: path.resolve(eventFile) }
  const events = []
  const invalidLines = []
  const lines = fs.readFileSync(eventFile, 'utf8').split('\n')
  for (const [index, line] of lines.entries()) {
    if (!line.trim()) continue
    try {
      const event = JSON.parse(line)
      validateEvent(event)
      events.push(event)
    } catch (error) {
      invalidLines.push({ line: index + 1, reason: error.code || 'invalid_event' })
    }
  }
  return { events, invalidLines, eventFile: path.resolve(eventFile) }
}

function summarizeNanoEvents(events, invalidLines = [], eventFile = DEFAULT_EVENT_FILE) {
  const byTier = { light: 0, mid: 0, full: 0 }
  const byStatus = { passed: 0, failed: 0 }
  const durationValues = []
  const tokenTotals = {}
  const headroomMinimums = {}
  const signalCounts = {
    sensitivePath: 0,
    dependencyBoundaryCrossed: 0,
    stepFileThreshold: 0,
    cumulativeFileThreshold: 0,
    lowTokenThreshold: 0,
  }
  let riskSignalEvents = 0
  let tokenSignalEvents = 0

  for (const event of events) {
    if (byTier[event.verificationTier] !== undefined) byTier[event.verificationTier] += 1
    if (byStatus[event.status] !== undefined) byStatus[event.status] += 1
    if (typeof event.durationMs === 'number') durationValues.push(event.durationMs)

    if (event.tokenUsage && typeof event.tokenUsage === 'object') {
      for (const [provider, value] of Object.entries(event.tokenUsage)) {
        if (typeof value === 'number' && Number.isFinite(value)) tokenTotals[provider] = (tokenTotals[provider] || 0) + value
      }
    }

    const risk = event.riskInputs
    if (!risk) continue
    riskSignalEvents += 1
    if (risk.sensitivePath) signalCounts.sensitivePath += 1
    if (risk.dependencyBoundaryCrossed) signalCounts.dependencyBoundaryCrossed += 1
    if (typeof risk.stepFileCount === 'number' && risk.stepFileCount > STEP_FILE_THRESHOLD) signalCounts.stepFileThreshold += 1
    if (typeof risk.cumulativeFileCount === 'number' && risk.cumulativeFileCount > CUMULATIVE_FILE_THRESHOLD) signalCounts.cumulativeFileThreshold += 1
    const providerValues = risk.providerHeadroom && typeof risk.providerHeadroom === 'object'
      ? Object.entries(risk.providerHeadroom)
      : []
    const hasTokenSignal = typeof risk.remainingTokenPct === 'number' || providerValues.some(([, value]) => typeof value === 'number')
    if (hasTokenSignal) tokenSignalEvents += 1
    for (const [provider, value] of providerValues) {
      if (typeof value !== 'number' || !Number.isFinite(value)) continue
      headroomMinimums[provider] = headroomMinimums[provider] === undefined ? value : Math.min(headroomMinimums[provider], value)
    }
    const lowTokenSignal = (typeof risk.remainingTokenPct === 'number' && risk.remainingTokenPct <= LOW_TOKEN_PCT_THRESHOLD)
      || providerValues.some(([, value]) => typeof value === 'number' && value <= LOW_TOKEN_PCT_THRESHOLD)
    if (lowTokenSignal) signalCounts.lowTokenThreshold += 1
  }

  const hasEnoughEvents = events.length >= MIN_EVENTS_FOR_UPDATE
  const hasEnoughRiskSignals = riskSignalEvents >= MIN_RISK_SIGNAL_EVENTS
  const hasEnoughTokenSignals = tokenSignalEvents >= MIN_TOKEN_SIGNAL_EVENTS
  const thresholdDecision = {
    eligibleForUpdate: hasEnoughEvents && hasEnoughRiskSignals && hasEnoughTokenSignals && invalidLines.length === 0,
    current: {
      cumulativeFileThreshold: CUMULATIVE_FILE_THRESHOLD,
      stepFileThreshold: STEP_FILE_THRESHOLD,
      lowTokenPctThreshold: LOW_TOKEN_PCT_THRESHOLD,
    },
    sampleRequirements: {
      minimumEvents: MIN_EVENTS_FOR_UPDATE,
      minimumRiskSignalEvents: MIN_RISK_SIGNAL_EVENTS,
      minimumTokenSignalEvents: MIN_TOKEN_SIGNAL_EVENTS,
    },
    reason: invalidLines.length
      ? '원장에 손상된 줄이 있어 먼저 복구해야 함'
      : !hasEnoughEvents
        ? `이벤트 표본 부족(${events.length}/${MIN_EVENTS_FOR_UPDATE})`
        : !hasEnoughRiskSignals
          ? `위험도 입력 표본 부족(${riskSignalEvents}/${MIN_RISK_SIGNAL_EVENTS})`
          : !hasEnoughTokenSignals
            ? `provider/token 잔여량 표본 부족(${tokenSignalEvents}/${MIN_TOKEN_SIGNAL_EVENTS})`
          : '표본 조건 충족 — 자동 변경하지 말고 사람이 분포와 실패율을 검토할 것',
  }

  return {
    eventFile: path.resolve(eventFile),
    total: events.length,
    invalidLines: invalidLines.length,
    byTier,
    byStatus,
    durationMs: {
      count: durationValues.length,
      median: percentile(durationValues, 0.5),
      p95: percentile(durationValues, 0.95),
    },
    tokenTotals,
    headroomMinimums,
    riskSignalEvents,
    tokenSignalEvents,
    signalCounts,
    thresholdDecision,
  }
}

function main() {
  const eventFile = process.argv[2] || process.env.NANO_EVENT_FILE || DEFAULT_EVENT_FILE
  const loaded = readNanoEvents(eventFile)
  process.stdout.write(`${JSON.stringify(summarizeNanoEvents(loaded.events, loaded.invalidLines, loaded.eventFile))}\n`)
}

if (require.main === module) main()

module.exports = {
  DEFAULT_EVENT_FILE,
  MIN_EVENTS_FOR_UPDATE,
  MIN_RISK_SIGNAL_EVENTS,
  MIN_TOKEN_SIGNAL_EVENTS,
  readNanoEvents,
  summarizeNanoEvents,
  percentile,
}
