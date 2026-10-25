"""Identity resolution: request email -> stable user_id -> every identifier
the subject has used (current snapshot plus user_audit_log history)."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .evidence import norm_phone


@dataclass
class Subject:
    user_id: str
    name: str
    identifiers: set[str] = field(default_factory=set)   # strong identifiers (email/hash/phone/user)
    emails: set[str] = field(default_factory=set)
    provenance: dict[str, str] = field(default_factory=dict)  # identifier -> source (snapshot / audit_id)
    match_note: str = ""


def _ident(field_name: str, value: str) -> str | None:
    if not value:
        return None
    if field_name == "email":
        return "email:" + value.strip().lower()
    if field_name == "email_hash":
        return "hash:" + value.strip().lower()
    if field_name == "phone":
        d = norm_phone(value)
        return "phone:" + d if len(d) >= 8 else None
    return None


class IdentityResolver:
    def __init__(self, dataset_dir: Path):
        self.users = {r["user_id"]: r for r in csv.DictReader(open(dataset_dir / "user_identities.csv", encoding="utf-8"))}
        self.audit = list(csv.DictReader(open(dataset_dir / "user_audit_log.csv", encoding="utf-8")))
        self.audit_by_user: dict[str, list[dict]] = {}
        for a in self.audit:
            self.audit_by_user.setdefault(a["user_id"], []).append(a)
        # email -> [(user_id, source)]
        self.email_index: dict[str, list[tuple[str, str]]] = {}
        for uid, u in self.users.items():
            self.email_index.setdefault(u["email"].strip().lower(), []).append((uid, "user_identities"))
        for a in self.audit:
            if a["field_name"] == "email":
                for v in (a["old_value"], a["new_value"]):
                    self.email_index.setdefault(v.strip().lower(), []).append((a["user_id"], a["audit_id"]))

    def resolve(self, email: str, audit_at: str) -> Subject | None:
        cands = {}
        for uid, src in self.email_index.get(email.strip().lower(), []):
            cands.setdefault(uid, src)
        if not cands:
            return None
        if len(cands) > 1:
            # prefer the user whose current email equals the request email
            exact = [u for u in cands if self.users.get(u, {}).get("email", "").lower() == email.lower()]
            uid = sorted(exact or cands)[0]
            note = f"ambiguous email matched {sorted(cands)}; chose {uid}"
        else:
            uid = next(iter(cands))
            note = f"matched via {cands[uid]}"
        return self.subject(uid, audit_at, note)

    def subject(self, uid: str, audit_at: str, note: str = "") -> Subject:
        u = self.users[uid]
        s = Subject(uid, u["name"], match_note=note)
        s.identifiers.add("user:" + uid)
        s.provenance["user:" + uid] = "user_identities"
        for f in ("email", "email_hash", "phone"):
            i = _ident(f, u[f])
            if i:
                s.identifiers.add(i)
                s.provenance.setdefault(i, "user_identities")
        for a in self.audit_by_user.get(uid, []):
            # old values were genuinely the subject's identifiers before the change;
            # new values only count once the change happened (at or before audit time)
            vals = [a["old_value"]] + ([a["new_value"]] if a["event_time"] <= audit_at else [])
            for v in vals:
                i = _ident(a["field_name"], v)
                if i:
                    s.identifiers.add(i)
                    s.provenance.setdefault(i, a["audit_id"])
        s.emails = {i[6:] for i in s.identifiers if i.startswith("email:")}
        return s
