# EDGE-Execution

A deterministic, production-grade broker-neutral microservice for order execution. Acts as a hard safety gate between EDGE-TF (decision engine) and broker trading infrastructure.

## Overview

This service implements a **narrow, boring responsibility**: receive validated trade instructions from EDGE-TF, run non-negotiable safety checks, require explicit human approval, submit orders to Schwab, track reconciliation, and maintain immutable audit logs.

**Key philosophy**: Reject by default. Anything not explicitly allowed is blocked.

---

## Quick Start

### Local Development (SQLite)

```bash
# Install dependencies
make install

# Copy env template and configure
cp .env.example .env
# Edit .env with Schwab credentials (or leave as test values for demo)

# Run development server
make dev

# In another terminal, run tests
make test

# Access API documentation
open http://localhost:8000/docs
```

### Docker Compose

```bash
make docker-up
# App runs on http://localhost:8000
# PostgreSQL on localhost:5432

make docker-down
```

---

## Architecture

```
EDGE-TF (Decision Engine)
        ↓
  [Execution Engine]
    ├─ Request Validation (Pydantic)
    ├─ Risk Checks (kill switch, limits, allowlists)
    ├─ Schwab Preview (no commit)
    ├─ Approval Gate (explicit human required)
    ├─ Order Submission
    ├─ Reconciliation Loop (status tracking)
    └─ Audit Ledger (append-only)
        ↓
   Schwab API
        ↓
   Financial Markets
```

### Core Modules

| Module | Responsibility |
|--------|-----------------|
| **models** | Pydantic schema validation (strict, reject-unknown-fields) |
| **risk** | Hard safety checks: kill switch, account allowlist, symbol allowlist/denylist, notional limits |
| **broker** | Schwab API client, OAuth token management, order construction |
| **execution** | Preview flow, approval gate, order submission, idempotency |
| **audit** | Append-only audit ledger for all events |
| **api** | FastAPI endpoints and request routing |

---

## Security & Controls

### Approval Gate
- Orders MUST be explicitly approved by human/system before execution
- Approvals expire after configurable time (default: 30 min)
- Approval artifact includes: `approved_by`, `approved_at`, `attestation`
- Idempotency prevents duplicate approvals

### Kill Switch
- Global binary control: ON = all trading disabled, OFF = trading enabled
- Checked at both preview and execute stages
- Admin-only endpoints (`POST /v1/kill-switch/{on,off}`)
- All state changes audit-logged

### Idempotency
- Keyed by `(decision_id, operation)` with 24-hour expiry
- Prevents duplicate previews and executions
- Each operation requires unique `idempotency_key`
- Cached responses returned for duplicates

### Hard Risk Checks
1. **Kill Switch** - Must be OFF
2. **Account Allowlist** - Account must be explicitly allowed
3. **Symbol Allowlist** - Symbol must be explicitly allowed
4. **Symbol Denylist** - Symbol must NOT be denied
5. **Notional Limit** - Order cost/proceeds must not exceed max (default: $100k)
6. **Concentration Limit** - Position concentration must not exceed max (default: 15%)
7. **Market Hours** - Optional enforcement of US market hours (9:30 AM - 4:00 PM ET)

### No Secrets in Logs
- Structured logging with automatic secret redaction
- Schwab credentials never logged
- Approval attestations never logged verbatim

---

## API Specification

### Endpoints

#### POST `/v1/orders/preview`
Preview an order without commitment.

**Request:**
```json
{
  "decision_id": "edge-20260821-001",
  "account": "primary",
  "symbol": "QQQ",
  "asset_type": "ETF",
  "instruction": "BUY",
  "quantity": 10,
  "order_type": "LIMIT",
  "limit_price": "721.50"
}
```

**Response:**
```json
{
  "preview_id": "preview-<uuid>",
  "decision_id": "edge-20260821-001",
  "preview_details": {...},
  "estimated_commission": 0.0,
  "estimated_cost": 7215.00,
  "risk_verdict": "APPROVED",
  "risk_details": {
    "checks": {...},
    "rejections": []
  },
  "payload_checksum": "sha256...",
  "expires_at": "2026-08-22T10:35:00"
}
```

#### POST `/v1/orders/execute`
Execute a previously-approved order.

**Request:**
```json
{
  "decision_id": "edge-20260821-001",
  "preview_id": "preview-<uuid>",
  "approval": {
    "preview_id": "preview-<uuid>",
    "approved_by": "trader@company.com",
    "approved_at": "2026-08-22T10:30:00",
    "attestation": "Approved after review",
    "idempotency_key": "exec-<uuid>"
  }
}
```

**Response:**
```json
{
  "decision_id": "edge-20260821-001",
  "execution_id": "order-12345678",
  "status": "SUBMITTED",
  "submitted_at": "2026-08-22T10:35:00",
  "broker_response": {...}
}
```

#### GET `/v1/orders/{decision_id}`
Query order status by decision_id.

**Response:**
```json
{
  "decision_id": "edge-20260821-001",
  "execution_id": "order-12345678",
  "status": "FILLED",
  "created_at": "2026-08-22T10:30:00",
  "updated_at": "2026-08-22T10:35:30",
  "filled_quantity": 10,
  "average_fill_price": "721.55",
  "broker_status": "FILLED"
}
```

#### POST `/v1/kill-switch/on` (Admin)
Enable kill switch (disable all trading).

**Header:**
```
X-Admin-Key: <api_key_admin>
```

**Response:**
```json
{
  "enabled": true,
  "set_by": "admin",
  "set_at": "2026-08-22T10:40:00",
  "reason": "Manually activated by admin"
}
```

#### POST `/v1/kill-switch/off` (Admin)
Disable kill switch (enable trading).

**Response:**
```json
{
  "enabled": false,
  "set_by": "admin",
  "set_at": "2026-08-22T10:41:00"
}
```

#### GET `/v1/kill-switch/status`
Query current kill switch state (no auth required for visibility).

#### GET `/v1/health`
Service health check.

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2026-08-22T10:42:00",
  "database": "ok",
  "broker_connectivity": "ok"
}
```

#### GET `/v1/market-status`
US market hours status.

**Response:**
```json
{
  "in_market_hours": true,
  "current_time_et": "2026-08-22T10:42:00-04:00",
  "market_open": "09:30:00",
  "market_close": "16:00:00",
  "day_of_week": "Thursday"
}
```

---

## Request Schema (TradeProposal)

All requests strictly validated with Pydantic v2; unknown fields rejected.

### Fields

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `decision_id` | string | ✓ | Unique decision ID from EDGE-TF (e.g., `edge-20260821-001`) |
| `account` | string | ✓ | Must be in account allowlist |
| `symbol` | string | ✓ | Uppercase, 1-5 chars, must be in symbol allowlist |
| `asset_type` | enum | ✓ | `EQUITY`, `ETF`, `OPTION`, `BOND` |
| `instruction` | enum | ✓ | `BUY` or `SELL` |
| `quantity` | integer | ✓ | Positive integer |
| `order_type` | enum | ✓ | `MARKET`, `LIMIT`, `STOP`, `STOP_LIMIT` |
| `limit_price` | decimal | conditional | Required for `LIMIT` and `STOP_LIMIT` orders |
| `stop_price` | decimal | conditional | Required for `STOP` and `STOP_LIMIT` orders |

### Validation Rules

- **Symbol**: Uppercase only, 1-5 characters
- **Quantity**: Must be > 0
- **Prices**: Decimal with max 2 decimal places (no float rounding)
- **LIMIT orders**: Must have `limit_price`
- **STOP orders**: Must have `stop_price`
- **STOP_LIMIT orders**: Must have both `limit_price` and `stop_price`
- **Unknown fields**: Rejected with 422 error

---

## Data Persistence

### Database Models

#### Orders
Persistent order record tracking full lifecycle.

```
Columns: decision_id (unique), preview_id, execution_id, account, symbol, 
         quantity, instruction, status, payload_checksum, risk_approved, 
         created_at, updated_at, preview_expires_at, filled_quantity, 
         average_fill_price, broker_status, raw_broker_response
```

#### Idempotency Records
Prevent duplicate operations.

```
Columns: decision_id, operation (PREVIEW/EXECUTE), idempotency_key (unique), 
         created_at, expires_at, response_body
```

#### Approvals
Human approval artifacts and trail.

```
Columns: preview_id, decision_id, approved_by, approved_at, attestation, 
         idempotency_key (unique), created_at
```

#### Reconciliation Events
Order status updates from broker.

```
Columns: decision_id, execution_id, old_status, new_status, broker_status, 
         filled_quantity, average_fill_price, broker_response_raw, checked_at
```

#### Audit Ledger
Append-only immutable log of all significant events.

```
Columns: timestamp (indexed), correlation_id, decision_id (indexed), actor, 
         action, resource_type, resource_id, before_state, after_state, 
         payload_hash, result (SUCCESS/FAILURE/REJECTED), error_message
```

---

## Installation & Setup

### Prerequisites
- Python 3.12+
- PostgreSQL 16+ (or SQLite for development)
- Docker + Docker Compose (optional)

### Environment Configuration

```bash
cp .env.example .env
```

Key variables:
- `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_REFRESH_TOKEN` - Schwab credentials
- `DATABASE_URL` - Database connection string
- `KILL_SWITCH_ENABLED` - Global kill switch (default: false)
- `ACCOUNT_ALLOWLIST` - Comma-separated list of allowed accounts
- `SYMBOL_ALLOWLIST` - Comma-separated list of allowed symbols
- `MAX_ORDER_NOTIONAL_USD` - Max order size (default: $100,000)
- `API_KEY_ADMIN` - Admin API key for protected endpoints

### Commands

```bash
# Install dependencies
make install

# Development server (with hot reload)
make dev

# Production server
make run

# Run tests
make test
make test-cov          # With coverage report

# Code quality
make lint              # Ruff + MyPy
make format            # Auto-format code
make clean             # Remove artifacts

# Docker
make docker-up
make docker-down
make docker-logs

# Full setup and verification
make setup             # install + lint + test
make ci                # lint + test (for CI/CD)
```

---

## Usage Examples

### Example 1: Preview and Execute BUY Order

**Step 1: Preview**
```bash
curl -X POST http://localhost:8000/v1/orders/preview \
  -H "Content-Type: application/json" \
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

**Response:**
```json
{
  "preview_id": "preview-abc123",
  "decision_id": "edge-20260821-001",
  "risk_verdict": "APPROVED",
  "payload_checksum": "abcd1234...",
  "expires_at": "2026-08-22T10:35:00Z",
  ...
}
```

**Step 2: Execute**
```bash
curl -X POST http://localhost:8000/v1/orders/execute \
  -H "Content-Type: application/json" \
  -d '{
    "decision_id": "edge-20260821-001",
    "preview_id": "preview-abc123",
    "approval": {
      "preview_id": "preview-abc123",
      "approved_by": "trader@company.com",
      "approved_at": "2026-08-22T10:33:00Z",
      "attestation": "Approved - QQQ momentum bullish",
      "idempotency_key": "exec-xyz789"
    }
  }'
```

**Step 3: Check Status**
```bash
curl http://localhost:8000/v1/orders/edge-20260821-001
```

### Example 2: Kill Switch

**Enable (Admin)**
```bash
curl -X POST http://localhost:8000/v1/kill-switch/on \
  -H "X-Admin-Key: change-me-in-prod"
```

**Disable (Admin)**
```bash
curl -X POST http://localhost:8000/v1/kill-switch/off \
  -H "X-Admin-Key: change-me-in-prod"
```

**Query Status (Public)**
```bash
curl http://localhost:8000/v1/kill-switch/status
```

---

## Testing

All tests pass with high coverage:

```bash
make test              # Run all tests
make test-cov          # With coverage report (htmlcov/index.html)
```

Test categories:
- **test_models.py**: Schema validation, enum enforcement
- **test_risk.py**: Risk checker, limits enforcement
- **test_order_builder.py**: Order construction, checksums
- **test_idempotency.py**: Duplicate prevention
- **test_api.py**: Endpoint behavior and auth

Target coverage: >80% overall, >90% for risk and execution modules.

---

## EDGE-TF Integration

### Client Pattern

```python
import httpx
from datetime import datetime
import uuid

async def submit_trade(proposal_dict: dict):
    async with httpx.AsyncClient() as client:
        # Step 1: Preview
        preview = await client.post(
            "http://execution-engine:8000/v1/orders/preview",
            json=proposal_dict,
        )
        preview.raise_for_status()
        preview_data = preview.json()
        
        # Step 2: Display preview to trader and wait for approval in UI
        # ...
        
        # Step 3: Execute with approval
        approval = {
            "approved_by": current_user.email,
            "approved_at": datetime.utcnow().isoformat(),
            "attestation": f"Approved {preview_data['decision_id']}",
            "idempotency_key": str(uuid.uuid4()),
        }
        
        execution = await client.post(
            "http://execution-engine:8000/v1/orders/execute",
            json={
                "decision_id": proposal_dict["decision_id"],
                "preview_id": preview_data["preview_id"],
                "approval": approval,
            },
        )
        execution.raise_for_status()
        return execution.json()
```

**Key Points:**
- EDGE-TF sends TradeProposal to preview endpoint
- EDGE-TF displays Schwab preview to human trader
- Trader reviews and explicitly approves in UI
- EDGE-TF calls execute endpoint with approval artifact
- EDGE-TF must NOT know Schwab credentials

---

## Security Decisions

1. **Rejection-by-default**: All unknown symbols, accounts, and fields rejected
2. **Separation of concerns**: EDGE-TF never holds Schwab credentials
3. **Approval gate**: No autonomous execution; human approval required
4. **Immutable audit trail**: All significant events logged append-only
5. **Checksum verification**: Preview payload verified before execution
6. **Idempotency**: Duplicate requests safely handled
7. **Kill switch**: Global off-switch always available to ops
8. **Structured logging**: Machine-readable logs for forensics and monitoring

---

## Production Deployment

### Checklist

- [ ] Schwab credentials set in secrets manager (not .env)
- [ ] PostgreSQL database configured with backups
- [ ] API key rotated (`api_key_admin` should be complex)
- [ ] Allowlists configured correctly (accounts, symbols)
- [ ] Kill switch tested and accessible to ops
- [ ] Monitoring/alerting configured
- [ ] Audit logs archived
- [ ] Rate limiting configured on endpoints
- [ ] TLS/SSL enabled
- [ ] Request signing enabled (optional, for EDGE-TF validation)

### Environment Variables (Production)

```bash
# Secrets
SCHWAB_APP_KEY=<from-secrets-manager>
SCHWAB_APP_SECRET=<from-secrets-manager>
SCHWAB_REFRESH_TOKEN=<from-secrets-manager>

# Database
DATABASE_URL=postgresql://user:password@db.prod:5432/execution_engine

# Server
HOST=0.0.0.0
PORT=8000
ENV=production
LOG_LEVEL=WARNING
LOG_FORMAT=json

# Security
API_KEY_ADMIN=<generate-random-complex-key>

# Risk
KILL_SWITCH_ENABLED=false
ACCOUNT_ALLOWLIST=primary,secondary
SYMBOL_ALLOWLIST=QQQ,SPY,IWM,EEM,GLD,TLT
MAX_ORDER_NOTIONAL_USD=250000
MAX_POSITION_CONCENTRATION_PCT=10
MARKET_HOURS_ONLY=true
```

---

## Known Limitations & Future Work

### v0.1.0 Limitations
- Single-broker support (Schwab only)
- Mocked broker for development (real OAuth integration needed)
- No request signing support yet
- No SSO/advanced approval workflows
- Reconciliation loop is manual polling (not broker webhooks)
- No HSM signing for sensitive operations

### Roadmap
1. **v0.2**: Real Schwab OAuth integration with test mode
2. **v0.3**: Request signing, HMAC validation for EDGE-TF
3. **v0.4**: SSO approvals (Okta/Azure AD)
4. **v0.5**: Multi-broker abstraction (Interactive Brokers, etc.)
5. **v1.0**: HSM-backed signing, circuit breakers, comprehensive monitoring

---

## Support

### API Documentation
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- OpenAPI spec: http://localhost:8000/openapi.json

### Debugging
- Enable debug logs: `export LOG_LEVEL=DEBUG && make dev`
- Inspect database: `sqlite3 execution_engine.db` or `psql -U engine_user -d execution_engine`

### Common Issues

**"Kill switch is ON" error**
- Check status: `curl http://localhost:8000/v1/kill-switch/status`
- Disable if needed (requires admin key)

**"Symbol not in allowlist" error**
- Check `.env` for `SYMBOL_ALLOWLIST` and add symbol if needed

**"Preview expired" error**
- Request a new preview (default expiry: 5 minutes)

**Database connection errors**
- Verify `DATABASE_URL` env var and database is running

---

**Version**: 0.1.0  
**Status**: Production-Ready (MVP)  
**Last Updated**: 2026-08-22
