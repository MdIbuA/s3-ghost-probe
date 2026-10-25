"""Scene-by-scene demo for screen recording: press Enter to advance.

    python code/demo/run_demo.py                 # all scenes
    python code/demo/run_demo.py --no-agent      # skip the Strands/Claude scene
    python code/demo/run_demo.py --auto          # no pauses (rehearsal)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUCKET = None

BOLD, CYAN, RESET = "\033[1m", "\033[96m", "\033[0m"


def bucket() -> str:
    import boto3
    s = boto3.Session()
    acct = s.client("sts").get_caller_identity()["Account"]
    return f"ghost-audit-demo-{acct}-{s.region_name or 'us-east-1'}"


def scene(title: str, caption: str, cmd: list[str], auto: bool) -> None:
    print(f"\n{BOLD}{CYAN}=== {title} ==={RESET}\n{caption}\n")
    print(f"{BOLD}> {' '.join(c if ' ' not in c else repr(c) for c in cmd[1:])}{RESET}\n", flush=True)
    subprocess.run(cmd, cwd=ROOT)
    if not auto:
        input(f"\n{CYAN}[Enter for next scene]{RESET}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-agent", action="store_true")
    ap.add_argument("--auto", action="store_true")
    a = ap.parse_args()
    if sys.platform == "win32":
        import os
        os.system("")  # enable ANSI colors in the Windows console
        sys.stdout.reconfigure(encoding="utf-8")
    b = bucket()
    py = sys.executable
    if not a.auto:
        input(f"{CYAN}Ghost Probe demo. Bucket: {b}. [Enter to start]{RESET}")
    scene("1. BEFORE: the agent with snapshot evidence only",
          "Soo-jin (case_25) asked us to 'inspect the actual systems'.",
          [py, "code/live_audit.py", "--case", "case_25"], a.auto)
    scene("2. AFTER: the same agent with the live S3 ghost probe",
          "One new tool: read every S3 version, match every identifier she has ever used.",
          [py, "code/live_audit.py", "--case", "case_25", "--s3-bucket", b], a.auto)
    if not a.no_agent:
        scene("3. The tool inside a Strands agent (Claude)",
              "Plain @tool: the agent decides to call it on its own.",
              [py, "code/strands_agent.py", "--model", "anthropic",
               f"Soo-jin's request is case_25. Our support attachments live in S3 bucket {b}. "
               "Was her data really deleted?"], a.auto)
    print(f"\n{BOLD}{CYAN}=== Ghost data, found. ==={RESET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
