# EDGE Trading dashboard — existing-site review

**Status: audit only. No product code changed as part of this document.**
Scope: `dashboard/index.html` (the entire frontend — one static file, no build
step, no framework, no server-side routing) plus the backend endpoints it
calls in `src/api/server.py` and friends.

## Method

Static read of `dashboard/index.html` end to end, cross-checked against the
backend routes/models it calls. No frontend test suite exists to run (see
"Test coverage" note below), so "current behavior" here is asserted from
reading the code paths directly, not from an automated report. Where this
document says a specific scenario was live-verified, that was manual
browser testing done while building the feature in question, not a
repeatable test.

**Test coverage, honestly stated up front:** there is no automated test
tooling for the frontend anywhere in this repo — no `package.json`, no
Jest/Playwright/Vitest config, nothing under `dashboard/` but `index.html`,
`robots.txt`, `sitemap.xml`. Every row's "Test coverage" below is either
"None (manual only)" or, where the feature calls a real backend endpoint,
that endpoint's own pytest coverage — which tests the API contract, not
that the dashboard calls it correctly or renders the response right.

---

## 1. Feature → implementation map

| Feature | UI location | State source | API call(s) | Test coverage |
|---|---|---|---|---|
| Terminal tab | `data-tab-panel="terminal"` | — (layout only) | — | None (manual only) |
| Market Scanner tab | `data-tab-panel="scanner"` | `sectorPct`, `marqueePct` (in-memory, reset on reload) | none — fully simulated, see §"Paper Trading & simulation" below | None (manual only) |
| Strategies & Algos tab | `data-tab-panel="strategies"` | — (layout only) | — | None (manual only) |
| Journal & Risk tab | `data-tab-panel="journal"` | — (layout only) | — | None (manual only) |
| Paper Trading (order execution) | header pill (`#modePillMain`), Terminal | `localStorage['edge.paperState.v1']` (`paperState` object: balance, positions, orders, quotes) | Intercepted client-side in `api()` before any fetch: `demoPreviewOrder()`/`demoExecuteOrder()` for `/v1/orders/preview`\|`/v1/orders/execute`; `DEMO_RESPONSES` table answers every other GET locally | None (manual only). Root cause of finding #1/#2/#3 below lives entirely in this interception table |
| Simulated quotes | Terminal (sizing calculator, scalper bar), Market Scanner | `paperState.quotes` (order-fill pricing) and a **separate** in-memory `l2MidPrice` map (DOM ladder) — two independent random walks, deliberately not unified (see code comment above `renderL2()`) | `demoQuote()` intercepts `GET /v1/quotes` when in Paper Trading | None (manual only) |
| Level 2 / DOM | Terminal, next to chart | `l2MidPrice` (in-memory only, not persisted) | none — always simulated, labeled "🧪 Simulated depth — no live order-book feed connected" regardless of Paper/Live mode (there is no real L2 endpoint in this backend at all yet) | None (manual only) |
| TradingView charting | Terminal, `#tvChartContainer` | third-party TradingView widget, symbol from `#chartSymbol` | none (client-side embed, no backend call) | None (manual only) |
| Order preview & confirmation | Terminal, "New order" card + approval modal | `lastPreview` (in-memory) | `POST /v1/orders/preview`, `POST /v1/orders/execute` (both intercepted in Paper Trading) | `tests/test_brokers.py`, `tests/test_config_schwab_wiring.py` cover the **backend** order/broker path; the dashboard's call of it has no test |
| Positions & Activity | Terminal | server response / `paperState` in Paper Trading | `GET /v1/account/{account}/positions`, `GET /v1/orders?...&limit=12` | Backend: `tests/test_brokers.py`. Dashboard rendering: none |
| Bracket orders | Terminal, "Bracket orders" card | server response (real accounts) — **not simulated** in Paper Trading | `GET /v1/orders/bracket`, `POST /v1/orders/bracket/attach`, `POST /v1/orders/bracket/{id}/cancel` | Backend: none found under `tests/` specific to this dashboard's call shape. See finding #3 |
| Watchlists | Terminal, "Watchlists" card | server response (real accounts); empty in Paper Trading | `GET /v1/watchlists`, `POST /v1/watchlists/{list}/items`, `DELETE /v1/watchlists/{list}/items/{symbol}` | None found for this route's dashboard usage. See finding #1 |
| Price alerts | Terminal, "Price alerts" card | server response (real accounts); empty in Paper Trading | `GET /v1/alerts?active_only=true`, `POST /v1/alerts`, `DELETE /v1/alerts/{id}` | None found for this route's dashboard usage. See finding #1 |
| Automated trading agent | Strategies & Algos, "My trading agent" | server response (`dailyPlanState`); refused in Paper Trading | `GET /v1/autonomous/plan`, `POST /v1/autonomous/start`, `POST /v1/autonomous/disarm` | Backend: `src/execution/daily_plan.py` has behavior covered indirectly via other suites; the dashboard's start/stop UI has no test. See finding #2 |
| Strategy signals | Strategies & Algos, "Suggested trades" | server response | `GET /v1/strategies/signals?status=PENDING`; empty list in Paper Trading | None (manual only) |
| Strategy performance cards | Strategies & Algos, "How these strategies have performed" | `readLiveUpdatesCache()` (localStorage, 3h TTL) or `demoLiveUpdates()` in Paper Trading | `POST /v1/backtest/run` (real), fabricated locally when in Paper Trading | Backend: `src/backtest/` has its own suite. See finding #6 |
| Transaction history & CSV export | Journal & Risk | server response / `paperState.orders` | `GET /v1/orders?{filters}` | None (manual only) |
| PnL calendar (win/loss heatmap) | Journal & Risk | derived client-side from the same `/v1/orders` response | (shares the call above) | None (manual only) |
| Trading controls (kill switch) | Journal & Risk | server response; refused in Paper Trading | `GET /v1/kill-switch/status`, `POST /v1/kill-switch/on`\|`/off` | Backend: `src/execution/kill_switch_state.py` (97% coverage per last full run). Dashboard integration: none |
| Connection settings | Journal & Risk, "Connection" card | `localStorage['edge.baseUrl']`, `localStorage['edge.account']`; admin key in `sessionStorage['edge.adminKey']` | none directly — these values are read by every other call | Config parsing: `tests/test_cors_config.py`, `tests/test_config_schwab_wiring.py` (backend-side only) |

---

## 2. The nine findings

### 1. Watchlists/price alerts "fail" in Paper Trading

**Root cause.** Not a bug in the sense of hitting a wrong URL — `GET` on
both is already intercepted and answered locally (empty lists), so viewing
them works. `POST`/`DELETE` (add a symbol, set an alert) are **not** in
`DEMO_RESPONSES`; they fall through to `api()`'s generic non-GET refusal:
> "Paper Trading — this action isn't simulated yet. Connect your live API
> (top-right) to do this for real."

This is deliberate (see the comment above `DEMO_RESPONSES`: only
order preview/execute are genuinely simulated; nothing else is faked
against sample data). The finding is real as a **UX** problem, though: the
refusal only appears as a toast that self-dismisses in ~2.6s, and the
watchlist/alert forms give no indication beforehand that Paper Trading
won't accept the action.

**Smallest safe fix.** Don't simulate watchlists/alerts (that's a bigger,
separate feature). Instead, disable the "Add to list" / "Set alert"
buttons while in Paper Trading and replace them with inline copy explaining
why ("Connect your live API to save watchlists and alerts"), matching the
pattern already used elsewhere (e.g., the bracket-toggle field hides itself
on SELL). No new API surface, no behavior change to what actually happens —
just stops the user from taking an action guaranteed to be refused.

**Files that would change.** `dashboard/index.html` only: the two card
templates (add `id`s for a disabled-state wrapper) and `refreshDemoUi()` or
a new small helper called alongside it to toggle those two forms.

**Tests to add.** None possible today (no frontend test runner). If one is
ever added, the two extra "form" test files to see: `#wlAddBtn` disabled
state at `effectiveDemo() === true`, `#alertAddBtn` same.

**Open question.** Should watchlists/alerts eventually become genuinely
Paper-Trading-simulated (persisted in `paperState`, like orders are), or
should they stay real-only forever? That's a scope decision, not a bug fix.

---

### 2. Agent doesn't visibly start on "Start trading"

**Root cause.** `$('startAgentBtn')`'s handler calls `POST
/v1/autonomous/start`, which is **not** in `DEMO_RESPONSES` and has no
Paper Trading special-case (unlike order preview/execute). In Paper
Trading it hits the same generic refusal as finding #1, surfaced only via
`toast('Could not start: ' + e.message)`. The button's own busy state
(`"Checking recent performance…"`) reverts a moment later with no other
visible trace, so it can look like nothing happened at all rather than
like a deliberate refusal.

**Smallest safe fix.** Same shape as finding #1: when
`effectiveDemo()` is true, replace the "Start trading" button with a short
explanation card ("The automated agent trades against a real account only
— connect your live API to use it") instead of letting the click reach
`api()` and fail. This is more important than #1 because the current
failure mode (button flashes busy, then reverts, generic toast) reads as
broken rather than as "not available in this mode."

**Files that would change.** `dashboard/index.html`: `#startAgentForm`
card markup + the `startAgentBtn` click handler (guard clause at the top,
mirroring the symbol/quantity validation pattern already used in
`previewBtn`'s handler).

**Tests to add.** None possible today; same caveat as #1.

**Open question.** None — this one's unambiguous once explained; it just
needs an inline explanatory state, not a design decision.

---

### 3. Some trading-control actions aren't simulated in Paper Trading

**Root cause.** This is the general case of #1/#2: kill-switch on/off,
watchlist add/remove, alert add/remove, and bracket-attach/cancel are all
real mutations with no Paper Trading path. This was a deliberate scope
boundary set when Paper Trading's order-execution engine was built (see
the comment block above `demoPreviewOrder`/`demoExecuteOrder`): "the
simulator only goes as far as orders, so nothing else here can be mistaken
for a real action succeeding."

**Smallest safe fix.** Apply the same "disable + explain" treatment from
#1/#2 to the kill switch toggle in Journal & Risk. This is the one place
worth prioritizing beyond watchlists/alerts, because unlike a watchlist a
kill-switch click looks like a real safety control — a user reasonably
expects clicking it to do *something* in a practice environment.

**Files that would change.** `dashboard/index.html`: the "Trading
controls" card + `$('ksToggle')`'s click handler.

**Tests to add.** None possible today.

**Open question.** Should the kill switch specifically become simulated
(toggle a `paperState.killSwitchOn` flag that the scalper bar and order
ticket actually respect)? That would make Paper Trading a more complete
practice environment, but it's new scope, not a fix — flagging for a
decision, not doing it here.

---

### 4. "All systems working" can show while features are unavailable

**Root cause.** `refreshHealth()` reports exactly one thing: whether
`GET /v1/health` returns `{"status": "healthy"}` — real backend liveness,
nothing else. It has never claimed anything about Paper Trading, the
admin key, or any specific feature's availability. The label "All systems
working" is broader than what's actually measured, and it sits in the same
header row as the Paper Trading pill, inviting a reasonable reader to
assume the two are related ("the system says everything's fine, so why did
my alert fail?").

**Smallest safe fix.** Rename the label to something scoped to what it
actually checks — e.g. "Server reachable" / "API online" — rather than
"All systems working." Purely a copy change, zero logic change.

**Files that would change.** `dashboard/index.html`: two string literals
inside `refreshHealth()`.

**Tests to add.** None possible today.

**Open question.** None.

---

### 5. Chart and order-ticket symbols can disconnect

**Root cause.** `#chartSymbol` (the chart/scalper-bar/Level-2 symbol) and
`#fSymbol` (the order ticket's own symbol field) are two independent
`<input>` elements with no synchronization after initial page load —
confirmed in code: `initChart()` seeds `#chartSymbol` from `#fSymbol`'s
value **once**, and nothing keeps them in sync afterward. This was a known
trade-off made explicitly when the scalper bar was built (see that
session's own reasoning: syncing them bidirectionally was deferred as
out-of-scope/risk at the time) and got a second source of truth added
later (the Market Scanner's "click a tile to chart it" also only sets
`#chartSymbol`). Today a user can, for example, chart NVDA, then place an
order that still says QQQ in the ticket, with no visual cue they've
diverged.

**Smallest safe fix.** One-way sync only, on the lower-risk direction:
when `#chartSymbol` changes (chart Load button, scalper qty resolution,
heatmap tile click), also update `#fSymbol` to match, since the scalper
bar and heatmap already implicitly assume "the charted symbol is the one
you're trading." Do **not** sync the other direction (typing a new
`#fSymbol` mid-ticket-fill should not yank the chart out from under
someone reading it) — that asymmetry needs to be a documented, deliberate
choice, not another silent gap.

**Files that would change.** `dashboard/index.html`: `chartLoadBtn`'s
click handler, `chartSymbolFromScanner()`, and the scalper bar's symbol
resolution — three call sites, one extra line each.

**Tests to add.** None possible today.

**Open question.** Confirm the one-way direction above is actually what's
wanted before implementing — the alternative (fully bidirectional sync) is
a bigger behavior change and changes what "the order ticket" means (no
longer independently settable while a chart is up).

---

### 6. Strategy results say "illustrative" but not how they were produced

**Root cause.** `demoLiveUpdates()`'s data is clearly labeled
("Illustrative sample data — connect your live API...") but the
methodology behind either the illustrative numbers *or* the real
`/v1/backtest/run` results — lookback window (3 years), notional per trade
($1,000, matching `settings.autonomous_notional_per_trade_usd`), whether
commissions/slippage are modeled — only appears in a **transient loading
message** ("Testing your strategies against 3 years of real market
data…") that disappears once results load, and in code comments no user
ever sees. The persistent `#liveUpdatesMeta` text after loading says
nothing about provenance.

**Smallest safe fix.** Add one persistent line under the results (not just
during loading) stating the backtest window, notional, and cost
assumptions, sourced from the same constants already in the code
(`LIVE_UPDATES_NOTIONAL_USD`, the 3-year window used in the real
`/v1/backtest/run` call). Pure additive copy, no logic change.

**Files that would change.** `dashboard/index.html`: `renderLiveUpdates()`.

**Tests to add.** None possible today.

**Open question.** None — this is a copy addition, not a design decision.

---

### 7. "AI"/agent wording outpaces actual explainability & governance

**Root cause.** "My trading agent" is described as something you "hand
money to, it trades your best strategies until you stop it" — language
that reads as more autonomous/adaptive than what it actually is: a
deterministic rotation across a fixed strategy catalog
(`src/strategy/catalog.py`), armed/disarmed by explicit user action, with
no learning or self-modification. There's no dedicated "AI" feature in
this codebase today (the one that was proposed this session — an "AI Trade
Copilot" producing fabricated technical analysis — was declined rather
than built, precisely over this exact class of concern). So the gap isn't
a governance *control* that's missing; it's that the existing, honest,
rules-based system is described in language borrowed from a more
autonomous-sounding product category.

**Smallest safe fix.** Copy-only change: reword "My trading agent" 's
description to name what it actually does — e.g. "Automatically rotates
between your best-performing strategies from a fixed catalog, using rules
you can see under Strategies & Algos" — and drop "agent" framing that
implies independent judgment it doesn't have.

**Files that would change.** `dashboard/index.html`: one `<h2>` hint
string.

**Tests to add.** None possible today.

**Open question.** Is "agent" as a name for this feature staying (it's
used throughout: `agent_id`, `#startAgentForm`, `AgentExposureGuard`,
etc.) or should the *feature name itself* change too? That's a bigger
rename than a copy fix and would touch the backend's own vocabulary —
flagging, not deciding.

---

### 8. Client-side credentials & configurable execution-server settings

**Status: already reviewed and substantially fixed earlier this session**
(commit `aeb08e8`, pushed to `main`). Documenting current state rather than
re-finding the same issues:

- Admin key: moved from `localStorage` to `sessionStorage` with a one-time
  migration; cleared on tab/browser close, not shared cross-tab. Does not
  (cannot) defend against an XSS payload executing in the page right now —
  that needs a server-issued httpOnly cookie, which this static-file
  architecture has no way to provide.
- `#baseUrl` (the configurable execution-server address): now requires
  `https://` unless the host is localhost/a private LAN address, closing
  the plaintext-credential-over-the-network gap. Not a domain whitelist —
  self-hosting on a custom domain is this app's supported design.
- A real, confirmed stored-XSS bug (unescaped `formatSymbol()` output,
  reachable via a paper-traded order's symbol) was found and fixed in the
  same pass, along with watchlist/alert render paths.
- `x-admin-key` is only ever attached inside `api()`'s own
  `base() + path` fetch — never to an arbitrary URL — so there's no
  header-leak path to a wrong host distinct from the `#baseUrl` gate above.

**Remaining gap worth a follow-up, not found before:** the sector heatmap's
tile markup (`data-symbol="${symbol}"`, `dashboard/index.html` inside
`renderSectorHeatmap()`) doesn't run its `symbol` value through
`escapeHtml()`. Today `symbol` only ever comes from the hardcoded
`SECTOR_GROUPS` array (not user input), so this isn't currently
exploitable — but it's an inconsistency with the "escape everything that
reaches innerHTML" rule adopted in the XSS fix, and it's one edit away
from becoming a real gap if that array is ever made configurable.

**Smallest safe fix.** Wrap `symbol` with `escapeHtml()` in
`renderSectorHeatmap()`'s template, for consistency, even though it's not
exploitable today.

**Files that would change.** `dashboard/index.html`, one line.

**Tests to add.** None possible today.

**Open question.** None.

---

### 9. Accessibility: names and keyboard support

**Root cause, with concrete examples found in this pass:**

- **Real gap:** `.sector-tile` (Market Scanner heatmap) is a plain `<div>`
  with a `click` listener, not a `<button>` — not keyboard-focusable, no
  accessible role or name, unreachable via Tab and unusable via
  Enter/Space. This is the clearest genuine issue found.
- **Weak, not absent:** most icon-only controls (`✕` remove/cancel buttons
  on watchlist rows, price alerts, bracket orders, the cleared-plan
  button; `↻ Reset`) rely on a `title` attribute for their accessible
  name. `title` is an inconsistent fallback across screen readers and
  gives keyboard-only sighted users nothing (it's hover-only) — it should
  be `aria-label` (or visible text), not the sole mechanism.
- **Already correct:** the kill-switch toggle and the slide-to-execute
  range input both already have explicit `aria-label`s. The tab bar,
  scalper buttons, and `.seg` toggles are real `<button type="button">`
  elements, so they're keyboard-operable by default (Tab + Enter/Space
  already work); they just lack `aria-label`s beyond their own visible
  text, which is generally fine since the visible text *is* the accessible
  name for a real button.

**Smallest safe fix, in two parts (do them separately, not as one commit):**
1. Change every `title="..."` on an icon-only `<button>` to
   `aria-label="..."` using the exact same copy already there (zero
   wording decisions needed — the good copy already exists, it's just on
   the wrong attribute). Keep `title` too if a hover tooltip is still
   wanted; `aria-label` and `title` can coexist.
2. Convert `.sector-tile` from `<div>` to `<button type="button">` (or add
   `role="button" tabindex="0"` plus a `keydown` handler for Enter/Space if
   changing the tag risks the existing `flex-grow` tile-sizing CSS, which
   is written against a generic block element, not button's default
   styling — worth checking live before choosing between the two).

**Files that would change.** `dashboard/index.html`: the ~6 `title=`
attributes identified above, plus `renderSectorHeatmap()`'s tile template
and its CSS if the tag changes.

**Tests to add.** None possible today; if a frontend test runner is ever
added, an axe-core pass over the page would catch regressions here going
forward.

**Open question.** For the sector tiles specifically: `<button>` vs.
`div[role=button][tabindex=0]` — recommend trying `<button>` first live
and only falling back to the ARIA-role approach if the flex-grow sizing
breaks under a button's default styling.

---

## 3. Suggested sequencing

Smallest, least risky, most user-visible first:

1. **#4** (health-pill label) — one-line copy change, zero risk.
2. **#9 part 1** (`title` → `aria-label`) — copy-attribute change, zero
   behavior risk.
3. **#2** (agent start explanation) and **#1**/**#3** (watchlist/alert/
   kill-switch explanation) — same pattern, do together since they share
   one small "disabled + explained in Paper Trading" helper.
4. **#6** (methodology line) and **#7** (agent copy) — pure copy, no logic.
5. **#8 remainder** (escape the sector-tile symbol) — trivial, do whenever
   convenient.
6. **#5** (chart/ticket symbol sync) — needs the one-way-only decision
   confirmed first (see its open question).
7. **#9 part 2** (sector tile → real button) — needs a quick live check of
   whether it breaks the flex-grow tile sizing before committing to the
   approach.

Nothing above touches order execution, broker credentials, or account
state — all are dashboard-only copy/markup/small-handler changes.
