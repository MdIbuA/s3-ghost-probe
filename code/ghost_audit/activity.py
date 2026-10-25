"""System-activity stream: catalog links, aliases, and case-window association.

Activity rows are *claims*. They are used for two things only:
  1. Linking resources that carry no identifiers in any snapshot (e.g. an
     account that is already gone, or an analytics row that was anonymized)
     to the deletion case whose workflow touched them.
  2. Hinting the data category the workflow intended (DELETE vs REDACT vs
     ANONYMIZE) when the evidence itself does not name the category.
They are never used as proof of a resource's state.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .evidence import RESOURCE_RE, extract_identifiers


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


WORKFLOW_TYPES = {"DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD", "CHECK_DATA_FLOW",
                  "CHECK_RESTORE_ACCESS", "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "RETAIN_RECORD"}


@dataclass
class Activity:
    activity_id: str
    event_time: str
    event_type: str
    source: str
    target: str
    resource_id: str
    result: str
    details: str


class ActivityStream:
    def __init__(self, dataset_dir: Path):
        rows = list(csv.DictReader(open(dataset_dir / "system_activity.csv", encoding="utf-8")))
        self.acts = [Activity(r["activity_id"], r["event_time"], r["event_type"], r["source_system"],
                              r["target_system"], r["resource_id"], r["reported_result"], r["details"])
                     for r in rows]
        self.by_resource: dict[str, list[Activity]] = {}
        for a in self.acts:
            self.by_resource.setdefault(a.resource_id, []).append(a)
        # Data-catalog links: resource -> identifiers, and display alias -> resource
        self.catalog: dict[str, tuple[set[str], Activity]] = {}
        self.aliases: dict[str, str] = {}
        for a in self.acts:
            if a.event_type == "LINK_RESOURCE_IDENTITY":
                m = re.search(r"linked (\S+) \(([^)]+)\)", a.details) or re.search(r"confirmed (\S+) \(([^)]+)\)", a.details)
                if m and RESOURCE_RE.fullmatch(m.group(1)):
                    rid, alias = m.group(1), m.group(2)
                    self.catalog[rid] = (extract_identifiers(a.details.split(")", 1)[1]), a)
                    if alias != rid and len(alias) >= 6:
                        self.aliases[alias] = rid
        self.workflow = [a for a in self.acts if a.source == "deletion_worker" and a.event_type in WORKFLOW_TYPES]
        self.classify = [a for a in self.acts if a.event_type == "CLASSIFY_REQUEST"]

    def workflow_window(self, reported_at: str, before_h: float = 2.0, after_h: float = 2.5) -> list[Activity]:
        t = ts(reported_at)
        lo, hi = t - timedelta(hours=before_h), t + timedelta(hours=after_h)
        return [a for a in self.workflow if lo <= ts(a.event_time) <= hi]

    def intended_action(self, resource_id: str, before: str) -> str | None:
        """Latest treatment the deletion workflow attempted for the resource."""
        best = None
        for a in self.by_resource.get(resource_id, []):
            if a.source == "deletion_worker" and a.event_time <= before and a.event_type in {
                    "DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD"}:
                if best is None or a.event_time > best.event_time:
                    best = a
        return best.event_type if best else None
