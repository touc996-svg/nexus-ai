"""Pydantic models mirroring finding.schema.json (vendored beside this file).

Unknown fields are preserved (the schema allows additionalProperties). Models
are used only for validation; nothing here performs I/O.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Cloud = Literal["aws", "azure", "gcp"]
Severity = Literal["critical", "high", "medium", "low", "informational"]
SEVERITY_RANK = {"informational": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str
    ref: str


class Finding(BaseModel):
    model_config = ConfigDict(extra="allow")

    finding_id: str = Field(min_length=1)
    cloud: Cloud
    provider: str
    native_id: str | None = None
    severity: Severity
    title: str
    description: str | None = None
    resource_id: str | None = None
    account_id: str | None = None
    region: str | None = None
    observed_at: datetime
    mitre_techniques: list[str] = Field(default_factory=list)
    raw_payload: dict[str, Any] | None = None
    finding_type: str | None = None
    status: Literal["active", "archived", "unknown"] | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    ingested_at: datetime | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    native_severity: Any = None

    @field_validator("observed_at", "first_seen", "last_seen", "ingested_at")
    @classmethod
    def _tz(cls, v):
        return _aware(v) if v is not None else v


class Incident(BaseModel):
    """Minimal incident shape. AEGIS-X publishes no incident schema yet, so this
    is a NEXUS-side assumption: an id, a title and the finding_ids it groups."""

    model_config = ConfigDict(extra="allow")

    incident_id: str = Field(min_length=1)
    title: str
    finding_ids: list[str] = Field(default_factory=list)
    status: str | None = None
    severity: Severity | None = None
    created_at: datetime | None = None
    summary: str | None = None

    @field_validator("created_at")
    @classmethod
    def _tz(cls, v):
        return _aware(v) if v is not None else v
