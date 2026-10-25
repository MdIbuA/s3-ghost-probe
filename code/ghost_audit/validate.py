"""Contract validation of a written output.csv (run on the file on disk)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .planner import FINDING_OPS, OP_STATE, PHASE_OPS, REMEDIATION_OPS, VERIFICATION_OPS

KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}
STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}
RETAIN_SYSTEMS = {"security_service", "legal_service", "backup_service"}


def validate_output(out_path: Path, requests_path: Path, dataset_dir: Path, expected_rows: int | None = None) -> list[str]:
    errs: list[str] = []
    warnings: list[str] = []
    reqs = {r["case_id"]: r for r in csv.DictReader(open(requests_path, encoding="utf-8"))}
    manifest = {r["file_id"]: r for r in csv.DictReader(open(dataset_dir / "evidence_files.csv", encoding="utf-8"))}
    acts = {r["activity_id"]: r["event_time"] for r in csv.DictReader(open(dataset_dir / "system_activity.csv", encoding="utf-8"))}
    audits = {r["audit_id"]: r["event_time"] for r in csv.DictReader(open(dataset_dir / "user_audit_log.csv", encoding="utf-8"))}

    raw = out_path.read_text(encoding="utf-8")
    if not raw.startswith("case_id,deletion_status,steps_json\n") and not raw.startswith("case_id,deletion_status,steps_json\r\n"):
        errs.append("header must be exactly case_id,deletion_status,steps_json")
    rows = list(csv.reader(open(out_path, encoding="utf-8", newline="")))
    body = rows[1:]
    if expected_rows is not None and len(body) != expected_rows:
        errs.append(f"expected {expected_rows} data rows, found {len(body)}")
    ids = [r[0] for r in body]
    if ids != sorted(ids):
        errs.append("rows are not sorted by case_id")
    if len(set(ids)) != len(ids):
        errs.append("duplicate case_id rows")
    missing = set(reqs) - set(ids)
    extra = set(ids) - set(reqs)
    if missing:
        errs.append(f"missing cases: {sorted(missing)[:5]}")
    if extra:
        errs.append(f"unknown cases: {sorted(extra)[:5]}")
    for r in body:
        if len(r) != 3:
            errs.append(f"{r[:1]}: expected 3 fields, found {len(r)}")
            continue
        cid, status, sj = r
        if status not in STATUSES:
            errs.append(f"{cid}: invalid status {status}")
        try:
            steps = json.loads(sj)
        except json.JSONDecodeError:
            errs.append(f"{cid}: steps_json is not valid JSON")
            continue
        if not isinstance(steps, list):
            errs.append(f"{cid}: steps_json is not an array")
            continue
        audit_at = reqs.get(cid, {}).get("audit_at", "9999")
        if status == "NOT_APPLICABLE" and steps:
            errs.append(f"{cid}: NOT_APPLICABLE must have []")
        if status == "INCOMPLETE" and not steps:
            errs.append(f"{cid}: INCOMPLETE with no steps")
        if status == "COMPLETE" and not steps:
            warnings.append(f"{cid}: COMPLETE with no linkable records (nothing to cite)")
        phases = [s.get("phase") for s in steps]
        if status == "COMPLETE" and any(p != "VERIFICATION" for p in phases):
            errs.append(f"{cid}: COMPLETE must contain verification only")
        if status == "INCOMPLETE" and not {"FINDING", "REMEDIATION", "VERIFICATION"} <= set(phases):
            errs.append(f"{cid}: INCOMPLETE must contain findings, remediation and verification")
        prev = None
        for i, s in enumerate(steps):
            if set(s) != KEYS:
                errs.append(f"{cid} step {i+1}: keys {sorted(s)}")
                continue
            if s["step_no"] != i + 1:
                errs.append(f"{cid}: step numbering not consecutive at {i+1}")
            ph, op = s["phase"], s["operation"]
            if ph not in PHASE_OPS or op not in PHASE_OPS[ph]:
                errs.append(f"{cid} step {i+1}: operation {op} not allowed in phase {ph}")
                continue
            if OP_STATE[op] != s["state"]:
                errs.append(f"{cid} step {i+1}: state {s['state']} does not match {op}")
            k = (["FINDING", "REMEDIATION", "VERIFICATION"].index(ph), PHASE_OPS[ph].index(op), s["system_id"], s["resource_id"])
            if prev is not None and k < prev:
                errs.append(f"{cid} step {i+1}: ordering violation")
            prev = k
            ev = s["evidence_id"]
            needs_ev = ph == "FINDING" or (ph == "VERIFICATION" and status == "COMPLETE")
            if needs_ev:
                t = manifest.get(ev, {}).get("captured_at") or acts.get(ev) or audits.get(ev)
                if not ev or t is None:
                    errs.append(f"{cid} step {i+1}: invalid evidence reference {ev!r}")
                elif t > audit_at:
                    errs.append(f"{cid} step {i+1}: evidence {ev} is after audit_at")
            elif ev != "":
                errs.append(f"{cid} step {i+1}: proposed step must have empty evidence_id")
            if op == "DELETE_RECORD" and s["system_id"] in RETAIN_SYSTEMS:
                errs.append(f"{cid} step {i+1}: deletes a policy-retained record")
    validate_output.warnings = warnings
    return errs
