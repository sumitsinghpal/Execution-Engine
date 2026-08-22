"""
Order builder: construct deterministic Schwab order payloads from TradeProposal.
This ensures repeatable, auditable order construction.
"""

from decimal import Decimal

from src.models.orders import OrderType, TradeProposal
from src.logging_config import get_logger

logger = get_logger(__name__)


class SchwabOrderBuilder:
    """Build Schwab API order specs deterministically."""
    
    def build_order_spec(self, proposal: TradeProposal, account_id: str) -> dict:
        """
        Convert TradeProposal to Schwab order JSON.
        All fields must be deterministic and reproducible.
        """
        
        order_type_map = {
            OrderType.MARKET: "MARKET",
            OrderType.LIMIT: "LIMIT",
            OrderType.STOP: "STOP",
            OrderType.STOP_LIMIT: "STOP_LIMIT",
        }
        
        order_spec = {
            "orderId": proposal.decision_id,  # Use decision_id as idempotency key
            "accountId": account_id,
            "symbol": proposal.symbol,
            "assetType": proposal.asset_type.value,
            "quantity": proposal.quantity,
            "instruction": proposal.instruction.value,
            "orderType": order_type_map[proposal.order_type],
        }
        
        # Add conditional fields
        if proposal.limit_price:
            order_spec["limitPrice"] = str(proposal.limit_price)
        
        if proposal.stop_price:
            order_spec["stopPrice"] = str(proposal.stop_price)
        
        logger.info(
            "order_spec_built",
            decision_id=proposal.decision_id,
            symbol=proposal.symbol,
            quantity=proposal.quantity,
        )
        
        return order_spec
    
    @staticmethod
    def compute_payload_checksum(proposal: TradeProposal) -> str:
        """
        Compute deterministic SHA256 checksum of normalized proposal.
        Used to verify preview hasn't been modified before execute.
        """
        import hashlib
        import json
        
        normalized = {
            "decision_id": proposal.decision_id,
            "account": proposal.account,
            "symbol": proposal.symbol,
            "asset_type": proposal.asset_type.value,
            "instruction": proposal.instruction.value,
            "quantity": proposal.quantity,
            "order_type": proposal.order_type.value,
            "limit_price": str(proposal.limit_price) if proposal.limit_price else None,
            "stop_price": str(proposal.stop_price) if proposal.stop_price else None,
        }
        
        # Canonical JSON: sort keys, no spaces
        canonical = json.dumps(normalized, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode()).hexdigest()
