"""Turn an assessed case into the ordered steps_json array.

Ordering contract (problem_statement.md): phases FINDING -> REMEDIATION ->
VERIFICATION; inside a phase, the operation order of the official table, then
system_id, then resource_id ascending. Because DISABLE_DATA_FLOW and
BLOCK_RESTORE come first in that table, recreation paths are always shut
before any record is deleted, redacted or anonymized.
"""
from __future__ import annotations

import json

from .audit import CaseResult, ResourceAssessment
from .evidence import ABSENT, ANONYMIZED, BLOCKED, DISABLED, PRESENT, REDACTED

FINDING_OPS = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"]
REMEDIATION_OPS = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"]
VERIFICATION_OPS = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                    "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
OP_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
PHASE_OPS = {"FINDING": FINDING_OPS, "REMEDIATION": REMEDIATION_OPS, "VERIFICATION": VERIFICATION_OPS}

TARGET_OPS = {
    # target -> (finding, remediation, verification)
    ABSENT: ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    REDACTED: ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    ANONYMIZED: ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    DISABLED: ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    BLOCKED: ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
    PRESENT: (None, None, "VERIFY_RECORD_PRESENT"),
}


def _step(phase, ra: ResourceAssessment, op, evidence_id=""):
    return {"phase": phase, "system_id": ra.system, "resource_id": ra.resource_id,
            "operation": op, "state": OP_STATE[op], "evidence_id": evidence_id}


def build_steps(case: CaseResult) -> list[dict]:
    if case.status == "NOT_APPLICABLE":
        return []
    steps = []
    complete = case.status == "COMPLETE"
    for ra in case.resources.values():
        f_op, r_op, v_op = TARGET_OPS[ra.target]
        if ra.compliant is False and f_op:
            steps.append(_step("FINDING", ra, f_op, ra.evidence_id))
            steps.append(_step("REMEDIATION", ra, r_op))
        # COMPLETE: verification proves the current state and cites the proof;
        # a record with no pre-audit proof cannot be "verified" and is left out
        # (recorded in the case notes). INCOMPLETE: verification describes the
        # checks to run after remediation, so it has no evidence yet.
        if complete and not ra.evidence_id:
            note = f"{ra.resource_id}: no pre-audit evidence of its state; verification step omitted"
            if note not in case.notes:
                case.notes.append(note)
            continue
        steps.append(_step("VERIFICATION", ra, v_op, ra.evidence_id if complete else ""))
    return order_steps(steps)


def order_steps(steps: list[dict]) -> list[dict]:
    phase_rank = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}

    def key(s):
        ops = PHASE_OPS[s["phase"]]
        return (phase_rank[s["phase"]], ops.index(s["operation"]), s["system_id"], s["resource_id"])

    out = sorted(steps, key=key)
    return [{"step_no": i + 1, **s} for i, s in enumerate(out)]


def serialize(steps: list[dict]) -> str:
    keys = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]
    return json.dumps([{k: s[k] for k in keys} for s in steps], ensure_ascii=False, separators=(",", ":"))
