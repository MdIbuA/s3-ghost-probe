"""Ghost Data Still Exists - deletion audit agent.

Usage (from the repository root):
    python code/main.py                     # audit dataset/deletion_requests.csv -> ./output.csv
    python code/main.py --no-model          # rules + cached model output only, never calls the model
    python code/main.py --requests dataset/sample_deletion_requests.csv --out code/evaluation/sample_predictions.csv

All 550 requests (scored + sample) are loaded together because they share one
activity stream; only rows from --requests are written.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost_audit.pipeline import read_requests, run, write_output, write_trace  # noqa: E402
from ghost_audit.validate import validate_output  # noqa: E402
from ghost_audit.usage import write_usage_json  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(ROOT / "dataset"))
    ap.add_argument("--requests", default=None, help="request CSV to answer (default dataset/deletion_requests.csv)")
    ap.add_argument("--out", default=str(ROOT / "output.csv"))
    ap.add_argument("--trace", default=None, help="optional JSON file with per-case provenance")
    ap.add_argument("--no-model", action="store_true", help="do not call the local model; use cache only")
    ap.add_argument("--usage-json", default=str(ROOT / "code" / "evaluation" / "last_run_usage.json"))
    args = ap.parse_args(argv)

    dataset = Path(args.dataset)
    req_path = Path(args.requests) if args.requests else dataset / "deletion_requests.csv"
    all_files = [dataset / "deletion_requests.csv", dataset / "sample_deletion_requests.csv"]
    if req_path.resolve() not in {p.resolve() for p in all_files}:
        all_files.append(req_path)
    cases, info = run(dataset, all_files, ROOT / "code" / "cache", offline=args.no_model)
    wanted = {r["case_id"] for r in read_requests(req_path)}
    out = Path(args.out)
    write_output(cases, wanted, out)
    if args.trace:
        write_trace(cases, wanted, Path(args.trace))
    write_usage_json(info["usage"], len(wanted), Path(args.usage_json))

    for e in info["evidence_errors"]:
        print("evidence warning:", e, file=sys.stderr)
    errs = validate_output(out, req_path, dataset, expected_rows=len(wanted))
    statuses = {}
    for c in cases:
        if c.case_id in wanted:
            statuses[c.status] = statuses.get(c.status, 0) + 1
    print(f"wrote {out} rows={len(wanted)} statuses={json.dumps(statuses)}")
    for w in getattr(validate_output, "warnings", []):
        print("validation warning:", w, file=sys.stderr)
    if errs:
        print(f"VALIDATION FAILED ({len(errs)} problems):", file=sys.stderr)
        for e in errs[:50]:
            print("  ", e, file=sys.stderr)
        return 1
    print("validation: all contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
