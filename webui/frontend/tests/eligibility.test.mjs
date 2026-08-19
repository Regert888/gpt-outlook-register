import assert from 'node:assert/strict'
import test from 'node:test'

import {
  formatGCashDetail,
  gcashProbeRequestConfig,
  gcashStatusType,
  summarizeGCash,
} from '../src/eligibility.js'


test('GCash API calls carry the explicit side-effect acknowledgement', () => {
  assert.deepEqual(gcashProbeRequestConfig(), {
    headers: {
      'X-GCash-Probe-Confirmation': 'checkout-side-effects-acknowledged',
    },
    timeout: 3_600_000,
  })
})


test('summarizeGCash reports all three classifications in English', () => {
  const summary = summarizeGCash({
    'one@example.com': { classification: 'eligible' },
    'two@example.com': { classification: 'ineligible' },
    'three@example.com': { classification: 'unknown' },
  })

  assert.deepEqual(summary.counts, { eligible: 1, ineligible: 1, unknown: 1 })
  assert.equal(summary.text, 'Completed: 1 eligible, 1 ineligible, 1 unknown')
})

test('unknown status detail includes the last conclusive verdict', () => {
  const text = formatGCashDetail(
    {
      classification: 'unknown',
      decision: 'checkout_timeout',
      checked_at: 100,
      last_conclusive: {
        classification: 'eligible',
        decision: 'gcash_zero_due_available',
      },
    },
    (value) => `time:${value}`,
  )

  assert.match(text, /Decision: checkout_timeout/)
  assert.match(text, /Checked: time:100/)
  assert.match(text, /Last conclusive: eligible/)
})

test('status colors distinguish eligible, ineligible, and unknown', () => {
  assert.equal(gcashStatusType('eligible'), 'success')
  assert.equal(gcashStatusType('ineligible'), 'warning')
  assert.equal(gcashStatusType('unknown'), 'info')
})
