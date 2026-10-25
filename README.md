# Ghost Probe: give your agent eyes behind S3 delete markers

**My agent said "Deleted." One S3 tool proved it wrong.**

`s3_ghost_probe` is a read-only Amazon S3 tool for AI agents. It finds personal data that a
deletion workflow *thinks* it removed. On a versioned bucket, `DeleteObject` only adds a delete
marker: the console says the file is gone, but every earlier version can still be read. The probe
walks every version and delete marker, matches them against everything that identifies the person
(current and historical emails, phone numbers, email hashes, user IDs), and reads the bucket's
replication, lifecycle and Object Lock settings.

Entry for the AWS Builder Center **"Give your Agent a Tool"** challenge (October 2026).
Demo video: https://drive.google.com/file/d/15hkWzt9d4iPxLSWxMiDmxKhChqo2gQiH/view

| | Before | After (+ `s3_ghost_probe`) |
|---|---|---|
| Verdict on case_25 | COMPLETE | **INCOMPLETE** |
| Sees | Evidence snapshots | The live bucket: every version, every delete marker |
| Hidden copies found | 0 | 2 recoverable files, one matched on the person's *previous* phone number |

Full write-up: [docs/aws_challenge_submission.md](docs/aws_challenge_submission.md)

## Reuse the tool in your own agent

The probe has no dependency on the rest of this project's audit code. Give it any boto3 S3
client and a set of normalized identifiers:

```python
from ghost_audit.s3_probe import S3GhostProbe

result = S3GhostProbe().probe(
    "my-bucket",
    {"email:jane@example.com", "phone:447700900111", "user:user_42"},
    prefix="support/",
)
for hit in result.hits:          # every matching version, current or hidden
    print(hit.key, hit.version_id, "latest" if hit.is_latest else "behind a delete marker", hit.locked)
print(result.notes)              # e.g. "no lifecycle rule expires noncurrent versions ..."
```

As a [Strands Agents](https://strandsagents.com) tool, see `code/strands_agent.py`:

```python
agent = Agent(model=model, tools=[audit_request, s3_ghost_probe])
```

`result.observations` uses the same `Observation` shape as every other evidence parser in this
agent, so the policy engine and planner needed no changes.

**Permissions:** read-only. Least-privilege IAM policy: [docs/s3_probe_iam_policy.json](docs/s3_probe_iam_policy.json).

## Run the full before/after demo

The agent audits the fictional HackerRank Orchestrate "Ghost Data Still Exists" dataset, which
is not copied here. Get it from HackerRank's public starter repo:

```bash
git clone https://github.com/MdIbuA/s3-ghost-probe.git && cd s3-ghost-probe
git clone --depth 1 https://github.com/interviewstreet/hackerrank-orchestrate-october26.git upstream
cp -r upstream/dataset ./dataset        # Windows PowerShell: Copy-Item -Recurse upstream\dataset .\dataset
pip install -r code/requirements.txt
```

Then, with AWS credentials configured for boto3:

```bash
python code/demo/seed_s3_demo.py                                  # private, versioned demo bucket; prints its name
python code/live_audit.py --case case_25                          # BEFORE
python code/live_audit.py --case case_25 --s3-bucket <bucket>     # AFTER
python code/demo/run_demo.py                                      # all scenes, Enter to advance
python code/demo/seed_s3_demo.py --teardown                       # delete the demo bucket
```

### The Strands agent

```bash
# Claude via the Anthropic API: put ANTHROPIC_API_KEY=... in an uncommitted .env
python code/strands_agent.py --model anthropic "Was case_25 really deleted? Attachments are in S3 bucket <bucket>"
python code/strands_agent.py --model bedrock   "..."   # Claude on Amazon Bedrock
python code/strands_agent.py --no-s3 --model anthropic "..."   # the BEFORE agent, without the tool
```

## Tests

```bash
python -m pytest code/tests/test_s3_probe.py -q   # 7 tests on an in-memory S3 fake; no AWS, no dataset
python -m pytest code/tests -q                    # all 19
```

## Layout

| Path | What |
|---|---|
| `code/ghost_audit/s3_probe.py` | **The new tool** |
| `code/ghost_audit/live.py` | Folds probe results into an audited case |
| `code/live_audit.py` | Before/after command for one case |
| `code/strands_agent.py` | Strands Agents front end (Anthropic, Bedrock or Ollama) |
| `code/demo/` | Demo bucket seeding/teardown and the scene runner |
| `code/tests/test_s3_probe.py` | Tool tests on a fake S3 |
| `code/ghost_audit/*` (rest) | The original audit agent: intent, identity history, evidence parsing, policy, planning |
| `code/README.md` | The original agent's architecture and decisions |

## License

MIT. See [LICENSE](LICENSE).
