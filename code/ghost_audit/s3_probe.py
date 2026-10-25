"""Live Amazon S3 probe: the agent's one new tool.

The rest of the agent reasons over evidence *snapshots*. This tool lets it look
at a real bucket at audit time and answer the question snapshots cannot: is
any version of the subject's data still recoverable?

On a versioned bucket, DeleteObject only stacks a delete marker on top of the
key. The console, `aws s3 ls` and every "list objects" call then say the
object is gone, while the old bytes stay readable by version id until each
version is deleted explicitly or a lifecycle rule expires it. A deletion
workflow that "succeeded" against such a bucket leaves ghost data.

For every key under a prefix the probe walks *all* versions and delete
markers, matches each version against the subject's identifiers (every
current and historical email, phone, email hash and user_id from the identity
resolver) in the key name, user metadata, object tags and, for small text
objects, the content. It also reads the bucket's recreation/restore surface:
replication rules (copies the data to another bucket), lifecycle rules for
noncurrent versions, and Object Lock retention / legal holds (which make a
version a RETAIN record instead of a DELETE one).

The result is plain `Observation`s, the same shape every evidence parser
produces, so the auditor's policy, planner and ordering apply unchanged.

The probe is read-only (List/Get calls only; see docs/s3_probe_iam_policy.json).
Object content is untrusted data: it is only regex-matched for identifiers and
is never executed or sent to a model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .evidence import ABSENT, ACTIVE, PRESENT, Observation, extract_identifiers

TEXT_SUFFIXES = (".txt", ".json", ".csv", ".log", ".md", ".eml", ".xml", ".html", ".tsv", ".yaml", ".yml")
MAX_CONTENT_BYTES = 256 * 1024

SYSTEM = "object_storage"
FLOW_SYSTEM = "integration_service"


def _iso(dt) -> str:
    if isinstance(dt, str):
        return dt
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _code(exc) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


@dataclass
class VersionHit:
    key: str
    version_id: str
    last_modified: str
    is_latest: bool
    matched: set[str]
    others: set[str]            # identifiers of *other* known people in the same object
    locked: str = ""            # "legal_hold" / "retention_until=..." when Object Lock protects it


@dataclass
class ProbeResult:
    bucket: str
    prefix: str
    probed_at: str
    versioning: str = "Disabled"
    observations: list[Observation] = field(default_factory=list)
    hits: list[VersionHit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    scanned_versions: int = 0
    scanned_keys: int = 0


class S3GhostProbe:
    def __init__(self, s3_client=None, max_content_bytes: int = MAX_CONTENT_BYTES, now: str | None = None):
        if s3_client is None:
            import boto3
            s3_client = boto3.client("s3")
        self.s3 = s3_client
        self.max_bytes = max_content_bytes
        self.now = now

    # ------------------------------------------------------------------
    def probe(self, bucket: str, subject_ids: set[str], prefix: str = "",
              other_owners: dict[str, str] | None = None) -> ProbeResult:
        """`subject_ids`: normalized identifiers (`email:..`, `phone:..`, `hash:..`, `user:..`).
        `other_owners`: identifier -> user_id of everyone else the agent knows, used to
        spot objects shared with another person (those are redacted, not deleted)."""
        other_owners = other_owners or {}
        now = self.now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = ProbeResult(bucket, prefix, now)
        res.versioning = self.s3.get_bucket_versioning(Bucket=bucket).get("Status", "Disabled")
        lock_enabled = self._lock_enabled(bucket)

        versions, markers = self._list_versions(bucket, prefix)
        res.scanned_versions = len(versions)
        by_key: dict[str, list[dict]] = {}
        for v in versions:
            by_key.setdefault(v["Key"], []).append(v)
        res.scanned_keys = len(set(by_key) | {m["Key"] for m in markers})

        for key, vs in sorted(by_key.items()):
            for v in vs:
                ids = self._identifiers(bucket, key, v)
                matched = ids & subject_ids
                if not matched:
                    continue
                others = {i for i in ids if i in other_owners and i not in subject_ids}
                hit = VersionHit(key, v["VersionId"], _iso(v["LastModified"]), bool(v.get("IsLatest")),
                                 matched, others)
                if lock_enabled:
                    hit.locked = self._lock_state(bucket, key, v["VersionId"], now)
                res.hits.append(hit)

        latest_marker = {m["Key"]: m for m in markers if m.get("IsLatest")}
        keys_hit = sorted({h.key for h in res.hits})
        for key in keys_hit:
            res.observations.append(self._key_observation(res, key, latest_marker.get(key)))
        res.observations += self._replication(bucket, keys_hit, res)
        self._lifecycle(bucket, res, keys_hit)
        return res

    # ------------------------------------------------------------------
    def _list_versions(self, bucket: str, prefix: str) -> tuple[list[dict], list[dict]]:
        versions, markers = [], []
        kw = {"Bucket": bucket, "Prefix": prefix}
        while True:
            page = self.s3.list_object_versions(**kw)
            versions += page.get("Versions", [])
            markers += page.get("DeleteMarkers", [])
            if not page.get("IsTruncated"):
                return versions, markers
            kw["KeyMarker"] = page.get("NextKeyMarker")
            kw["VersionIdMarker"] = page.get("NextVersionIdMarker")

    def _identifiers(self, bucket: str, key: str, v: dict) -> set[str]:
        vid = v["VersionId"]
        parts = [key]
        head = self.s3.head_object(Bucket=bucket, Key=key, VersionId=vid)
        parts += [f"{k}={val}" for k, val in (head.get("Metadata") or {}).items()]
        try:
            tags = self.s3.get_object_tagging(Bucket=bucket, Key=key, VersionId=vid).get("TagSet", [])
            parts += [f"{t['Key']}={t['Value']}" for t in tags]
        except Exception as exc:
            if _code(exc) not in {"AccessDenied", "NoSuchTagSet"}:
                raise
        ctype = (head.get("ContentType") or "").lower()
        is_text = ctype.startswith("text/") or "json" in ctype or "csv" in ctype or key.lower().endswith(TEXT_SUFFIXES)
        if is_text and v.get("Size", 0) > 0:
            body = self.s3.get_object(Bucket=bucket, Key=key, VersionId=vid,
                                      Range=f"bytes=0-{self.max_bytes - 1}")["Body"].read()
            parts.append(body.decode("utf-8", errors="replace"))
        return extract_identifiers("\n".join(parts))

    def _lock_enabled(self, bucket: str) -> bool:
        try:
            cfg = self.s3.get_object_lock_configuration(Bucket=bucket)
        except Exception as exc:
            if _code(exc) in {"ObjectLockConfigurationNotFoundError", "AccessDenied"}:
                return False
            raise
        return cfg.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") == "Enabled"

    def _lock_state(self, bucket: str, key: str, vid: str, now: str) -> str:
        try:
            hold = self.s3.get_object_legal_hold(Bucket=bucket, Key=key, VersionId=vid)
            if hold.get("LegalHold", {}).get("Status") == "ON":
                return "legal_hold"
        except Exception as exc:
            if _code(exc) not in {"NoSuchObjectLockConfiguration", "InvalidRequest"}:
                raise
        try:
            ret = self.s3.get_object_retention(Bucket=bucket, Key=key, VersionId=vid).get("Retention", {})
            until = ret.get("RetainUntilDate")
            if until and _iso(until) > now:
                return f"retention_until={_iso(until)}"
        except Exception as exc:
            if _code(exc) not in {"NoSuchObjectLockConfiguration", "InvalidRequest"}:
                raise
        return ""

    def _key_observation(self, res: ProbeResult, key: str, marker: dict | None) -> Observation:
        hits = sorted((h for h in res.hits if h.key == key), key=lambda h: h.last_modified)
        uri = f"s3://{res.bucket}/{key}"
        current = [h for h in hits if h.is_latest]
        noncurrent = [h for h in hits if not h.is_latest]
        locked = [h for h in hits if h.locked]
        shared = sorted({o for h in hits for o in h.others})
        if current:
            detail = f"current version {current[0].version_id} still holds the subject's data"
        elif marker is not None:
            detail = (f"looks deleted (delete marker {marker['VersionId']} at {_iso(marker['LastModified'])}) "
                      f"but {len(noncurrent)} noncurrent version(s) are still recoverable")
        else:
            detail = f"{len(noncurrent)} noncurrent version(s) still recoverable"
        detail += "; versions: " + ", ".join(h.version_id for h in hits)
        detail += "; matched: " + ", ".join(sorted({m for h in hits for m in h.matched}))
        category = "SUPPORT_ATTACHMENT"
        if locked:
            category = "LEGAL_HOLD_RECORD"
            detail += "; object lock: " + ", ".join(f"{h.version_id}={h.locked}" for h in locked)
        elif shared:
            # an attachment that also names someone else does not "belong only to
            # the deletion subject" (rule_06): it is redacted like a shared message
            category = "SUPPORT_MESSAGE"
            detail += f"; also identifies other people ({len(shared)} identifier(s))"
        latest = hits[-1]
        return Observation(
            file_id=f"{uri}?versionId={latest.version_id}", captured_at=res.probed_at, obs_time=res.probed_at,
            system=SYSTEM, resource_id=uri, state=PRESENT,
            location="current" if current else "noncurrent", category=category,
            identifiers={m for h in hits for m in h.matched}, pii_exposed=True, detail=detail)

    def _replication(self, bucket: str, keys: list[str], res: ProbeResult) -> list[Observation]:
        try:
            cfg = self.s3.get_bucket_replication(Bucket=bucket).get("ReplicationConfiguration", {})
        except Exception as exc:
            if _code(exc) in {"ReplicationConfigurationNotFoundError", "AccessDenied"}:
                return []
            raise
        out = []
        for i, rule in enumerate(cfg.get("Rules", [])):
            filt = rule.get("Filter", {})
            rprefix = filt.get("Prefix", filt.get("And", {}).get("Prefix", rule.get("Prefix", "")))
            covered = [k for k in keys if k.startswith(rprefix or "")]
            if not covered:
                continue
            rid = rule.get("ID") or f"rule{i + 1}"
            dest = rule.get("Destination", {}).get("Bucket", "?")
            enabled = rule.get("Status") == "Enabled"
            deletes = rule.get("DeleteMarkerReplication", {}).get("Status") == "Enabled"
            detail = (f"replication rule {rid} -> {dest} covers {len(covered)} matched key(s); "
                      f"delete markers {'are' if deletes else 'are NOT'} replicated")
            out.append(Observation(
                file_id=f"s3://{bucket}?replication={rid}", captured_at=res.probed_at, obs_time=res.probed_at,
                system=FLOW_SYSTEM, resource_id=f"s3-replication://{bucket}/{rid}",
                state=ACTIVE if enabled else "DISABLED", category="DATA_FLOW", detail=detail))
            if enabled:
                res.notes.append(f"replica bucket {dest} must be probed too: it may hold its own copies")
        return out

    def _lifecycle(self, bucket: str, res: ProbeResult, keys: list[str]) -> None:
        if res.versioning == "Disabled" or not keys:
            return
        try:
            rules = self.s3.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
        except Exception as exc:
            if _code(exc) in {"NoSuchLifecycleConfiguration", "AccessDenied"}:
                rules = []
            else:
                raise
        expiring = [r for r in rules if r.get("Status") == "Enabled" and r.get("NoncurrentVersionExpiration")]
        if not expiring:
            res.notes.append("no lifecycle rule expires noncurrent versions: deleted objects stay recoverable "
                             "until each version is deleted by id")


def absent_observation(bucket: str, prefix: str, probed_at: str) -> Observation:
    """Proof that the probe looked and found nothing for the subject."""
    uri = f"s3://{bucket}/{prefix}"
    return Observation(file_id=f"{uri}?probe={probed_at}", captured_at=probed_at, obs_time=probed_at,
                       system=SYSTEM, resource_id=uri, state=ABSENT,
                       detail="no version or delete-marked version under this prefix matches the subject")
