// Tests for the pure backtest-provenance/methodology logic inside
// dashboard/index.html (computeLosingStreak, combinedLosingStreak,
// deriveBacktestMeta) that back the "How these strategies have performed"
// card's Methodology panel on the Strategies & Algos tab.
//
// Same extraction pattern as the other tests/test_*.mjs files in this
// repo: pulls the functions' source text verbatim out of the real
// dashboard/index.html (between the "TESTABLE" markers) and evaluates
// just that snippet, so these tests exercise the actual production code.
//
// Run with: node --test tests/test_backtest_provenance.mjs

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';
import { test } from 'node:test';

const __dirname = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(__dirname, '..', 'dashboard', 'index.html'), 'utf-8');
const match = html.match(
  /\/\/ ---- TESTABLE: pure backtest provenance\/methodology[\s\S]*?\/\/ ---- END TESTABLE ----/
);
if (!match) {
  throw new Error('Could not find the TESTABLE backtest-provenance block in dashboard/index.html — did it move or get renamed?');
}
// eslint-disable-next-line no-new-func
const { computeLosingStreak, combinedLosingStreak, deriveBacktestMeta } = new Function(
  `'use strict';\n${match[0]}\nreturn { computeLosingStreak, combinedLosingStreak, deriveBacktestMeta };`
)();

const trade = (exit_date, pnl_usd) => ({ exit_date, pnl_usd });

test('computeLosingStreak: returns null when there is no trade list at all (illustrative sample data)', () => {
  assert.equal(computeLosingStreak(undefined), null);
  assert.equal(computeLosingStreak([]), null);
});

test('computeLosingStreak: finds the longest run of consecutive losses in chronological order', () => {
  const trades = [
    trade('2024-01-01', 100),
    trade('2024-01-05', -50),
    trade('2024-01-10', -30),
    trade('2024-01-15', -20),
    trade('2024-01-20', 40),
    trade('2024-01-25', -10),
  ];
  assert.equal(computeLosingStreak(trades), 3);
});

test('computeLosingStreak: a breakeven trade (pnl exactly 0) resets the streak, same as a win', () => {
  const trades = [trade('2024-01-01', -10), trade('2024-01-05', -10), trade('2024-01-10', 0), trade('2024-01-15', -10)];
  assert.equal(computeLosingStreak(trades), 2);
});

test('computeLosingStreak: sorts by exit_date first — order in the input array must not matter', () => {
  const inOrder = [trade('2024-01-01', -10), trade('2024-01-05', -10), trade('2024-01-10', 10)];
  const shuffled = [inOrder[2], inOrder[0], inOrder[1]];
  assert.equal(computeLosingStreak(shuffled), computeLosingStreak(inOrder));
  assert.equal(computeLosingStreak(shuffled), 2);
});

test('computeLosingStreak: a trade still open at the end of the window (no exit_date) is excluded', () => {
  const trades = [trade('2024-01-01', -10), { exit_date: null, pnl_usd: -999 }, trade('2024-01-05', -10)];
  assert.equal(computeLosingStreak(trades), 2);
});

test('combinedLosingStreak: merges every pair\'s trades into one chronological streak, not a max or average of per-pair streaks', () => {
  const results = [
    { symbol: 'QQQ', trades: [trade('2024-01-01', -10), trade('2024-01-10', 10)] }, // pair A: streak of 1
    { symbol: 'SPY', trades: [trade('2024-01-03', -10), trade('2024-01-05', -10)] }, // pair B: streak of 2
  ];
  // Merged chronologically: 01-01(-), 01-03(-), 01-05(-), 01-10(+) — three losses in a row across pairs
  assert.equal(combinedLosingStreak(results), 3);
});

test('combinedLosingStreak: null when no result has a trade list (illustrative sample data)', () => {
  const results = [{ symbol: 'QQQ' }, { symbol: 'SPY' }];
  assert.equal(combinedLosingStreak(results), null);
});

test('deriveBacktestMeta: illustrative sample data reports "Illustrative example" for every fabricated field, never a real date range', () => {
  const meta = deriveBacktestMeta({
    isSample: true, symbols: ['QQQ', 'SPY', 'IWM'], startDate: null, endDate: null,
    notionalPerTradeUsd: 1000, startingCapitalUsd: 100000, slippageBps: 5, commissionUsd: 0,
    computedAtIso: '2026-09-25T00:00:00Z',
  });
  assert.equal(meta.provenance, 'Illustrative example');
  assert.equal(meta.dateRange, 'Illustrative example');
  assert.equal(meta.startingCapital, 'Illustrative example');
  assert.equal(meta.positionSize, 'Illustrative example');
  assert.equal(meta.feesAndSlippage, 'Illustrative example');
  assert.equal(meta.sampleType, 'Illustrative example');
  // The instrument list itself is real (it's what the illustration is modeled on), not fabricated data
  assert.equal(meta.instrumentAndTimeframe, 'QQQ, SPY, IWM · Daily bars');
});

test('deriveBacktestMeta: a real backtest reports its actual real values, never "Illustrative example"', () => {
  const meta = deriveBacktestMeta({
    isSample: false, symbols: ['QQQ', 'SPY', 'IWM'], startDate: '2023-09-25', endDate: '2026-09-25',
    notionalPerTradeUsd: 1000, startingCapitalUsd: 100000, slippageBps: 5, commissionUsd: 0,
    computedAtIso: '2026-09-25T00:00:00Z',
  });
  assert.match(meta.provenance, /Verified/);
  assert.equal(meta.dateRange, '2023-09-25 to 2026-09-25');
  assert.equal(meta.startingCapital, 100000);
  assert.equal(meta.positionSize, 1000);
  assert.equal(meta.feesAndSlippage, '5 bps (0.05%) slippage per fill, no commission');
  assert.match(meta.sampleType, /Out-of-sample/);
  assert.equal(meta.dataTimestamp, '2026-09-25T00:00:00Z');
});

test('deriveBacktestMeta: a real backtest missing its date range (e.g. a stale pre-upgrade cache) says "Not available", never a guessed range', () => {
  const meta = deriveBacktestMeta({
    isSample: false, symbols: ['QQQ'], startDate: null, endDate: null,
    notionalPerTradeUsd: 1000, startingCapitalUsd: 100000, slippageBps: 5, commissionUsd: 0,
    computedAtIso: null,
  });
  assert.equal(meta.dateRange, 'Not available');
  assert.equal(meta.dataTimestamp, 'Not available');
});

test('deriveBacktestMeta: benchmark definition is always shown, sample or real — it describes the method, not a computed number', () => {
  const sample = deriveBacktestMeta({ isSample: true, symbols: [], startDate: null, endDate: null });
  const real = deriveBacktestMeta({ isSample: false, symbols: [], startDate: null, endDate: null });
  assert.equal(sample.benchmarkDefinition, real.benchmarkDefinition);
  assert.match(sample.benchmarkDefinition, /buy-and-hold/i);
});
