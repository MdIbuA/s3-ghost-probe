# Usage report — final full-dataset run

## Models

| Provider | Model | Purpose |
|---|---|---|
| Ollama, self-hosted on the developer machine | `qwen3.5:latest` (9.7B, Q4_K_M) | Screenshot transcription (vision), request-intent second opinion |

No hosted API was called. No API key is used.

## How the counts were measured

Every model call goes through `ghost_audit/llm.py`. For each live call, Ollama returns
`prompt_eval_count` (input tokens) and `eval_count` (output tokens), and the client stores
them with the response in `code/cache/llm/`. All counts below are those **measured**
values, not estimates.

The run that produced `output.csv` (`python code/main.py`) read every response from that
cache: 409 cached calls, 0 live calls, about 2 s wall time (re-measured in the final audit; output sha256 `145c61c0…2d12`). The tokens below are the
measured tokens of the live calls that originally produced those cached responses. That is
the real model usage behind `output.csv`.

## Per-model, per-purpose totals

| Model / purpose | Calls | Input tokens | Output tokens | Total tokens | Live generation time |
|---|---|---|---|---|---|
| qwen3.5:latest — vision transcription (11 screenshots) | 11 | 23,829 | 8,469 | 32,298 | ≈1,429 s |
| qwen3.5:latest — intent classification (398 distinct request texts) | 398 | 76,391 | 12,736 | 89,127 | ≈2,838 s |
| **Overall** | **409** | **100,220** | **21,205** | **121,425** | ≈4,267 s |

* Requests in `output.csv`: 518. The 398 intent calls cover every distinct request text in
  the 518 eval and 32 sample requests; identical texts are classified once.
* **Average tokens per request** (121,425 / 518): **≈234 tokens**.
* Vision calls: 1 per screenshot (11 of 94 evidence files). PDFs, SQL, logs and JSON are
  parsed deterministically with no model.
* The first vision warm-up run aborted with an HTTP 500 from Ollama on the second image. It was
  rerun after retry logic was added. Token counts of failed attempts were not recorded and are not
  included.

## Cost

| Item | Total | Per request |
|---|---|---|
| API spend | **$0.00** | **$0.00** |
| Estimated cost | $0.00 (local model, no per-token price) | $0.00 |

The only cost is local compute: about 71 minutes of model time on one consumer GPU. No
hosted-model price comparison is claimed, because none was measured.

## Reproduce

```bash
python code/main.py --no-model   # cached responses only; uncached screenshots are skipped with a warning, intent falls back to rules
python code/main.py              # live calls for anything not yet cached
```

`code/evaluation/last_run_usage.json` is regenerated on every run with the same counters.
