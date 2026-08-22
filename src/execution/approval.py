from __future__ import annotations

from datetime import UTC, datetime

from src.models.orders import ApprovalArtifact


def verify_approval_artifact(artifact: ApprovalArtifact) -> None:
    if "approve" not in artifact.attestation.lower():
        raise ValueError("approval attestation must explicitly include approval language")
    approved_at = artifact.approved_at if artifact.approved_at.tzinfo else artifact.approved_at.replace(tzinfo=UTC)
    if approved_at > datetime.now(UTC):
        raise ValueError("approval timestamp cannot be in the future")
