"""Usage accounting for the usage report (measured counts only)."""
from __future__ import annotations

import json
from pathlib import Path

from .llm import UsageLedger

PROVIDER = "Ollama (local, self-hosted)"
# A local model has no per-token price, so cost is reported as $0 API spend.
PRICE_PER_MTOK = {"input": 0.0, "output": 0.0}


def write_usage_json(ledger: UsageLedger, n_requests: int, path: Path) -> dict:
    per = {}
    tot = {"live_calls": 0, "cached_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    for (model, purpose), s in ledger.summary().items():
        per[f"{model}|{purpose}"] = s
        for k in tot:
            tot[k] += s[k]
    tot["total_tokens"] = tot["prompt_tokens"] + tot["completion_tokens"]
    tot["avg_tokens_per_request"] = tot["total_tokens"] / n_requests if n_requests else 0
    tot["estimated_cost_usd"] = (tot["prompt_tokens"] * PRICE_PER_MTOK["input"] +
                                 tot["completion_tokens"] * PRICE_PER_MTOK["output"]) / 1e6
    out = {"provider": PROVIDER, "requests": n_requests, "per_model_purpose": per, "totals": tot}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out
