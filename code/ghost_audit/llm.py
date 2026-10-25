"""Thin client for the local Ollama model plus a usage ledger.

Every model call goes through `OllamaClient.generate`, which records the
provider, model name, measured prompt/completion token counts and latency in
`UsageLedger`. Responses are cached on disk keyed by a hash of
(model, prompt, image bytes) so reruns are deterministic and free; cached hits
are counted separately from live calls.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = os.environ.get("GHOST_AUDIT_MODEL", "qwen3.5:latest")
DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


@dataclass
class UsageRecord:
    purpose: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    cached: bool


@dataclass
class UsageLedger:
    records: list[UsageRecord] = field(default_factory=list)

    def add(self, rec: UsageRecord) -> None:
        self.records.append(rec)

    def summary(self) -> dict:
        out: dict = {}
        for r in self.records:
            key = (r.model, r.purpose)
            s = out.setdefault(key, {"live_calls": 0, "cached_calls": 0, "prompt_tokens": 0,
                                     "completion_tokens": 0, "seconds": 0.0})
            if r.cached:
                s["cached_calls"] += 1
            else:
                s["live_calls"] += 1
            # Token counts are the counts measured when the response was produced
            # (stored alongside cached responses), so totals reflect real usage.
            s["prompt_tokens"] += r.prompt_tokens
            s["completion_tokens"] += r.completion_tokens
            s["seconds"] += r.seconds
        return out


class ModelUnavailable(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, cache_dir: Path, ledger: UsageLedger, model: str = DEFAULT_MODEL,
                 host: str = DEFAULT_HOST, offline: bool = False):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = ledger
        self.model = model
        self.host = host.rstrip("/")
        self.offline = offline

    def _key(self, prompt: str, image: bytes | None) -> str:
        h = hashlib.sha256()
        h.update(self.model.encode())
        h.update(b"\0")
        h.update(prompt.encode("utf-8"))
        if image:
            h.update(b"\0img\0")
            h.update(image)
        return h.hexdigest()[:32]

    def generate_json(self, purpose: str, prompt: str, image_path: Path | None = None,
                      timeout: int = 1200) -> dict:
        image = Path(image_path).read_bytes() if image_path else None
        key = self._key(prompt, image)
        cache_file = self.cache_dir / f"{purpose}_{key}.json"
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            self.ledger.add(UsageRecord(purpose, cached["model"], cached["prompt_tokens"],
                                        cached["completion_tokens"], 0.0, True))
            return cached["response"]
        if self.offline:
            raise ModelUnavailable(f"offline mode and no cached response for {purpose}:{key}")
        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "think": False,
            "options": {"temperature": 0, "seed": 7},
        }
        if image:
            body["images"] = [base64.b64encode(image).decode()]
        req = urllib.request.Request(f"{self.host}/api/generate", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        t0 = time.time()
        raw = None
        for attempt in range(3):
            try:
                raw = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == 2:
                    raise ModelUnavailable(f"Ollama call failed: {exc}") from exc
                time.sleep(10 * (attempt + 1))
        elapsed = time.time() - t0
        try:
            response = json.loads(raw["response"])
        except json.JSONDecodeError as exc:
            raise ModelUnavailable(f"model returned non-JSON output for {purpose}") from exc
        rec = {
            "model": raw.get("model", self.model),
            "prompt_tokens": int(raw.get("prompt_eval_count") or 0),
            "completion_tokens": int(raw.get("eval_count") or 0),
            "seconds": round(elapsed, 2),
            "response": response,
        }
        cache_file.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        self.ledger.add(UsageRecord(purpose, rec["model"], rec["prompt_tokens"],
                                    rec["completion_tokens"], elapsed, False))
        return response
