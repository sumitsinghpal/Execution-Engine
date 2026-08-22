# schwab-execution-engine

Deterministic, reject-by-default execution microservice for EDGE-TF trade decisions.

## Architecture

```text
EDGE-TF -> /v1/orders/preview -> risk checks -> Schwab preview -> immutable audit
UI human approval -> /v1/orders/execute -> re-check risk -> Schwab submit -> reconciliation
```

- EDGE-TF remains decision-only.
- Schwab credentials are only used in this service.
- No autonomous trade generation is implemented.

## Threat model summary

- API key gate on admin kill-switch endpoints.
- Strict schema validation (`extra=forbid`, enums, decimal price handling).
- Idempotency controls on preview/execute paths.
- Immutable audit ledger with payload hashes and correlation IDs.
- Secrets only from env vars (`SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_REFRESH_TOKEN`).

## Repository layout

- `src/api/server.py`: FastAPI server and routes
- `src/execution/`: preview/execute/idempotency/reconciliation/approval logic
- `src/risk/`: hard risk checks
- `src/broker/`: deterministic order builder + Schwab client (mock/live)
- `src/models/orders.py`: schemas and persistence models
- `src/audit/ledger.py`: append-only audit entries

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp .env.example .env
```

Run locally:

```bash
make run
```

Run with Docker:

```bash
docker compose up --build
```

## Environment variables

See `.env.example`.

## Commands

- `make run`
- `make test`
- `make lint`
- `make migrate`

## Execution API v1

- `POST /v1/orders/preview`
- `POST /v1/orders/execute`
- `GET /v1/orders/{decision_id}`
- `POST /v1/kill-switch/on`
- `POST /v1/kill-switch/off`
- `GET /v1/health`

Sample preview request:

```bash
curl -X POST http://localhost:8000/v1/orders/preview \
  -H 'content-type: application/json' \
  -d '{
    "decision_id": "edge-20260821-001",
    "account": "primary",
    "symbol": "QQQ",
    "asset_type": "ETF",
    "instruction": "BUY",
    "quantity": 10,
    "order_type": "LIMIT",
    "limit_price": "721.50"
  }'
```

Sample execute request:

```bash
curl -X POST http://localhost:8000/v1/orders/execute \
  -H 'content-type: application/json' \
  -d '{
    "decision_id": "edge-20260821-001",
    "preview_id": "<preview_id>",
    "idempotency_key": "idem-12345678",
    "approval": {
      "approved_by": "trader.one",
      "approved_at": "2026-08-21T15:10:00Z",
      "attestation": "I approve execution of this trade",
      "signature": "optional"
    }
  }'
```

Failure examples:

- kill switch ON => `RISK_REJECTED`
- malformed payload => `422` validation error
- expired preview => `PREVIEW_EXPIRED`

## EDGE-TF integration contract

1. EDGE-TF sends strict `TradeProposal` to `/v1/orders/preview`.
2. UI renders preview details and requires explicit human approval.
3. UI sends approval artifact with `decision_id` + `preview_id` to `/v1/orders/execute`.
4. EDGE-TF must not store Schwab credentials.

## Risk controls checklist

- [x] Kill switch gating
- [x] Account allowlist
- [x] Symbol allowlist/denylist
- [x] Max order notional
- [x] Max concentration limit
- [x] Optional market-hours policy
- [x] Idempotency table with (`decision_id`, `operation`)
- [x] Approval-required execution path
- [x] Reconciliation transition guardrails

## Known limitations / next steps

- Replace mock/stub OAuth and live Schwab adapters with production token refresh flow.
- Add external signer/HSM support for request signing.
- Add SSO-backed approval and stronger RBAC.
- Add multi-broker abstraction layer.
