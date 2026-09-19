"""Tests for the sequential /dedup Telegram triage flow (handlers.py): one AI-assessed
suggestion at a time, plain-text sim/não/ajuste reply, never a batch action — see
[[dedup_unitary_approval_required]]. Covers all four actionable shapes: people/resources
note-pair merge, a 2-way decision-group merge (with DecisionRecord DB cleanup), and a
diary-mention wikify. A multi-way decision group and projects/resource-id-collisions
stay a plain report — no merge primitive for those, tested in test_dedup_merge.py's
scope notes instead.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from rysos.config import settings
from rysos.db import get_db_session, DecisionRecord, DedupSuggestion, TelegramDraftSession
from rysos.telegram.handlers import handle_message
from rysos.vault.manager import VaultManager


def _setup(tmp_path: Path, monkeypatch, user_id: int = 900) -> VaultManager:
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", AsyncMock(return_value={"ok": True}))
    return vault


def _fake_assess_merge(kind, context):
    # Deterministic regardless of which side find_people_duplicates put in "a" vs "b":
    # the fuller name ("Marta Duarte") always survives.
    survivor_path = context["a_path"] if context["a_name"] == "Marta Duarte" else context["b_path"]
    return {
        "recommendation": "merge",
        "rationale": "Mesma pessoa, grafias diferentes.",
        "survivor_path": survivor_path,
        "consolidated_summary": "Nota consolidada de teste.",
    }


def _msg(user_id: int, text: str) -> dict:
    return {
        "message_id": 1,
        "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": text,
    }


async def _run_dedup(tmp_path: Path, monkeypatch, assess_fn, user_id: int = 900) -> VaultManager:
    vault = _setup(tmp_path, monkeypatch, user_id)
    vault.create_person_note(name="Marta")
    vault.create_person_note(name="Marta Duarte")
    monkeypatch.setattr("rysos.telegram.handlers.gemini_ai.assess_dedup_finding", assess_fn)

    await handle_message(_msg(user_id, "/dedup"))
    return vault


@pytest.mark.asyncio
async def test_dedup_command_queues_one_suggestion_and_sends_its_card(tmp_path: Path, monkeypatch):
    user_id = 900
    await _run_dedup(tmp_path, monkeypatch, _fake_assess_merge, user_id)

    async with get_db_session() as session:
        rows = (await session.execute(select(DedupSuggestion))).scalars().all()
        draft = await session.get(TelegramDraftSession, user_id)

    assert len(rows) == 1
    assert rows[0].kind == "people"
    assert rows[0].status == "pending"
    assert draft is not None
    assert draft.status == "awaiting_dedup_reply"

    from rysos.telegram.handlers import telegram_bot
    sent_texts = [c.kwargs.get("text", c.args[-1] if c.args else "") for c in telegram_bot.send_message.call_args_list]
    assert any("Marta" in t and "Marta Duarte" in t for t in sent_texts)


@pytest.mark.asyncio
async def test_dedup_reply_sim_merges_notes_and_closes_the_batch(tmp_path: Path, monkeypatch):
    user_id = 901
    vault = await _run_dedup(tmp_path, monkeypatch, _fake_assess_merge, user_id)

    await handle_message(_msg(user_id, "sim"))

    async with get_db_session() as session:
        row = (await session.execute(select(DedupSuggestion))).scalars().one()
        draft = await session.get(TelegramDraftSession, user_id)

    assert row.status == "applied"
    assert draft is None  # batch closed, no more pending

    assert not (vault.vault_path / "05_People" / "Marta.md").exists()
    assert (vault.vault_path / "08_Archive" / "People" / "Marta.md").exists()
    survivor = vault.read_file(vault.vault_path / "05_People" / "Marta Duarte.md")
    assert "Nota consolidada de teste." in survivor

    from rysos.telegram.handlers import telegram_bot
    sent_texts = [c.kwargs.get("text", "") for c in telegram_bot.send_message.call_args_list]
    assert any("Mesclado" in t for t in sent_texts)
    assert any("concluída" in t for t in sent_texts)


@pytest.mark.asyncio
async def test_dedup_reply_nao_rejects_without_touching_the_vault(tmp_path: Path, monkeypatch):
    user_id = 902
    vault = await _run_dedup(tmp_path, monkeypatch, _fake_assess_merge, user_id)

    await handle_message(_msg(user_id, "não"))

    async with get_db_session() as session:
        row = (await session.execute(select(DedupSuggestion))).scalars().one()
        draft = await session.get(TelegramDraftSession, user_id)

    assert row.status == "rejected"
    assert draft is None
    assert (vault.vault_path / "05_People" / "Marta.md").exists()
    assert (vault.vault_path / "05_People" / "Marta Duarte.md").exists()
    assert not (vault.vault_path / "08_Archive" / "People" / "Marta.md").exists()


def _fake_assess_keep_separate(kind, context):
    return {
        "recommendation": "keep_separate", "rationale": "Perfis distintos.",
        "survivor_path": None, "consolidated_summary": None,
    }


@pytest.mark.asyncio
async def test_dedup_reply_nao_with_trailing_words_still_rejects(tmp_path: Path, monkeypatch):
    """2026-09-18 Telegram thread: Rafael answered a keep_separate card with 'não,
    manter separado' — echoing the card's own suggestion — and it fell through to the
    ambiguous adjustment bucket instead of closing cleanly as a rejection. 'não' is safe
    to match on the reply's first word (it never writes to the vault), unlike 'sim'."""
    user_id = 907
    vault = await _run_dedup(tmp_path, monkeypatch, _fake_assess_keep_separate, user_id)

    await handle_message(_msg(user_id, "não,manter separado"))

    async with get_db_session() as session:
        row = (await session.execute(select(DedupSuggestion))).scalars().one()
        draft = await session.get(TelegramDraftSession, user_id)

    assert row.status == "rejected"
    assert draft is None
    from rysos.telegram.handlers import telegram_bot
    sent_texts = [c.kwargs.get("text", "") for c in telegram_bot.send_message.call_args_list]
    assert any("Não vou mesclar sozinho" in t for t in sent_texts)
    assert not any("ajuste" in t for t in sent_texts)


@pytest.mark.asyncio
async def test_dedup_reply_sim_with_trailing_words_is_never_auto_applied(tmp_path: Path, monkeypatch):
    """'sim' stays a strict exact match — it's the one reply that writes to the vault
    (a merge), so a hedged 'sim, mas confere antes' must not be read as a blank confirm."""
    user_id = 908
    vault = await _run_dedup(tmp_path, monkeypatch, _fake_assess_merge, user_id)

    await handle_message(_msg(user_id, "sim, mas confere antes"))

    async with get_db_session() as session:
        row = (await session.execute(select(DedupSuggestion))).scalars().one()

    assert row.status == "adjusted"  # not "applied" — never auto-merged from a hedge
    assert (vault.vault_path / "05_People" / "Marta.md").exists()
    assert not (vault.vault_path / "08_Archive" / "People").exists()


@pytest.mark.asyncio
async def test_dedup_reply_freetext_is_recorded_never_auto_applied(tmp_path: Path, monkeypatch):
    user_id = 903
    vault = await _run_dedup(tmp_path, monkeypatch, _fake_assess_merge, user_id)

    await handle_message(_msg(user_id, "mescla mas mantém o título Marta Duarte"))

    async with get_db_session() as session:
        row = (await session.execute(select(DedupSuggestion))).scalars().one()

    assert row.status == "adjusted"
    finding = json.loads(row.finding_json)
    assert finding["owner_adjustment_note"] == "mescla mas mantém o título Marta Duarte"
    # never auto-applied: both notes still present, nothing archived
    assert (vault.vault_path / "05_People" / "Marta.md").exists()
    assert (vault.vault_path / "05_People" / "Marta Duarte.md").exists()
    assert not (vault.vault_path / "08_Archive" / "People").exists()


@pytest.mark.asyncio
async def test_dedup_needs_review_is_not_interactive(tmp_path: Path, monkeypatch):
    def _fake_needs_review(kind, context):
        return {"recommendation": "needs_review", "rationale": "", "survivor_path": None, "consolidated_summary": None}

    user_id = 904
    await _run_dedup(tmp_path, monkeypatch, _fake_needs_review, user_id)

    async with get_db_session() as session:
        rows = (await session.execute(select(DedupSuggestion))).scalars().all()
        draft = await session.get(TelegramDraftSession, user_id)

    assert rows == []
    assert draft is None


@pytest.mark.asyncio
async def test_dedup_decisions_merge_via_sim_deletes_loser_db_row(tmp_path: Path, monkeypatch):
    user_id = 905
    vault = _setup(tmp_path, monkeypatch, user_id)
    path_a = vault.create_decision_note(
        decision_id="DEC-2026-00001", title="Aprovar orçamento",
        file_stem="DEC-2026-00001 - Aprovar orçamento",
    )
    path_b = vault.create_decision_note(
        decision_id="DEC-2026-00002", title="Aprovar orçamento",
        file_stem="DEC-2026-00002 - Aprovar orçamento",
    )
    async with get_db_session() as session:
        session.add(DecisionRecord(id="DEC-2026-00001", title="Aprovar orçamento",
                                    area="Negocios_e_Governanca", note_path=str(path_a)))
        session.add(DecisionRecord(id="DEC-2026-00002", title="Aprovar orçamento",
                                    area="Negocios_e_Governanca", note_path=str(path_b)))
        await session.commit()

    def _fake_assess(kind, context):
        assert kind == "decisions"
        survivor_path = context["a_path"] if "00001" in context["a_name"] else context["b_path"]
        return {
            "recommendation": "merge", "rationale": "Mesma decisão extraída duas vezes.",
            "survivor_path": survivor_path, "consolidated_summary": "Resumo consolidado.",
        }
    monkeypatch.setattr("rysos.telegram.handlers.gemini_ai.assess_dedup_finding", _fake_assess)

    await handle_message(_msg(user_id, "/dedup"))

    async with get_db_session() as session:
        suggestion = (await session.execute(select(DedupSuggestion))).scalars().one()
    assert suggestion.kind == "decisions"

    await handle_message(_msg(user_id, "sim"))

    async with get_db_session() as session:
        row = (await session.execute(select(DedupSuggestion))).scalars().one()
        remaining = (await session.execute(select(DecisionRecord))).scalars().all()

    assert row.status == "applied"
    assert [d.id for d in remaining] == ["DEC-2026-00001"]
    assert not path_b.exists()
    assert (vault.vault_path / "08_Archive" / "Decisions" / path_b.name).exists()


@pytest.mark.asyncio
async def test_dedup_diary_link_via_sim_wikifies_mention(tmp_path: Path, monkeypatch):
    user_id = 906
    vault = _setup(tmp_path, monkeypatch, user_id)
    vault.create_person_note(name="Heitor Sakamoto")
    cockpit_path = vault.vault_path / "00_Cockpit" / "Daily" / "2026-09-18.md"
    cockpit_path.write_text(
        "---\ntitle: Daily Cockpit\n---\n# Cockpit\n\n"
        "<!-- notas-livres:start -->\nFalei com Heitor Sakamoto sobre o projeto.\n"
        "<!-- notas-livres:end -->\n",
        encoding="utf-8",
    )

    def _fake_assess(kind, context):
        assert kind == "diary_candidates"
        return {
            "recommendation": "link_diary_mention", "rationale": "Menção clara à mesma pessoa.",
            "survivor_path": context["orphan_path"], "consolidated_summary": None,
        }
    monkeypatch.setattr("rysos.telegram.handlers.gemini_ai.assess_dedup_finding", _fake_assess)

    await handle_message(_msg(user_id, "/dedup"))

    async with get_db_session() as session:
        suggestion = (await session.execute(select(DedupSuggestion))).scalars().one()
    assert suggestion.kind == "diary_link"

    await handle_message(_msg(user_id, "sim"))

    async with get_db_session() as session:
        row = (await session.execute(select(DedupSuggestion))).scalars().one()

    assert row.status == "applied"
    content = vault.read_file(cockpit_path)
    assert "[[05_People/Heitor Sakamoto|Heitor Sakamoto]]" in content
