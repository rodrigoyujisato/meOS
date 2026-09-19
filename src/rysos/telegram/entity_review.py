"""Telegram review flow for transcript `EntityCandidate` rows.

One **notification per meeting** with pending candidates: a short count plus a
pointer to the Triagem note. All resolution — per item and in bulk — happens by
editing that note (the single durable surface); the notification carries no
buttons. `entgrp:`/`entcand:` callbacks are still handled for cards sent before
this change.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from rysos.connectors.entity_apply import (
    apply_resolution, expand_bulk, plan_sibling_resolutions, BULK_MACROS,
)
from rysos.config import settings
from rysos.connectors.project_links import (
    MARK_DONE, payload_of, link_item, mark, meeting_projects, resolve_item_path,
)
from rysos.db import get_db_session, EntityCandidate, DecisionRecord
from rysos.vault.manager import VaultManager
from rysos.telegram.bot import telegram_bot

logger = logging.getLogger("rysos.telegram.entity_review")

_KIND_LABEL = {
    "person": "👤 Pessoa", "project": "🚀 Projeto",
    "decision": "⚖️ Decisão", "resource": "📄 Recurso",
}
_REVIEWABLE = tuple(_KIND_LABEL)
_MIN_SCORE = 0.85


def _now() -> datetime:
    return datetime.now()


def _matches(cand: EntityCandidate) -> list[dict[str, Any]]:
    return json.loads(cand.suggested_matches_json or "[]")


# --- card rendering ---------------------------------------------------------

def _counts(rows: list[EntityCandidate]) -> dict[str, int]:
    c = {"accept": 0, "ignore": 0, "decision": 0, "resource": 0,
         "person": 0, "project": 0}
    for r in rows:
        if r.status != "pending":
            continue
        if r.kind in ("person", "project"):
            c[r.kind] += 1
            ms = _matches(r)
            if ms and float(ms[0].get("score", 0) or 0) >= _MIN_SCORE:
                c["accept"] += 1
            elif not ms:
                c["ignore"] += 1
        elif r.kind == "decision":
            c["decision"] += 1
        elif r.kind == "resource":
            c["resource"] += 1
    return c


def _card(meeting_name: str, rows: list[EntityCandidate], sha8: str = "") -> str | None:
    """A buttonless notification: the pending count plus the path of the Triagem note
    to edit. ``None`` when nothing is pending. ``sha8`` is unused, kept for call-site
    compatibility with the legacy callback path."""
    pending = [r for r in rows if r.status == "pending"]
    if not pending:
        return None
    c = _counts(pending)
    body = [
        f"🗂️ <b>Triagem — {meeting_name}</b>",
        f"{c['project']} projeto(s), {c['person']} pessoa(s), "
        f"{c['decision']} decisão(ões), {c['resource']} recurso(s) para validar.",
        "",
        f"Resolva na nota: <code>07_Inbox_Agent/Triagem/{meeting_name}.md</code>",
    ]
    return "\n".join(body)


# --- push ------------------------------------------------------------------

async def push_meeting_review(sha: str, chat_id: Any, note_label: str | None = None) -> int:
    """DM one consolidated card for a transcript's still-unnotified pending
    candidates. Claims the rows (`notified_at`) and commits before sending, so a
    mid-send crash loses the card, not the durable Triagem note. Idempotent: a
    second call sends nothing. Returns 1 if a card was sent, else 0.

    ``note_label`` names the Triagem note in the card — pass it for diary entries,
    whose Triagem filename (``Diario <date> [sha8]``) differs from the journal-note
    stem in ``meeting_note_path``.
    """
    if not (telegram_bot.is_configured() and chat_id):
        return 0
    async with get_db_session() as session:
        rows = (await session.execute(
            select(EntityCandidate).where(
                EntityCandidate.transcript_sha == sha,
                EntityCandidate.status == "pending",
                EntityCandidate.notified_at.is_(None),
                EntityCandidate.kind.in_(_REVIEWABLE),
            ).order_by(EntityCandidate.id)
        )).scalars().all()
        if not rows:
            return 0
        text = _card(note_label or Path(rows[0].meeting_note_path).stem, rows)
        now = _now()
        for r in rows:
            r.notified_at = now
        await session.commit()

    if text is None:
        return 0
    try:
        await telegram_bot.send_message(chat_id=chat_id, text=text)
        return 1
    except Exception as e:
        logger.warning("Falha ao enviar card de Triagem: %s", e)
        return 0


async def push_pending_reviews(chat_id: Any) -> int:
    """On-demand (`/triagem`): (re)send one consolidated card for every meeting
    that still has pending reviewable candidates, regardless of `notified_at`
    (unlike `push_meeting_review`, which only fires once). Read-only — does not
    claim rows. Returns the number of cards sent."""
    if not (telegram_bot.is_configured() and chat_id):
        return 0
    async with get_db_session() as session:
        rows = (await session.execute(
            select(EntityCandidate).where(
                EntityCandidate.status == "pending",
                EntityCandidate.kind.in_(_REVIEWABLE),
            ).order_by(EntityCandidate.id)
        )).scalars().all()

    by_sha: dict[str, list[EntityCandidate]] = {}
    for r in rows:
        by_sha.setdefault(r.transcript_sha, []).append(r)

    sent = 0
    for sha, group in by_sha.items():
        text = _card(Path(group[0].meeting_note_path).stem, group)
        if text is None:
            continue
        try:
            await telegram_bot.send_message(chat_id=chat_id, text=text)
            sent += 1
        except Exception as e:
            logger.warning("Falha ao enviar card de Triagem (%s): %s", sha[:8], e)
    return sent


async def _feed_learning(learn_calls: list[tuple[dict[str, str], str]]) -> None:
    if not learn_calls:
        return
    try:
        from rysos.ai.learning import adaptive_kb
        for learn, surface in learn_calls:
            if learn and learn.get("resolved_path"):
                await adaptive_kb.record_concept_observation(
                    term=learn["term"], concept_type="entity",
                    metadata={"resolved_path": learn["resolved_path"], "surface_forms": [surface]},
                )
    except Exception as e:
        logger.warning("Falha ao registrar conceitos aprendidos: %s", e)


# --- callbacks ------------------------------------------------------------

async def handle_entgrp_callback(callback_query: dict[str, Any], parts: list[str]) -> None:
    """`entgrp:<sha8>:<macro>` — apply a bulk macro to every still-pending row of
    the meeting, then re-render the card with whatever is left."""
    cb_id = callback_query.get("id")
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    sha8 = parts[1] if len(parts) > 1 else ""
    macro = parts[2] if len(parts) > 2 else ""

    if macro == "noop":
        await telegram_bot.answer_callback_query(
            cb_id, text="Abra a nota de Triagem no Obsidian para resolver item a item.", show_alert=True)
        return
    if macro not in BULK_MACROS or not sha8:
        await telegram_bot.answer_callback_query(cb_id)
        return

    vault = VaultManager()
    learn_calls: list[tuple[dict[str, str], str]] = []
    async with get_db_session() as session:
        rows = (await session.execute(
            select(EntityCandidate).where(
                EntityCandidate.transcript_sha.like(f"{sha8}%"),
                EntityCandidate.kind.in_(_REVIEWABLE),
            ).order_by(EntityCandidate.id)
        )).scalars().all()
        all_pending = (await session.execute(
            select(EntityCandidate).where(EntityCandidate.status == "pending")
        )).scalars().all()
        pending = [r for r in rows if r.status == "pending"]
        plans = expand_bulk(macro, pending)
        done = 0
        for c, act, tgt in plans:
            try:
                outcome = await asyncio.to_thread(apply_resolution, vault, c, act, tgt)
            except Exception as e:
                logger.warning("entgrp %s: falha em %s: %s", macro, c.surface_form, e)
                continue
            if outcome.decision_row:
                session.add(DecisionRecord(**outcome.decision_row))
            c.status = outcome.status
            c.resolved_at = _now()
            if outcome.learn:
                learn_calls.append((outcome.learn, c.surface_form))
            done += 1
            # review once, applied everywhere: same person/project pending in other transcripts
            for sib, sact, stgt in plan_sibling_resolutions(c, c.status, c.resolved_to, all_pending):
                try:
                    so = await asyncio.to_thread(apply_resolution, vault, sib, sact, stgt)
                except Exception:
                    continue
                sib.status = so.status
                sib.resolved_at = _now()
                if so.learn:
                    learn_calls.append((so.learn, sib.surface_form))
                done += 1
        await session.commit()
        left = [r for r in rows if r.status == "pending"]
        meeting_name = Path(rows[0].meeting_note_path).stem if rows else ""

    await _feed_learning(learn_calls)
    await telegram_bot.answer_callback_query(cb_id, text=f"{done} item(ns) resolvido(s).")

    rebuilt = _card(meeting_name, left) if left else None
    if rebuilt:
        await telegram_bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=rebuilt)
    else:
        await telegram_bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=f"🗂️ <b>Triagem — {meeting_name}</b>\n✅ {done} item(ns) resolvido(s). Nada pendente.")


async def handle_entcand_callback(callback_query: dict[str, Any], parts: list[str]) -> None:
    """Legacy per-item callback `entcand:<id>:new|link:<index>|skip` — still handled
    for cards sent before the consolidation. New meetings use `entgrp:` only."""
    cb_id = callback_query.get("id")
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    try:
        cand_id = int(parts[1])
    except (IndexError, ValueError):
        await telegram_bot.answer_callback_query(cb_id)
        return
    action = parts[2] if len(parts) > 2 else ""

    async with get_db_session() as session:
        cand = await session.get(EntityCandidate, cand_id)
        if cand is None:
            await telegram_bot.answer_callback_query(cb_id, text="Item não encontrado.", show_alert=True)
            return
        if cand.status != "pending":
            await telegram_bot.answer_callback_query(cb_id, text="Item já resolvido.", show_alert=True)
            return

        vault = VaultManager()
        if action == "skip":
            res = apply_resolution(vault, cand, "skip")
        elif action == "new":
            res = await asyncio.to_thread(apply_resolution, vault, cand, "new")
            if res.decision_row:
                session.add(DecisionRecord(**res.decision_row))
        elif action == "link":
            try:
                mi = int(parts[3])
            except (IndexError, ValueError):
                await telegram_bot.answer_callback_query(cb_id)
                return
            ms = _matches(cand)
            if mi >= len(ms):
                await telegram_bot.answer_callback_query(cb_id, text="Opção expirada.", show_alert=True)
                return
            res = await asyncio.to_thread(apply_resolution, vault, cand, "link", ms[mi])
        else:
            await telegram_bot.answer_callback_query(cb_id)
            return

        cand.status = res.status
        cand.resolved_at = _now()
        surface = cand.surface_form
        learn = res.learn
        await session.commit()

    await _feed_learning([(learn, surface)] if learn else [])
    await telegram_bot.answer_callback_query(cb_id, text="Feito.")
    await telegram_bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=res.summary)


# --- project-link question (strict rule: an ambiguous decision/resource is asked) ----

_ITEM_LABEL = {"decision": ("A decisão", "Ela"), "resource": ("O recurso", "Ele")}


async def ask_project_link(
    cand: EntityCandidate, title: str, meeting_path: Path, projects: list[dict[str, Any]],
) -> bool:
    """DMs one question for a decision/resource born in a meeting that touched several
    projects and does not cite any. Answered by `prjlnk:<cand_id>:<index|all|none>`, where
    index is the position in the meeting's projects sorted by title. True when delivered."""
    chat_id = settings.TELEGRAM_CHAT_ID
    if not (telegram_bot.is_configured() and chat_id):
        return False
    noun, pron = _ITEM_LABEL.get(cand.kind, ("O item", "Ele"))
    text = (
        f"{noun} «{html.escape(title)}» saiu da reunião «{html.escape(meeting_path.stem)}», que tratou de "
        f"{', '.join(html.escape(p['title']) for p in projects)}. {pron} pertence a qual projeto?"
    )
    rows = [[{"text": p["title"][:40], "callback_data": f"prjlnk:{cand.id}:{i}"}] for i, p in enumerate(projects)]
    rows.append([
        {"text": "Todos", "callback_data": f"prjlnk:{cand.id}:all"},
        {"text": "Nenhum", "callback_data": f"prjlnk:{cand.id}:none"},
    ])
    try:
        return bool(await telegram_bot.send_message(chat_id=chat_id, text=text, reply_markup={"inline_keyboard": rows}))
    except Exception as e:
        logger.warning("Falha ao enviar pergunta de projeto: %s", e)
        return False


async def handle_prjlnk_callback(callback_query: dict[str, Any], parts: list[str]) -> None:
    """`prjlnk:<cand_id>:<index|all|none>` — writes the chosen project edge(s)."""
    cb_id = callback_query.get("id")
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    try:
        cand_id, choice = int(parts[1]), parts[2]
    except (IndexError, ValueError):
        await telegram_bot.answer_callback_query(cb_id)
        return

    async with get_db_session() as session:
        cand = await session.get(EntityCandidate, cand_id)
        if cand is None or payload_of(cand).get("project_link") == MARK_DONE:
            await telegram_bot.answer_callback_query(cb_id, text="Item já resolvido.", show_alert=True)
            return
        vault = VaultManager()
        found = await resolve_item_path(session, vault, cand)
        if found is None:
            await telegram_bot.answer_callback_query(cb_id, text="Nota não encontrada.", show_alert=True)
            return
        projects = await asyncio.to_thread(vault.list_projects)
        options = await asyncio.to_thread(meeting_projects, vault, Path(cand.meeting_note_path), projects)
        if choice == "none":
            chosen: list[dict[str, Any]] = []
        elif choice == "all":
            chosen = options
        else:
            try:
                chosen = [options[int(choice)]]
            except (ValueError, IndexError):
                await telegram_bot.answer_callback_query(cb_id, text="Opção expirada.", show_alert=True)
                return
        for p in chosen:
            await asyncio.to_thread(link_item, vault, cand.kind, found[0], p, found[1])
        mark(cand, MARK_DONE)
        await session.commit()

    await telegram_bot.answer_callback_query(cb_id, text="Feito.")
    summary = ", ".join(p["title"] for p in chosen) if chosen else "nenhum projeto"
    await telegram_bot.edit_message_text(
        chat_id=chat_id, message_id=message_id,
        text=f"🔗 «{html.escape(found[1])}» vinculado a: {html.escape(summary)}.",
    )
