// Tests for the pure chart/order-ticket symbol sync state machine inside
// dashboard/index.html (symbolSyncOnChartChange, symbolSyncOnOrderEdit,
// symbolSyncOnUseChartSymbol, symbolSyncMismatch).
//
// Same extraction pattern as tests/test_system_status.mjs: this repo has no
// frontend build/test tooling, so rather than adding one just to test a few
// functions, this file pulls their source text verbatim out of the real
// dashboard/index.html (between the "TESTABLE" markers) and evaluates just
// that snippet — these tests exercise the actual production code, not a
// hand-copied duplicate that could drift from it.
//
// Each DOM entry point in dashboard/index.html (the chart Load button, Enter
// key, a Market Scanner heatmap tile, initChart(), typing in the order
// Symbol field, loading a strategy signal or scalper trade into the ticket,
// and the "Use chart symbol in order ticket" button) calls exactly one of
// these four functions and then re-renders from the result — so testing the
// state machine here covers the real behavior of every one of those paths.
//
// Run with: node --test tests/test_symbol_sync.mjs

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';
import { test } from 'node:test';

const __dirname = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(__dirname, '..', 'dashboard', 'index.html'), 'utf-8');
const match = html.match(
  /\/\/ ---- TESTABLE: pure chart\/order-ticket symbol sync logic[\s\S]*?\/\/ ---- END TESTABLE ----/
);
if (!match) {
  throw new Error('Could not find the TESTABLE symbol-sync block in dashboard/index.html — did it move or get renamed?');
}
// eslint-disable-next-line no-new-func
const {
  symbolSyncOnChartChange,
  symbolSyncOnOrderEdit,
  symbolSyncOnUseChartSymbol,
  symbolSyncMismatch,
} = new Function(`'use strict';\n${match[0]}\nreturn {
  symbolSyncOnChartChange, symbolSyncOnOrderEdit, symbolSyncOnUseChartSymbol, symbolSyncMismatch,
};`)();

test('chart tile changes chart symbol (Market Scanner heatmap, Load button, or Enter — same path)', () => {
  const state = { chartSymbol: 'QQQ', orderSymbol: 'QQQ' };
  const next = symbolSyncOnChartChange(state, 'NVDA');
  assert.equal(next.chartSymbol, 'NVDA');
  // The order-ticket symbol must never move just because the chart did.
  assert.equal(next.orderSymbol, 'QQQ');
});

test('synchronization copies the chart symbol into the order ticket', () => {
  const state = { chartSymbol: 'NVDA', orderSymbol: 'QQQ' };
  const next = symbolSyncOnUseChartSymbol(state);
  assert.equal(next.orderSymbol, 'NVDA');
  assert.equal(next.chartSymbol, 'NVDA');
});

test('manual order symbol remains unchanged until synchronization', () => {
  let state = { chartSymbol: 'QQQ', orderSymbol: 'QQQ' };
  // A heatmap tile (or the Load button, or Enter) changes only the chart —
  // the user's order symbol survives untouched, however many times it happens.
  state = symbolSyncOnChartChange(state, 'NVDA');
  assert.equal(state.orderSymbol, 'QQQ');
  state = symbolSyncOnChartChange(state, 'TSM');
  assert.equal(state.orderSymbol, 'QQQ');
  // Only the explicit sync action ever copies it over.
  state = symbolSyncOnUseChartSymbol(state);
  assert.equal(state.orderSymbol, 'TSM');
});

test('a manual edit to the order symbol never touches the chart symbol', () => {
  const state = { chartSymbol: 'NVDA', orderSymbol: 'QQQ' };
  const next = symbolSyncOnOrderEdit(state, 'AAPL');
  assert.equal(next.orderSymbol, 'AAPL');
  assert.equal(next.chartSymbol, 'NVDA');
});

test('mismatch warning appears when symbols differ and disappears once they match', () => {
  let state = { chartSymbol: 'QQQ', orderSymbol: 'QQQ' };
  assert.equal(symbolSyncMismatch(state), false);

  state = symbolSyncOnChartChange(state, 'NVDA');
  assert.equal(symbolSyncMismatch(state), true);

  state = symbolSyncOnUseChartSymbol(state);
  assert.equal(symbolSyncMismatch(state), false);

  // Also disappears if the user happens to manually type the same symbol
  // the chart is already showing, without using the sync button.
  state = symbolSyncOnChartChange(state, 'TSM');
  assert.equal(symbolSyncMismatch(state), true);
  state = symbolSyncOnOrderEdit(state, 'TSM');
  assert.equal(symbolSyncMismatch(state), false);
});

test('mismatch is never flagged while either symbol is still empty (nothing charted or typed yet)', () => {
  assert.equal(symbolSyncMismatch({ chartSymbol: '', orderSymbol: '' }), false);
  assert.equal(symbolSyncMismatch({ chartSymbol: 'QQQ', orderSymbol: '' }), false);
  assert.equal(symbolSyncMismatch({ chartSymbol: '', orderSymbol: 'QQQ' }), false);
});

test('symbol values are normalized (trimmed and uppercased) before comparison', () => {
  const state = symbolSyncOnChartChange({ chartSymbol: '', orderSymbol: '' }, '  nvda  ');
  assert.equal(state.chartSymbol, 'NVDA');
  const withOrder = symbolSyncOnOrderEdit(state, 'nvda');
  assert.equal(symbolSyncMismatch(withOrder), false);
});
