"""Broker-neutral account profiles.

Trade proposals refer to aliases only. Broker account identifiers and credential
profiles are resolved internally by the selected adapter.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class BrokerName(str, Enum):
    """Supported broker integrations."""

    PAPER = "paper"
    SCHWAB = "schwab"


class AccountProfile(BaseModel):
    """A safe local alias for a broker account."""

    broker: BrokerName
    credential_profile: Optional[str] = None
    account_hash: Optional[str] = None
    live_enabled: bool = False

    model_config = ConfigDict(extra="forbid")