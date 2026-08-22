"""Tests for idempotency control."""

from datetime import datetime, timedelta

import pytest

from src.execution.idempotency import IdempotencyManager, IdempotencyRecord, Operation


class TestIdempotencyManager:
    """Test idempotency tracking."""
    
    def test_register_operation(self, test_db):
        """Register an operation."""
        manager = IdempotencyManager(test_db)
        
        record = manager.register(
            decision_id="edge-20260821-001",
            operation=Operation.PREVIEW,
            idempotency_key="key-123",
            response_body='{"preview_id": "preview-456"}',
        )
        
        assert record.decision_id == "edge-20260821-001"
        assert record.operation == "PREVIEW"
        assert record.idempotency_key == "key-123"
    
    def test_is_duplicate_detects_same_operation(self, test_db):
        """is_duplicate detects existing operation."""
        manager = IdempotencyManager(test_db)
        
        # Register first operation
        manager.register(
            decision_id="edge-20260821-002",
            operation=Operation.PREVIEW,
            idempotency_key="key-234",
        )
        
        # Check for duplicate
        assert manager.is_duplicate("edge-20260821-002", Operation.PREVIEW)
    
    def test_is_duplicate_ignores_different_operation(self, test_db):
        """is_duplicate ignores different operations."""
        manager = IdempotencyManager(test_db)
        
        # Register PREVIEW
        manager.register(
            decision_id="edge-20260821-003",
            operation=Operation.PREVIEW,
            idempotency_key="key-345",
        )
        
        # EXECUTE is different, should not be duplicate
        assert not manager.is_duplicate("edge-20260821-003", Operation.EXECUTE)
    
    def test_is_duplicate_ignores_expired_records(self, test_db):
        """is_duplicate ignores expired records."""
        manager = IdempotencyManager(test_db)
        
        # Manually create an expired record
        expired_record = IdempotencyRecord(
            decision_id="edge-20260821-004",
            operation=Operation.PREVIEW,
            idempotency_key="key-456",
            expires_at=datetime.utcnow() - timedelta(hours=1),  # Expired
        )
        test_db.add(expired_record)
        test_db.commit()
        
        # Should not be detected as duplicate
        assert not manager.is_duplicate("edge-20260821-004", Operation.PREVIEW)
    
    def test_get_existing_response(self, test_db):
        """Retrieve cached response."""
        manager = IdempotencyManager(test_db)
        
        response_body = '{"preview_id": "preview-789"}'
        manager.register(
            decision_id="edge-20260821-005",
            operation=Operation.PREVIEW,
            idempotency_key="key-567",
            response_body=response_body,
        )
        
        cached = manager.get_existing_response("edge-20260821-005", Operation.PREVIEW)
        assert cached == response_body
    
    def test_get_existing_response_returns_none_if_not_found(self, test_db):
        """get_existing_response returns None if not found."""
        manager = IdempotencyManager(test_db)
        
        cached = manager.get_existing_response("nonexistent", Operation.PREVIEW)
        assert cached is None
