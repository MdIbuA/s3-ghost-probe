# Ghost Data Still Exists — deletion audit agent

An evidence-first agent that decides, for each request in `dataset/deletion_requests.csv`,
whether a deletion is `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE`, and emits the ordered
findings → remediation → verification steps.

## Setup

* Python 3.10+ (tested on 3.10.0, Windows 11). Standard library only for the core
  pipeline (`csv`, `json`, `sqlite3`, `re`).
* `pymupdf` for PDF text extraction: `pip install pymupdf`
* `pytest` (optional) for the test-suite: `pip install pytest`
* Optional local model: [Ollama](https://ollama.com) serving `qwen3.5:latest`
  (`ollama pull qwen3.5`). Override with `GHOST_AUDIT_MODEL` / `OLLAMA_HOST`.
  No API keys or secrets are used anywhere.

Model responses are cached in `code/cache/llm/` (keyed by a hash of model + prompt +
image bytes), so a rerun is deterministic and makes no model calls.

## Run

From the repository root:

```bash
python code/main.py                       # writes ./output.csv (518 rows) and validates it
python code/main.py --no-model            # never call the model: use cached responses only
python code/main.py --trace trace.json    # also dump per-case provenance (links, states, evidence)
python code/evaluation/main.py --verbose  # score the 32 public samples against sample_output.csv
python -m pytest code/tests -q            # team-created adversarial cases
```

`main.py` exits non-zero if the written `output.csv` fails any contract check
(header, row count, ordering, vocabulary, numbering, evidence references, timing).

## Architecture

```
load CSVs ─► parse evidence (logs/JSON/SQL/TXT/PDF/PNG) ─► Observations
                                                               │
request ─► intent (rules + local LLM 2nd opinion) ─► identity resolution (snapshot + audit history)
                                                               │
          link resources to the case  ◄── evidence identifiers in the case window
                                      ◄── data-catalog LINK_RESOURCE_IDENTITY rows
                                      ◄── deletion-workflow activity in the reporting window
                                                               │
          state at audit_at (latest pre-audit observation; any surviving replica copy wins)
                                                               │
          policy (deletion_rules.csv) ─► target state per record ─► findings / plan / checks
                                                               │
          deterministic ordering + serialization ─► output.csv ─► validator (on the file)
```

| Module | Role |
|---|---|
| `ghost_audit/evidence.py` | One normalizer per format → `Observation(file, time, system, resource, state, identifiers)`. Seven log dialects (key=value, bracketed, pipe, syslog, JSON lines, nested `message="..."`), trace-id joining of lookup keys to results, SQLite execution of SQL snapshots (CREATE/INSERT only), JSON bundles/exports, handover text, PDF text, screenshot transcriptions. |
| `ghost_audit/vision.py` | Screenshot → JSON transcription with the local vision model. Only transcribes; rows are kept separate so identifiers never bleed between table rows. |
| `ghost_audit/identity.py` | Request email → stable `user_id` → every current/historical email, phone, hash (`user_audit_log.csv`). |
| `ghost_audit/activity.py` | Activity stream: catalog links, aliases (e.g. statement numbers), workflow events, intended treatment. Activity rows are claims: they help *find* records, never prove state. |
| `ghost_audit/audit.py` | Case linking (evidence → catalog → workflow attribution with one-to-one account matching and an owner index that refuses records naming another person), state reconstruction, policy, status. |
| `ghost_audit/intent.py` | Deletion-authorization classifier: multilingual rules with negation/purpose detection; the local LLM is asked too and decides only when the rules are not confident. |
| `ghost_audit/planner.py` | Steps and ordering (phase → official operation order → system_id → resource_id). |
| `ghost_audit/validate.py` | Contract validator run on the written CSV. |
| `evaluation/` | Sample scoring (`main.py`, `metrics.py`) and the required reports. |
| `tests/` | Adversarial fixtures (old email, non-deletion, misleading success + post-audit evidence, tombstone/retention/recreation ordering, injection + decoy + malformed file). |

## Key decisions

* **Claims vs proof.** `reported_status`, `reported_result` and workflow rows never establish
  state. State comes only from evidence files captured at or before `audit_at`; findings and
  COMPLETE verifications cite that file.
* **Time.** A record is linked to a case when an observation naming the subject falls in the
  case window (`requested_at − 30 d` … `audit_at`, never reaching back into an earlier case of the
  same person). State and citations use only files whose manifest `captured_at ≤ audit_at`.
* **Replica drift.** At the latest pre-audit instant, any surviving copy (primary or replica)
  makes the record present.
* **Policy.** Category from the evidence record type when given, else from the system; support
  items are attachments (DELETE) when evidence or workflow marks them so, else messages (REDACT);
  analytics → ANONYMIZE; security audit / legal hold → RETAIN (verify present, never deleted);
  backups → RETAIN with restore blocked; tombstones → REDACT (stable `user_id` may stay).
* **Recreation first.** The official operation order puts `DISABLE_DATA_FLOW` and
  `BLOCK_RESTORE` before any record change.
* **Untrusted evidence.** File contents are matched with regexes and compared to known
  identifiers; SQL is executed in an in-memory SQLite database; model prompts quote content as
  data and the model never decides a status.

## Known limitations

* Records that carry no identifiers anywhere (already-deleted accounts, anonymized analytics)
  can only be attributed through workflow timing. When several cases share a reporting
  timestamp and the workflow results match, attribution is a tie broken by event/case order
  (logged in the trace notes).
* An authorized request with no linkable records is reported `COMPLETE` with `[]` and a trace
  note; nothing can be cited for it.

## Live S3 ghost probe (added tool)

`ghost_audit/s3_probe.py` lets the agent probe a live Amazon S3 bucket at audit time: every
object version and delete marker is matched against the subject's current and historical
identifiers, and replication, noncurrent-version lifecycle and Object Lock are read. The result is
ordinary `Observation`s, so policy and planning are unchanged. It is read-only
(`docs/s3_probe_iam_policy.json`) and does not affect `output.csv`.

```bash
python code/live_audit.py --case case_25                       # snapshot evidence only
python code/live_audit.py --case case_25 --s3-bucket <bucket>  # + live S3 probe
python code/strands_agent.py "Was case_25 really deleted? Bucket: <bucket>"   # Strands Agents front end
```

Write-up: [`docs/aws_challenge_submission.md`](../docs/aws_challenge_submission.md).
