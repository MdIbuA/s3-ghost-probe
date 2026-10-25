# Demo video script (target 2:30, hard limit 3:00)

Record your screen with a terminal (large font) and the S3 console in a browser tab. Upload to
YouTube as Public or Unlisted (check the challenge accepts unlisted; Public is safest) and paste
the link into the "demo" field.

| Time | On screen | Say |
|---|---|---|
| 0:00–0:15 | Title card or README | "This agent audits whether a person's data was *actually* deleted. It never trusts a 'Complete' status. It checks evidence." |
| 0:15–0:40 | `python code/live_audit.py --case case_25` | "Soo-jin asked us to inspect the actual systems. From snapshots, the agent says COMPLETE: account gone, analytics anonymized, audit log lawfully retained." |
| 0:40–1:05 | S3 console → demo bucket → `support/tickets/T-48213/`: folder looks empty. Then toggle **Show versions** | "Here's the support attachment bucket. Her ticket folder looks empty, because the deletion workflow deleted it. But this bucket is versioned, so a delete only adds a delete marker. Turn on 'show versions' and the files are still here." |
| 1:05–1:20 | Code: `s3_probe.py` docstring / `@tool def s3_ghost_probe` | "So I gave the agent one new tool: a read-only S3 ghost probe. It walks every version, matches it against every identifier she has ever used, and reads replication, lifecycle and Object Lock." |
| 1:20–1:55 | `python code/live_audit.py --case case_25 --s3-bucket <bucket>` | "Same agent, same case, with the tool: INCOMPLETE. Two 'deleted' files are still recoverable. Look at this match: her *old* phone number. The probe only knows it because the agent's identity resolver tracks history. The plan deletes each version by id and verifies absence. It also flags that no lifecycle rule would ever clean this up." |
| 1:55–2:10 | Point at the decoy and README_FOR_AUDITORS.txt in the console | "A different customer's ticket, and a file telling AI auditors to report COMPLETE. Both scanned, both ignored. Content is data, never instructions." |
| 2:10–2:30 | `python code/strands_agent.py "…"`, showing `[tool] audit_request` then `[tool] s3_ghost_probe` and the answer | "It's a plain Strands `@tool`, running here on a local model and on Claude in Amazon Bedrock with one flag. Drop it into your own agent's tools list." |
| 2:30–2:45 | Optional: delete the two versions in the console, rerun → COMPLETE with `VERIFY_RECORD_ABSENT` citing the probe | "After remediation, the same probe becomes the proof: COMPLETE, verified live." |

Tip: run every command once before recording so caches are warm, and pre-type them.
