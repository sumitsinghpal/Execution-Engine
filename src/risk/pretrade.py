"""
Pre-trade validation utilities.
Market hours, trading halts, and other market-level checks.
"""

from datetime import datetime, time

import pytz

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)


class MarketHoursValidator:
    """Check if current time is within market hours."""
    
    # US market hours: 9:30 AM - 4:00 PM ET, Mon-Fri
    MARKET_OPEN = time(9, 30)
    MARKET_CLOSE = time(16, 0)
    
    def __init__(self):
        self.settings = get_settings()
        self.eastern = pytz.timezone("US/Eastern")
    
    def is_market_hours(self) -> bool:
        """Check if current time is within US market hours."""
        if not self.settings.market_hours_only:
            return True
        
        now = datetime.now(self.eastern)
        
        # Market closed on weekends (5=Saturday, 6=Sunday)
        if now.weekday() >= 5:
            return False
        
        # Check time range
        current_time = now.time()
        return self.MARKET_OPEN <= current_time < self.MARKET_CLOSE
    
    def get_market_status(self) -> dict:
        """Get human-readable market status."""
        now = datetime.now(self.eastern)
        in_hours = self.is_market_hours()
        
        return {
            "in_market_hours": in_hours,
            "current_time_et": now.isoformat(),
            "market_open": self.MARKET_OPEN.isoformat(),
            "market_close": self.MARKET_CLOSE.isoformat(),
            "day_of_week": now.strftime("%A"),
        }


class TradingHaltValidator:
    """
    Check for trading halts (placeholder).
    In production, would query real-time halt data from exchange.
    """
    
    HALTED_SYMBOLS = set()  # Populate from external data source
    
    @classmethod
    def is_symbol_halted(cls, symbol: str) -> bool:
        """Check if symbol is under trading halt."""
        return symbol in cls.HALTED_SYMBOLS
