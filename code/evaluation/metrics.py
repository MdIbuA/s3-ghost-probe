"""Scoring of predicted rows against reference rows (development only).

Metrics
  status_accuracy   - deletion_status exact match per case
  step P/R/F1       - multiset match of full steps (all fields except step_no)
  field_accuracy    - for steps aligned on (phase, system_id, resource_id), share of
                      operation/state/evidence_id fields that agree (lenient: extra
                      predicted steps are not penalized)
  field_accuracy_strict_positional - step i vs step i on all 7 fields, denominator
                      = longer list, so extra and missing steps both count
  sequence_accuracy - steps_json array identical (order included)
"""
from __future__ import annotations

import csv
import json
from collections import Counter

FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def load(path) -> dict[str, tuple[str, list]]:
    return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"]))
            for r in csv.DictReader(open(path, encoding="utf-8"))}


def tup(s):
    return tuple(s[f] for f in FIELDS)


def score(pred: dict, gold: dict) -> dict:
    cases = sorted(gold)
    status_ok = seq_ok = 0
    tp = fp = fn = 0
    f_match = f_total = 0
    strict_match = strict_total = 0
    per_case = []
    missing_find = missing_rem = unsupported = 0
    for cid in cases:
        gs, gsteps = gold[cid]
        ps, psteps = pred.get(cid, ("<missing>", []))
        status_ok += ps == gs
        same_seq = [tup(s) for s in psteps] == [tup(s) for s in gsteps]
        seq_ok += same_seq
        gc, pc = Counter(map(tup, gsteps)), Counter(map(tup, psteps))
        inter = sum((gc & pc).values())
        tp += inter
        fp += sum(pc.values()) - inter
        fn += sum(gc.values()) - inter
        for s in gsteps:
            if tup(s) not in pc:
                missing_find += s["phase"] == "FINDING"
                missing_rem += s["phase"] == "REMEDIATION"
        for s in psteps:
            if s["phase"] == "FINDING" and tup(s) not in gc:
                unsupported += 1
        galign = {(s["phase"], s["system_id"], s["resource_id"]): s for s in gsteps}
        for s in psteps:
            g = galign.get((s["phase"], s["system_id"], s["resource_id"]))
            if g:
                f_total += 3
                f_match += sum(s[f] == g[f] for f in ("operation", "state", "evidence_id"))
        f_total += 3 * max(0, len(gsteps) - sum(1 for s in psteps if (s["phase"], s["system_id"], s["resource_id"]) in galign))
        # strict variant: step i vs step i on all 7 fields, denominator = longer
        # list, so extra predicted steps are penalized too
        L = max(len(gsteps), len(psteps))
        strict_total += 7 * L
        for i in range(min(len(gsteps), len(psteps))):
            strict_match += sum(gsteps[i][k] == psteps[i][k] for k in ("step_no",) + FIELDS)
        per_case.append({"case_id": cid, "gold": gs, "pred": ps, "seq_ok": same_seq,
                         "missing": [tup(s) for s in gsteps if tup(s) not in pc],
                         "extra": [tup(s) for s in psteps if tup(s) not in gc]})
    n = len(cases)
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {
        "cases": n,
        "status_accuracy": status_ok / n,
        "step_precision": p, "step_recall": r, "step_f1": 2 * p * r / (p + r) if p + r else 0.0,
        "field_accuracy": f_match / f_total if f_total else 0.0,
        "field_accuracy_strict_positional": strict_match / strict_total if strict_total else 0.0,
        "sequence_accuracy": seq_ok / n,
        "missing_finding_steps": missing_find, "missing_remediation_steps": missing_rem,
        "unsupported_findings": unsupported,
        "per_case": per_case,
    }
