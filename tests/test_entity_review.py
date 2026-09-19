"""Tests for the Phase 2 Telegram entity-review flow (rysos.telegram.entity_review)."""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from rysos.ai.gemini import _coerce_minutes
from rysos.core import rysos_core
from rysos.db import get_db_session, EntityCandidate, DecisionRecord
from rysos.telegram import entity_review as er
from rysos.vault.manager import VaultManager

SHA = "d" * 64


def _meeting_with_ata(vault: VaultManager, *, person_name="Nilton Zeich", person_org="Zeich Health",
                      with_project=False):
    """A meeting note carrying a rich ata block with one unresolved person line
    (and, when `with_project`, one unresolved project line)."""
    mp = vault.create_meeting_note(
        summary="Alinhamento Acervo",
        start_time=datetime(2026, 9, 5, 10), end_time=datetime(2026, 9, 5, 11),
        attendees=[], context_brief="brief",
    )
    base = {"title": "Alinhamento Acervo",
            "people": [{"name": person_name, "role": "", "org": person_org, "mentions": "citou o biobanco"}]}
    if with_project:
        base["projects"] = [{"name": "Acervo", "status": "", "summary": ""}]
    rich = _coerce_minutes(base, meeting_year=2026)
    vault.append_transcript_minutes(
        mp, SHA, title="Alinhamento Acervo", rich=rich, resolved_names={},
        source_link="[[07_Inbox_Agent/Transcricoes/Processado/x.txt]]", meeting_date="2026-09-05",
    )
    return mp


async def _add_candidate(mp: Path, **over):
    row = dict(
        transcript_sha=SHA, meeting_note_path=str(mp), kind="person",
        surface_form="Nilton Zeich", normalized_form="nilton zeich",
        context_sentence="citou o biobanco",
        payload_json=json.dumps({"role": "Diretor", "org": "Zeich Health"}),
        suggested_matches_json=None, status="pending",
    )
    row.update(over)
    async with get_db_session() as session:
        c = EntityCandidate(**row)
        session.add(c)
        await session.commit()
        return c.id


def _mock_bot(monkeypatch):
    send = AsyncMock(return_value={"ok": True})
    edit = AsyncMock(return_value={"ok": True})
    ans = AsyncMock(return_value=True)
    monkeypatch.setattr(er.telegram_bot, "is_configured", lambda: True)
    monkeypatch.setattr(er.telegram_bot, "send_message", send)
    monkeypatch.setattr(er.telegram_bot, "edit_message_text", edit)
    monkeypatch.setattr(er.telegram_bot, "answer_callback_query", ans)
    return send, edit, ans


def _cb(cand_id, action):
    return {"id": "cbq1", "data": f"entcand:{cand_id}:{action}",
            "message": {"chat": {"id": 42}, "message_id": 7}}


def _grp_cb(macro, sha8=SHA[:8]):
    return {"id": "cbq1", "data": f"entgrp:{sha8}:{macro}",
            "message": {"chat": {"id": 42}, "message_id": 7}}


# --- push (consolidated card) --------------------------------------------

async def test_push_sends_one_consolidated_card_and_is_idempotent(tmp_path, monkeypatch):
    vault = rysos_core.vault
    mp = _meeting_with_ata(vault)
    await _add_candidate(mp, normalized_form="nilton zeich", surface_form="Nilton Zeich")
    await _add_candidate(mp, kind="project", normalized_form="biobanco", surface_form="Acervo",
                         payload_json=json.dumps({"summary": "rede de biobancos"}))
    # a meeting_date flag row must NOT be pushed
    await _add_candidate(mp, kind="meeting_date", normalized_form="2026-09-05", surface_form="2026-09-05")

    send, _edit, _ans = _mock_bot(monkeypatch)
    n = await er.push_meeting_review(SHA, chat_id=42)
    assert n == 1
    assert send.await_count == 1  # ONE consolidated notification, not one per item
    card_text = send.await_args.kwargs["text"]
    assert "1 projeto(s), 1 pessoa(s)" in card_text
    assert "07_Inbox_Agent/Triagem/" in card_text  # points to the note, no buttons
    assert "reply_markup" not in send.await_args.kwargs

    async with get_db_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(EntityCandidate).where(EntityCandidate.transcript_sha == SHA)
        )).scalars().all()
    by_kind = {r.kind: r for r in rows}
    assert by_kind["person"].notified_at is not None
    assert by_kind["project"].notified_at is not None
    assert by_kind["meeting_date"].notified_at is None

    send.reset_mock()
    assert await er.push_meeting_review(SHA, chat_id=42) == 0
    assert send.await_count == 0


async def test_push_pending_reviews_resends_already_notified_meetings(tmp_path, monkeypatch):
    vault = rysos_core.vault
    mp = _meeting_with_ata(vault)
    # rows already notified once (the /triagem backlog case) -> push_meeting_review is a no-op
    await _add_candidate(mp, normalized_form="nilton zeich", surface_form="Nilton Zeich",
                         notified_at=datetime(2026, 9, 5, 12))
    await _add_candidate(mp, kind="decision", normalized_form="seguir com a", surface_form="Seguir com A",
                         payload_json=json.dumps({"status": "decidido"}), notified_at=datetime(2026, 9, 5, 12))
    await _add_candidate(mp, kind="meeting_date", normalized_form="2026-09-05", surface_form="2026-09-05")

    send, _edit, _ans = _mock_bot(monkeypatch)
    assert await er.push_meeting_review(SHA, chat_id=42) == 0        # notified -> nothing
    assert await er.push_pending_reviews(chat_id=42) == 1           # on-demand -> one card
    assert send.await_count == 1
    assert "1 pessoa(s), 1 decisão(ões)" in send.await_args.kwargs["text"]

    async with get_db_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(EntityCandidate).where(EntityCandidate.transcript_sha == SHA))).scalars().all()
    assert all(r.status == "pending" for r in rows)  # read-only: nothing resolved


async def test_push_pending_reviews_silent_when_nothing_pending(tmp_path, monkeypatch):
    _mock_bot(monkeypatch)
    assert await er.push_pending_reviews(chat_id=42) == 0


async def test_push_noop_when_not_configured(tmp_path, monkeypatch):
    vault = rysos_core.vault
    mp = _meeting_with_ata(vault)
    await _add_candidate(mp)
    monkeypatch.setattr(er.telegram_bot, "is_configured", lambda: True)
    assert await er.push_meeting_review(SHA, chat_id=None) == 0


# --- bulk callbacks (entgrp) -------------------------------------------

async def test_entgrp_accept_suggestions_links_and_marks_resolved(tmp_path, monkeypatch):
    vault = rysos_core.vault
    mp = _meeting_with_ata(vault)
    vault.create_person_note(name="Nilton Zeixeira", role="", organization="")
    cid = await _add_candidate(
        mp, suggested_matches_json=json.dumps([
            {"name": "Nilton Zeixeira", "path": str(vault.vault_path / "05_People" / "Nilton Zeixeira.md"), "score": 0.9},
        ]),
    )
    _mock_bot(monkeypatch)
    await er.handle_entgrp_callback(_grp_cb("accept_suggestions"), ["entgrp", SHA[:8], "accept_suggestions"])

    async with get_db_session() as session:
        cand = await session.get(EntityCandidate, cid)
    assert cand.status == "linked"
    assert "[[05_People/Nilton Zeixeira|Nilton Zeich]]" in mp.read_text(encoding="utf-8")


async def test_entgrp_ignore_unmatched_dismisses_only_suggestionless(tmp_path, monkeypatch):
    vault = rysos_core.vault
    mp = _meeting_with_ata(vault)
    no_match = await _add_candidate(mp, normalized_form="saka", surface_form="Heit", suggested_matches_json=None)
    with_match = await _add_candidate(
        mp, normalized_form="nilton zeich", surface_form="Nilton Zeich",
        suggested_matches_json=json.dumps([{"name": "Nilton Zeixeira", "path": "05_People/Nilton Zeixeira.md", "score": 0.9}]),
    )
    _mock_bot(monkeypatch)
    await er.handle_entgrp_callback(_grp_cb("ignore_unmatched"), ["entgrp", SHA[:8], "ignore_unmatched"])

    async with get_db_session() as session:
        a = await session.get(EntityCandidate, no_match)
        b = await session.get(EntityCandidate, with_match)
    assert a.status == "dismissed"
    assert b.status == "pending"  # had a suggestion -> untouched


async def test_entgrp_noop_points_to_the_note(tmp_path, monkeypatch):
    _send, _edit, ans = _mock_bot(monkeypatch)
    await er.handle_entgrp_callback(_grp_cb("noop"), ["entgrp", SHA[:8], "noop"])
    assert "Triagem" in ans.await_args.kwargs.get("text", "")


async def test_entgrp_register_decisions_creates_records(tmp_path, monkeypatch):
    vault = rysos_core.vault
    mp = _meeting_with_ata(vault)
    await _add_candidate(
        mp, kind="decision", normalized_form="seguir com o fornecedor a",
        surface_form="Seguir com o fornecedor A",
        payload_json=json.dumps({"status": "decidido", "rationale": "preço"}),
    )
    _mock_bot(monkeypatch)
    await er.handle_entgrp_callback(_grp_cb("register_decisions"), ["entgrp", SHA[:8], "register_decisions"])

    async with get_db_session() as session:
        from sqlalchemy import select
        decs = (await session.execute(select(DecisionRecord))).scalars().all()
    assert len(decs) == 1 and decs[0].title == "Seguir com o fornecedor A"


# --- resolution execution ------------------------------------------------

async def test_new_person_creates_note_and_patches_ata(tmp_path, monkeypatch):
    vault = rysos_core.vault
    mp = _meeting_with_ata(vault)
    cid = await _add_candidate(mp)
    _mock_bot(monkeypatch)

    from rysos.telegram.handlers import handle_callback_query  # exercises the real dispatch
    monkeypatch.setattr("rysos.config.settings.TELEGRAM_ALLOWED_USER_IDS", [1])
    q = _cb(cid, "new"); q["from"] = {"id": 1}
    await handle_callback_query(q)

    assert (vault.vault_path / "05_People" / "Nilton Zeich.md").exists()
    body = mp.read_text(encoding="utf-8")
    assert "[[05_People/Nilton Zeich|Nilton Zeich]]" in body
    assert "não confirmado" not in body.split("### 📄 Ata da transcrição:")[1]
    async with get_db_session() as session:
        cand = await session.get(EntityCandidate, cid)
    assert cand.status == "confirmed_new" and cand.resolved_at is not None


async def test_new_project_never_clobbers_existing_project_note(tmp_path, monkeypatch):
    vault = rysos_core.vault
    mp = _meeting_with_ata(vault, with_project=True)
    vault.create_project_note(title="Acervo", outcome_description="META REAL: rede nacional de biobancos")
    original = (vault.vault_path / "01_Projects" / "Acervo.md").read_text(encoding="utf-8")
    cid = await _add_candidate(
        mp, kind="project", normalized_form="biobanco", surface_form="Acervo",
        payload_json=json.dumps({"summary": "algo bem diferente"}),
    )
    _mock_bot(monkeypatch)
    await er.handle_entcand_callback(_cb(cid, "new"), ["entcand", str(cid), "new"])

    after = (vault.vault_path / "01_Projects" / "Acervo.md").read_text(encoding="utf-8")
    assert after == original  # existing project note survived untouched
    assert "[[01_Projects/Acervo|Acervo]]" in mp.read_text(encoding="utf-8")


async def test_link_enriches_only_blank_fields(tmp_path, monkeypatch):
    vault = rysos_core.vault
    mp = _meeting_with_ata(vault)
    # existing person note with a populated role, blank org
    vault.create_person_note(name="Nilton Zeixeira", role="Consultor", organization="")
    populated = (vault.vault_path / "05_People" / "Nilton Zeixeira.md").read_text(encoding="utf-8")
    cid = await _add_candidate(
        mp,
        payload_json=json.dumps({"role": "Diretor", "org": "Zeich Health"}),
        suggested_matches_json=json.dumps([
            {"name": "Nilton Zeixeira", "path": "05_People/Nilton Zeixeira.md", "score": 0.82},
        ]),
    )
    _mock_bot(monkeypatch)
    await er.handle_entcand_callback(_cb(cid, "link:0"), ["entcand", str(cid), "link", "0"])

    after = (vault.vault_path / "05_People" / "Nilton Zeixeira.md").read_text(encoding="utf-8")
    assert 'role: "Consultor"' in after            # non-blank left untouched
    assert 'organization: "Zeich Health"' in after  # blank filled
    assert after != populated
    body = mp.read_text(encoding="utf-8")
    assert "[[05_People/Nilton Zeixeira|Nilton Zeich]]" in body
    async with get_db_session() as session:
        cand = await session.get(EntityCandidate, cid)
    assert cand.status == "linked" and cand.resolved_to == "05_People/Nilton Zeixeira.md"


async def test_skip_dismisses_and_double_tap_is_safe(tmp_path, monkeypatch):
    vault = rysos_core.vault
    mp = _meeting_with_ata(vault)
    cid = await _add_candidate(mp)
    _send, _edit, ans = _mock_bot(monkeypatch)

    await er.handle_entcand_callback(_cb(cid, "skip"), ["entcand", str(cid), "skip"])
    async with get_db_session() as session:
        cand = await session.get(EntityCandidate, cid)
    assert cand.status == "dismissed"

    ans.reset_mock()
    await er.handle_entcand_callback(_cb(cid, "new"), ["entcand", str(cid), "new"])
    assert ans.await_args.kwargs.get("text") == "Item já resolvido."
    # note was never patched by the stale tap
    assert "[[05_People/" not in mp.read_text(encoding="utf-8").split("### 📄 Ata")[1]


async def test_decision_new_creates_decision_record(tmp_path, monkeypatch):
    vault = rysos_core.vault
    mp = _meeting_with_ata(vault)
    cid = await _add_candidate(
        mp, kind="decision", normalized_form="seguir com fornecedor a",
        surface_form="Seguir com fornecedor A",
        payload_json=json.dumps({"status": "decidido", "rationale": "melhor preço"}),
    )
    _mock_bot(monkeypatch)
    await er.handle_entcand_callback(_cb(cid, "new"), ["entcand", str(cid), "new"])

    async with get_db_session() as session:
        from sqlalchemy import select
        decs = (await session.execute(select(DecisionRecord))).scalars().all()
        cand = await session.get(EntityCandidate, cid)
    assert len(decs) == 1 and decs[0].title == "Seguir com fornecedor A"
    assert decs[0].note_path and Path(decs[0].note_path).exists()
    assert cand.status == "confirmed_new"


# --- patch_transcript_entity invariants (C1) -----------------------------

def test_patch_transcript_entity_idempotent_and_below_marker_only(tmp_path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    mp = _meeting_with_ata(vault)
    before = mp.read_text(encoding="utf-8")
    marker = vault._transcript_marker(SHA)
    frontmatter_before = before.split(marker)[0]

    changed1 = vault.patch_transcript_entity(
        mp, SHA, kind="person", surface_form="Nilton Zeich",
        link_target=str(tmp_path / "05_People" / "Nilton Zeixeira.md"), display_name="Nilton Zeich",
    )
    changed2 = vault.patch_transcript_entity(
        mp, SHA, kind="person", surface_form="Nilton Zeich",
        link_target=str(tmp_path / "05_People" / "Nilton Zeixeira.md"), display_name="Nilton Zeich",
    )
    after = mp.read_text(encoding="utf-8")
    assert changed1 is True and changed2 is False
    assert after.split(marker)[0] == frontmatter_before  # frontmatter + human sections byte-equal
    assert "[[05_People/Nilton Zeixeira|Nilton Zeich]]" in after
    assert "não confirmado" not in after.split(marker)[1]


def test_patch_transcript_entity_no_partial_name_collision(tmp_path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    mp = _meeting_with_ata(vault, person_name="Nilton Zeich")
    # a bare "Nilton" resolution must not touch the "Nilton Zeich" line
    changed = vault.patch_transcript_entity(
        mp, SHA, kind="person", surface_form="Nilton",
        link_target=str(tmp_path / "05_People" / "Nilton.md"),
    )
    assert changed is False
    assert "**Nilton Zeich**" in mp.read_text(encoding="utf-8")


def test_enrich_person_note_fills_blank_only(tmp_path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Ana Reis", role="CEO", organization="")
    orig = (vault.vault_path / "05_People" / "Ana Reis.md").read_text(encoding="utf-8")

    assert vault.enrich_person_note("Ana Reis", role="Diretora", org="Acme") is True
    after = (vault.vault_path / "05_People" / "Ana Reis.md").read_text(encoding="utf-8")
    assert 'role: "CEO"' in after and 'organization: "Acme"' in after

    assert vault.enrich_person_note("Ana Reis", role="Outra") is False  # nothing blank left
    assert vault.enrich_person_note("Quem Nao Existe", role="X") is False
    assert after != orig


def test_enrich_person_note_overwrite_replaces_existing_value(tmp_path):
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    vault.create_person_note(name="Ana Reis", role="CEO", organization="Acme")

    # Default (overwrite=False) still leaves the filled fields alone.
    assert vault.enrich_person_note("Ana Reis", role="Diretora") is False

    assert vault.enrich_person_note("Ana Reis", role="Diretora", overwrite=True) is True
    after = (vault.vault_path / "05_People" / "Ana Reis.md").read_text(encoding="utf-8")
    assert 'role: "Diretora"' in after
    assert 'organization: "Acme"' in after  # untouched field, not passed this call


# --- dispatch -----------------------------------------------------------

async def test_dispatch_routes_entcand_before_draft_query(monkeypatch):
    from rysos.telegram import handlers
    monkeypatch.setattr("rysos.config.settings.TELEGRAM_ALLOWED_USER_IDS", [1])
    seen = {}

    async def _fake(cbq, parts):
        seen["parts"] = parts

    monkeypatch.setattr("rysos.telegram.entity_review.handle_entcand_callback", _fake)
    await handlers.handle_callback_query({
        "id": "x", "data": "entcand:5:link:2", "from": {"id": 1},
        "message": {"chat": {"id": 1}, "message_id": 1},
    })
    assert seen["parts"] == ["entcand", "5", "link", "2"]
