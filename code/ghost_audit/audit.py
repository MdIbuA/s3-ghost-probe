"""Case reconstruction: link resources to each deletion case, reconstruct
their state at audit_at, apply policy, and decide the deletion status.

Pipeline per case
  1. intent      -> NOT_APPLICABLE when the request does not authorize deletion
  2. identity    -> Subject (user_id + every current/historical identifier)
  3. linking     -> evidence observations carrying the subject's identifiers
                    inside the case window, catalog links, and deletion-workflow
                    activity in the case's reporting window (claims used only
                    to *find* resources, never as proof of state)
  4. state       -> latest observation at or before audit_at from a file
                    captured at or before audit_at (replica drift: any copy
                    still present counts as present)
  5. policy      -> required end state per data category (deletion_rules.csv)
  6. decision    -> findings for every non-compliant resource; COMPLETE only
                    when nothing is non-compliant
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from .activity import ActivityStream, ts
from .evidence import (ABSENT, ACTIVE, ANONYMIZED, BLOCKED, DISABLED, IDENTIFIABLE, PRESENT, REDACTED,
                       EvidenceIndex, Observation, resource_system)
from .identity import IdentityResolver, Subject

# --------------------------------------------------------------------------
# policy

# data category -> required action, loaded from deletion_rules.csv at runtime
ACTION_TARGET = {"DELETE": ABSENT, "REDACT": REDACTED, "ANONYMIZE": ANONYMIZED, "RETAIN": PRESENT}

SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "cache_service": "CACHE_ENTRY",
    "analytics_platform": "ANALYTICS_PROFILE",
    "export_service": "USER_EXPORT",
    "marketing_platform": "MARKETING_PROFILE",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "security_service": "SECURITY_AUDIT_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "backup_service": "BACKUP_ARCHIVE",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "object_storage": "SUPPORT_ATTACHMENT",
    "support_platform": "SUPPORT_MESSAGE",
    "integration_service": "DATA_FLOW",
}

COMPLIANT = {
    ABSENT: {ABSENT},
    ANONYMIZED: {ANONYMIZED, ABSENT},
    REDACTED: {REDACTED, ANONYMIZED, ABSENT},
    PRESENT: {PRESENT, REDACTED},
    DISABLED: {DISABLED},
    BLOCKED: {BLOCKED},
}

POLICY_TYPES = {"ANONYMIZE_RECORD", "REDACT_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "RETAIN_RECORD"}
MIN_ATTRIBUTION_SCORE = -1.5   # below this the event is left unattributed

STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}


def _case_num(case_id: str) -> int:
    digits = "".join(ch for ch in case_id if ch.isdigit())
    return int(digits) if digits else 0


def load_rules(dataset_dir: Path) -> dict[str, dict]:
    return {r["data_category"]: r for r in csv.DictReader(open(dataset_dir / "deletion_rules.csv", encoding="utf-8"))}


@dataclass
class ResourceAssessment:
    system: str
    resource_id: str
    links: list[str] = field(default_factory=list)       # why this resource belongs to the case
    category: str = ""
    rule_id: str = ""
    target: str = ""                                      # required end state
    state: str | None = None                              # state at audit_at (None = no evidence)
    evidence_id: str = ""                                 # proof of `state`
    state_note: str = ""

    @property
    def compliant(self) -> bool | None:
        if self.state is None:
            return None
        return self.state in COMPLIANT[self.target]


@dataclass
class CaseResult:
    case_id: str
    request: dict
    intent: dict
    subject: Subject | None = None
    resources: dict[tuple[str, str], ResourceAssessment] = field(default_factory=dict)
    status: str = ""
    notes: list[str] = field(default_factory=list)


class Auditor:
    def __init__(self, dataset_dir: Path, evidence: EvidenceIndex, identities: IdentityResolver,
                 activity: ActivityStream):
        self.dataset_dir = dataset_dir
        self.ev = evidence
        self.idr = identities
        self.act = activity
        self.rules = load_rules(dataset_dir)
        self.obs_by_res = evidence.by_resource()
        # owner index: which known people does each resource's evidence name?
        ident_owner: dict[str, set[str]] = {}
        for uid in identities.users:
            for i in identities.subject(uid, "9999").identifiers:
                ident_owner.setdefault(i, set()).add(uid)
        self.res_owners: dict[str, set[str]] = {}
        for o in evidence.observations:
            for i in o.identifiers:
                if i in ident_owner:
                    self.res_owners.setdefault(o.resource_id, set()).update(ident_owner[i])
        for rid, (ids, _a) in activity.catalog.items():
            for i in ids:
                if i in ident_owner:
                    self.res_owners.setdefault(rid, set()).update(ident_owner[i])

    # ------------------------------------------------------------------
    # linking

    def case_windows(self, cases: list[CaseResult]) -> dict[str, tuple[str, str]]:
        """Observation window per case: [lower, audit_at]. The lower bound keeps a
        later request by the same person from inheriting an earlier case's records."""
        by_user: dict[str, list[CaseResult]] = {}
        for c in cases:
            if c.subject:
                by_user.setdefault(c.subject.user_id, []).append(c)
        win = {}
        for c in cases:
            if not c.subject:
                continue
            lower = (ts(c.request["requested_at"]) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for other in by_user[c.subject.user_id]:
                if other is not c and other.request["audit_at"] < c.request["requested_at"]:
                    lower = max(lower, other.request["audit_at"])
            win[c.case_id] = (lower, c.request["audit_at"])
        return win

    def link_all(self, cases: list[CaseResult]) -> None:
        active = [c for c in cases if c.subject and c.intent["final"] == "DELETION"]
        win = self.case_windows(active)
        # 1. evidence identifiers -> (case, resource)
        claims: dict[tuple[str, str], dict[str, list[str]]] = {}
        for o in self.ev.observations:
            if not o.identifiers:
                continue
            for c in active:
                shared = o.identifiers & c.subject.identifiers
                if not shared:
                    continue
                lo, hi = win[c.case_id]
                if lo <= o.obs_time <= hi:
                    reason = f"{o.file_id}: {sorted(shared)[0]}"
                    claims.setdefault(o.key(), {}).setdefault(c.case_id, []).append(reason)
        # 2. data-catalog links (LINK_RESOURCE_IDENTITY)
        for rid, (ids, a) in self.act.catalog.items():
            for c in active:
                shared = ids & c.subject.identifiers
                lo, hi = win[c.case_id]
                if shared and lo <= a.event_time <= hi:
                    key = (resource_system(rid), rid)
                    claims.setdefault(key, {}).setdefault(c.case_id, []).append(f"{a.activity_id}: catalog")
        by_id = {c.case_id: c for c in active}
        claimed_resources = set()
        for key, per_case in claims.items():
            # Each person named by the record gets it in one of their cases (the
            # earliest audit, since case windows of one person do not overlap);
            # a record shared by two requesters belongs to both.
            per_subject: dict[str, list[str]] = {}
            for cid in per_case:
                per_subject.setdefault(by_id[cid].subject.user_id, []).append(cid)
            for cids in per_subject.values():
                best = min(cids, key=lambda cid: by_id[cid].request["audit_at"])
                ra = by_id[best].resources.setdefault(key, ResourceAssessment(*key))
                ra.links += per_case[best]
            claimed_resources.add(key[1])
        # A case whose evidence-linked records were still being processed after
        # the status report belongs to a "post-report" workflow; pre-report
        # successful policy actions are then less likely to be its own.
        for c in active:
            rep = ts(c.request["reported_at"])
            c.post_report = any(
                0 < (ts(a.event_time) - rep).total_seconds() <= 3 * 3600
                for key in c.resources for a in self.act.by_resource.get(key[1], [])
                if a.source == "deletion_worker")
        # 3. deletion workflow activity in each case's reporting window
        event_cands: dict[str, list[CaseResult]] = {}
        event_of: dict[str, object] = {}
        for c in active:
            for a in self.act.workflow_window(c.request["reported_at"]):
                if a.resource_id in claimed_resources:
                    continue
                event_cands.setdefault(a.resource_id, []).append(c)
                event_of[a.resource_id] = a
        # (a) primary-profile deletions: global one-to-one matching, best score
        #     first, at most one account per case.
        # (b) other workflow events: in time order, scores recomputed after
        #     each assignment so ties spread one-to-one across cases.
        def is_account(rid):
            return resource_system(rid) == "account_service" and event_of[rid].event_type == "DELETE_RECORD"

        pairs = []
        for rid in event_cands:
            if not is_account(rid):
                continue
            owners = self.res_owners.get(rid)
            for c in event_cands[rid]:
                if owners and c.subject.user_id not in owners:
                    continue
                if any(k[0] == "account_service" for k in c.resources):
                    continue  # evidence already identified this case's account
                s = self._score(c, event_of[rid])
                if s is not None:
                    pairs.append((round(s, 1), event_of[rid].event_time, _case_num(c.case_id), rid, c))
        pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
        taken_r, taken_c = set(), set()
        for score, _t, _n, rid, c in pairs:
            if rid in taken_r or c.case_id in taken_c:
                continue
            rivals = [p[4].case_id for p in pairs if p[3] == rid and p[0] == score and p[4].case_id not in taken_c]
            if len(rivals) > 1:
                c.notes.append(f"{rid}: account deletion event tied between {rivals}; assigned in time/case order")
            taken_r.add(rid)
            taken_c.add(c.case_id)
            a = event_of[rid]
            ra = c.resources.setdefault(("account_service", rid), ResourceAssessment("account_service", rid))
            ra.links.append(f"{a.activity_id}: workflow {a.event_type} {a.result} in reporting window")

        act_count: dict[tuple[str, str], int] = {}
        for rid in sorted(event_cands, key=lambda r: event_of[r].event_time):
            if is_account(rid):
                continue
            a = event_of[rid]
            sysname = resource_system(rid)
            scored = []
            owners = self.res_owners.get(rid)
            for c in event_cands[rid]:
                if owners and c.subject.user_id not in owners:
                    continue  # evidence names a different person as this record's subject
                s = self._score(c, a)
                if s is None:
                    continue
                scored.append((round(s, 1), -act_count.get((c.case_id, sysname), 0), -_case_num(c.case_id), c))
            if not scored:
                continue
            scored.sort(key=lambda t: t[:3], reverse=True)
            best = scored[0]
            if best[0] < MIN_ATTRIBUTION_SCORE:
                continue
            tied = [t[3].case_id for t in scored if t[:2] == best[:2]]
            c = best[3]
            if len(tied) > 1:
                c.notes.append(f"{rid}: workflow event tied between {tied}; assigned in case order")
            act_count[(c.case_id, sysname)] = act_count.get((c.case_id, sysname), 0) + 1
            key = (sysname, rid)
            ra = c.resources.setdefault(key, ResourceAssessment(*key))
            ra.links.append(f"{a.activity_id}: workflow {a.event_type} {a.result} in reporting window")

    def _score(self, c: CaseResult, a) -> float | None:
        """Higher = more plausible that workflow event `a` belongs to case `c`."""
        delta_h = (ts(a.event_time) - ts(c.request["reported_at"])).total_seconds() / 3600
        score = 0.0
        sysname = resource_system(a.resource_id)
        if sysname == "account_service" and a.event_type == "DELETE_RECORD":
            # the primary-profile deletion precedes the status report, and its
            # result is what the workflow summarized as reported_status
            if not (-2.0 <= delta_h <= 0.5):
                return None
            if STATUS_RESULT.get(c.request["reported_status"]) == a.result:
                score += 5
            # observed in every sample: the profile deletion runs ~1h before the report
            score -= abs(delta_h + 1.0)
            delta_h = 0.0
        else:
            post = getattr(c, "post_report", False)
            if a.event_type in POLICY_TYPES:
                # completed policy actions are part of the work summarized by the
                # report (they precede it); failed ones are retried afterwards
                if a.result == "SUCCESS":
                    score -= 3 if delta_h > 0 else 0
                    score -= 2 if post else 0
                else:
                    score -= 3 if delta_h < 0 else 0
                    score += 1 if post else 0
            elif delta_h > 0 and post:
                score += 1
        # resource seen in an evidence file that also carries this case's other resources
        files = {o.file_id for o in self.obs_by_res.get((sysname, a.resource_id), [])}
        case_files = {l.split(":")[0] for ra in c.resources.values() for l in ra.links}
        if files & case_files:
            score += 3
        score -= abs(delta_h) / 10
        return score

    # ------------------------------------------------------------------
    # state, policy, decision

    def assess(self, c: CaseResult) -> None:
        audit_at = c.request["audit_at"]
        if not c.resources.get(("account_service",)) and not any(k[0] == "account_service" for k in c.resources):
            c.notes.append("no account resource could be linked to this case")
        self._retention_scope(c)
        case_files = {l.split(":")[0] for r in c.resources.values() for l in r.links if l.startswith("evidence_")}
        for key, ra in sorted(c.resources.items()):
            self._policy(ra, audit_at, c.request)
            self._state(ra, audit_at, case_files)
        noncompliant = [ra for ra in c.resources.values() if ra.compliant is False and ra.target != PRESENT]
        c.status = "INCOMPLETE" if noncompliant else "COMPLETE"
        if not c.resources:
            c.notes.append("no record of this subject could be linked to the case from participant-visible "
                           "evidence; reported as COMPLETE with no verifiable steps")

    def _retention_scope(self, c: CaseResult) -> None:
        """rule_12 retains security audit records *within the supplied retention
        period*. When evidence supplies a retention period for some of the
        subject's audit records, records shown without any retention period are
        not treated as policy-retained records of this case."""
        sec = [k for k in c.resources if k[0] == "security_service"]

        def has_retention(key):
            return any(re.search(r"retention_until|retain until|retention", o.detail, re.I)
                       for o in self.obs_by_res.get(key, []))

        with_ret = [k for k in sec if has_retention(k)]
        if with_ret and len(with_ret) < len(sec):
            for k in sec:
                if k not in with_ret:
                    c.notes.append(f"{k[1]}: no supplied retention period; not reported as a retained record")
                    del c.resources[k]

    def _policy(self, ra: ResourceAssessment, audit_at: str, request: dict) -> None:
        cat = SYSTEM_CATEGORY.get(ra.system, "USER_PROFILE")
        obs = self.obs_by_res.get((ra.system, ra.resource_id), [])
        hinted = [o.category for o in obs if o.category]
        intended = self.act.intended_action(ra.resource_id, audit_at)
        if ra.system == "support_platform":
            attach = any("attachment" in (o.detail or "").lower() or "attachment" in
                         self.ev.files.get(o.file_id, {}).get("file_path", "") for o in obs)
            if "SUPPORT_ATTACHMENT" in hinted or (not hinted and (attach or intended == "DELETE_RECORD")):
                cat = "SUPPORT_ATTACHMENT"
        if ra.system == "analytics_platform" and intended == "DELETE_RECORD":
            cat = "SECONDARY_IDENTIFIER"  # hash/contact keyed copy the workflow treats as an identifier
        if hinted and hinted[-1] in self.rules and ra.system not in {"support_platform"}:
            cat = hinted[-1]
        ra.category = cat
        if ra.system == "integration_service":
            ra.target, ra.rule_id = DISABLED, "data_flow"
            return
        rule = self.rules.get(cat)
        if rule is None:
            ra.target, ra.rule_id = ABSENT, "default"
            return
        ra.rule_id = rule["rule_id"]
        if cat == "BACKUP_ARCHIVE":
            ra.target = BLOCKED      # retained, but restore access must be blocked
        else:
            ra.target = ACTION_TARGET[rule["required_action"]]

    def _state(self, ra: ResourceAssessment, audit_at: str, case_files: set[str] = frozenset()) -> None:
        obs = [o for o in self.obs_by_res.get((ra.system, ra.resource_id), [])
               if o.obs_time <= audit_at and self.ev.files[o.file_id]["captured_at"] <= audit_at]
        if obs:
            latest_t = max(o.obs_time for o in obs)
            latest = [o for o in obs if o.obs_time == latest_t]
            # replica drift / conflicting copies at the same instant: the
            # non-compliant copy wins (any surviving copy means data remains)
            bad = [o for o in latest if o.state not in COMPLIANT[ra.target]]
            pick = sorted(bad or latest, key=lambda o: (self.ev.files[o.file_id]["captured_at"], o.file_id))[-1]
            ra.state = pick.state
            ra.evidence_id = pick.file_id
            ra.state_note = pick.detail[:160]
            return
        if ra.system == "account_service":
            proof = self._absence_by_omission(ra, audit_at, case_files)
            if proof:
                ra.state, ra.evidence_id, ra.state_note = ABSENT, proof, "not listed in profile snapshot"

    def _absence_by_omission(self, ra: ResourceAssessment, audit_at: str, case_files: set[str] = frozenset()) -> str | None:
        """A snapshot that covers the account store but does not list the
        account proves it absent at the snapshot time. Prefer the snapshot that
        also carries this case's other records (the same point-in-time capture),
        then the latest one."""
        best = None
        for fid, system, snap_t in self.ev.coverage:
            if system != ra.system or snap_t > audit_at or self.ev.files[fid]["captured_at"] > audit_at:
                continue
            rank = (fid in case_files, snap_t, fid)
            if best is None or rank > best:
                best = rank
        return best[2] if best else None
