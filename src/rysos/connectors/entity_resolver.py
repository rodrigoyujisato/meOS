"""Deterministic resolution of transcript-extracted entities against the vault.

Pure module: it reads a pre-built vault index (people / projects lists) but never
writes a note and never touches Telegram. For each person / project / decision /
resource the model pulled out of a transcript it assigns one disposition:

  auto_link  — a confident match to an existing vault note (exact, full multi-token,
               surname-position token, or first-name + composite key). Safe to link
               with no human confirmation.
  candidate  — needs validation (fuzzy match, homonym, brand-new, or a decision/
               resource). Carries `suggested_matches` and the source sentence.
  drop       — a one-shot role-less person mention that fails the salience filter.
               Rendered in the ata body but produces no `EntityCandidate`.

The robust name/dedup logic lives in `rysos.connectors.entity_match`; this module
maps its output onto `ResolvedEntity` and keeps the learned short-circuit + the
per-kind priority (project > person > decision > resource).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from rysos.connectors import entity_match
from rysos.vault.manager import VaultManager

AUTO_LINK = "auto_link"
CANDIDATE = "candidate"
DROP = "drop"


@dataclass
class ResolvedEntity:
    kind: str                       # person | project | decision | resource | extraction_failed
    surface_form: str
    normalized_form: str
    context_sentence: str
    payload: dict[str, Any]
    disposition: str                # AUTO_LINK | CANDIDATE | DROP
    link_target: Optional[str] = None
    suggested_matches: list[dict[str, Any]] = field(default_factory=list)
    identity_key: str = ""          # cross-transcript grouping key (computed, not stored)


class EntityResolver:
    def __init__(self, vault: VaultManager):
        self.vault = vault
        self._learned: dict[str, str] = {}
        self._people: list[dict[str, Any]] = []
        self._projects: list[dict[str, Any]] = []
        self._raw = ""

    def resolve(
        self,
        rich: dict[str, Any],
        *,
        raw_text: str = "",
        people_index: Optional[list[dict[str, Any]]] = None,
        projects_index: Optional[list[dict[str, Any]]] = None,
        learned: Optional[dict[str, str]] = None,
    ) -> list[ResolvedEntity]:
        """`people_index` / `projects_index` are `VaultManager.list_people()` /
        `list_projects()` results, built once per run by the caller (hoisted out of
        the per-transcript loop). `learned` maps a normalised person surface form to
        a vault path the user has already confirmed (from `AdaptiveConcept`)."""
        self._learned = learned or {}
        self._people = people_index if people_index is not None else self.vault.list_people()
        self._projects = projects_index if projects_index is not None else self.vault.list_projects()
        self._raw = raw_text or ""

        out: list[ResolvedEntity] = []
        out += self._resolve_projects(rich.get("projects", []))
        out += self._resolve_people(rich.get("people", []))
        out += self._resolve_decisions(rich.get("decisions", []))
        out += self._resolve_resources(rich.get("resources", []))
        return out

    def _learned_link(self, norm: str) -> Optional[str]:
        """Auto-link only when the confirmed mapping is unambiguous: exact hit on the
        normalised surface, multi-token surface, and no OTHER learned term shares its
        first token (the two-Niltons guard)."""
        path = self._learned.get(norm)
        if not path:
            return None
        tokens = norm.split()
        if len(tokens) < 2:
            return None
        first = tokens[0]
        if sum(1 for t in self._learned if t.split()[:1] == [first]) > 1:
            return None
        return path

    # -- people ---------------------------------------------------------------
    def _resolve_people(self, people: list[dict[str, Any]]) -> list[ResolvedEntity]:
        res: list[ResolvedEntity] = []
        seen: set[str] = set()
        for p in people:
            name = (p.get("name") or "").strip()
            norm = self.vault._normalize_for_match(name)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            payload = {"role": p.get("role", ""), "org": p.get("org", ""), "mentions": p.get("mentions", "")}
            ctx = p.get("mentions", "")
            ik = entity_match.identity_key("person", name, org=payload["org"])

            if not entity_match.should_queue_person(name, payload, self._raw):
                res.append(ResolvedEntity("person", name, norm, ctx, payload, DROP, identity_key=ik))
                continue

            m = entity_match.match_against(
                name, self._people, kind="person",
                org=payload["org"], role=payload["role"],
            )
            if m["decision"] == "auto":
                res.append(ResolvedEntity("person", name, norm, ctx, payload, AUTO_LINK,
                                          link_target=m["target"],
                                          suggested_matches=m["suggested_matches"], identity_key=ik))
                continue

            learned = self._learned_link(norm)
            if learned:
                res.append(ResolvedEntity("person", name, norm, ctx, payload, AUTO_LINK,
                                          link_target=learned, identity_key=ik))
                continue

            res.append(ResolvedEntity("person", name, norm, ctx, payload, CANDIDATE,
                                      suggested_matches=m["suggested_matches"], identity_key=ik))
        return res

    # -- projects -----------------------------------------------------------------
    def _resolve_projects(self, projects: list[dict[str, Any]]) -> list[ResolvedEntity]:
        res: list[ResolvedEntity] = []
        seen: set[str] = set()
        for pr in projects:
            name = (pr.get("name") or "").strip()
            norm = self.vault._normalize_for_match(name)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            payload = {"status": pr.get("status", ""), "summary": pr.get("summary", "")}
            ctx = pr.get("summary", "")
            ik = entity_match.identity_key("project", name)
            m = entity_match.match_against(name, self._projects, kind="project")
            if m["decision"] == "auto":
                res.append(ResolvedEntity("project", name, norm, ctx, payload, AUTO_LINK,
                                          link_target=m["target"],
                                          suggested_matches=m["suggested_matches"], identity_key=ik))
            else:
                res.append(ResolvedEntity("project", name, norm, ctx, payload, CANDIDATE,
                                          suggested_matches=m["suggested_matches"], identity_key=ik))
        return res

    # -- decisions & resources: always candidates --------------------------------
    def _resolve_decisions(self, decisions: list[dict[str, Any]]) -> list[ResolvedEntity]:
        res: list[ResolvedEntity] = []
        seen: set[str] = set()
        for dc in decisions:
            title = (dc.get("title") or "").strip()
            norm = self.vault._normalize_for_match(title)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            res.append(ResolvedEntity(
                "decision", title, norm, dc.get("rationale", ""),
                {"status": dc.get("status", "em_analise"), "rationale": dc.get("rationale", ""), "owner": dc.get("owner", "")},
                CANDIDATE, identity_key=entity_match.identity_key("decision", title),
            ))
        return res

    def _resolve_resources(self, resources: list[dict[str, Any]]) -> list[ResolvedEntity]:
        res: list[ResolvedEntity] = []
        seen: set[str] = set()
        for r in resources:
            name = (r.get("name") or "").strip()
            norm = self.vault._normalize_for_match(name)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            res.append(ResolvedEntity(
                "resource", name, norm, r.get("note", ""),
                {"type": r.get("type", ""), "note": r.get("note", "")},
                CANDIDATE, identity_key=entity_match.identity_key("resource", name),
            ))
        return res
