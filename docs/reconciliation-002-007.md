# HedgeHog execution reconciliation candidate — 2026.09.25-rc1

Status: code candidate, not a deployed autonomous release. No live orders were placed.

## Source provenance

Base: sumitsinghpal/Execution-Engine main at 98298ac (full SHA in release-manifest.json).
Drive inputs: HedgeHog-Execution-Engine-002-007-final.zip and HedgeHog-Trade-TF-002-007-final.zip downloaded from Runtime. Their original bytes and SHA256 digests are retained in the delivery archive.
GitHub permission rechecked: pull=true, push=false. This branch is local; no upstream branch or PR was published.

## Changes

- Kept GitHub's dashboard, Schwab translation, refresh-token persistence, account reconciliation, brackets and paper autonomous trader.
- Adapted the Drive Robinhood bridge client to the newer BrokerAdapter interface, including explicit market-data capabilities. No account or broker fallback.
- Routed account calls by configured broker. Multiple live brokers require an explicit MARKET_DATA_BROKER for market-data operations. Schwab token/rate-limit caching remains on the underlying Schwab adapter.
- Bound new previews to persisted order terms, strategy/agent identity, execution mode and account profile. Changing those requires a new decision and approval. Legacy previews must be recreated.
- Rejected changed payloads reusing a decision ID; checked approval preview IDs, risk rejection and UTC timestamp conversion.
- Added permanent database submission claims before broker calls. Concurrent/restarted requests cannot resubmit the same decision. Unknown submission outcomes remain claimed for operator reconciliation; no guessed broker order IDs.
- Disabled automatic retries for Schwab order POSTs after ambiguous transport failures. Read/preview retry behavior remains available.
- Added cancellation forwarding for supported adapters, reporting a request rather than falsely confirming cancellation.
- Allowed direct submitted-to-filled/canceled reconciliation and successive partial-fill quantity updates.
- Replaced Trade-TF's nonexistent /intents Engine route with preview/execute/status calls and X-Admin-Key authentication. Required explicit account, asset/order types, whole quantity and caller-supplied approval.
- Added authentication and strategy identity to the autonomy preview client. Fixed constructor compatibility with the older five-argument coordinator form.
- Connected the strategy registry to production autonomy selection and persisted version/hash contracts in SQLite. Conflicting strategy versions block the run. Missing Algo-TF refinement still returns REFINE_UNAVAILABLE.

## Verified evidence

Engine full regression: 684 passed, 2 skipped. Two subsequently added Schwab timeout/cancellation cases also pass; the focused final suite is 11 passed. Trade-TF full suite: 57 passed, 8 skipped. Skipped tests are not counted as verified.

The actual Trade-TF HTTP client was exercised against the Engine ASGI app and paper broker: preview, explicit approval, execution, receipt, cumulative fill status and repeat request without a second order. No real broker connection was used. Logs are included.

## Ticket status — do not bulk-close

| Ticket | Candidate evidence | Still open |
|---|---|---|
| 002 | Compatible authenticated Robinhood client and account router | Running authenticated host bridge, account/capability contract verification, existing chat-route smoke test |
| 003 | Exact preview/route binding, explicit approval, persistent claim | Production identity/authority issuance and any separately authorized unattended mandate |
| 004 | Canonical HTTP handoff and paper receipt/status integration | Individual fill-event export and ongoing Trade-TF lifecycle ingestion; full cross-runtime ownership integration |
| 005 | Existing typed refinement retained; provenance reaches coordinator | Actual Algo-TF service output-to-order/exit compilation and deployed service integration |
| 006 | REFINE_UNAVAILABLE retained and tested; missing order IDs cannot report success | Live timeout/recovery evidence, broker-specific rejection reconciliation |
| 007 | Persistent registry called by autonomy coordinator; conflict test passes | Migration of all real Gatekeeper cards to explicit versions; cross-agent registry adoption |

## Material limits

This is not a complete autonomous execution release. Trade-TF's get_fills now explicitly reports that individual fill-event export is unsupported; get_status returns the Engine's broker-reported cumulative fill fields. There is no background consumer connecting those fields to all Trade-TF lifecycle state transitions. The coordinator still stops at WAITING_APPROVAL. Sumit's autonomous trader retains its existing paper-only execution ownership; it was not promoted to live or joined to a shared strategy-signal ownership queue.

The Drive-only standalone paper ledger was not spliced into the newer GitHub account model: both have different state semantics. Original sources remain in the archive for a deliberate ledger migration. Paper account-equity simulation is not evidence of real broker balances.

Unknown submissions require reconciliation before operator recovery. Do not clear submission_claims to retry an ambiguous order. No automatic claim-reset endpoint is included. Cancellation support is adapter-dependent; paper orders fill synchronously and have no pending order to cancel. Multi-leg/TWAP/VWAP child submission and restart semantics were not certified by the new single-order claim tests.

No broker credentials or flags were changed. No deployment, Mac Drive transfer, email delivery, native Recursor queue update or live-broker smoke test is claimed. The included recursor-status.json is an importable status report, not evidence of queue ingestion.

## Apply and rollback

1. Preserve the running code, environment and database; inspect the patch against the exact base commit.
2. Apply engine-reconciliation.patch at the repo root. Deploy Engine and the matching Trade-TF package together in a test environment. The cross-project contract test expects sibling Execution-Engine and Trade-TF directories.
3. Run normal init_db with all application models imported. The change adds preview_bindings and submission_claims tables without altering existing order columns. Existing previews must be regenerated and approved.
4. Configure Trade-TF EXECUTION_ENGINE_API_KEY and a durable TRADER_TF_STRATEGY_REGISTRY_PATH. For Robinhood, configure ROBINHOOD_BRIDGE_URL, ROBINHOOD_BRIDGE_TOKEN and explicit account credential_profile. Set MARKET_DATA_BROKER if more than one live broker is configured. Live flags remain an independent deployment choice.
5. Resolve the open acceptance items before calling this a unified release or closing tickets.

Rollback must include inspection of in-flight orders and submission claims first. Reverting to old code while an unknown submission exists would remove duplicate-submission protection. Retain database backups and the original ZIPs; do not blindly delete the new tables or restore stale order state.
