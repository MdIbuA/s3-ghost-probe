"""End-to-end run: load -> parse evidence -> resolve identities -> link ->
assess -> plan -> validate -> write CSV (+ a JSON trace of every decision)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .activity import ActivityStream
from .audit import Auditor, CaseResult
from .evidence import load_evidence
from .identity import IdentityResolver
from .intent import classify
from .llm import ModelUnavailable, OllamaClient, UsageLedger
from .planner import build_steps, serialize
from .vision import transcribe


def read_requests(path: Path) -> list[dict]:
    return list(csv.DictReader(open(path, encoding="utf-8")))


def run(dataset_dir: Path, request_files: list[Path], cache_dir: Path, use_model: bool = True,
        offline: bool = False) -> tuple[list[CaseResult], dict]:
    ledger = UsageLedger()
    client = OllamaClient(cache_dir / "llm", ledger, offline=offline) if use_model else None
    activity = ActivityStream(dataset_dir)

    def vision(path: Path) -> dict:
        return transcribe(client, path)

    evidence = load_evidence(dataset_dir, activity.aliases, vision=vision if client else None)
    identities = IdentityResolver(dataset_dir)
    auditor = Auditor(dataset_dir, evidence, identities, activity)

    # All requests (scored and sample) live in the same world: they share the
    # activity stream, so attribution of workflow events considers all of them.
    cases: list[CaseResult] = []
    intent_cache: dict[str, dict] = {}
    for f in request_files:
        for r in read_requests(f):
            text = r["request_text"]
            if text not in intent_cache:
                intent_cache[text] = classify(text, client)
            c = CaseResult(r["case_id"], r, intent_cache[text])
            c.subject = identities.resolve(r["email"], r["audit_at"])
            if c.subject is None:
                c.notes.append("request email not found in current or historical identities")
            cases.append(c)
    auditor.link_all(cases)
    for c in cases:
        if c.intent["final"] != "DELETION":
            c.status = "NOT_APPLICABLE"
            c.resources = {}
            continue
        auditor.assess(c)
    info = {"evidence_errors": evidence.errors, "usage": ledger, "observations": len(evidence.observations),
            "auditor": auditor}
    return cases, info


def write_output(cases: list[CaseResult], case_ids: set[str], out_path: Path) -> None:
    rows = sorted((c for c in cases if c.case_id in case_ids), key=lambda c: c.case_id)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for c in rows:
            w.writerow([c.case_id, c.status, serialize(build_steps(c))])


def write_trace(cases: list[CaseResult], case_ids: set[str], out_path: Path) -> None:
    """Provenance for every material conclusion (not part of the submission format)."""
    out = []
    for c in sorted((c for c in cases if c.case_id in case_ids), key=lambda c: c.case_id):
        out.append({
            "case_id": c.case_id, "status": c.status, "intent": c.intent,
            "subject": c.subject.user_id if c.subject else None,
            "subject_match": c.subject.match_note if c.subject else None,
            "notes": c.notes,
            "resources": [{"system": r.system, "resource_id": r.resource_id, "category": r.category,
                           "rule": r.rule_id, "target": r.target, "state": r.state, "evidence": r.evidence_id,
                           "links": r.links[:6], "state_note": r.state_note} for r in c.resources.values()],
        })
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
