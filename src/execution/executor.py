"""
Core execution orchestration: preview and execute flows.
"""

import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from sqlmodel import SQLModel, Session, select, Field

from src.broker.order_builder import OrderBuilder
from src.brokers.base import BrokerAdapter, BrokerAuthenticationError
from src.brokers.factory import build_broker_adapter
from src.config import get_settings
from src.execution.approval import ApprovalManager
from src.execution.idempotency import IdempotencyManager, Operation
from src.execution.kill_switch_state import KillSwitchService
from src.execution.symbol_coordination import SymbolCoordinationGuard
from src.logging_config import get_logger
from src.models.orders import (
    OrderPreview,
    OrderStatus,
    TradeProposal,
    ExecutionReceipt,
    OrderStatus_Model,
)
from src.risk.limits import RiskChecker

logger = get_logger(__name__)


class OrderRecord(SQLModel, table=True):
    """Persistent order record."""
    
    __tablename__ = "orders"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    decision_id: str = Field(unique=True, index=True)
    agent_id: str = Field(default="default", index=True)
    preview_id: Optional[str] = Field(default=None, index=True)
    execution_id: Optional[str] = Field(default=None, index=True)
    account: str
    symbol: str
    quantity: int
    instruction: str
    # What was actually previewed — execute_order() rebuilds the broker
    # order spec from THESE, not hardcoded defaults. Losing this at
    # execute time silently turns every LIMIT/STOP order into an
    # unprotected MARKET order at the one moment it reaches the broker,
    # which is exactly the class of bug this system exists to prevent.
    asset_type: str = "ETF"
    order_type: str = "MARKET"
    limit_price: Optional[str] = None
    stop_price: Optional[str] = None
    status: str = OrderStatus.PREVIEWED.value
    payload_checksum: str
    risk_approved: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    preview_expires_at: Optional[datetime] = None
    filled_quantity: int = 0
    average_fill_price: Optional[str] = None
    broker_status: Optional[str] = None
    broker_message: Optional[str] = None
    raw_broker_response: Optional[str] = None
    # Estimated notional at preview time (see OrderPreview.estimated_cost),
    # persisted so AgentExposureGuard can sum an agent's committed capital
    # for the day without re-deriving prices from scratch. Stored as a
    # string, matching average_fill_price above, to avoid float drift.
    estimated_notional_usd: Optional[str] = None
    # Advisory strategy metadata (see TradeProposal.strategy_* fields) —
    # not enforced by the broker, just carried through for audit/display.
    strategy_id: Optional[str] = None
    strategy_stop_loss_price: Optional[str] = None
    strategy_take_profit_price: Optional[str] = None


class Executor:
    """Orchestrate order preview and execution."""
    
    def __init__(
        self,
        session: Session,
        mock_broker: bool = False,
        broker: Optional[BrokerAdapter] = None,
    ):
        self.session = session
        self.settings = get_settings()
        self.builder = OrderBuilder()
        self.broker = broker or self._build_broker_adapter(mock_broker)
        self.risk_checker = RiskChecker()
        self.idempotency = IdempotencyManager(session)
        self.approval_manager = ApprovalManager(session)
    
    async def preview_order(self, proposal: TradeProposal) -> OrderPreview:
        """
        Execute preview flow:
        1. Validate schema and freshness
        2. Run risk checks
        3. Build order spec
        4. Call Schwab preview
        5. Persist and return preview
        """
        
        # Check for duplicate preview. This must actually short-circuit:
        # OrderRecord.decision_id is a unique column, so letting execution
        # fall through to insert a second preview row for the same
        # decision_id below would raise a database integrity error instead
        # of a clean response.
        if self.idempotency.is_duplicate(proposal.decision_id, Operation.PREVIEW):
            cached = self.idempotency.get_existing_response(proposal.decision_id, Operation.PREVIEW)
            if cached:
                logger.warning("duplicate_preview_returned_cached_response", decision_id=proposal.decision_id)
                return OrderPreview.model_validate_json(cached)
            logger.warning("duplicate_preview_no_cached_response_found", decision_id=proposal.decision_id)

        # Get current kill switch state — both the fleet-wide switch and
        # this proposal's own agent's switch; either one blocks it.
        kill_switch_on = self._get_kill_switch_state(proposal.agent_id)

        # Fetch a live quote for stale-quote / notional / limit-price-sanity
        # checks. A failure to fetch one is not fatal here — it's not an
        # auth failure, just no quote — RiskChecker's quote-freshness check
        # correctly fails closed on quote=None rather than silently skipping
        # those checks.
        try:
            quote = await self.broker.get_quote(proposal.symbol)
        except BrokerAuthenticationError as exc:
            self._shutdown_on_auth_failure(exc)
            raise
        except Exception as exc:
            logger.warning("quote_fetch_failed", decision_id=proposal.decision_id, symbol=proposal.symbol, error=str(exc))
            quote = None

        # Run risk checks
        verdict = self.risk_checker.evaluate(proposal, kill_switch_on, quote=quote)

        # Cross-agent coordination check: does this order, combined with
        # what every agent has already committed to this (account, symbol)
        # today, exceed the fleet-wide combined cap — even though this
        # order alone is within its own agent's limits. Only meaningful
        # when RiskChecker could price the order at all and settings has
        # opted into a combined cap; see SymbolCoordinationGuard's own
        # docstring for the concurrency caveat.
        if verdict.notional_usd is not None and self.settings.max_combined_symbol_notional_usd is not None:
            coordination_report = SymbolCoordinationGuard(self.session, settings=self.settings).check(
                proposal.account, proposal.symbol, verdict.notional_usd
            )
            verdict.checks["combined_symbol_exposure_ok"] = not coordination_report.breached
            if coordination_report.breached:
                verdict.approved = False
                verdict.rejections.append(
                    f"Combined notional ${coordination_report.combined_notional_usd} across all agents for "
                    f"'{proposal.symbol}' in account '{proposal.account}' would exceed the fleet-wide cap "
                    f"${coordination_report.cap_usd}"
                )

        # Compute checksum for later verification
        checksum = OrderBuilder.compute_payload_checksum(proposal)
        
        # Build order spec from an internal alias profile; no raw account IDs are accepted.
        profile = self.settings.get_account_profile(proposal.account)
        order_spec = self.builder.build_order_spec(proposal, proposal.account)
        
        # Call the selected broker preview implementation.
        try:
            broker_response = await self.broker.preview_order(profile, order_spec)
        except BrokerAuthenticationError as exc:
            self._shutdown_on_auth_failure(exc)
            raise
        
        # Generate preview ID
        preview_id = f"preview-{uuid.uuid4()}"

        # Determine expiry
        expires_at = datetime.utcnow() + timedelta(
            minutes=self.settings.schwab_preview_expiry_min
        )

        estimated_cost = broker_response.get("estimatedTotalInvestment", 0.0)

        # Persist order record
        order_record = OrderRecord(
            decision_id=proposal.decision_id,
            agent_id=proposal.agent_id,
            preview_id=preview_id,
            account=proposal.account,
            symbol=proposal.symbol,
            quantity=proposal.quantity,
            instruction=proposal.instruction.value,
            asset_type=proposal.asset_type.value,
            order_type=proposal.order_type.value,
            limit_price=str(proposal.limit_price) if proposal.limit_price is not None else None,
            stop_price=str(proposal.stop_price) if proposal.stop_price is not None else None,
            status=OrderStatus.PREVIEWED.value,
            payload_checksum=checksum,
            risk_approved=verdict.approved,
            preview_expires_at=expires_at,
            estimated_notional_usd=str(estimated_cost),
            strategy_id=proposal.strategy_id,
            strategy_stop_loss_price=str(proposal.strategy_stop_loss_price) if proposal.strategy_stop_loss_price is not None else None,
            strategy_take_profit_price=str(proposal.strategy_take_profit_price) if proposal.strategy_take_profit_price is not None else None,
        )
        self.session.add(order_record)
        self.session.commit()
        
        # Log to audit ledger
        self._audit_log(
            "ORDER_PREVIEWED",
            proposal.decision_id,
            {"preview_id": preview_id, "risk_approved": verdict.approved, "agent_id": proposal.agent_id},
        )
        
        # Build response
        preview = OrderPreview(
            preview_id=preview_id,
            decision_id=proposal.decision_id,
            preview_details=broker_response,
            estimated_commission=broker_response.get("estimatedCommission", 0.0),
            estimated_cost=estimated_cost,
            risk_verdict="APPROVED" if verdict.approved else "REJECTED",
            risk_details={"checks": verdict.checks, "rejections": verdict.rejections},
            payload_checksum=checksum,
            expires_at=expires_at,
        )

        # Register idempotency with the actual response, so a duplicate
        # preview request (see the check above) can return this same result.
        self.idempotency.register(
            proposal.decision_id,
            Operation.PREVIEW,
            f"preview:{proposal.decision_id}",
            response_body=preview.model_dump_json(),
        )

        return preview
    
    async def execute_order(
        self,
        decision_id: str,
        preview_id: str,
        approved_by: str,
        approved_at: datetime,
        attestation: str,
        idempotency_key: str,
    ) -> ExecutionReceipt:
        """
        Execute approved order:
        1. Verify preview exists and is not expired
        2. Verify approval
        3. Re-run critical risk checks
        4. Submit to Schwab
        5. Persist execution and return receipt
        """
        
        # Check for duplicate execution. This must actually short-circuit and
        # return the original result, not just log a warning and continue —
        # otherwise a retried/duplicated request resubmits to the broker a
        # second time, which is exactly what idempotency protection exists
        # to prevent.
        if self.idempotency.is_duplicate(decision_id, Operation.EXECUTE):
            cached = self.idempotency.get_existing_response(decision_id, Operation.EXECUTE)
            if cached:
                logger.warning("duplicate_execute_returned_cached_response", decision_id=decision_id)
                return ExecutionReceipt.model_validate_json(cached)
            logger.warning("duplicate_execute_no_cached_response_found", decision_id=decision_id)

        # Fetch order record
        stmt = select(OrderRecord).where(OrderRecord.decision_id == decision_id)
        order = self.session.exec(stmt).first()

        if not order:
            raise ValueError(f"Order {decision_id} not found")

        # Verify preview hasn't expired
        if order.preview_expires_at and datetime.utcnow() > order.preview_expires_at:
            raise ValueError(f"Preview expired at {order.preview_expires_at}")

        # Verify preview_id matches
        if order.preview_id != preview_id:
            raise ValueError(f"Preview ID mismatch: expected {order.preview_id}, got {preview_id}")

        # Verify the approval supplied with this request is complete and
        # fresh. There is no separate "record approval first" endpoint in
        # this API — the approval artifact arrives inline in this single
        # call — so there is no pre-existing database record to look up yet.
        # (Checking approval_manager.verify_approval() here, before ever
        # calling record_approval(), would always fail: it queries for a
        # record that cannot exist until the line below creates it. That bug
        # meant no order could ever be executed through this endpoint.)
        if not approved_by or not approved_by.strip():
            raise ValueError("Approval must include a non-empty approved_by.")
        if not attestation or not attestation.strip():
            raise ValueError("Approval must include a non-empty attestation.")

        approved_at_naive = approved_at.replace(tzinfo=None) if approved_at.tzinfo else approved_at
        approval_age = datetime.utcnow() - approved_at_naive
        if approval_age < timedelta(0):
            raise ValueError("Approval timestamp is in the future.")
        if approval_age > timedelta(minutes=self.approval_manager.MAX_APPROVAL_AGE_MINUTES):
            raise ValueError(
                f"Approval is stale ({approval_age.total_seconds() / 60:.1f} min old; "
                f"must be within {self.approval_manager.MAX_APPROVAL_AGE_MINUTES} min)."
            )

        # Record approval for the audit trail
        self.approval_manager.record_approval(
            preview_id=preview_id,
            decision_id=decision_id,
            approved_by=approved_by,
            approved_at=approved_at,
            attestation=attestation,
            idempotency_key=idempotency_key,
        )

        # Re-run kill switch check (critical) — both fleet-wide and this
        # order's own agent's switch, so halting one agent mid-flight still
        # blocks its in-flight orders at execute time, not just at preview.
        kill_switch_on = self._get_kill_switch_state(order.agent_id)
        if kill_switch_on:
            raise ValueError("Kill switch is ON - order rejected")

        # Build and submit order using what was ACTUALLY previewed — not a
        # hardcoded MARKET/ETF, which would silently discard a previewed
        # LIMIT/STOP order's price protection at the one moment it reaches
        # the broker. See OrderRecord.order_type/limit_price/stop_price,
        # populated in preview_order() above.
        order_spec = self.builder.build_order_spec(
            TradeProposal(
                decision_id=decision_id,
                agent_id=order.agent_id,
                account=order.account,
                symbol=order.symbol,
                asset_type=order.asset_type,
                instruction=order.instruction,
                quantity=order.quantity,
                order_type=order.order_type,
                limit_price=Decimal(order.limit_price) if order.limit_price is not None else None,
                stop_price=Decimal(order.stop_price) if order.stop_price is not None else None,
            ),
            order.account,
        )
        
        profile = self.settings.get_account_profile(order.account)
        try:
            broker_response = await self.broker.submit_order(profile, order_spec)
        except BrokerAuthenticationError as exc:
            self._shutdown_on_auth_failure(exc)
            raise
        
        # Extract broker order ID
        execution_id = broker_response.get("orderId", f"exec-{uuid.uuid4()}")
        
        # Update order record
        order.execution_id = execution_id
        order.status = OrderStatus.SUBMITTED.value
        order.updated_at = datetime.utcnow()
        order.broker_status = broker_response.get("status")
        order.raw_broker_response = json.dumps(broker_response)
        
        self.session.add(order)
        self.session.commit()

        receipt = ExecutionReceipt(
            decision_id=decision_id,
            execution_id=execution_id,
            status=OrderStatus.SUBMITTED,
            submitted_at=datetime.utcnow(),
            broker_response=broker_response,
        )

        # Register idempotency with the actual response body, so a duplicate
        # request (see the check at the top of this method) can return the
        # original result instead of resubmitting to the broker.
        self.idempotency.register(
            decision_id, Operation.EXECUTE, idempotency_key,
            response_body=receipt.model_dump_json(),
        )

        # Audit log
        self._audit_log(
            "ORDER_EXECUTED",
            decision_id,
            {"execution_id": execution_id, "approved_by": approved_by, "agent_id": order.agent_id},
        )

        return receipt
    
    async def get_order_status(self, decision_id: str) -> OrderStatus_Model:
        """Query order status."""
        stmt = select(OrderRecord).where(OrderRecord.decision_id == decision_id)
        order = self.session.exec(stmt).first()
        
        if not order:
            raise ValueError(f"Order {decision_id} not found")
        
        return OrderStatus_Model(
            decision_id=decision_id,
            agent_id=order.agent_id,
            execution_id=order.execution_id,
            status=OrderStatus(order.status),
            created_at=order.created_at,
            updated_at=order.updated_at,
            filled_quantity=order.filled_quantity,
            broker_status=order.broker_status,
            broker_message=order.broker_message,
        )
    
    def _get_kill_switch_state(self, agent_id: str) -> bool:
        """
        Get current kill switch state for a given agent.

        This must read the same persisted state the admin API endpoints
        (POST /v1/kill-switch/on and /off, and their per-agent equivalents
        under /v1/kill-switch/agents/{agent_id}) actually write —
        previously it read settings.kill_switch_enabled, a static default
        sourced from .env that those endpoints never touched, so turning
        the kill switch "on" through the API did not stop a single new
        order from being previewed or executed. settings.kill_switch_enabled
        is still consulted as a startup default (e.g. force it on for a
        given environment via .env), but the persisted, API-controlled
        state always wins once anyone has actually toggled it.

        Checks both the fleet-wide switch and this agent's own switch (see
        KillSwitchService.is_halted) — a deployment running multiple
        coordinating agents needs to be able to halt one misbehaving agent
        without an all-stop, while the fleet-wide switch still overrides
        every agent regardless of its own state.
        """
        if self.settings.kill_switch_enabled:
            return True
        return KillSwitchService(self.session).is_halted(agent_id)

    def _shutdown_on_auth_failure(self, exc: BrokerAuthenticationError) -> None:
        """
        A broker authentication failure (expired/revoked refresh token) means
        every subsequent call will fail identically until a human
        re-authenticates interactively — there is nothing productive left
        for this process to retry. Automatically trip the kill switch so the
        system halts and demands attention, rather than surfacing this one
        error and silently accepting the next request as if nothing were
        wrong.
        """
        logger.critical("broker_authentication_failed_auto_shutdown", error=str(exc))
        KillSwitchService(self.session).set_state(
            enabled=True,
            set_by="system",
            reason=f"Auto-shutdown: broker authentication failed ({exc})",
        )

    def _build_broker_adapter(self, mock_broker: bool) -> BrokerAdapter:
        """Choose the configured broker without allowing implicit live trading — see src/brokers/factory.py."""
        return build_broker_adapter(self.settings, mock_broker=mock_broker)
    
    def _audit_log(self, action: str, decision_id: str, details: dict) -> None:
        """Write audit log entry."""
        logger.info(
            "audit_log",
            action=action,
            decision_id=decision_id,
            details=details,
            timestamp=datetime.utcnow().isoformat(),
        )
