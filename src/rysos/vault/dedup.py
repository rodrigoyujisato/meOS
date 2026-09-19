"""Read-only vault integrity scan: near-duplicate entities, id collisions, and a
wikilink health check (orphan notes, phantom links).

Deliberately detection-only (see 2026-09-16/17 Telegram feedback + [[decision_id_
collision_incident]] / [[resource_id_collision_fixed]] memories): merging two notes
rewrites backlinks across the whole vault, and a wrong merge is hard to undo. Every
function here returns a report; nothing here writes to the vault. `/dedup` (the
Telegram command) surfaces these reports so the owner decides what, if anything, to
do about each one by hand.

Pure functions operate on `(path, content)` pairs so they're testable without a
real vault — see `scan_vault` for how VaultManager feeds them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Not real markdown content — Obsidian's own config, and templates whose `{{ placeholder
# }}` syntax isn't a real wikilink. Excluded from BOTH scanning-as-a-source and being an
# orphan candidate; nothing here is part of the real note graph in either direction.
_NOT_CONTENT_PREFIXES = (".obsidian", "_templates")

# Notes that are fine to have NO incoming link (excluded from the orphan check only):
# archived notes, dated Cockpit snapshots, the transient Inbox where items are processed
# and moved out, and the 4 fixed PARA Area notes are all things other notes point FROM,
# not things that need a backlink pointing TO them to count as "found." They're still
# scanned as link SOURCES, though — e.g. a daily Cockpit snapshot is the only place that
# ever links to a given day's Decisions, so excluding it as a source too would make every
# Decision note look orphaned regardless of whether it's actually reachable.
_NO_INCOMING_REQUIRED_PREFIXES = _NOT_CONTENT_PREFIXES + (
    "08_Archive", "00_Cockpit", "07_Inbox_Agent", "02_Areas",
)

_WIKILINK_RE = re.compile(r"\[\[(.+?)\]\]")
_ID_RE = re.compile(r"^id:\s*(\S+)\s*$", re.MULTILINE)


def _matches_prefix(rel_path: str, prefixes: tuple[str, ...]) -> bool:
    return any(rel_path == p or rel_path.startswith(p + "/") for p in prefixes)


def _strip_accents(text: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")


def _normalize_stem(stem: str) -> str:
    t = _strip_accents(stem).lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class DedupReport:
    people: list[dict[str, Any]] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    resource_id_collisions: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    orphan_notes: list[str] = field(default_factory=list)
    phantom_links: list[dict[str, Any]] = field(default_factory=list)
    diary_candidates: list[dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.people or self.projects or self.resources or self.resource_id_collisions
            or self.decisions or self.orphan_notes or self.phantom_links
            or self.diary_candidates
        )


def find_people_duplicates(
    vault, given_cutoff: float = 0.85, surname_cutoff: float = 0.80, bare_cutoff: float = 0.85,
) -> list[dict[str, Any]]:
    """Unordered pairs of 05_People notes plausibly the same person (see
    VaultManager._person_pair_score for the given-name/surname matching logic — a shared
    given name or honorific alone is deliberately not enough)."""
    people = vault.list_people()
    seen: set[tuple[str, str]] = set()
    pairs: list[dict[str, Any]] = []
    for p in people:
        for match in vault.similar_people_scored(
            p["name"], given_cutoff=given_cutoff, surname_cutoff=surname_cutoff, bare_cutoff=bare_cutoff
        ):
            key = tuple(sorted((p["path"], match["path"])))
            if key in seen:
                continue
            seen.add(key)
            pairs.append({
                "a": p["name"], "a_path": p["path"],
                "b": match["name"], "b_path": match["path"],
                "score": match["score"],
            })
    return sorted(pairs, key=lambda x: x["score"], reverse=True)


def find_project_duplicates(vault, cutoff: float = 0.8) -> list[dict[str, Any]]:
    """Unordered pairs of 01_Projects notes whose titles near-match."""
    projects = vault.list_projects()
    seen: set[tuple[str, str]] = set()
    pairs: list[dict[str, Any]] = []
    for p in projects:
        for match in vault.find_similar_projects(p["title"], cutoff=cutoff):
            key = tuple(sorted((p["path"], match["path"])))
            if key in seen:
                continue
            seen.add(key)
            pairs.append({
                "a": p["title"], "a_path": p["path"],
                "b": match["title"], "b_path": match["path"],
                "score": match["score"],
            })
    return sorted(pairs, key=lambda x: x["score"], reverse=True)


def find_resource_duplicates(vault, cutoff: float = 0.7) -> list[dict[str, Any]]:
    """Unordered pairs of 06_Resources notes whose titles near-match — the same real-world
    document/product mentioned across meetings under a different name each time (e.g. "Contrato
    XYZ" vs "Minuta Contratual XYZ"), not an id collision (see `find_resource_id_collisions`).
    `create_resource_note`/`ensure_resource_note` only guard exact-title matches, so this fuzzy
    check is what actually surfaces the XYZ/Meridian class of duplicate for the owner to merge by
    hand — see [[resource_id_collision_fixed]] on why this module never merges automatically."""
    resources = vault.list_resources()
    seen: set[tuple[str, str]] = set()
    pairs: list[dict[str, Any]] = []
    for r in resources:
        for match in vault.find_similar_resources(r["name"], cutoff=cutoff):
            key = tuple(sorted((r["path"], match["path"])))
            if key in seen:
                continue
            seen.add(key)
            pairs.append({
                "a": r["name"], "a_path": r["path"],
                "b": match["title"], "b_path": match["path"],
                "score": match["score"],
            })
    return sorted(pairs, key=lambda x: x["score"], reverse=True)


def find_resource_id_collisions(vault) -> list[dict[str, Any]]:
    """Groups of 06_Resources notes that share an `id:` frontmatter value.

    Not a content-duplicate signal (see [[resource_id_collision_fixed]]) — these are
    typically distinct resources whose id generator collided. Reported so a fresh
    collision (a regression, or another historical batch) gets caught early, not so
    it gets merged.
    """
    resources_dir = vault.vault_path / "06_Resources"
    by_id: dict[str, list[str]] = {}
    for sub in ("Ideias_e_Criatividade", "Leituras_e_Pesquisas", "Frameworks_e_Metodos"):
        sub_dir = resources_dir / sub
        if not sub_dir.exists():
            continue
        for file in sorted(sub_dir.glob("*.md")):
            text = vault.read_file(file) or ""
            m = _ID_RE.search(text)
            if m:
                by_id.setdefault(m.group(1), []).append(file.stem)
    return [
        {"id": rid, "titles": titles}
        for rid, titles in sorted(by_id.items())
        if len(titles) > 1
    ]


def find_decision_duplicates(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Groups of DecisionRecord-shaped dicts ({id, title, area}) sharing a normalized
    (area, title) key. Pure — pass `[{"id": d.id, "title": d.title, "area": d.area}
    for d in ...]` from the caller's own DB query."""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for d in decisions:
        key = (_normalize_stem(d.get("area") or ""), _normalize_stem(d.get("title") or ""))
        if not key[1]:
            continue
        by_key.setdefault(key, []).append(d)
    return [
        {"area": area, "title": items[0]["title"], "ids": [i["id"] for i in items]}
        for (area, _title_norm), items in by_key.items()
        if len(items) > 1
        for area in [items[0].get("area") or ""]
    ]


def _index_key(rel_path: str) -> str:
    """The name a wikilink target must normalize-match to hit `rel_path`.

    A `.md` note is indexed by its bare stem (Obsidian resolves `[[Note]]` and
    `[[Note.md]]` identically). Anything else (attachments: `.txt`, `.pdf`, ...) is
    indexed by its full filename WITH extension, since that's how Obsidian links to
    non-note files and how the owner's own transcript cross-references are written
    (`[[07_Inbox_Agent/Transcricoes/Processado/nilton.txt]]`).
    """
    name = Path(rel_path).name
    if name.lower().endswith(".md"):
        return _normalize_stem(name[:-3])
    return _normalize_stem(name)


def _target_key(target: str) -> str:
    """Mirrors `_index_key` on the wikilink side: strip a literal trailing `.md`
    before normalizing, so `[[folder/Note.md]]` and `[[Note]]` key identically."""
    name = target.rsplit("/", 1)[-1]
    if name.lower().endswith(".md"):
        name = name[:-3]
    return _normalize_stem(name)


def check_link_health(
    files: list[tuple[str, str]],
    all_paths: list[str] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Pure orphan/phantom-link check. `files` is `[(rel_path, content), ...]` for every
    markdown file (link sources are only ever markdown). `all_paths` is every real path
    in the vault — markdown notes AND attachments (transcripts, PDFs, ...) — the
    existence universe a link target is checked against; defaults to `files`' own
    paths when omitted (attachment-free callers, e.g. tests). Returns
    (orphan_rel_paths, phantom_links).

    Link SOURCES exclude only `_NOT_CONTENT_PREFIXES` — a Cockpit snapshot or an
    archived note still has real outgoing links worth both following (so the note it
    points to isn't wrongly flagged orphan) and checking for phantoms. Orphan
    CANDIDATES additionally exclude `_NO_INCOMING_REQUIRED_PREFIXES` — those are
    unlinked-by-design (see the module docstring), not a real integrity problem.
    """
    link_sources = [(p, c) for p, c in files if not _matches_prefix(p, _NOT_CONTENT_PREFIXES)]
    orphan_candidates = [(p, c) for p, c in files if not _matches_prefix(p, _NO_INCOMING_REQUIRED_PREFIXES)]
    universe = all_paths if all_paths is not None else [p for p, _c in files]

    stem_index: dict[str, list[str]] = {}
    for rel_path in universe:
        stem_index.setdefault(_index_key(rel_path), []).append(rel_path)

    referenced: set[str] = set()
    phantom_links: list[dict[str, Any]] = []
    for rel_path, content in link_sources:
        for m in _WIKILINK_RE.finditer(content):
            # Split off `#heading` / `|alias` ourselves rather than in the regex: a
            # note title can itself contain `]` (e.g. "Meridian [Estratégia]"), so the
            # link body is captured whole (non-greedy up to the first `]]`) and only
            # `|`/`#` — which filenames don't contain — split target from the rest.
            raw = m.group(1).strip()
            target = raw.split("|", 1)[0].split("#", 1)[0].strip()
            if not target or "{{" in target:
                continue
            hits = stem_index.get(_target_key(target))
            if hits:
                referenced.update(hits)
            else:
                phantom_links.append({"source": rel_path, "target": target})

    orphan_notes = [
        rel_path for rel_path, _content in orphan_candidates
        if rel_path not in referenced
    ]
    return sorted(orphan_notes), phantom_links


def collect_vault_files(vault) -> tuple[list[tuple[str, str]], list[str]]:
    """One full read of every markdown file, plus every real path (attachments included).
    Call this ONCE per `/dedup` run and thread the result into `scan_vault`, `find_backlinks`
    and `find_diary_backlink_candidates` — each of those used to do its own full vault scan,
    which meant a single triage pass (`scan_vault` + a `find_backlinks` call per side of every
    finding) re-read every note in the vault several times over an rclone mount."""
    md_files: list[tuple[str, str]] = []
    all_paths: list[str] = []
    for file in vault.vault_path.rglob("*"):
        if not file.is_file():
            continue
        rel = str(file.relative_to(vault.vault_path))
        all_paths.append(rel)
        if file.suffix.lower() == ".md":
            md_files.append((rel, vault.read_file(file) or ""))
    return md_files, all_paths


def build_backlink_index(files: list[tuple[str, str]]) -> dict[str, list[dict[str, Any]]]:
    """Every `[[...]]` reference in `files`, keyed by the normalized target it resolves to
    (via `_target_key`) — pure, one pass over content already in memory. Preserves each
    source's own alias text (`[[Note|alias]]`), which `find_backlinks`/`build_finding_context`
    need to show real usage, not just "N notes link here"."""
    index: dict[str, list[dict[str, Any]]] = {}
    for rel_path, content in files:
        if _matches_prefix(rel_path, _NOT_CONTENT_PREFIXES):
            continue
        for m in _WIKILINK_RE.finditer(content):
            raw = m.group(1).strip()
            target = raw.split("|", 1)[0].split("#", 1)[0].strip()
            if not target or "{{" in target:
                continue
            alias = raw.split("|", 1)[1].strip() if "|" in raw else target
            index.setdefault(_target_key(target), []).append(
                {"source_path": rel_path, "target": target, "alias": alias}
            )
    return index


def find_backlinks(vault, target_rel_path: str, index: dict[str, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    """Every backlink to `target_rel_path`. Pass a precomputed `index` (from
    `build_backlink_index`, built once per triage run) to avoid re-scanning the vault per
    call — this is the standalone/test convenience path only, doing its own full scan when
    `index` is omitted. `target_rel_path` may be absolute (as `DedupReport` findings store
    it, via `list_people`/`list_resources`) or vault-relative — both are normalized."""
    target_path = Path(target_rel_path)
    if target_path.is_absolute():
        target_rel_path = str(target_path.relative_to(vault.vault_path))
    if index is None:
        files, _all_paths = collect_vault_files(vault)
        index = build_backlink_index(files)
    hits = index.get(_index_key(target_rel_path), [])
    return [h for h in hits if h["source_path"] != target_rel_path]


_WIKILINK_BODY_RE = re.compile(r"^([^#|]+)(#[^|]*)?(\|.*)?$")


def rewrite_wikilinks(content: str, old_stem: str, new_stem: str) -> str:
    """Rewrites every wikilink in `content` whose target resolves (via `_target_key`) to
    `old_stem` so it points at `new_stem` instead — preserving that link's own `#heading`
    and `|alias` text untouched, and keeping a folder prefix if the original had one (only
    the filename segment is swapped). Pure string transform, used by
    `VaultManager.repoint_backlinks` to fix up every note that pointed at a `/dedup`
    merge's loser. Idempotent: a link that already points at `new_stem` doesn't match
    `old_stem` and is left alone, so re-running this over already-repointed content is a
    no-op rather than a double-rewrite."""
    old_key = _target_key(old_stem)

    def _repl(m: re.Match) -> str:
        raw = m.group(1)
        body = raw.strip()
        parts = _WIKILINK_BODY_RE.match(body)
        if not parts:
            return m.group(0)
        target, heading, alias = parts.group(1).strip(), parts.group(2) or "", parts.group(3) or ""
        if not target or _target_key(target) != old_key:
            return m.group(0)
        new_target = f"{target.rsplit('/', 1)[0]}/{new_stem}" if "/" in target else new_stem
        return f"[[{new_target}{heading}{alias}]]"

    return _WIKILINK_RE.sub(_repl, content)


def find_diary_backlink_candidates(
    vault, md_files: list[tuple[str, str]], orphan_notes: list[str],
) -> list[dict[str, Any]]:
    """Safety net for the class of gap fixed by hand this session (Heitor Sakamoto /
    Vinicius Oliveira, 2026-09-16): an orphan 05_People/06_Resources note whose name is
    mentioned in plain text — never wikified — inside a Cockpit Daily's "Notas Livres do
    Dia" free-text block. `process_cockpit_free_notes` (core.py) already resolves this
    going forward on each day it runs; this exists for the day it didn't (a missed
    scheduler tick, a note authored before the pipeline existed). Deterministic, no LLM.

    NOTE (deliberately narrow scope): this only checks notes `scan_vault` already flagged
    as orphans. A plain-text mention of someone who already HAS an incoming link from some
    other meeting note is not caught here — flagged as a known gap, not silently widened,
    per the approved plan's scope.

    One entry per orphan, with every matching daily grouped under `mentions` (not one entry
    per orphan-per-day) — otherwise five mentions of the same person become five near-
    identical Telegram prompts. Returns candidates only — nothing here wikifies anything.
    """
    orphan_targets = [
        rel for rel in orphan_notes
        if rel.startswith("05_People/") or rel.startswith("06_Resources/")
    ]
    if not orphan_targets:
        return []

    daily_entries: list[tuple[str, str, str, str]] = []
    for rel_path, content in md_files:
        if not rel_path.startswith("00_Cockpit/Daily/"):
            continue
        free_text = vault.extract_free_notes(content)
        if not free_text:
            continue
        daily_entries.append((rel_path, Path(rel_path).stem, free_text, _normalize_stem(free_text)))

    candidates: list[dict[str, Any]] = []
    for rel_path in orphan_targets:
        name = Path(rel_path).stem
        normalized_name = _normalize_stem(name)
        if not normalized_name or len(normalized_name) < 3:
            continue
        # Word-boundary match: normalized text is already space-separated tokens (accents/
        # punctuation stripped by _normalize_stem), so \b around the full name prevents a
        # short name matching inside an unrelated longer word.
        name_pattern = re.compile(r"\b" + re.escape(normalized_name) + r"\b")
        raw_pattern = re.compile(re.escape(name), re.IGNORECASE)
        mentions = []
        for cockpit_path, cockpit_date, free_text, normalized_free in daily_entries:
            if not name_pattern.search(normalized_free):
                continue
            m = raw_pattern.search(free_text)
            mentions.append({
                "cockpit_path": cockpit_path,
                "cockpit_date": cockpit_date,
                "matched_text": m.group(0) if m else name,
            })
        if mentions:
            candidates.append({"orphan_path": rel_path, "orphan_name": name, "mentions": mentions})
    return candidates


def scan_vault(
    vault,
    decisions: list[dict[str, Any]] | None = None,
    files: tuple[list[tuple[str, str]], list[str]] | None = None,
) -> DedupReport:
    """Orchestrates every check above against a live VaultManager. `decisions` is the
    caller's own DecisionRecord query result (this module has no DB access). `files` is an
    optional precomputed `collect_vault_files(vault)` result — pass it when the caller will
    also build a backlink index / run `find_diary_backlink_candidates` in the same triage
    pass, so the vault is only read once total."""
    report = DedupReport(
        people=find_people_duplicates(vault),
        projects=find_project_duplicates(vault),
        resources=find_resource_duplicates(vault),
        resource_id_collisions=find_resource_id_collisions(vault),
        decisions=find_decision_duplicates(decisions or []),
    )

    md_files, all_paths = files if files is not None else collect_vault_files(vault)

    orphans, phantoms = check_link_health(md_files, all_paths)
    # Full lists here — len() must reflect the true count. Callers (e.g. the /dedup
    # Telegram message) are responsible for capping what they actually display.
    report.orphan_notes = orphans
    report.phantom_links = phantoms
    report.diary_candidates = find_diary_backlink_candidates(vault, md_files, orphans)
    return report
