"""Append one JSON line per request to the audit log, including requests that were stopped."""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDIT_PATH = ROOT / "audit.jsonl"  # ignored by git

FIELDS = [
    "timestamp", "audit_id", "question", "route", "status", "function", "params",
    "estimated_bytes", "approved", "bytes_processed", "bytes_billed", "row_count",
    "error", "model_name", "summary_fallback",
]


def append(path, record):
    """Write one record as one line. Values that JSON cannot hold (dates) are written as text."""
    line = {field: record.get(field) for field in FIELDS}
    line["timestamp"] = line["timestamp"] or datetime.now(timezone.utc).isoformat()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, default=str) + "\n")
    return line
