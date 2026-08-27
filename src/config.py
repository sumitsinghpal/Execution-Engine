"""
Application configuration using Pydantic Settings.
All configuration comes from environment variables; no hardcoded secrets.
"""

from decimal import Decimal
from typing import Optional

from pydantic import ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings

from src.accounts.profiles import AccountProfile, BrokerName
from src.agents.profiles import AgentRiskProfile


class Settings(BaseSettings):
    """Application configuration from environment variables."""
    
    model_config = ConfigDict(env_file=".env", case_sensitive=False)
    
    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    env: str = "development"
    
    # Database
    database_url: str = "sqlite:///./execution_engine.db"
    
    # Execution mode defaults to non-live simulation. LIVE is not implemented.
    execution_mode: str = "PAPER"

    # Account aliases prevent EDGE-TF callers from providing raw broker account IDs.
    account_profiles: dict[str, AccountProfile] = Field(
        default_factory=lambda: {
            "primary": AccountProfile(broker=BrokerName.PAPER),
            "retirement": AccountProfile(broker=BrokerName.PAPER),
            "paper": AccountProfile(broker=BrokerName.PAPER),
        }
    )

    # Schwab credentials must be explicitly supplied for the Schwab adapter.
    schwab_app_key: Optional[str] = None
    schwab_app_secret: Optional[str] = None
    schwab_refresh_token: Optional[str] = None
    schwab_redirect_uri: Optional[str] = None

    # The plain Schwab account number to trade against — never a hash,
    # which is looked up automatically (see
    # SchwabBrokerAdapter._resolve_account_hash). Setting this auto-
    # registers `schwab_account_alias` into account_profiles below as a
    # SCHWAB-broker profile, so it becomes usable as a `account` value
    # without hand-editing this file. It still is NOT usable until an
    # operator also adds it to ACCOUNT_ALLOWLIST — registering the alias
    # and allowing it to trade are deliberately separate steps.
    schwab_account_number: Optional[str] = None
    schwab_account_alias: str = "schwab_live"

    # Kill switch
    kill_switch_enabled: bool = False
    
    # Account allowlist. Stored raw (comma-separated) because pydantic-settings tries
    # to JSON-parse any list[str]-typed field loaded from an env var / .env file and
    # crashes on a plain "primary,secondary" string. See the *_allowlist/*_denylist
    # properties below for the parsed list form actually used elsewhere in the code.
    account_allowlist_raw: str = Field(default="primary", validation_alias="ACCOUNT_ALLOWLIST")

    # Symbol configuration (comma-separated strings; see parsed properties below)
    symbol_allowlist_raw: str = Field(
        default="QQQ,SPY,IWM,EEM,GLD,TLT", validation_alias="SYMBOL_ALLOWLIST"
    )
    symbol_denylist_raw: str = Field(default="", validation_alias="SYMBOL_DENYLIST")
    
    # Risk limits
    max_order_notional_usd: Decimal = Decimal("100000")
    max_position_concentration_pct: Decimal = Decimal("15")
    market_hours_only: bool = True

    # Stale-quote protection: a quote older than this is refused outright
    # rather than trusted for notional/price-sanity checks, and a LIMIT
    # order priced further than this from the live quote is rejected as
    # likely mispriced (fat-finger protection) rather than silently routed.
    max_quote_age_seconds: int = 10
    max_limit_price_deviation_pct: Decimal = Decimal("0.03")

    # Options expiration allowlist: an OPTION order whose contract expires
    # sooner than this is rejected as likely an accidental/mispriced
    # 0-days-to-expiration trade, and one expiring further out than this
    # (longer than a typical LEAP) is rejected as outside what this system
    # has been reviewed to trade. Completes the "expiration allowlist"
    # item on the original safety checklist — unreachable before options
    # could route at all.
    min_option_expiration_days: int = 1
    max_option_expiration_days: int = 730

    # Daily loss/drawdown shutdown: once an account's net liquidation value
    # falls this far below its captured start-of-day baseline, trading
    # auto-halts via the kill switch until a human clears it. See
    # DrawdownGuard (src/execution/drawdown_guard.py).
    max_daily_drawdown_pct: Decimal = Decimal("0.05")

    # Per-agent risk overrides, keyed by agent_id — see src/agents/profiles.py.
    # An agent with no entry here uses the global defaults above unchanged.
    agent_risk_profiles: dict[str, AgentRiskProfile] = Field(default_factory=dict)

    # Cross-agent coordination: the combined committed notional across
    # EVERY agent trading a given (account, symbol) today, checked at
    # preview time so two agents that are each individually under their
    # own limits can't collectively over-concentrate in one symbol without
    # either noticing. Opt-in (None disables it) — see
    # src/execution/symbol_coordination.py.
    max_combined_symbol_notional_usd: Optional[Decimal] = None

    # Autonomous strategy scanning (src/execution/strategy_scanner.py): a
    # background loop periodically evaluates every strategy in
    # src/strategy/catalog.py against every symbol below, using live price
    # history (real Schwab data if configured, synthetic otherwise). A
    # fired signal is only ever recorded for human review — nothing here
    # places or previews an order automatically.
    strategy_scan_enabled: bool = True
    strategy_scan_interval_sec: int = 60
    strategy_watchlist_raw: str = Field(default="QQQ,SPY,IWM", validation_alias="STRATEGY_WATCHLIST")

    @property
    def strategy_watchlist(self) -> list[str]:
        return self._split_csv(self.strategy_watchlist_raw)

    @strategy_watchlist.setter
    def strategy_watchlist(self, value: list[str]) -> None:
        self.strategy_watchlist_raw = ",".join(value)

    # EDGE-TF connector (src/execution/edge_tf_connector.py): polls EDGE-TF's
    # own execution gateway (api/execution_app.py in the EDGE-TF repo, a
    # separate process/repo) for trades it has already approved, and records
    # them as external signals for human review — same "no automatic order"
    # guarantee as the internal strategy scanner above. Disabled by default;
    # both the URL and token must be set for it to do anything, matching how
    # Schwab credentials are opt-in rather than assumed. The token must match
    # EDGE_EXECUTION_TOKEN on the EDGE-TF side.
    edge_tf_connector_enabled: bool = False
    edge_tf_gateway_url: Optional[str] = None
    edge_tf_gateway_token: Optional[str] = None
    edge_tf_poll_interval_sec: int = 60
    edge_tf_executor_id: str = "execution-engine"

    # Autonomous trading (src/execution/autonomous_trader.py): unlike the
    # strategy scanner above, this ACTUALLY submits orders — no human
    # approval step. Every decision is still 100% rule-based (the strategy
    # catalog's fixed entry rules + a standardized risk:reward exit, never
    # an LLM judgment call — see src/agentic/llm_narrator.py's docstring)
    # and still runs through the same preview/risk-checks/kill-switch gate
    # as any other order, scoped to its own agent_id so it can be halted
    # independently via /v1/kill-switch/agents/{autonomous_agent_id}/on.
    # Hard-coded to the paper broker regardless of this or any other
    # setting — see autonomous_trader._build_broker()'s docstring for why
    # that's a code-level guarantee, not a config one. Disabled by default.
    autonomous_trading_enabled: bool = False
    autonomous_agent_id: str = "autonomous-trader"
    autonomous_account: str = "primary"
    # Golden Cross, Turtle 20-Day Breakout, and RSI(2) Pullback — three
    # historically well-documented strategies spanning trend-following,
    # breakout, and mean-reversion (see src/strategy/catalog.py for
    # citations). Deliberately starts small; add more of the catalog's ids
    # here later.
    autonomous_strategy_ids_raw: str = Field(
        default="golden_cross,turtle_donchian,rsi2_connors", validation_alias="AUTONOMOUS_STRATEGY_IDS"
    )
    autonomous_watchlist_raw: str = Field(default="QQQ,SPY,IWM", validation_alias="AUTONOMOUS_WATCHLIST")
    # Standardized exit: stop-loss at risk_pct below entry, take-profit at
    # reward_risk_ratio times that same distance above entry — one uniform
    # rule for every strategy above, replacing each strategy's own
    # individually-taught convention (only used here, not for human-
    # reviewed signals from the strategy scanner, which keep showing the
    # real cited rule). Default 1% stop / 1:2 reward:risk.
    autonomous_risk_pct: Decimal = Decimal("0.01")
    autonomous_reward_risk_ratio: Decimal = Decimal("2")
    # Fixed-notional position sizing (see risk_reward.size_position) —
    # deliberately simple, not volatility-sized.
    autonomous_notional_per_trade_usd: Decimal = Decimal("1000")
    autonomous_scan_interval_sec: int = 60

    @property
    def autonomous_strategy_ids(self) -> list[str]:
        return self._split_csv(self.autonomous_strategy_ids_raw)

    @autonomous_strategy_ids.setter
    def autonomous_strategy_ids(self, value: list[str]) -> None:
        self.autonomous_strategy_ids_raw = ",".join(value)

    @property
    def autonomous_watchlist(self) -> list[str]:
        return self._split_csv(self.autonomous_watchlist_raw)

    @autonomous_watchlist.setter
    def autonomous_watchlist(self, value: list[str]) -> None:
        self.autonomous_watchlist_raw = ",".join(value)

    # LLM narration (src/agentic/llm_narrator.py) — orchestration and
    # rationale-writing only, consulted after an order already went
    # through; never a decision-maker. Opt-in like Schwab credentials: the
    # system runs with deterministic template narration when no key is
    # set. Uses the Anthropic Messages API shape by default; point
    # llm_api_base_url elsewhere for a different provider with the same
    # request/response shape.
    llm_narration_enabled: bool = False
    llm_api_key: Optional[str] = None
    llm_model: str = "claude-sonnet-5"
    llm_api_base_url: str = "https://api.anthropic.com/v1/messages"

    # API Security
    api_key_admin: str = "change-me-in-prod"
    request_signing_enabled: bool = False
    request_signing_private_key: Optional[str] = None
    
    # Logging
    log_level: str = "INFO"
    log_format: str = "json"
    
    # Schwab API settings
    schwab_api_timeout_sec: int = 30
    schwab_preview_timeout_sec: int = 10
    schwab_preview_expiry_min: int = 5
    schwab_retry_max_attempts: int = 3
    schwab_retry_backoff_sec: float = 1.0

    @model_validator(mode="after")
    def _register_schwab_account_alias(self) -> "Settings":
        """
        If a Schwab account number is configured but the alias it should
        live under hasn't been explicitly defined in account_profiles
        already, register it now as an unresolved SCHWAB profile (no
        account_hash — resolved lazily and automatically on first live
        broker call, see SchwabBrokerAdapter). Runs once at Settings
        construction; an explicit entry already present for that alias
        (e.g. a caller pre-populating account_profiles directly) is left
        untouched rather than overwritten.
        """
        if self.schwab_account_number and self.schwab_account_alias not in self.account_profiles:
            self.account_profiles = {
                **self.account_profiles,
                self.schwab_account_alias: AccountProfile(
                    broker=BrokerName.SCHWAB, credential_profile="schwab_main"
                ),
            }
        return self

    def get_account_profile(self, alias: str) -> AccountProfile:
        """Resolve a safe account alias or fail closed."""
        profile = self.account_profiles.get(alias)
        if not profile:
            raise ValueError(f"Unknown account alias: {alias}")
        return profile

    def get_agent_risk_profile(self, agent_id: str) -> AgentRiskProfile:
        """
        An agent with no configured profile gets an all-defaults
        AgentRiskProfile — every field None, meaning "use the global
        setting" — never a missing-config error. Unlike account aliases,
        an unrecognized agent_id is expected and fine: a new agent can
        start trading under fleet-wide defaults before anyone gets around
        to giving it its own tighter limits.
        """
        return self.agent_risk_profiles.get(agent_id, AgentRiskProfile())

    @staticmethod
    def _split_csv(value: str) -> list[str]:
        return [s.strip() for s in value.split(",") if s.strip()]

    @property
    def account_allowlist(self) -> list[str]:
        return self._split_csv(self.account_allowlist_raw)

    @account_allowlist.setter
    def account_allowlist(self, value: list[str]) -> None:
        self.account_allowlist_raw = ",".join(value)

    @property
    def symbol_allowlist(self) -> list[str]:
        return self._split_csv(self.symbol_allowlist_raw)

    @symbol_allowlist.setter
    def symbol_allowlist(self, value: list[str]) -> None:
        self.symbol_allowlist_raw = ",".join(value)

    @property
    def symbol_denylist(self) -> list[str]:
        return self._split_csv(self.symbol_denylist_raw)

    @symbol_denylist.setter
    def symbol_denylist(self, value: list[str]) -> None:
        self.symbol_denylist_raw = ",".join(value)


def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
