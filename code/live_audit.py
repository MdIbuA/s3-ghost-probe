"""Audit one request, then re-audit it with the live S3 ghost probe.

    python code/live_audit.py --case case_25                          # BEFORE: snapshot evidence only
    python code/live_audit.py --case case_25 --s3-bucket my-bucket    # AFTER: + live S3 probe
    python code/live_audit.py --case case_25 --s3-bucket my-bucket --prefix support/ --json

The S3 probe is read-only. AWS credentials come from the normal boto3 chain
(environment, ~/.aws, SSO, instance role).
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost_audit.live import apply_probe, other_owners  # noqa: E402
from ghost_audit.pipeline import run  # noqa: E402
from ghost_audit.planner import build_steps  # noqa: E402
from ghost_audit.s3_probe import S3GhostProbe  # noqa: E402


def render(title: str, case, steps: list[dict]) -> str:
    notes = {(ra.system, ra.resource_id): ra.state_note for ra in case.resources.values()}
    lines = [f"== {title}: {case.case_id} -> {case.status}", ""]
    for s in steps:
        ev = f"  [{s['evidence_id']}]" if s["evidence_id"] else ""
        lines.append(f"  {s['step_no']:>2}. {s['phase']:<12} {s['operation']:<25} {s['system_id']}:{s['resource_id']}{ev}")
        why = notes.get((s["system_id"], s["resource_id"]))
        if s["phase"] == "FINDING" and why:
            lines.append(f"      -> {why}")
    if not steps:
        lines.append("  (no steps)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--dataset", default=str(ROOT / "dataset"))
    ap.add_argument("--s3-bucket", default=None, help="probe this bucket live (omit for the BEFORE run)")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--region", default=None)
    ap.add_argument("--json", action="store_true", help="print machine-readable result")
    args = ap.parse_args(argv)

    dataset = Path(args.dataset)
    files = [dataset / "deletion_requests.csv", dataset / "sample_deletion_requests.csv"]
    cases, info = run(dataset, files, ROOT / "code" / "cache", offline=True)
    case = next((c for c in cases if c.case_id == args.case), None)
    if case is None:
        print(f"unknown case {args.case}", file=sys.stderr)
        return 2
    before = copy.deepcopy(case)
    before_steps = build_steps(before)
    out = {"case_id": case.case_id, "subject": case.subject.user_id if case.subject else None,
           "before": {"status": before.status, "steps": before_steps}}
    text = [render("BEFORE (snapshot evidence)", before, before_steps)]

    if args.s3_bucket:
        if case.subject is None:
            print("request email does not resolve to a known person; nothing to probe for", file=sys.stderr)
            return 2
        import boto3
        probe = S3GhostProbe(boto3.client("s3", region_name=args.region))
        auditor = info["auditor"]
        res = probe.probe(args.s3_bucket, case.subject.identifiers, args.prefix,
                          other_owners(auditor, case.subject.user_id))
        apply_probe(auditor, case, res)
        after_steps = build_steps(case)
        out["after"] = {"status": case.status, "steps": after_steps, "probe_notes": res.notes,
                        "scanned": {"keys": res.scanned_keys, "versions": res.scanned_versions,
                                    "versioning": res.versioning}}
        text.append("")
        text.append(f"   live S3 probe of s3://{args.s3_bucket}/{args.prefix}: versioning={res.versioning}, "
                    f"{res.scanned_keys} keys / {res.scanned_versions} versions scanned, "
                    f"{len({h.key for h in res.hits})} key(s) match {case.subject.user_id} "
                    f"({len(case.subject.identifiers)} identifiers incl. history)")
        text.append(render("AFTER (snapshot evidence + live S3 probe)", case, after_steps))
        for n in res.notes:
            text.append(f"  note: {n}")

    print(json.dumps(out, indent=1) if args.json else "\n".join(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
