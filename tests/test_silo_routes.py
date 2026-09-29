"""
Tests for src/api/silo_routes.py — the /v1/silo/* admin API. Covers the
HTTP layer only (admin-key gating, request/response shape); the mandate
logic itself (validator, HMAC, evaluate_candidate) is covered in
tests/test_silo_runtime.py.
"""
import httpx
import pytest

import src.api.silo_routes as silo_routes_module
from src.execution.silo_runtime import SiloRuntime


def _mandate_body(mode="AUTONOMOUS_PAPER", **overrides):
    body = dict(
        silo_id="test-silo", armed_by="test", mode=mode, timezone="UTC", runtime_end="23:59",
        trades_per_minute=10, max_per_trade_usd="1000", max_concentration_pct="100",
        authorized_symbols=["QQQ"], authorized_strategy_ids=["golden_cross"],
        decision_bands={"execute_above": 0.7, "resize_above": 0.5},
    )
    body.update(overrides)
    return body


@pytest.fixture
def isolated_silo(monkeypatch, tmp_path, test_settings):
    """Points silo_routes.SiloRuntime at a tmp_path-scoped file, and its get_settings() at the same test_settings the app itself uses."""
    runtime = SiloRuntime(str(tmp_path / "silo-state.json"))
    monkeypatch.setattr(silo_routes_module, "SiloRuntime", lambda *a, **k: runtime)
    monkeypatch.setattr(silo_routes_module, "get_settings", lambda: test_settings)
    return runtime


async def _client(app_with_test_db):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app_with_test_db), base_url="http://test")


@pytest.mark.asyncio
async def test_status_requires_admin_key(app_with_test_db, isolated_silo):
    async with await _client(app_with_test_db) as client:
        response = await client.get("/v1/silo/status")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_arm_requires_admin_key(app_with_test_db, isolated_silo):
    async with await _client(app_with_test_db) as client:
        response = await client.post("/v1/silo/arm", json=_mandate_body())
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_arm_and_status_roundtrip_for_a_paper_mandate(app_with_test_db, isolated_silo, test_settings):
    headers = {"x-admin-key": test_settings.api_key_admin}
    async with await _client(app_with_test_db) as client:
        arm_response = await client.post("/v1/silo/arm", json=_mandate_body(), headers=headers)
        status_response = await client.get("/v1/silo/status", headers=headers)

    assert arm_response.status_code == 200
    assert status_response.status_code == 200
    body = status_response.json()
    assert body["armed"] is True
    assert body["mandate"]["silo_id"] == "test-silo"


@pytest.mark.asyncio
async def test_arm_and_status_roundtrip_for_a_live_mandate_verifies_via_admin_key(app_with_test_db, isolated_silo, test_settings):
    headers = {"x-admin-key": test_settings.api_key_admin}
    live_body = _mandate_body(
        mode="AUTONOMOUS_LIVE", broker="robinhood", account_alias="robinhood_live",
        live_execution_authorized=True, approved_algos=["algo-1"],
    )
    async with await _client(app_with_test_db) as client:
        arm_response = await client.post("/v1/silo/arm", json=live_body, headers=headers)
        status_response = await client.get("/v1/silo/status", headers=headers)

    assert arm_response.status_code == 200
    assert arm_response.json()["mandate"]["mandate_hmac"]  # stamped by /arm
    assert status_response.status_code == 200
    assert status_response.json()["armed"] is True


@pytest.mark.asyncio
async def test_arm_rejects_a_live_mandate_missing_required_fields(app_with_test_db, isolated_silo, test_settings):
    headers = {"x-admin-key": test_settings.api_key_admin}
    async with await _client(app_with_test_db) as client:
        # mode=AUTONOMOUS_LIVE but none of the live-only fields set
        response = await client.post("/v1/silo/arm", json=_mandate_body(mode="AUTONOMOUS_LIVE"), headers=headers)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_disarm_requires_admin_key(app_with_test_db, isolated_silo):
    async with await _client(app_with_test_db) as client:
        response = await client.post("/v1/silo/disarm")
    assert response.status_code == 403
