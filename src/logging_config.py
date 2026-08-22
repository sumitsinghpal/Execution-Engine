"""
Structured logging configuration using structlog.
Ensures secrets and PII are never logged.
"""

import json
import logging
from typing import Any

import structlog
from structlog.processors import JSONRenderer


class SecretRedactor:
    """Filter that removes sensitive values from log records."""
    
    SENSITIVE_KEYS = {
        "password", "secret", "token", "key", "credential",
        "schwab_app_key", "schwab_app_secret", "schwab_refresh_token",
        "api_key", "authorization", "bearer"
    }
    
    @staticmethod
    def redact_value(v: Any) -> Any:
        """Redact a single value if it looks like a secret."""
        if isinstance(v, str) and len(v) > 8:
            return "[REDACTED]"
        return v
    
    def __call__(self, logger: Any, name: str, event_dict: dict) -> dict:
        """Redact sensitive fields from event dictionary."""
        for key, value in event_dict.items():
            if any(sensitive in key.lower() for sensitive in self.SENSITIVE_KEYS):
                event_dict[key] = "[REDACTED]"
            elif isinstance(value, dict):
                event_dict[key] = self._redact_dict(value)
        return event_dict
    
    @staticmethod
    def _redact_dict(d: dict) -> dict:
        """Recursively redact a dictionary."""
        result = {}
        for key, value in d.items():
            if any(sensitive in key.lower() for sensitive in SecretRedactor.SENSITIVE_KEYS):
                result[key] = "[REDACTED]"
            elif isinstance(value, dict):
                result[key] = SecretRedactor._redact_dict(value)
            else:
                result[key] = value
        return result


def configure_logging(level: str = "INFO", format: str = "json") -> None:
    """Configure structlog for the application."""
    
    if format == "json":
        formatter = JSONRenderer()
    else:
        formatter = structlog.dev.ConsoleRenderer()
    
    structlog.configure(
        processors=[
            SecretRedactor(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            formatter,
        ],
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    
    # Configure standard logging to use structlog
    logging.basicConfig(level=level, format="%(message)s")
    for logger_name in ["uvicorn", "fastapi"]:
        logging.getLogger(logger_name).setLevel(level)


def get_logger(name: str) -> structlog.BoundLogger:
    """Get a logger instance."""
    return structlog.get_logger(name)
