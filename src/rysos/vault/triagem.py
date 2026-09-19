"""Triagem note — the human-facing audit surface for transcript entity candidates.

One note per meeting, at ``07_Inbox_Agent/Triagem/<meeting-stem>.md``, listing every
entity the transcript pipeline refused to create automatically. Phase 1 only *renders*
it (the owner reads it and acts in Obsidian, or waits for the Phase 2 Telegram flow); the
``resolução:`` line is written for humans and for the Phase 3 parse-back.

Idempotent: if the note already carries this transcript's ``<!-- triagem:<sha> -->``
marker it is left untouched, so any ``- [x]`` the user ticked or notes they added survive
a re-scan.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import select

from rysos.connectors.entity_apply import apply_resolution, expand_bulk, plan_sibling_resolutions
from rysos.connectors.entity_match import grouping_key as _grouping_key
from rysos.connectors.entity_resolver import CANDIDATE, ResolvedEntity
from rysos.db import get_db_session, DecisionRecord, EntityCandidate, ProcessedTranscript

logger = logging.getLogger("rysos.vault.triagem")

_TRIAGEM_SUBDIR = ("07_Inbox_Agent", "Triagem")

# Kinds the note actually renders a resolvable line for. The archive/keep gate counts
# only these — a bare ``extraction_failed`` flag row (core.py) has no ``resolução:``
# line, so it must not pin a note open forever.
_RESOLVABLE_KINDS = ("person", "project", "decision", "resource", "meeting_date")

# Priority order: project > person > decision > resource. The Telegram card offers
# only bulk actions; this note is the full per-item surface.
_KIND_HEADERS = [
    ("project", "## 🚀 Projetos"),
    ("person", "## 👤 Pessoas"),
    ("decision", "## ⚖️ Decisões"),
    ("resource", "## 📄 Recursos"),
]

_STUB_BY_KIND = {
    "person": "NOVO | =<nome exato> | IGNORAR",
    "project": "NOVO | =<nome exato> | IGNORAR",
    "decision": "REGISTRAR | =<título exato> | IGNORAR",
    "resource": "CRIAR | IGNORAR",
}

# Bulk macros: <primary phrase> -> macro key. Applied when the resolução line reads SIM.
_BULK_BLOCK = "## ⚡ Ações em massa"
_BULK_PHRASES = [
    ("aceitar sugestões fortes", "accept_suggestions",
     "vincula toda pessoa/projeto que tenha um \"parecido com\" de score alto"),
    ("ignorar itens sem correspondência", "ignore_unmatched",
     "descarta toda pessoa/projeto sem nenhum \"parecido com\""),
    ("registrar todas as decisões", "register_decisions",
     "cria um DecisionRecord para cada decisão abaixo"),
    ("criar notas para todos os recursos", "create_resources",
     "marca cada recurso abaixo como novo"),
]
# keyed by the accent-folded phrase (see _fold); read_triagem_resolutions folds too


def _triagem_marker(sha: str) -> str:
    return f"<!-- triagem:{sha[:12]} -->"


def _candidate_block(r: ResolvedEntity) -> list[str]:
    payload: dict[str, Any] = r.payload or {}
    if r.kind == "person":
        head_extra = ", ".join(x for x in (payload.get("role", ""), payload.get("org", "")) if x)
    elif r.kind in ("project", "decision"):
        head_extra = payload.get("status", "")
    elif r.kind == "resource":
        head_extra = payload.get("type", "")
    else:
        head_extra = ""

    lines = [f"- [ ] **{r.surface_form}**" + (f" — {head_extra}" if head_extra else "")]
    if r.context_sentence:
        lines.append(f'      frase: "{r.context_sentence}"')
    if r.kind in ("person", "project"):
        if r.suggested_matches:
            pretty = ", ".join(
                f"{m.get('name') or m.get('title')} ({m.get('score')})" for m in r.suggested_matches
            )
            lines.append(f"      parecido com: {pretty}")
        else:
            lines.append("      parecido com: nenhum")
    lines.append(f"      resolução: {_STUB_BY_KIND.get(r.kind, _STUB_BY_KIND['person'])}")
    return lines


def _dedupe_by_identity(group: list[ResolvedEntity]) -> list[ResolvedEntity]:
    """Collapse person/project candidates that share an identity key (e.g. "Nilton"
    and "Dr Nilton" in the same meeting) into one block; extra surfaces are noted
    on the primary's context sentence. Other kinds pass through unchanged."""
    if not group or group[0].kind not in ("person", "project"):
        return group
    seen: dict[str, ResolvedEntity] = {}
    extras: dict[str, list[str]] = {}
    for r in group:
        org = (r.payload or {}).get("org", "")
        sug = (r.suggested_matches or [None])[0]
        key = _grouping_key(r.kind, r.surface_form, org=org, top_suggestion=sug)
        if key not in seen:
            seen[key] = r
        elif r.surface_form != seen[key].surface_form:
            extras.setdefault(key, []).append(r.surface_form)
    out = []
    for key, r in seen.items():
        if extras.get(key):
            also = ", ".join(dict.fromkeys(extras[key]))
            ctx = (r.context_sentence or "").rstrip(". ")
            r = ResolvedEntity(
                r.kind, r.surface_form, r.normalized_form,
                (f"{ctx}. também citado como: {also}" if ctx else f"também citado como: {also}"),
                r.payload, r.disposition, r.link_target, r.suggested_matches, r.identity_key,
            )
        out.append(r)
    return out


def _bulk_block_lines() -> list[str]:
    """The ``## ⚡ Ações em massa`` block. No longer rendered in fresh Triagem notes —
    the per-item ``resolução:`` list is the single surface. Kept for the one-off
    ``migrate_entity_candidates.py`` and for parsing notes written before this change;
    ``read_triagem_resolutions`` still honours a ``SIM`` on one of these lines."""
    lines = [_BULK_BLOCK,
             "> Escreva `SIM` na `resolução:` de uma linha abaixo para aplicá-la a todos os itens de uma vez."]
    for phrase, _key, hint in _BULK_PHRASES:
        lines.append(f"- [ ] **{phrase}** — {hint}")
        lines.append("      resolução: (SIM para aplicar)")
    lines.append("")
    return lines


_DATE_SECTION_HEADER = "## 📅 Data da reunião"
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# "NOVO =<nome desejado>" (also REGISTRAR/CRIAR — same "new" action across kinds) lets
# the user correct the AI's literal transcription of a name before the note is created.
# Matched case-insensitively against the raw (unfolded) text so accents/casing in the
# desired name itself are preserved — only the keyword is case/prefix-checked.
_NEW_WITH_NAME_RE = re.compile(
    r"^\s*(?:novo|new|registrar|register|criar|create)\s*=\s*(.+?)\s*$", re.IGNORECASE
)
# Words that mean "the derived date is right, just confirm it" — resolve the
# meeting_date row without renaming the meeting note.
_CONFIRM_WORDS = frozenset({"confirmar", "confirmado", "confirmo", "confirma", "manter", "ok"})


def _date_section_lines(date_str: str) -> list[str]:
    """The ``## 📅 Data da reunião`` block: one resolvable line for the mtime-derived,
    still-unconfirmed meeting date. Shared by :func:`write_triagem_note` and the
    self-heal in :func:`apply_pending_triagem` so the two never drift."""
    return [
        _DATE_SECTION_HEADER,
        f"- [ ] **{date_str}** (derivada da data do arquivo)",
        f"      resolução: {_DATE_STUB}",
    ]


def _ensure_date_section(text: str, date_str: str) -> str:
    """Splice the ``## 📅 Data da reunião`` block into a Triagem note that has a pending
    ``meeting_date`` row but predates the block (or never rendered it). Idempotent — a
    note that already carries the header is returned unchanged. Inserted just before the
    first section header / candidate line so it reads above the entity groups."""
    if _DATE_SECTION_HEADER in text:
        return text
    lines = text.splitlines()
    block = _date_section_lines(date_str) + [""]
    for i, ln in enumerate(lines):
        if ln.startswith("## ") or _CAND_LINE.match(ln):
            return "\n".join(lines[:i] + block + lines[i:]).rstrip() + "\n"
    return text.rstrip() + "\n\n" + "\n".join(block).rstrip() + "\n"


def write_triagem_note(
    vault,
    meeting_path: Path,
    title: str,
    date_str: str,
    resolved: Iterable[ResolvedEntity],
    *,
    transcript_sha: str,
    date_unconfirmed: bool = False,
    extraction_degraded: bool = False,
    note_stem: Optional[str] = None,
    source_folder: str = "04_Meetings",
) -> Path:
    """Render the triagem note for one meeting (or diary entry). Returns its path. No-op
    (returns path unchanged) when the note already carries this transcript's marker.

    ``note_stem`` overrides the ``07_Inbox_Agent/Triagem/<stem>.md`` filename — the diary
    pipeline passes a per-entry stem so two entries on one day don't overwrite each
    other. ``source_folder`` is the folder the header ``meeting:`` wikilink points into
    (``09_Journal`` for diary entries). The parse-back path (`read_triagem_resolutions`
    / `apply_pending_triagem`) keys off the marker and ``resolução:`` lines only, so
    neither override affects it.
    """
    base = vault.vault_path.joinpath(*_TRIAGEM_SUBDIR)
    base.mkdir(parents=True, exist_ok=True)
    note_path = base / f"{note_stem or meeting_path.stem}.md"
    marker = _triagem_marker(transcript_sha)

    existing = vault.read_file(note_path)
    if existing is not None and marker in existing:
        return note_path

    candidates = [r for r in resolved if r.disposition == CANDIDATE]
    meeting_link = f"[[{source_folder}/{meeting_path.stem}|{title}]]"

    lines = [
        "---",
        "type: triagem",
        f'meeting: "{meeting_link}"',
        f"date: {date_str}",
        "status: pending",
        "---",
        marker,
        f"# 🗂️ Triagem: {title}",
        "",
        "> Nenhuma entidade abaixo foi criada ou vinculada automaticamente. Resolva cada item"
        " (aqui, ou pelos botões no Telegram).",
        "> Para corrigir o nome da nota nova, use `NOVO =<nome desejado>` (ou"
        " `REGISTRAR`/`CRIAR =<nome desejado>`). Ao vincular com `=<nome exato>`, cargo e"
        " organização de uma pessoa existente são atualizados com o valor mais recente.",
    ]
    if date_unconfirmed:
        lines.append("> ⚠️ Data da reunião não confirmada (derivada da data do arquivo).")
        lines.append("")
        lines.extend(_date_section_lines(date_str))
    if extraction_degraded:
        lines.append(
            "> ⚠️ Extração degradada: o modelo não conseguiu estruturar esta ata. A nota de"
            " reunião está quase vazia — revise a transcrição bruta anexa e reprocesse se"
            " necessário (renomeie o arquivo para gerar um novo hash)."
        )
    lines.append("")

    if not candidates:
        lines.append("_Nenhuma entidade pendente — tudo já existia no vault ou nada foi citado._")
    else:
        for kind, header in _KIND_HEADERS:
            group = [r for r in candidates if r.kind == kind]
            if not group:
                continue
            lines.append(header)
            for r in _dedupe_by_identity(group):
                lines.extend(_candidate_block(r))
            lines.append("")

    vault.write_file_atomic(note_path, "\n".join(lines).rstrip() + "\n")
    return note_path


# --- Phase 3: parse-back ------------------------------------------------------

_STUB = "NOVO | =<nome exato> | IGNORAR"
_DATE_STUB = "escreva `confirmar` para manter a data acima, ou a data correta AAAA-MM-DD"
_BULK_STUB = "(SIM para aplicar)"
_UNEDITED_STUBS = {_STUB, _DATE_STUB, _BULK_STUB, "REGISTRAR | IGNORAR", "CRIAR | IGNORAR"}
_YES = {"sim", "s", "x", "ok", "yes", "y"}
_CAND_LINE = re.compile(r"^- \[[ xX]\] \*\*(.+?)\*\*")
_RESOLUCAO_LINE = re.compile(r"^\s*resolução:\s*(.*?)\s*$")
_MARKER_RE = re.compile(r"<!-- triagem:([0-9a-fA-F]{12}) -->")
_STATUS_PENDING_RE = re.compile(r"^status:\s*pending\s*$", re.MULTILINE)
_STATUS_RESOLVED_RE = re.compile(r"^status:\s*resolved\s*$", re.MULTILINE)
# A bare outcome line appended under a candidate (not a `resolução:` line). Stripped
# and regenerated on every rewrite so a note that stays pending doesn't accrete them.
_ANNOTATION_LINE = re.compile(r"^\s+(?:✅|⚠️|—)\s")
_ARCHIVE_SUBDIR = ("08_Archive", "Triagem")
_ARCHIVE_FOOTER = "_Triagem concluída em "


def _fold(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)
    ).strip().lower()


_BULK_BY_PHRASE = {_fold(p): key for (p, key, _h) in _BULK_PHRASES}


def read_triagem_resolutions(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse a Triagem note body into ``(sha_prefix, resolutions)`` where each resolution
    is ``{"surface", "action" (new|link|skip), "target" (exact name for link),
    "desired_name" (new only, optional)}``.

    A ``resolução:`` line still on the ``NOVO | =<nome exato> | IGNORAR`` stub — or empty —
    is skipped: only lines the user actually edited count. ``NOVO =<nome desejado>``
    (also REGISTRAR/CRIAR) carries that name through as ``desired_name``, overriding the
    AI's literal transcription as the created note's title.
    """
    m = _MARKER_RE.search(text or "")
    sha_prefix = m.group(1).lower() if m else ""

    cur: Optional[dict[str, Any]] = None
    parsed: list[dict[str, Any]] = []
    for ln in (text or "").splitlines():
        hm = _CAND_LINE.match(ln)
        if hm:
            cur = {"surface": hm.group(1).strip(), "resolucao": None}
            parsed.append(cur)
            continue
        if cur is not None:
            rm = _RESOLUCAO_LINE.match(ln)
            if rm:
                cur["resolucao"] = rm.group(1).strip()

    out: list[dict[str, Any]] = []
    for p in parsed:
        raw = p["resolucao"] or ""
        if (not raw or raw in _UNEDITED_STUBS or "<nome exato>" in raw
                or "AAAA-MM-DD" in raw or "SIM para aplicar" in raw
                or raw.startswith("✅") or raw.startswith("⚠️") or raw.startswith("—")):
            continue
        folded = _fold(raw)
        macro = _BULK_BY_PHRASE.get(_fold(p["surface"]))
        if macro:
            if folded in _YES:
                out.append({"surface": p["surface"], "action": "bulk", "macro": macro, "target": None})
            continue
        if _ISO_DATE.fullmatch(raw):
            out.append({"surface": p["surface"], "action": "redate", "target": raw})
        elif folded in _CONFIRM_WORDS and _ISO_DATE.fullmatch(p["surface"]):
            # "confirmar" on the ## 📅 line: keep the derived date, resolve the row,
            # no rename. Reuses the redate branch (rename is a no-op for the same date).
            out.append({"surface": p["surface"], "action": "redate", "target": p["surface"]})
        elif (nm := _NEW_WITH_NAME_RE.match(raw)):
            out.append({
                "surface": p["surface"], "action": "new", "target": None,
                "desired_name": nm.group(1).strip(),
            })
        elif folded in ("novo", "new", "registrar", "register", "criar", "create"):
            out.append({"surface": p["surface"], "action": "new", "target": None})
        elif folded in ("ignorar", "ignore", "skip", "nao", "não"):
            out.append({"surface": p["surface"], "action": "skip", "target": None})
        elif raw.lstrip().startswith("="):
            out.append({"surface": p["surface"], "action": "link", "target": raw.lstrip()[1:].strip()})
        else:
            logger.info("Triagem: resolução não reconhecida %r para %r", raw, p["surface"])
    return sha_prefix, out


def _rewrite_resolved(text: str, annotations: dict[str, str], *, resolved: bool) -> str:
    """Annotate each acted line with its outcome and, when ``resolved``, flip the
    frontmatter to ``status: resolved``.

    A ``✅``/``—`` annotation *replaces* the ``resolução:`` line — the item is done and
    must not be retried on the next scan. A ``⚠️`` annotation is *appended* on its own
    line and the user's ``resolução:`` is left intact, so once the blocker clears (e.g.
    the link target note is created) the next scan retries it automatically.

    ``status`` only flips when ``resolved`` (no ``EntityCandidate`` still pending for the
    transcript). A partially-resolved note stays ``status: pending`` and is rescanned.
    """
    if resolved:
        text = _STATUS_PENDING_RE.sub("status: resolved", text, count=1)
    out_lines: list[str] = []
    cur_surface: Optional[str] = None
    for ln in text.splitlines():
        if _ANNOTATION_LINE.match(ln):
            continue  # stale appended outcome — regenerated below
        hm = _CAND_LINE.match(ln)
        if hm:
            cur_surface = hm.group(1).strip()
            out_lines.append(ln)
            continue
        rm = _RESOLUCAO_LINE.match(ln)
        if rm and cur_surface in annotations:
            indent = ln[: len(ln) - len(ln.lstrip())]
            ann = annotations[cur_surface]
            if ann.startswith("⚠️"):
                out_lines.append(ln)                     # keep resolução: for retry
                out_lines.append(f"{indent}{ann}")
            else:
                out_lines.append(f"{indent}resolução: {ann}")
            cur_surface = None
        else:
            out_lines.append(ln)
    return "\n".join(out_lines).rstrip() + "\n"


def _archive_triagem_note(vault, note_path: Path, text: str, *, summary: str = "") -> Path:
    """Move a Triagem note out of the inbox into ``08_Archive/Triagem/`` once nothing
    is left open for its transcript. Flips ``status: resolved``, appends a dated footer,
    and removes the source. Idempotent: re-archiving overwrites the destination and
    never doubles the footer."""
    dest_dir = vault.vault_path.joinpath(*_ARCHIVE_SUBDIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / note_path.name

    final = _STATUS_PENDING_RE.sub("status: resolved", text, count=1)
    if _ARCHIVE_FOOTER not in final:
        stamp = datetime.now().strftime("%Y-%m-%d")
        foot = f"{_ARCHIVE_FOOTER}{stamp}."
        if summary:
            foot += f" {summary}"
        final = final.rstrip() + "\n\n---\n\n" + foot + "_\n"

    vault.write_file_atomic(dest, final)
    if note_path.exists() and note_path.resolve() != dest.resolve():
        note_path.unlink(missing_ok=True)
    return dest


async def apply_pending_triagem(vault) -> int:
    """Scan ``07_Inbox_Agent/Triagem`` and reconcile every note against the DB, which
    is the single source of truth for what is open:

    * a note with **no** ``EntityCandidate`` still pending for its transcript is done —
      move it to ``08_Archive/Triagem/`` (covers notes resolved here, via Telegram, or
      by the old tudo-ou-nada path);
    * a note with pending rows and edited ``resolução:`` lines: apply each via
      :func:`apply_resolution`, annotate per item, then archive if that emptied the
      queue or leave it ``status: pending`` for the next scan if not.

    Runs at the top of ``process_pending_transcripts``. Returns candidates resolved.
    """
    base = vault.vault_path.joinpath(*_TRIAGEM_SUBDIR)
    if not base.exists():
        return 0
    total = 0
    for note_path in sorted(base.glob("*.md")):
        text = vault.read_file(note_path)
        if not text:
            continue
        try:
            total += await _apply_one_triagem_note(vault, note_path, text)
        except Exception as e:
            # A transient failure on one note (e.g. a SQLite "database is locked" from
            # racing another writer) must not stop the scan — every other note in this
            # inbox is independent and the user's hand-edited resolutions are waiting.
            logger.warning("Triagem: falha ao processar %s: %s", note_path.name, e)
            continue
    return total


async def _apply_one_triagem_note(vault, note_path: Path, text: str) -> int:
    """Reconcile one Triagem note against the DB. Returns candidates resolved."""
    total = 0
    if True:
        # Legacy: a note already flipped ``status: resolved`` (old path, or resolved
        # via Telegram) still sitting in the inbox — sweep it to the archive.
        if _STATUS_RESOLVED_RE.search(text) and not _STATUS_PENDING_RE.search(text):
            _archive_triagem_note(vault, note_path, text)
            return total

        sha_prefix, resolutions = read_triagem_resolutions(text)
        if not sha_prefix:
            return total

        async with get_db_session() as session:
            open_rows = (await session.execute(
                select(EntityCandidate).where(
                    EntityCandidate.transcript_sha.like(f"{sha_prefix}%"),
                    EntityCandidate.status == "pending",
                    EntityCandidate.kind.in_(_RESOLVABLE_KINDS),
                )
            )).scalars().all()
        if not open_rows:
            # DB says nothing is open for this transcript, regardless of frontmatter.
            _archive_triagem_note(vault, note_path, text)
            return total

        # Self-heal: a note with a pending meeting_date row but no ## 📅 block (written
        # before the block existed) can never be resolved from the note — splice the
        # block in so the user has a surface. Idempotent; the new line is an unedited
        # stub, so `resolutions` (already parsed above) is unaffected.
        md_row = next((r for r in open_rows if r.kind == "meeting_date"), None)
        if md_row is not None and _DATE_SECTION_HEADER not in text:
            text = _ensure_date_section(text, md_row.surface_form)
            logger.info("Triagem: bloco de data adicionado a %s (nota sem ## 📅)", note_path.name)
            if not resolutions:
                vault.write_file_atomic(note_path, text)
                return total

        if not resolutions:
            return total

        annotations: dict[str, str] = {}
        learn_calls: list[tuple[dict[str, str], str]] = []
        out_note_path = note_path
        stem_swap: tuple[str, str] | None = None
        async with get_db_session() as session:
            rows = (await session.execute(
                select(EntityCandidate).where(
                    EntityCandidate.transcript_sha.like(f"{sha_prefix}%"),
                    EntityCandidate.status == "pending",
                )
            )).scalars().all()

            async def _propagate(c) -> int:
                """Apply c's resolution to matching pending rows in OTHER transcripts."""
                if c.status not in ("linked", "confirmed_new", "dismissed"):
                    return 0
                sib_rows = (await session.execute(
                    select(EntityCandidate).where(EntityCandidate.status == "pending")
                )).scalars().all()
                n = 0
                for sib, act, tgt in plan_sibling_resolutions(c, c.status, c.resolved_to, sib_rows):
                    try:
                        so = await asyncio.to_thread(apply_resolution, vault, sib, act, tgt)
                    except Exception as e:
                        logger.warning("Triagem: propagação para %s falhou: %s", sib.surface_form, e)
                        continue
                    sib.status = so.status
                    sib.resolved_at = datetime.now()
                    if so.learn:
                        learn_calls.append((so.learn, sib.surface_form))
                    n += 1
                return n

            for res in resolutions:
                if res["action"] == "redate":
                    cand = next((r for r in rows if r.kind == "meeting_date" and r.status == "pending"), None)
                    if cand is None:
                        continue
                    old_mp = Path(cand.meeting_note_path)
                    new_mp = await asyncio.to_thread(
                        vault.rename_meeting_note_wikilink_safe, old_mp, res["target"],
                    )
                    if new_mp is None:
                        annotations[res["surface"]] = f"⚠️ não foi possível renomear para {res['target']} (nome em uso?)"
                        continue
                    # repoint every DB row of this transcript at the new note path
                    for r in (await session.execute(
                        select(EntityCandidate).where(EntityCandidate.transcript_sha.like(f"{sha_prefix}%"))
                    )).scalars().all():
                        r.meeting_note_path = str(new_mp)
                    for pt in (await session.execute(
                        select(ProcessedTranscript).where(
                            ProcessedTranscript.meeting_note_path == str(old_mp))
                    )).scalars().all():
                        pt.meeting_note_path = str(new_mp)
                    cand.status = "linked"
                    cand.resolved_to = str(new_mp)
                    cand.resolved_at = datetime.now()
                    verb = "confirmada" if new_mp == old_mp else "corrigida"
                    annotations[res["surface"]] = f"✅ data {verb} → {res['target']}"
                    total += 1
                    if new_mp != old_mp:  # move the Triagem note alongside its meeting note
                        # Defer the stem rewrite until *after* _rewrite_resolved: the
                        # annotation key is the old date string, and a bare-date stem
                        # would otherwise rewrite the candidate heading out from under it.
                        stem_swap = (old_mp.stem, new_mp.stem)
                        new_note_path = note_path.with_name(f"{new_mp.stem}.md")
                        if new_note_path != note_path and not new_note_path.exists():
                            note_path.unlink(missing_ok=True)
                            out_note_path = new_note_path
                    continue

                if res["action"] == "bulk":
                    plans = expand_bulk(res["macro"], rows)
                    done = 0
                    for c, act, tgt in plans:
                        try:
                            outcome = await asyncio.to_thread(apply_resolution, vault, c, act, tgt)
                        except Exception as e:
                            logger.warning("Triagem bulk %s: falha em %s: %s", res["macro"], c.surface_form, e)
                            continue
                        if outcome.decision_row:
                            # Isolated in a SAVEPOINT: a bad row here (e.g. a duplicate
                            # id) must not roll back every other candidate already
                            # resolved in this note's batch — that rollback is what
                            # turned one collision into a 34h/140-file retry storm
                            # (2026-09-14 incident). Only this row is lost, retried
                            # next scan.
                            try:
                                async with session.begin_nested():
                                    session.add(DecisionRecord(**outcome.decision_row))
                                    await session.flush()
                            except Exception as e:
                                logger.warning(
                                    "Triagem bulk %s: falha ao gravar DecisionRecord de %s: %s",
                                    res["macro"], c.surface_form, e,
                                )
                                continue
                        c.status = outcome.status
                        c.resolved_at = datetime.now()
                        if outcome.learn:
                            learn_calls.append((outcome.learn, c.surface_form))
                        done += 1
                        total += 1
                        total += await _propagate(c)
                    annotations[res["surface"]] = f"✅ {done} item(ns) processado(s)" if done else "— nada a aplicar"
                    continue

                norm = vault._normalize_for_match(res["surface"])
                cand = next((r for r in rows if r.normalized_form == norm and r.status == "pending"), None)
                if cand is None:
                    continue
                target = None
                if res["action"] == "link":
                    exact = (
                        vault._exact_person_match(res["target"]) if cand.kind == "person"
                        else vault._exact_project_match(res["target"]) if cand.kind == "project"
                        else vault._exact_resource_match(res["target"]) if cand.kind == "resource"
                        else vault._exact_decision_match(res["target"]) if cand.kind == "decision"
                        else None
                    )
                    if not exact:
                        annotations[res["surface"]] = f"⚠️ \"{res['target']}\" não encontrado no vault — pendente"
                        continue
                    target = {"name": res["target"], "path": exact["path"]}
                try:
                    outcome = await asyncio.to_thread(
                        apply_resolution, vault, cand, res["action"], target,
                        res.get("desired_name"), res["action"] == "link",
                    )
                except Exception as e:
                    annotations[res["surface"]] = f"⚠️ erro ao aplicar: {str(e)[:80]}"
                    continue
                if outcome.decision_row:
                    # See the bulk branch above: isolate so a bad row doesn't roll
                    # back every other resolution already applied in this pass.
                    try:
                        async with session.begin_nested():
                            session.add(DecisionRecord(**outcome.decision_row))
                            await session.flush()
                    except Exception as e:
                        annotations[res["surface"]] = f"⚠️ erro ao gravar decisão: {str(e)[:80]}"
                        continue
                cand.status = outcome.status
                cand.resolved_at = datetime.now()
                annotations[res["surface"]] = f"✅ {outcome.status} → {cand.resolved_to or '—'}"
                if outcome.learn:
                    learn_calls.append((outcome.learn, cand.surface_form))
                total += 1
                prop = await _propagate(cand)
                if prop:
                    annotations[res["surface"]] += f" (+{prop} em outras reuniões)"
                    total += prop
            await session.commit()

        for learn, surface in learn_calls:
            try:
                from rysos.ai.learning import adaptive_kb
                await adaptive_kb.record_concept_observation(
                    term=learn["term"], concept_type="entity",
                    metadata={"resolved_path": learn["resolved_path"], "surface_forms": [surface]},
                )
            except Exception as e:
                logger.warning("Triagem: falha ao registrar conceito para %s: %s", surface, e)

        # Recount against the DB: did this pass empty the queue for the transcript?
        async with get_db_session() as session:
            still_open = (await session.execute(
                select(EntityCandidate).where(
                    EntityCandidate.transcript_sha.like(f"{sha_prefix}%"),
                    EntityCandidate.status == "pending",
                    EntityCandidate.kind.in_(_RESOLVABLE_KINDS),
                )
            )).scalars().all()
        resolved_fully = not still_open

        if annotations or resolved_fully:
            final = _rewrite_resolved(text, annotations, resolved=resolved_fully)
            if stem_swap:
                final = final.replace(*stem_swap)
            if resolved_fully:
                _archive_triagem_note(
                    vault, out_note_path, final,
                    summary=(f"{total} item(ns) resolvidos." if total else ""),
                )
            elif final != text or out_note_path != note_path:
                # skip a no-op rewrite (stuck ⚠️ retry) — but the redate path already
                # unlinked note_path, so an unchanged move must still be written
                vault.write_file_atomic(out_note_path, final)
    return total
