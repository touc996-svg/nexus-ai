"""Pydantic models mirroring finding.schema.json (vendored beside this file).

Unknown fields are preserved (the schema allows additionalProperties). Models
are used only for validation; nothing here performs I/O.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Cloud = Literal["aws", "azure", "gcp"]
Severity = Literal["critical", "high", "medium", "low", "informational"]
SEVERITY_RANK = {"informational": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _require_timezone(value: datetime) -> datetime:
    """Reject timestamps without an explicit timezone offset."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include an explicit timezone offset")
    return value


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
        return _require_timezone(v) if v is not None else v


class Incident(BaseModel):
    """Correlated incident, mirroring AEGIS-X pipeline/schemas/incident.schema.json
    (one item of the document's ``incidents`` array)."""

    model_config = ConfigDict(extra="allow")

    incident_id: str = Field(min_length=1)
    member_finding_ids: list[str] = Field(min_length=1)
    highest_severity: Severity
    clouds: list[Cloud] = Field(min_length=1)
    accounts: list[str] = Field(default_factory=list)
    first_seen: datetime
    last_seen: datetime
    mitre_techniques: list[str] = Field(default_factory=list)
    threat_intel_indicators: list[str] = Field(default_factory=list)
    score: dict[str, Any]
    grouping_reasons: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    evidence_refs: list[EvidenceRef]
    raw_payload_refs: list[dict[str, Any]]

    @field_validator("first_seen", "last_seen")
    @classmethod
    def _tz(cls, v):
        return _require_timezone(v)
