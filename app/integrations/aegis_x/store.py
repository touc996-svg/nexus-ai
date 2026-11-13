"""Read-only loader for normalized AEGIS-X findings/incidents from local files.

Path comes from NEXUS_AEGIS_X_DATA_DIR. Files read: findings*.jsonl|json and
incidents*.jsonl|json. Invalid records are quarantined in memory (never
dropped silently, never written anywhere). No network, no credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from app.integrations.aegis_x.models import Finding, Incident

ENV_VAR = "NEXUS_AEGIS_X_DATA_DIR"
MAX_FILE_BYTES = 50 * 1024 * 1024


@dataclass
class QuarantinedRecord:
    source: str
    location: str
    reason: str

    def as_dict(self):
        return {"source": self.source, "location": self.location, "reason": self.reason}


@dataclass
class AegisXSnapshot:
    configured: bool
    data_dir: str | None
    findings: dict[str, Finding] = field(default_factory=dict)
    incidents: dict[str, Incident] = field(default_factory=dict)
    quarantine: list[QuarantinedRecord] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    error: str | None = None


def _iter_records(path: Path):
    """Yield (location, obj_or_None, error_or_None). Read-only."""
    if path.stat().st_size > MAX_FILE_BYTES:
        yield "file", None, "file exceeds size limit"
        return
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        for n, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                yield f"line {n}", json.loads(line), None
            except json.JSONDecodeError as exc:
                yield f"line {n}", None, f"invalid JSON: {exc.msg}"
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        yield "file", None, f"invalid JSON: {exc.msg}"
        return
    if isinstance(data, dict):
        for key in ("findings", "incidents", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        yield "file", None, "expected a JSON array or object"
        return
    for n, obj in enumerate(data):
        yield f"index {n}", obj, None


def _load(model, files, target, id_attr, snapshot):
    for path in files:
        snapshot.files_read.append(path.name)
        try:
            records = list(_iter_records(path))
        except (OSError, UnicodeDecodeError) as exc:
            snapshot.quarantine.append(QuarantinedRecord(path.name, "file", f"unreadable: {type(exc).__name__}"))
            continue
        for location, obj, err in records:
            if err:
                snapshot.quarantine.append(QuarantinedRecord(path.name, location, err))
                continue
            if not isinstance(obj, dict):
                snapshot.quarantine.append(QuarantinedRecord(path.name, location, "record is not an object"))
                continue
            try:
                item = model.model_validate(obj)
            except ValidationError as exc:
                fields = sorted({".".join(map(str, e["loc"])) or "record" for e in exc.errors()})
                snapshot.quarantine.append(
                    QuarantinedRecord(path.name, location, "schema validation failed: " + ", ".join(fields))
                )
                continue
            key = getattr(item, id_attr)
            if key in target:
                snapshot.quarantine.append(QuarantinedRecord(path.name, location, f"duplicate id {key!r}; first kept"))
                continue
            target[key] = item


def load_snapshot(data_dir: str | os.PathLike | None = None) -> AegisXSnapshot:
    raw = str(data_dir) if data_dir else os.getenv(ENV_VAR, "").strip()
    if not raw:
        return AegisXSnapshot(configured=False, data_dir=None, error=f"{ENV_VAR} is not set")
    root = Path(raw).expanduser()
    snapshot = AegisXSnapshot(configured=True, data_dir=str(root))
    if not root.is_dir():
        snapshot.error = "configured data directory does not exist"
        return snapshot
    def pick(prefix):
        return sorted(p for p in root.iterdir()
                      if p.is_file() and p.name.startswith(prefix) and p.suffix in {".json", ".jsonl"})
    _load(Finding, pick("findings"), snapshot.findings, "finding_id", snapshot)
    _load(Incident, pick("incidents"), snapshot.incidents, "incident_id", snapshot)
    return snapshot
