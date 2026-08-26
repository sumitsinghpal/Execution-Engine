"""
Daily loss/drawdown shutdown: captures each account's opening net
liquidation value once per trading day, and compares current equity
against that baseline before trading proceeds. Once the loss from that
baseline exceeds settings.max_daily_drawdown_pct, this automatically trips
the kill switch — the same "detect and halt, never auto-resume" pattern
already used by PositionReconciliationService.reconcile_or_halt() and the
broker-authentication-failure shutdown in Executor.

Nothing in this codebase tracked equity over the course of a day before
this: get_balances() existed per-adapter, but nothing captured a baseline
or compared against it, so a bad trading day had no automatic backstop.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Optional

from sqlmodel import Field, Session, SQLModel, select

from src.brokers.base import BrokerAdapter
from src.brokers.paper import PaperBrokerAdapter
from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)


class DailyEquityBaseline(SQLModel, table=True):
    """
    One row per (account, trading_date): the account's net liquidation
    value as first observed that day, captured at most once per day so a
    losing day's own drawdown can't quietly reset its own comparison point.
    """

    __tablename__ = "daily_equity_baseline"

    id: Optional[int] = Field(default=None, primary_key=True)
    account: str = Field(index=True)
    trading_date: str = Field(index=True)  # ISO date (YYYY-MM-DD), UTC
    baseline_equity: float
    captured_at: datetime = Field(default_factory=datetime.utcnow)


@dataclass
class DrawdownReport:
    account: str
    trading_date: str
    baseline_equity: float
    current_equity: float
    drawdown_pct: float
    breached: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account": self.account,
            "trading_date": self.trading_date,
            "baseline_equity": self.baseline_equity,
            "current_equity": self.current_equity,
            "drawdown_pct": self.drawdown_pct,
            "breached": self.breached,
        }


def _extract_equity(balances: Dict[str, Any]) -> Optional[float]:
    """
    Pulls a broker-neutral net liquidation value out of a get_balances()
    response. Both adapters normalize this under "net_liquidation_value"
    (see PaperBrokerAdapter and SchwabBrokerAdapter.get_balances()); returns
    None — never 0 — when it's missing, so a malformed/unexpected balances
    payload fails the drawdown check closed rather than reading as a total
    wipeout.
    """
    value = balances.get("net_liquidation_value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class DrawdownGuard:
    """Captures per-day equity baselines and halts trading on excess drawdown."""

    def __init__(self, session: Session, broker: Optional[BrokerAdapter] = None):
        self.session = session
        self.settings = get_settings()
        self.broker = broker or PaperBrokerAdapter()

    @staticmethod
    def _today() -> str:
        return datetime.utcnow().date().isoformat()

    def _get_baseline(self, account: str, trading_date: str) -> Optional[DailyEquityBaseline]:
        stmt = select(DailyEquityBaseline).where(
            DailyEquityBaseline.account == account,
            DailyEquityBaseline.trading_date == trading_date,
        )
        return self.session.exec(stmt).first()

    async def ensure_todays_baseline(self, account: str) -> DailyEquityBaseline:
        """
        Returns today's baseline for the account, capturing it from the
        broker's current balances if this is the first call today.
        Idempotent within a day: once captured, later calls return the
        same row untouched, even if equity has since moved.
        """
        trading_date = self._today()
        existing = self._get_baseline(account, trading_date)
        if existing is not None:
            return existing

        profile = self.settings.get_account_profile(account)
        balances = await self.broker.get_balances(profile)
        equity = _extract_equity(balances)
        if equity is None:
            raise ValueError(
                f"Cannot capture drawdown baseline for account '{account}': "
                f"broker balances response has no usable net_liquidation_value"
            )

        baseline = DailyEquityBaseline(
            account=account, trading_date=trading_date, baseline_equity=equity
        )
        self.session.add(baseline)
        self.session.commit()
        self.session.refresh(baseline)
        logger.info(
            "drawdown_baseline_captured",
            account=account,
            trading_date=trading_date,
            baseline_equity=equity,
        )
        return baseline

    async def check_drawdown(self, account: str) -> DrawdownReport:
        """
        Compares current equity against today's baseline (capturing it
        first if needed). Does not itself act on a breach — see
        check_and_halt below for the gate that trips the kill switch.
        """
        baseline = await self.ensure_todays_baseline(account)

        profile = self.settings.get_account_profile(account)
        balances = await self.broker.get_balances(profile)
        current_equity = _extract_equity(balances)
        if current_equity is None:
            raise ValueError(
                f"Cannot evaluate drawdown for account '{account}': "
                f"broker balances response has no usable net_liquidation_value"
            )

        if baseline.baseline_equity > 0:
            drawdown_pct = max(0.0, (baseline.baseline_equity - current_equity) / baseline.baseline_equity)
        else:
            drawdown_pct = 0.0

        breached = drawdown_pct >= float(self.settings.max_daily_drawdown_pct)

        report = DrawdownReport(
            account=account,
            trading_date=baseline.trading_date,
            baseline_equity=baseline.baseline_equity,
            current_equity=current_equity,
            drawdown_pct=drawdown_pct,
            breached=breached,
        )

        if breached:
            logger.critical("drawdown_limit_breached", **report.to_dict())
        else:
            logger.info("drawdown_check_ok", **report.to_dict())

        return report

    async def check_and_halt(self, account: str, halted_by: str = "drawdown_guard") -> DrawdownReport:
        """
        Intended entry point for pre-trade / periodic checks: evaluates
        drawdown and, if the limit is breached, automatically trips the
        kill switch — mirroring PositionReconciliationService.reconcile_or_halt.
        Trading stays halted until a human investigates and manually clears
        it via the admin-gated /v1/kill-switch/off; this call only ever
        halts, never resumes.
        """
        from src.execution.kill_switch_state import KillSwitchService  # deferred: avoid circular import

        report = await self.check_drawdown(account)
        if report.breached:
            KillSwitchService(self.session).set_state(
                enabled=True,
                set_by=halted_by,
                reason=(
                    f"Auto-halt: account '{account}' drawdown "
                    f"{report.drawdown_pct:.2%} breached the "
                    f"{float(self.settings.max_daily_drawdown_pct):.2%} daily limit "
                    f"(baseline={report.baseline_equity}, current={report.current_equity})"
                ),
            )
        return report


__all__ = ["DailyEquityBaseline", "DrawdownReport", "DrawdownGuard"]
