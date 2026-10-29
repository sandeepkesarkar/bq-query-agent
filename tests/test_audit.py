"""The audit line: its fields, and that a line written before job_id existed is still valid."""
import json

from agent import audit


def test_job_id_comes_right_after_bytes_billed():
    assert audit.FIELDS.index("job_id") == audit.FIELDS.index("bytes_billed") + 1


def test_append_writes_job_id_and_null_when_absent(tmp_path):
    path = tmp_path / "a.jsonl"
    audit.append(path, {"audit_id": "1", "job_id": "job_abc"})
    audit.append(path, {"audit_id": "2"})
    first, second = (json.loads(line) for line in path.read_text().splitlines())
    assert first["job_id"] == "job_abc" and second["job_id"] is None


def test_an_old_line_without_job_id_is_still_valid_json(tmp_path):
    path = tmp_path / "a.jsonl"
    old = {f: None for f in audit.FIELDS if f != "job_id"}
    path.write_text(json.dumps(old) + "\n")
    audit.append(path, {"audit_id": "new", "job_id": "j"})
    old_line, new_line = (json.loads(line) for line in path.read_text().splitlines())
    assert "job_id" not in old_line and new_line["job_id"] == "j"
