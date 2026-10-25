"""Fold a live S3 probe into an already-assessed case.

The probe's observations are linked to the case by identifier match (the probe
only returns objects naming the subject), go through the same policy table as
snapshot evidence, and the status is recomputed with the auditor's rule:
INCOMPLETE as soon as one non-retained record is non-compliant.
"""
from __future__ import annotations

from .audit import ACTION_TARGET, Auditor, CaseResult, ResourceAssessment
from .evidence import DISABLED, PRESENT
from .s3_probe import FLOW_SYSTEM, ProbeResult, absent_observation


def other_owners(auditor: Auditor, subject_uid: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for uid in auditor.idr.users:
        if uid == subject_uid:
            continue
        for i in auditor.idr.subject(uid, "9999").identifiers:
            out.setdefault(i, uid)
    return out


def apply_probe(auditor: Auditor, case: CaseResult, probe: ProbeResult) -> None:
    if case.status == "NOT_APPLICABLE":
        return
    obs = probe.observations or [absent_observation(probe.bucket, probe.prefix, probe.probed_at)]
    for o in obs:
        ra = ResourceAssessment(o.system, o.resource_id)
        ra.links.append(f"{o.file_id}: live S3 probe matched {', '.join(sorted(o.identifiers)) or 'nothing'}")
        if o.system == FLOW_SYSTEM:
            ra.category, ra.rule_id, ra.target = "DATA_FLOW", "data_flow", DISABLED
        else:
            ra.category = o.category or "SUPPORT_ATTACHMENT"
            rule = auditor.rules.get(ra.category, {})
            ra.rule_id = rule.get("rule_id", "default")
            ra.target = ACTION_TARGET.get(rule.get("required_action", "DELETE"), PRESENT)
        ra.state, ra.evidence_id, ra.state_note = o.state, o.file_id, o.detail
        case.resources[(ra.system, ra.resource_id)] = ra
    case.notes += [f"s3 probe: {n}" for n in probe.notes]
    noncompliant = [ra for ra in case.resources.values() if ra.compliant is False and ra.target != PRESENT]
    case.status = "INCOMPLETE" if noncompliant else "COMPLETE"
