// Tests for the pure "My trading agent" validation/display logic inside
// dashboard/index.html (agentValidateAmount, agentOnStart, deriveAgentDisplay).
//
// Same extraction pattern as the other tests/test_*.mjs files in this repo:
// this file pulls the functions' source text verbatim out of the real
// dashboard/index.html (between the "TESTABLE" markers) and evaluates just
// that snippet, so these tests exercise the actual production code.
//
// Covers: start, stop, repeated start, invalid amount, scan failure, and a
// reload round-trip. "Risk rejection" and "scan failure" (a strategy's
// scan throwing mid-cycle) are backend concerns — src/execution/
// autonomous_trader.py is where risk checks and per-symbol scan failures
// actually happen, and where they're tested (see
// tests/test_autonomous_trader.py's TestScanForEntries, in particular
// test_risk_rejection_blocks_the_entry_without_opening_a_position and
// test_a_failing_strategy_does_not_stop_the_rest_of_the_scan). This dashboard
// never runs strategies or places trades on its own — see the "My trading
// agent" module comment in dashboard/index.html — so there's nothing of
// that kind to unit-test on the frontend; "scan failure" here instead
// covers the dashboard's OWN failure mode: a failed status check must show
// a useful reason rather than silently rendering "Agent is off".
//
// Run with: node --test tests/test_agent_status.mjs

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';
import { test } from 'node:test';

const __dirname = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(__dirname, '..', 'dashboard', 'index.html'), 'utf-8');
const match = html.match(
  /\/\/ ---- TESTABLE: pure "My trading agent" validation\/display[\s\S]*?\/\/ ---- END TESTABLE ----/
);
if (!match) {
  throw new Error('Could not find the TESTABLE agent-status block in dashboard/index.html — did it move or get renamed?');
}
// eslint-disable-next-line no-new-func
const { agentValidateAmount, agentOnStart, deriveAgentDisplay } = new Function(
  `'use strict';\n${match[0]}\nreturn { agentValidateAmount, agentOnStart, deriveAgentDisplay };`
)();

const STRATEGY_IDS = ['orb', 'vwap_reversion', 'rsi2_connors'];

test('start: arms a plan and the display reports it as running', () => {
  const result = agentOnStart({ notionalPerTradeUsd: '1000', strategyIds: STRATEGY_IDS, startedBy: 'sumit', nowIso: '2026-09-25T00:00:00Z' });
  assert.equal(result.ok, true);
  assert.equal(result.plan.notional_per_trade_usd, '1000');
  assert.deepEqual(result.plan.strategy_ids, STRATEGY_IDS);

  const display = deriveAgentDisplay({ plan: result.plan, isPaperMode: true, liveAutonomousEnabled: null, scanIntervalSec: null, lastError: null });
  assert.equal(display.status, 'running');
  assert.equal(display.amountPerTradeUsd, 1000);
});

test('stop: clearing the plan (null) reports the agent as stopped', () => {
  const display = deriveAgentDisplay({ plan: null, isPaperMode: true, liveAutonomousEnabled: null, scanIntervalSec: null, lastError: null });
  assert.equal(display.status, 'stopped');
  assert.equal(display.amountPerTradeUsd, null);
});

test('repeated start replaces the plan rather than erroring or duplicating', () => {
  const first = agentOnStart({ notionalPerTradeUsd: '1000', strategyIds: STRATEGY_IDS, startedBy: 'sumit', nowIso: '2026-09-25T00:00:00Z' });
  assert.equal(first.ok, true);

  const second = agentOnStart({ notionalPerTradeUsd: '2500', strategyIds: STRATEGY_IDS, startedBy: 'sumit', nowIso: '2026-09-25T01:00:00Z' });
  assert.equal(second.ok, true);
  // The second start's numbers win — a fresh, single plan, not two plans
  // stacked or an error for "already running".
  assert.equal(second.plan.notional_per_trade_usd, '2500');
  assert.equal(second.plan.armed_at, '2026-09-25T01:00:00Z');
});

test('invalid amount: zero, negative, non-numeric, and scientific notation are all rejected with a useful reason', () => {
  for (const bad of ['0', '-5', 'abc', '', '  ', '1e3', '1E3']) {
    const result = agentOnStart({ notionalPerTradeUsd: bad, strategyIds: STRATEGY_IDS, startedBy: 'sumit', nowIso: '2026-09-25T00:00:00Z' });
    assert.equal(result.ok, false, `expected "${bad}" to be rejected`);
    assert.match(result.error, /valid amount/i);
    assert.equal(result.plan, undefined); // no plan is ever produced for a rejected amount
  }
  const valid = agentValidateAmount('1000');
  assert.equal(valid.ok, true);
  assert.equal(valid.amount, 1000);
});

test('scan failure: a failed status check surfaces a useful reason instead of silently showing "off"', () => {
  // The agent WAS running (plan still set — refreshDailyPlan() never wipes
  // it just because one poll failed) but the last check errored.
  const display = deriveAgentDisplay({
    plan: { strategy_ids: STRATEGY_IDS, notional_per_trade_usd: '1000' },
    isPaperMode: false,
    liveAutonomousEnabled: null,
    scanIntervalSec: null,
    lastError: 'Request timed out after 10s — check your server address and connection.',
  });
  assert.equal(display.status, 'running'); // not silently demoted to "stopped"
  assert.equal(display.error, 'Request timed out after 10s — check your server address and connection.');
});

test('the agent cannot bypass risk checks or place live orders from Paper Trading: mode is never anything but alert-only in paper mode', () => {
  // Even if a caller incorrectly passed liveAutonomousEnabled: true while
  // isPaperMode is also true, Paper Trading must still win — there is no
  // path in Paper Trading that can ever execute a trade on its own.
  const display = deriveAgentDisplay({
    plan: { strategy_ids: STRATEGY_IDS, notional_per_trade_usd: '1000' },
    isPaperMode: true,
    liveAutonomousEnabled: true,
    scanIntervalSec: 60,
    lastError: null,
  });
  assert.equal(display.mode, 'alert_only');
});

test('live mode reflects the server\'s real autonomous_trading_enabled switch rather than assuming it', () => {
  const off = deriveAgentDisplay({ plan: { strategy_ids: STRATEGY_IDS, notional_per_trade_usd: '1000' }, isPaperMode: false, liveAutonomousEnabled: false, scanIntervalSec: 60, lastError: null });
  assert.equal(off.mode, 'alert_only');
  assert.match(off.nextScan, /manual scan only/i);

  const on = deriveAgentDisplay({ plan: { strategy_ids: STRATEGY_IDS, notional_per_trade_usd: '1000' }, isPaperMode: false, liveAutonomousEnabled: true, scanIntervalSec: 60, lastError: null });
  assert.equal(on.mode, 'paper_auto_execution');
  assert.match(on.nextScan, /60s/);

  const unknown = deriveAgentDisplay({ plan: { strategy_ids: STRATEGY_IDS, notional_per_trade_usd: '1000' }, isPaperMode: false, liveAutonomousEnabled: null, scanIntervalSec: null, lastError: null });
  assert.equal(unknown.mode, 'unknown'); // never guessed as one of the two real modes
});

test('page reload: a plan restored from persistence alone reconstructs the exact same display as when it was armed', () => {
  const started = agentOnStart({ notionalPerTradeUsd: '750', strategyIds: STRATEGY_IDS, startedBy: 'sumit', nowIso: '2026-09-25T00:00:00Z' });
  const beforeReload = deriveAgentDisplay({ plan: started.plan, isPaperMode: true, liveAutonomousEnabled: null, scanIntervalSec: null, lastError: null });

  // Simulates a reload: nothing survives except whatever was persisted
  // (the plan object itself, JSON round-tripped exactly like localStorage
  // does it) — no other in-memory field this module depends on.
  const restoredPlan = JSON.parse(JSON.stringify(started.plan));
  const afterReload = deriveAgentDisplay({ plan: restoredPlan, isPaperMode: true, liveAutonomousEnabled: null, scanIntervalSec: null, lastError: null });

  assert.deepEqual(afterReload, beforeReload);
  assert.equal(afterReload.status, 'running');
  assert.equal(afterReload.amountPerTradeUsd, 750);
});

test('page reload: a stopped agent (no persisted plan) reloads as stopped, not "running" from stale memory', () => {
  const display = deriveAgentDisplay({ plan: null, isPaperMode: true, liveAutonomousEnabled: null, scanIntervalSec: null, lastError: null });
  assert.equal(display.status, 'stopped');
});
