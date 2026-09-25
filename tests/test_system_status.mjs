// Tests for deriveSystemStatus() — the pure, DOM-free function inside
// dashboard/index.html that decides what each System Status row shows.
//
// This repo has no frontend build/test tooling (no package.json, no test
// runner) — dashboard/ is one static HTML file. Rather than adding a new
// toolchain (jsdom, a bundler) just to test one function, this file extracts
// the function's own source text verbatim from the real file (between the
// "TESTABLE" markers in index.html) and evaluates just that snippet, so
// these tests exercise the actual production code, not a hand-copied
// duplicate that could drift from it.
//
// Run with: node --test tests/test_system_status.mjs

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';
import { test } from 'node:test';

const __dirname = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(__dirname, '..', 'dashboard', 'index.html'), 'utf-8');
const match = html.match(
  /\/\/ ---- TESTABLE: pure status derivation, no DOM\/fetch[\s\S]*?\/\/ ---- END TESTABLE ----/
);
if (!match) {
  throw new Error('Could not find the TESTABLE deriveSystemStatus() block in dashboard/index.html — did it move or get renamed?');
}
// eslint-disable-next-line no-new-func
const deriveSystemStatus = new Function(`'use strict';\n${match[0]}\nreturn deriveSystemStatus;`)();

// A fully "nothing checked yet" baseline — every test overrides only the
// signals it cares about, so each case reads as exactly what it's testing.
const base = () => ({
  demoMode: false,
  healthChecked: false, healthOk: false, healthError: false,
  marketChecked: false, marketOk: false,
  chartLoaded: false, chartAvailable: false,
  sigIntegrityConfigured: false, sigIntegrityChecked: false, sigIntegrityOk: false,
  alertsChecked: false, alertsOk: false,
  agentChecked: false, agentOk: false,
  storageWorks: true,
});

test('healthy paper mode', () => {
  const status = deriveSystemStatus({
    ...base(),
    demoMode: true,
    marketChecked: true, marketOk: true,
    chartLoaded: true, chartAvailable: true,
    storageWorks: true,
  });
  assert.equal(status.paperEngine, 'working');
  assert.equal(status.liveApi, 'unavailable_paper'); // never "healthy" for a broker it isn't talking to
  assert.equal(status.marketData, 'working');
  assert.equal(status.chartProvider, 'working');
  assert.equal(status.alertEngine, 'simulated');
  assert.equal(status.agentEngine, 'simulated');
  assert.equal(status.newsFeed, 'not_configured');
});

test('disconnected live API', () => {
  const status = deriveSystemStatus({
    ...base(),
    demoMode: false,
    healthChecked: true, healthError: true, // fetch itself failed, not just an unhealthy response
  });
  assert.equal(status.liveApi, 'disconnected');
});

test('unavailable news feed', () => {
  // No signal input affects this — there is no news provider in this build,
  // ever, regardless of mode or what else is healthy.
  assert.equal(deriveSystemStatus(base()).newsFeed, 'not_configured');
  assert.equal(deriveSystemStatus({ ...base(), demoMode: true }).newsFeed, 'not_configured');
});

test('failed alert service', () => {
  const status = deriveSystemStatus({
    ...base(),
    demoMode: false,
    alertsChecked: true, alertsOk: false,
  });
  assert.equal(status.alertEngine, 'failed');
});

test('checking state', () => {
  const status = deriveSystemStatus({
    ...base(),
    demoMode: false,
    // Nothing marked *Checked yet — everything that depends on an in-flight
    // request should read "checking", never a guessed working/failed.
  });
  assert.equal(status.liveApi, 'checking');
  assert.equal(status.marketData, 'checking');
  assert.equal(status.chartProvider, 'checking');
  assert.equal(status.alertEngine, 'checking');
  assert.equal(status.agentEngine, 'checking');
});

test('mixed healthy and failed dependencies', () => {
  const status = deriveSystemStatus({
    ...base(),
    demoMode: false,
    healthChecked: true, healthOk: true, // live API up
    marketChecked: true, marketOk: true, // market data up
    chartLoaded: true, chartAvailable: true, // chart up
    sigIntegrityConfigured: true, sigIntegrityChecked: true, sigIntegrityOk: false, // configured but down
    alertsChecked: true, alertsOk: false, // down
    agentChecked: true, agentOk: true, // up
    storageWorks: true,
  });
  assert.equal(status.liveApi, 'working');
  assert.equal(status.marketData, 'working');
  assert.equal(status.chartProvider, 'working');
  assert.equal(status.signalIntegrity, 'disconnected');
  assert.equal(status.alertEngine, 'failed');
  assert.equal(status.agentEngine, 'working');
  // A failed dependency must never be reported as anything but failed/disconnected —
  // this is the literal "do not claim a disconnected service is healthy" check.
  assert.notEqual(status.alertEngine, 'working');
  assert.notEqual(status.signalIntegrity, 'working');
});

test('signal-integrity: not configured vs configured-but-down are distinct states', () => {
  const notConfigured = deriveSystemStatus({ ...base(), sigIntegrityConfigured: false });
  const configuredButDown = deriveSystemStatus({
    ...base(), sigIntegrityConfigured: true, sigIntegrityChecked: true, sigIntegrityOk: false,
  });
  assert.equal(notConfigured.signalIntegrity, 'not_configured');
  assert.equal(configuredButDown.signalIntegrity, 'disconnected');
});

test('paper engine reflects real storage availability, not just demo mode', () => {
  const storageBlocked = deriveSystemStatus({ ...base(), demoMode: true, storageWorks: false });
  assert.equal(storageBlocked.paperEngine, 'failed');
});
