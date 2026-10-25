"""Development evaluation against dataset/sample_output.csv.

    python code/evaluation/main.py            # run pipeline on the 32 samples and score them
    python code/evaluation/main.py --verbose  # also print every mismatching case

The reference answers are read only here, after predictions are written; the
pipeline itself never opens sample_output.csv.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import importlib.util  # noqa: E402

from metrics import load, score  # noqa: E402

_spec = importlib.util.spec_from_file_location("audit_entry", HERE.parent / "main.py")
entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entry)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--pred", default=str(HERE / "sample_predictions.csv"))
    args = ap.parse_args(argv)
    run_args = ["--requests", str(ROOT / "dataset" / "sample_deletion_requests.csv"), "--out", args.pred,
                "--trace", str(HERE / "sample_trace.json"), "--usage-json", str(HERE / "sample_usage.json")]
    if args.no_model:
        run_args.append("--no-model")
    rc = entry.main(run_args)
    res = score(load(args.pred), load(ROOT / "dataset" / "sample_output.csv"))
    summary = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in res.items() if k != "per_case"}
    print(json.dumps(summary, indent=1))
    for pc in res["per_case"]:
        if args.verbose and (not pc["seq_ok"]):
            print(f"\n{pc['case_id']} gold={pc['gold']} pred={pc['pred']}")
            for m in pc["missing"]:
                print("   missing", m)
            for e in pc["extra"]:
                print("   extra  ", e)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
