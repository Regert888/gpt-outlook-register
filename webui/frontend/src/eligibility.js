export function gcashStatusType(classification) {
  return {
    eligible: 'success',
    ineligible: 'warning',
    unknown: 'info',
  }[classification] || 'info'
}

export function gcashProbeRequestConfig() {
  return {
    headers: {
      'X-GCash-Probe-Confirmation': 'checkout-side-effects-acknowledged',
    },
    timeout: 3_600_000,
  }
}

export function summarizeGCash(results) {
  const counts = { eligible: 0, ineligible: 0, unknown: 0 }
  for (const result of Object.values(results || {})) {
    const classification = result?.classification
    counts[classification in counts ? classification : 'unknown'] += 1
  }
  return {
    counts,
    text: `Completed: ${counts.eligible} eligible, ${counts.ineligible} ineligible, ${counts.unknown} unknown`,
  }
}

export function formatGCashDetail(check, formatTime = (value) => String(value)) {
  if (!check) return ''
  const parts = []
  if (check.decision) parts.push(`Decision: ${check.decision}`)
  if (check.amount_minor !== undefined && check.amount_minor !== null) {
    parts.push(`Amount: ${check.amount_minor} ${check.currency || ''}`.trim())
  } else if (check.currency) {
    parts.push(`Currency: ${check.currency}`)
  }
  if (check.checked_at) parts.push(`Checked: ${formatTime(check.checked_at)}`)
  if (check.classification === 'unknown' && check.last_conclusive?.classification) {
    parts.push(`Last conclusive: ${check.last_conclusive.classification}`)
  }
  return parts.join(' · ')
}
