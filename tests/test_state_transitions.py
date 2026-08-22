from __future__ import annotations

from src.execution.reconciliation import validate_transition


def test_valid_transition():
    assert validate_transition("SUBMITTED", "ACKNOWLEDGED")


def test_invalid_transition():
    assert not validate_transition("FILLED", "PARTIAL_FILL")
