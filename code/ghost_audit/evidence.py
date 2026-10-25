"""Evidence normalization.

Every participant-visible evidence file is converted into a flat list of
`Observation`s: one observation says "at time T, file F showed resource R in
system S with state X, and the record carried identifiers I". All downstream
reasoning (identity linking, state at audit time, policy) works on these
observations only, so each conclusion keeps its provenance (file_id).

Evidence content is treated as untrusted data: values are only ever matched
with regular expressions and compared with known identifiers; nothing in a
file is executed or interpreted as an instruction. SQL snapshots are loaded
into an in-memory SQLite database with only CREATE/INSERT statements allowed.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

RESOURCE_RE = re.compile(
    r"\b(account|search|analytics|support|export|data_flow|backup|audit_record|tombstone|"
    r"marketing|object|legal_hold|email_delivery|cache)_(\d{1,6})\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
PHONE_RE = re.compile(r"\+\d[\d\s().\-]{6,}\d")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")

PREFIX_SYSTEM = {
    "account": "account_service", "search": "search_service", "analytics": "analytics_platform",
    "support": "support_platform", "export": "export_service", "data_flow": "integration_service",
    "backup": "backup_service", "audit_record": "security_service", "tombstone": "identity_service",
    "marketing": "marketing_platform", "object": "object_storage", "legal_hold": "legal_service",
    "email_delivery": "email_delivery_service", "cache": "cache_service",
}

# States an observation can establish.
PRESENT = "PRESENT"            # record exists (and carries data)
ABSENT = "ABSENT"              # record not found
IDENTIFIABLE = "IDENTIFIABLE"  # record exists and still exposes direct identifiers
ANONYMIZED = "ANONYMIZED"      # record exists without any link to a person
REDACTED = "REDACTED"          # record exists, direct identifiers removed, stable ref may remain
ACTIVE = "ACTIVE"              # data flow running / restore allowed
DISABLED = "DISABLED"          # data flow stopped
BLOCKED = "BLOCKED"            # restore blocked

PII_KEYS = {"email", "phone", "full_name", "name", "display_name", "email_hash", "requester_email",
            "requester_name", "requester_phone", "owner_email", "owner_phone", "customer_email",
            "matched_value", "subject_selector"}
NULLS = {None, "", "null", "none", "None", "NULL", "[REDACTED]", "no direct identifier"}


def resource_system(rid: str) -> str:
    m = RESOURCE_RE.fullmatch(rid)
    return PREFIX_SYSTEM.get(m.group(1), "unknown") if m else "unknown"


def norm_phone(s: str) -> str:
    return re.sub(r"\D", "", s)


def extract_identifiers(text: str) -> set[str]:
    """All identifier tokens in a piece of text, normalized for matching."""
    ids: set[str] = set()
    for e in EMAIL_RE.findall(text):
        ids.add("email:" + e.lower().rstrip("."))
    for u in USER_RE.findall(text):
        ids.add("user:" + u)
    for h in HASH_RE.findall(text):
        ids.add("hash:" + h.lower())
    for p in PHONE_RE.findall(text):
        d = norm_phone(p)
        if len(d) >= 8:
            ids.add("phone:" + d)
    return ids


@dataclass
class Observation:
    file_id: str
    captured_at: str           # manifest capture time of the file
    obs_time: str              # time this specific record was observed
    system: str
    resource_id: str
    state: str
    location: str = "primary"  # primary / replica name for drift analysis
    category: str | None = None
    identifiers: set[str] = field(default_factory=set)
    pii_exposed: bool = False
    detail: str = ""

    def key(self):
        return (self.system, self.resource_id)


# ----------------------------------------------------------------------------
# state helpers


# fields that hold references/locators, not personal content
REFERENCE_KEYS = {"resource_id", "subject_reference", "customer_reference", "user_id", "lookup_value",
                  "archive_path", "archive_key", "object_key", "file_name", "query", "ticket_id"}


def _has_pii(rec: dict) -> bool:
    for k, v in rec.items():
        if k in PII_KEYS and v not in NULLS:
            return True
        if isinstance(v, dict) and _has_pii(v):
            return True
        # direct identifiers can also sit inside free text (message bodies,
        # uploader fields, notes): any email or phone there is still exposed
        if isinstance(v, str) and k not in REFERENCE_KEYS and v not in NULLS and (EMAIL_RE.search(v) or PHONE_RE.search(v)):
            return True
    return False


def _truthy(v) -> bool | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "allowed", "allow", "active", "enabled"}:
        return True
    if s in {"0", "false", "no", "deny", "denied", "blocked", "disabled", "stopped"}:
        return False
    return None


def record_state(system: str, rec: dict) -> str:
    """Derive a state from a full record (SQL row / JSON record)."""
    if system == "integration_service":
        en = _truthy(rec.get("enabled"))
        if en is None:
            en = str(rec.get("last_delivery_status", "")).lower() not in {"stopped", "disabled"}
        return ACTIVE if en else DISABLED
    if system == "backup_service":
        ra = rec.get("restore_allowed", rec.get("restore_enabled", rec.get("eligible")))
        if ra is None and "deletion_block" in rec:
            ra = not _truthy(rec.get("deletion_block"))
        if ra is None and "restore_status" in rec:
            ra = str(rec["restore_status"]).lower() not in {"blocked", "denied"}
        t = _truthy(ra)
        return BLOCKED if t is False else ACTIVE
    if system == "export_service":
        av = rec.get("download_available", rec.get("available"))
        if _truthy(av) is False:
            return ABSENT
        return IDENTIFIABLE if _has_pii(rec) else PRESENT
    if system == "security_service" or system == "legal_service":
        return PRESENT
    status = str(rec.get("account_status", "")).upper()
    if system == "account_service" and status in {"DELETED", "PURGED"}:
        return ABSENT
    if _has_pii(rec):
        return IDENTIFIABLE
    if system == "analytics_platform":
        ref = rec.get("customer_reference", rec.get("user_id"))
        return REDACTED if ref not in NULLS else ANONYMIZED
    if system in {"support_platform", "identity_service"}:
        return REDACTED
    return PRESENT


RESULT_STATE = {
    "NOT_FOUND": ABSENT, "ABSENT": ABSENT, "DELETED": ABSENT, "MISS": ABSENT,
    "FOUND": PRESENT, "PRESENT": PRESENT, "IDENTIFIABLE": IDENTIFIABLE, "EXPOSED": IDENTIFIABLE,
    "ANONYMIZED": ANONYMIZED, "REDACTED": REDACTED, "ACTIVE": ACTIVE, "ENABLED": ACTIVE,
    "DISABLED": DISABLED, "STOPPED": DISABLED, "BLOCKED": BLOCKED, "RESTORABLE": ACTIVE,
    "RETAINED": PRESENT,
}


# ----------------------------------------------------------------------------
# log parsing

KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("((?:[^"\\]|\\.)*)"|[^\s"]+)')


def _parse_kv(s: str) -> dict:
    out = {}
    for m in KV_RE.finditer(s):
        k = m.group(1)
        v = m.group(3) if m.group(3) is not None else m.group(2)
        v = v.replace('\\"', '"')
        out[k] = v
    if "message" in out and "=" in out["message"]:
        inner = _parse_kv(out.pop("message"))
        out.update(inner)
    return out


def parse_log_line(line: str) -> dict | None:
    m = TS_RE.match(line)
    if not m:
        return None
    ts = m.group(1)
    rest = line[len(ts):].strip()
    d: dict = {}
    if rest.startswith("{"):
        try:
            d = {k: (v if not isinstance(v, (dict, list)) else json.dumps(v)) for k, v in json.loads(rest).items()}
        except json.JSONDecodeError:
            d = {}
        d.setdefault("trace", d.get("trace_id"))
    elif rest.startswith("["):
        bm = re.match(r"\[(\w+)\]\s*\[([^\]]+)\]\s*\[([^\]]+)\]\s*([\w.]+):?\s*(.*)", rest)
        if bm:
            d = _parse_kv(bm.group(5))
            d.update(level=bm.group(1), service=bm.group(2), trace=bm.group(3), event=bm.group(4))
    elif rest.startswith("|") or " | " in rest[:40]:
        parts = [p.strip() for p in rest.split("|")]
        parts = [p for p in parts if p != ""]
        if len(parts) >= 5:
            d = _parse_kv(" ".join(parts[4:]))
            d.update(level=parts[0], service=parts[1], trace=parts[2], event=parts[3])
    elif rest.startswith("<"):
        sm = re.match(r"<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+-\s+-\s+(.*)", rest)
        if sm:
            d = _parse_kv(sm.group(4))
            d.update(service=sm.group(2), trace=sm.group(3))
    else:
        d = _parse_kv(rest)
        if not d and rest:
            first = rest.split()[0]
            d = {"level": first}
            d.update(_parse_kv(rest[len(first):]))
    if "requestId" in d and "trace" not in d:
        d["trace"] = d["requestId"]
    if "function" in d and "service" not in d:
        d["service"] = d["function"]
    d.setdefault("trace", d.get("trace_id"))
    d["ts"] = ts
    d["_raw"] = line
    return d


LOOKUP_KEYS = ("lookup_key", "subject_key", "match_key", "scan_key", "correlation_key", "subject_ref",
               "subject_reference", "customer_reference", "matched_value", "email", "customer_email",
               "owner_email", "requester_email")

# services whose log name differs from the canonical system id
SERVICE_ALIASES = {"profile_service": "account_service", "support_replica": "support_platform"}


def log_state(system: str, d: dict) -> str | None:
    ev = d.get("event", "")
    if "result" in d and d.get("event") == "record_lookup":
        return RESULT_STATE.get(str(d["result"]).upper())
    if system == "account_service":
        if d.get("rows") == "0" or d.get("http_status") == "404":
            return ABSENT
        if d.get("rows") not in (None, "0") or d.get("http_status") == "200":
            return IDENTIFIABLE
        return None
    if system == "search_service":
        if d.get("result") == "miss" or d.get("total_hits") == "0":
            return ABSENT
        if d.get("result") == "hit" or (d.get("total_hits") not in (None, "0")):
            return IDENTIFIABLE
        return None
    if system == "analytics_platform":
        if ev == "analytics_identity_scan" or d.get("direct_identifiers", "0") not in ("0", None):
            return IDENTIFIABLE
        if ev == "contact_lookup_completed":
            return IDENTIFIABLE if d.get("rows") != "0" else ABSENT
        if any(d.get(k) not in NULLS for k in ("email", "phone", "email_hash")):
            return IDENTIFIABLE
        if d.get("customer_reference") not in NULLS:
            return REDACTED
        if ev.startswith("analytics_profile_read"):
            return ANONYMIZED
        return None
    if system == "support_platform":
        if ev in {"message_pii_scan", "attachment_pii_scan"}:
            return IDENTIFIABLE
        if ev == "contact_query_completed":
            return IDENTIFIABLE if d.get("rows") != "0" else ABSENT
        if ev == "deletion_marker_check":
            return IDENTIFIABLE if d.get("marker_found") == "false" else ABSENT
        if any(d.get(k) not in NULLS for k in ("requester_email", "requester_name", "requester_phone")):
            return IDENTIFIABLE
        if ev in {"support_ticket_read_completed", "message_read"}:
            return REDACTED
        return None
    if system == "integration_service":
        if "enabled" in d:
            return ACTIVE if _truthy(d["enabled"]) else DISABLED
        return None
    if system == "backup_service":
        if "restore_allowed" in d:
            return ACTIVE if _truthy(d["restore_allowed"]) else BLOCKED
        if "decision" in d:
            return BLOCKED if str(d["decision"]).lower() == "deny" else ACTIVE
        if "eligible" in d:
            blocked = _truthy(d.get("deletion_block"))
            return ACTIVE if _truthy(d["eligible"]) and not blocked else BLOCKED
        return None
    if system == "export_service":
        if "available" in d:
            return IDENTIFIABLE if _truthy(d["available"]) else ABSENT
        return None
    if system == "security_service":
        if d.get("rows") == "0":
            return ABSENT
        if ev.startswith("audit_record_read") or "integrity_check" in d:
            return PRESENT
        return None
    if system == "legal_service":
        return PRESENT
    return None


def parse_log(file_id: str, captured_at: str, text: str) -> list[Observation]:
    lines = [parse_log_line(l) for l in text.splitlines()]
    lines = [d for d in lines if d]
    trace_keys: dict[str, set[str]] = {}
    for d in lines:
        tr = d.get("trace")
        if not tr:
            continue
        vals = " ".join(str(d.get(k)) for k in LOOKUP_KEYS if d.get(k) not in NULLS)
        trace_keys.setdefault(tr, set()).update(extract_identifiers(vals))
    obs = []
    for d in lines:
        rid = d.get("resource_id")
        if not rid or not RESOURCE_RE.fullmatch(rid):
            continue
        system = d.get("system") or resource_system(rid)
        svc = SERVICE_ALIASES.get(d.get("service", ""), d.get("service", ""))
        if d.get("event") != "record_lookup" and svc and svc != system and svc in PREFIX_SYSTEM.values():
            system = svc if resource_system(rid) == svc else resource_system(rid)
        state = log_state(system, d)
        if state is None:
            continue
        ids = extract_identifiers(" ".join(str(d.get(k)) for k in LOOKUP_KEYS if d.get(k) not in NULLS))
        # The trace's lookup key names the subject only when the result line
        # does not name someone else: a lookalike hit returned for the subject's
        # key must keep its own identity.
        if not any(i.startswith(("email:", "phone:", "hash:")) for i in ids):
            ids |= trace_keys.get(d.get("trace") or "", set())
        location = "replica" if "replica" in (d.get("service", "") + str(d.get("pool", ""))) else "primary"
        obs.append(Observation(file_id, captured_at, d["ts"], system, rid, state, location,
                               d.get("record_type"), ids, state == IDENTIFIABLE, d["_raw"][:300]))
    return obs


# ----------------------------------------------------------------------------
# structured records


def _rec_obs(file_id, captured_at, obs_time, system, rec, location="primary", category=None):
    rid = rec.get("resource_id")
    if not rid:
        return None
    system = system if system and system != "unknown" else resource_system(rid)
    if "result" in rec and "record_type" in rec:
        state = RESULT_STATE.get(str(rec["result"]).upper(), PRESENT)
        category = rec.get("record_type")
        ids = extract_identifiers(str(rec.get("subject_reference", "")) + " " + str(rec.get("lookup_value", "")))
        pii = state in {IDENTIFIABLE, PRESENT, ACTIVE} and any(not i.startswith("user:") for i in ids)
    else:
        state = record_state(system, rec)
        ids = extract_identifiers(json.dumps(rec, ensure_ascii=False))
        pii = _has_pii(rec)
    if system == "account_service" and state == PRESENT:
        state = IDENTIFIABLE
    return Observation(file_id, captured_at, obs_time, system, rid, state, location, category, ids, pii,
                       json.dumps(rec, ensure_ascii=False)[:300])


def parse_json(file_id: str, captured_at: str, data: dict) -> list[Observation]:
    obs: list[Observation] = []
    if "snapshots" in data:
        for snap in data["snapshots"]:
            t = snap.get("captured_at", captured_at)
            for rec in snap.get("records", []):
                o = _rec_obs(file_id, captured_at, rec.get("recorded_at", t),
                             rec.get("system") or snap.get("data_store"), _flatten_doc(rec))
                if o:
                    o.detail = snap.get("collection", "") + " " + o.detail
                    obs.append(o)
    if "service_exports" in data:
        t0 = data.get("exported_at", captured_at)
        for system, kinds in data["service_exports"].items():
            for kind, recs in kinds.items():
                for rec in recs:
                    t = rec.get("observed_at", t0)
                    if system == "account_service" and "status_code" in rec:
                        st = ABSENT if rec["status_code"] == 404 or rec.get("profile") is None else IDENTIFIABLE
                        prof = rec.get("profile") or {}
                        obs.append(Observation(file_id, captured_at, t, system, rec["resource_id"], st, "primary",
                                               None, extract_identifiers(json.dumps(prof, ensure_ascii=False)),
                                               st == IDENTIFIABLE, json.dumps(rec, ensure_ascii=False)[:300]))
                    elif system == "search_service" and "hits" in rec:
                        docs = rec["hits"].get("documents", [])
                        if not docs:
                            obs.append(Observation(file_id, captured_at, t, system, rec["resource_id"], ABSENT,
                                                   identifiers=extract_identifiers(rec.get("query", ""))))
                        for doc in docs:
                            o = _rec_obs(file_id, captured_at, t, system, doc)
                            if o:
                                if not any(i.startswith(("email:", "phone:", "hash:")) for i in o.identifiers):
                                    o.identifiers |= extract_identifiers(rec.get("query", ""))
                                obs.append(o)
                    else:
                        o = _rec_obs(file_id, captured_at, t, system, rec)
                        if o:
                            obs.append(o)
    if "records" in data and "snapshots" not in data:
        t0 = data.get("captured_at") or data.get("exported_at") or captured_at
        source = data.get("source", "") + " " + data.get("collection", "")
        for rec in data["records"]:
            rid = rec.get("resource_id", "")
            system = rec.get("system") or resource_system(rid)
            o = _rec_obs(file_id, captured_at, rec.get("recorded_at", t0), system, _flatten_doc(rec))
            if o:
                obs.append(o)
    return obs


def _flatten_doc(rec: dict) -> dict:
    """Lift nested 'document' fields (search docs) to the top level."""
    if isinstance(rec.get("document"), dict):
        out = dict(rec)
        out.update(rec["document"])
        out.pop("document")
        return out
    return rec


def parse_sql(file_id: str, captured_at: str, text: str) -> list[Observation]:
    # Only schema and data statements are executed; anything else is dropped.
    stmts = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        stmts.append(line)
    script = "\n".join(stmts)
    allowed = re.sub(r"(?im)^\s*(PRAGMA|ATTACH|DETACH|VACUUM)[^;]*;", "", script)
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(allowed)
    except sqlite3.Error:
        # fall back: execute statement by statement, skipping malformed ones
        con = sqlite3.connect(":memory:")
        for st in re.split(r";\s*\n", allowed):
            try:
                con.execute(st)
            except sqlite3.Error:
                pass
    m = re.search(r"Captured at:\s*(\S+)", text)
    snap_time = m.group(1) if m else captured_at
    scope = {}
    try:
        scope = dict(con.execute("SELECT origin_system, captured_at FROM snapshot_scope").fetchall())
    except sqlite3.Error:
        pass
    obs = []
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        if t == "snapshot_scope":
            continue
        cur = con.execute(f'SELECT * FROM "{t}"')
        cols = [c[0] for c in cur.description]
        loc = "primary"
        lm = re.search(r"(primary|read_replica|replica_\w+|reporting_replica|failover_replica)$", t)
        if lm:
            loc = lm.group(1)
        for row in cur.fetchall():
            rec = dict(zip(cols, row))
            system = rec.get("origin_system")
            if "record_json" in rec:
                try:
                    inner = _flatten_doc(json.loads(rec["record_json"]))
                except json.JSONDecodeError:
                    continue
                o = _rec_obs(file_id, captured_at, snap_time, system, inner, loc)
                if o:
                    o.detail = (rec.get("collection_name") or "") + " " + o.detail
            else:
                rid = rec.get("resource_id", "")
                sysname = resource_system(rid)
                o = _rec_obs(file_id, captured_at, scope.get(sysname, snap_time), sysname, rec, loc)
            if o:
                obs.append(o)
    # Absence-by-omission: an inventory snapshot that declares its scope proves
    # a resource of a scoped system is absent if not listed. Recorded lazily by
    # the auditor via `scope_systems`.
    return obs, {"scope": set(scope) if scope else None, "tables": tables, "snap_time": snap_time}


def parse_handover(file_id: str, captured_at: str, text: str) -> list[Observation]:
    obs = []
    for line in text.splitlines():
        line = line.strip().lstrip("-").strip()
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 6 and RESOURCE_RE.fullmatch(parts[2]):
            state = RESULT_STATE.get(parts[4].upper(), PRESENT)
            ids = extract_identifiers(parts[5])
            obs.append(Observation(file_id, captured_at, parts[0], parts[1], parts[2], state, "primary",
                                   parts[3], ids, state in {IDENTIFIABLE, PRESENT, ACTIVE} and
                                   any(not i.startswith("user:") for i in ids), line))
    return obs


def _doc_state(system: str, text: str, ids: set[str]) -> str:
    low = text.lower()
    direct = any(not i.startswith("user:") for i in ids)
    if system == "backup_service":
        return BLOCKED if re.search(r"restore (blocked|denied|disabled)|\bblocked\b|\bdeny\b", low) else ACTIVE
    if system == "integration_service":
        return DISABLED if re.search(r"disabled|paused|stopped|inactive", low) else ACTIVE
    if system in {"security_service", "legal_service"}:
        return PRESENT
    if system == "identity_service":
        return IDENTIFIABLE if direct else REDACTED
    if system == "analytics_platform":
        return IDENTIFIABLE if direct else ANONYMIZED
    if system == "export_service" and re.search(r"expired|deleted|unavailable", low):
        return ABSENT
    return IDENTIFIABLE


def _mentions(text: str, aliases: dict[str, str]) -> list[str]:
    found = [m.group(0) for m in RESOURCE_RE.finditer(text)]
    found += [rid for alias, rid in aliases.items() if alias in text]
    return list(dict.fromkeys(found))


def parse_document_text(file_id: str, captured_at: str, text: str, aliases: dict[str, str]) -> list[Observation]:
    """PDF text: a business document. Every resource (or catalog alias) it
    names is observed at capture time with the identifiers printed in the
    document. Documents are single-subject; multi-row registers without
    resource ids yield no observations."""
    obs = []
    rids = _mentions(text, aliases)
    ids = extract_identifiers(text)
    for rid in rids:
        system = resource_system(rid)
        state = _doc_state(system, text, ids)
        obs.append(Observation(file_id, captured_at, captured_at, system, rid, state, "primary", None, ids,
                               state == IDENTIFIABLE, " ".join(text.split())[:2000]))
    return obs


def _scalar_text(d: dict) -> str:
    return " | ".join(f"{k}: {v}" for k, v in d.items() if isinstance(v, (str, int, float)))


def parse_vision_json(file_id: str, captured_at: str, resp: dict, aliases: dict[str, str]) -> list[Observation]:
    """Screenshot transcription: rows are kept separate so identifiers of one
    table row are never attributed to a resource in another row."""
    obs = []
    units: list[str] = []
    for row in resp.get("table") or []:
        if isinstance(row, dict):
            units.append(_scalar_text(row))
    fields = resp.get("fields") or {}
    for k, v in fields.items():
        if isinstance(v, dict) and RESOURCE_RE.fullmatch(k):        # detail panel keyed by resource
            units.append(f"{k} | " + _scalar_text(v))
    header = resp.get("header") or {}
    head_txt = json.dumps(header, ensure_ascii=False)
    head_rids = _mentions(head_txt, aliases)
    if head_rids:
        # the page is about one record: header + scalar fields describe it
        scal = {k: v for k, v in fields.items() if isinstance(v, str) and not RESOURCE_RE.fullmatch(k)
                and not EMAIL_RE.fullmatch(k)}
        units.append(head_txt + " | " + _scalar_text(scal) + " | " + " ".join(map(str, resp.get("activity") or [])))
    seen = set()
    for u in units:
        rids = _mentions(u, aliases)
        if not rids:
            continue
        ids = extract_identifiers(u)
        for rid in rids:
            if rid in seen and not ids:
                continue
            seen.add(rid)
            system = resource_system(rid)
            state = _doc_state(system, u, ids)
            obs.append(Observation(file_id, captured_at, captured_at, system, rid, state, "primary", None, ids,
                                   state == IDENTIFIABLE, u[:300]))
    return obs


# ----------------------------------------------------------------------------


@dataclass
class EvidenceIndex:
    observations: list[Observation]
    files: dict[str, dict]                 # file_id -> manifest row
    sql_meta: dict[str, dict]              # file_id -> scope info
    doc_text: dict[str, str]               # file_id -> extracted text (pdf/image)
    errors: list[str]
    coverage: list[tuple[str, str, str]] = field(default_factory=list)  # (file_id, system, snapshot time)

    def by_resource(self) -> dict[tuple[str, str], list[Observation]]:
        out: dict = {}
        for o in self.observations:
            out.setdefault(o.key(), []).append(o)
        return out


def load_evidence(dataset_dir: Path, aliases: dict[str, str], vision=None) -> EvidenceIndex:
    manifest = list(csv.DictReader(open(dataset_dir / "evidence_files.csv", encoding="utf-8")))
    files = {r["file_id"]: r for r in manifest}
    observations: list[Observation] = []
    sql_meta: dict = {}
    doc_text: dict = {}
    errors: list[str] = []
    coverage: list = []
    for row in manifest:
        fid, cap = row["file_id"], row["captured_at"]
        path = dataset_dir / row["file_path"]
        if not path.exists():
            errors.append(f"{fid}: missing file {row['file_path']}")
            continue
        suffix = path.suffix.lower()
        try:
            if suffix == ".log":
                observations += parse_log(fid, cap, path.read_text(encoding="utf-8", errors="replace"))
            elif suffix == ".json":
                data = json.loads(path.read_text(encoding="utf-8"))
                observations += parse_json(fid, cap, data)
                for snap in data.get("snapshots", []):
                    coverage.append((fid, snap.get("data_store", ""), snap.get("captured_at", cap)))
                for system in data.get("service_exports", {}):
                    coverage.append((fid, system, data.get("exported_at", cap)))
            elif suffix == ".sql":
                obs, meta = parse_sql(fid, cap, path.read_text(encoding="utf-8", errors="replace"))
                observations += obs
                sql_meta[fid] = meta
                if any("profile" in t for t in meta["tables"]) or any(o.system == "account_service" for o in obs):
                    coverage.append((fid, "account_service", meta["snap_time"]))
            elif suffix == ".txt":
                observations += parse_handover(fid, cap, path.read_text(encoding="utf-8", errors="replace"))
            elif suffix == ".pdf":
                import pymupdf
                text = "\n".join(p.get_text() for p in pymupdf.open(path))
                doc_text[fid] = text
                observations += parse_document_text(fid, cap, text, aliases)
            elif suffix in {".png", ".jpg", ".jpeg"}:
                if vision is None:
                    errors.append(f"{fid}: image skipped (vision model unavailable)")
                    continue
                resp = vision(path)
                doc_text[fid] = json.dumps(resp, ensure_ascii=False)
                observations += parse_vision_json(fid, cap, resp, aliases)
            else:
                errors.append(f"{fid}: unsupported format {suffix}")
        except Exception as exc:  # malformed file: record and continue
            errors.append(f"{fid}: parse error {type(exc).__name__}: {exc}")
    return EvidenceIndex(observations, files, sql_meta, doc_text, errors, coverage)
