"""Tests for Telegram bot client, authorization, and multimodal conversational handlers."""

import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path

from rysos.config import settings
from rysos.telegram.bot import TelegramBot, format_for_telegram
from rysos.telegram.handlers import (
    is_authorized,
    handle_message,
    handle_callback_query,
    register_bot_commands,
    NATIVE_BOT_COMMANDS,
)
from rysos.vault.manager import VaultManager
from rysos.db import get_db_session, TelegramDraftSession, AdaptiveFeedbackLog


def test_native_bot_commands_are_well_formed_and_deterministic():
    import re
    from pathlib import Path as _P

    routing = _P("src/rysos/telegram/handlers.py").read_text()
    name_re = re.compile(r"^[a-z0-9_]{1,32}$")
    for entry in NATIVE_BOT_COMMANDS:
        name, desc = entry["command"], entry["description"]
        assert name_re.match(name), f"invalid Telegram command name: {name!r}"
        assert 1 <= len(desc) <= 256
        # every menu command must be wired to a fixed handler (no AI in the loop)
        assert f'"/{name}"' in routing, f"/{name} has no deterministic route"


@pytest.mark.asyncio
async def test_register_bot_commands_pushes_the_menu(monkeypatch):
    sent = {}
    async def fake_set(commands):
        sent["commands"] = commands
        return True
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.set_my_commands", fake_set)

    assert await register_bot_commands() is True
    assert sent["commands"] == NATIVE_BOT_COMMANDS
    assert [c["command"] for c in sent["commands"]][:3] == ["cockpit", "agenda", "decisoes"]


def test_format_for_telegram_converts_markdown_bold_to_html():
    # The reported bug: the LLM emits **bold**, Telegram (parse_mode=HTML) shows the asterisks
    assert format_for_telegram("Boleto da **Gasnorte** pago") == "Boleto da <b>Gasnorte</b> pago"
    assert format_for_telegram("__Reunião__ confirmada") == "<b>Reunião</b> confirmada"
    assert format_for_telegram("## Focos de hoje") == "<b>Focos de hoje</b>"
    assert format_for_telegram("- item um\n- item dois") == "• item um\n• item dois"


def test_format_for_telegram_leaves_real_html_and_plain_text_alone():
    src = "📌 <b>Título:</b> Expansão\n<i>tudo certo</i>\n<code>DEC-2026-001</code>"
    assert format_for_telegram(src) == src
    # single * / _ must survive (snake_case filenames, currency, math)
    assert format_for_telegram("arquivo Patrimonio_e_Financas.md") == "arquivo Patrimonio_e_Financas.md"
    assert format_for_telegram("margem de 3*4 e taxa_de_juros") == "margem de 3*4 e taxa_de_juros"


def test_format_for_telegram_escapes_bare_ampersand_but_keeps_entities():
    assert format_for_telegram("Negócios & Governança") == "Negócios &amp; Governança"
    assert format_for_telegram("já &amp; pronto") == "já &amp; pronto"  # not double-escaped
    assert format_for_telegram("") == ""


def test_telegram_bot_configuration():
    bot_unconfigured = TelegramBot(token="")
    assert not bot_unconfigured.is_configured()

    bot_configured = TelegramBot(token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
    assert bot_configured.is_configured()
    assert "https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz" in bot_configured.api_url


def test_authorization_whitelist(monkeypatch):
    # Case 1: Empty whitelist -> nobody is authorized
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [])
    assert is_authorized(99999) is False  # empty whitelist authorizes nobody (fail-closed)

    # Case 2: Configured whitelist
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [11111, 22222])
    assert is_authorized(11111) is True
    assert is_authorized(22222) is True
    assert is_authorized(33333) is False


@pytest.mark.asyncio
async def test_handle_start_command(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [100])

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)

    msg = {
        "message_id": 1,
        "from": {"id": 100, "first_name": "Rafael"},
        "chat": {"id": 100},
        "text": "/start",
    }
    await handle_message(msg)

    assert send_mock.called
    call_args = send_mock.call_args[1]
    assert call_args["chat_id"] == 100
    assert "Rafael" in call_args["text"]
    assert "/cockpit" in call_args["text"]


@pytest.mark.asyncio
async def test_handle_unauthorized_message(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [100])
    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)

    msg = {
        "message_id": 2,
        "from": {"id": 999, "username": "intruder"},
        "chat": {"id": 999},
        "text": "Olá rysOS",
    }
    await handle_message(msg)

    assert send_mock.called
    call_args = send_mock.call_args[1]
    assert "Acesso Restrito" in call_args["text"]


@pytest.mark.asyncio
async def test_conversational_capture_incomplete_then_voice_complete_then_save(tmp_path: Path, monkeypatch):
    user_id = 100
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)

    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)

    send_mock = AsyncMock(return_value={"ok": True})
    action_mock = AsyncMock(return_value=True)
    edit_mock = AsyncMock(return_value={"ok": True})
    ans_mock = AsyncMock(return_value=True)

    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", action_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.edit_message_text", edit_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.answer_callback_query", ans_mock)

    # Clean existing test draft if any
    async with get_db_session() as session:
        d = await session.get(TelegramDraftSession, user_id)
        if d:
            await session.delete(d)
            await session.commit()

    # Step 1: User sends initial idea with missing fields
    mock_incomplete_gemini = {
        "category": "project",
        "is_complete": False,
        "title": "Expansão Hospitalar",
        "summary": "Projeto de expansão para a rede hospitalar",
        "content": "",
        "extracted_fields": {"title": "Expansão Hospitalar"},
        "missing_fields": ["deadline", "stakeholders"],
        "conversational_reply": "Entendido, parece um ótimo projeto. Qual o prazo estimado e quem são os stakeholders envolvidos?",
    }
    monkeypatch.setattr("rysos.telegram.handlers.gemini_ai.process_conversational_note", lambda **kwargs: mock_incomplete_gemini)

    msg_step1 = {
        "message_id": 10,
        "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "projeto de expansão para a rede hospitalar",
    }
    await handle_message(msg_step1)

    # Bot should reply conversationally asking for missing fields
    assert send_mock.called
    assert "Qual o prazo estimado" in send_mock.call_args[1]["text"]

    async with get_db_session() as session:
        draft = await session.get(TelegramDraftSession, user_id)
        assert draft is not None
        assert draft.status == "drafting"
        assert draft.category == "project"

    # Step 2: User replies via Voice Note with the missing info
    mock_voice_file = {"file_path": "voice/audio123.oga"}
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.get_file", AsyncMock(return_value=mock_voice_file))
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.download_file_bytes", AsyncMock(return_value=b"fake_audio_bytes"))
    monkeypatch.setattr("rysos.telegram.handlers.gemini_ai.transcribe_audio", lambda bytes, **kw: "O prazo é até dezembro de 2026 e a stakeholder é a Dra. Renata")

    mock_complete_gemini = {
        "category": "project",
        "is_complete": True,
        "title": "Expansão Hospitalar",
        "summary": "Expansão da rede com prazo até dez/2026 e Dra. Renata como stakeholder",
        "content": "---\ntitle: Expansão Hospitalar\ndeadline: 2026-12-31\nstakeholders:\n  - Dra. Renata\n---\n# Expansão Hospitalar\nDetalhes do projeto.",
        "extracted_fields": {
            "title": "Expansão Hospitalar",
            "deadline": "2026-12-31",
            "stakeholders": ["Dra. Renata"],
            "outcome_description": "Ampliar a capacidade instalada da rede hospitalar até dez/2026.",
            "area": "Negócios & Governança",
        },
        "missing_fields": [],
        "conversational_reply": "Perfeito! Reuni todas as informações necessárias.",
    }
    monkeypatch.setattr("rysos.telegram.handlers.gemini_ai.process_conversational_note", lambda **kwargs: mock_complete_gemini)

    voice_msg = {
        "message_id": 11,
        "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "voice": {"file_id": "v_123"},
    }
    await handle_message(voice_msg)

    # Bot should now send preview with approval keyboard
    last_call = send_mock.call_args[1]
    assert "Nota Pronta" in last_call["text"]
    assert "reply_markup" in last_call
    assert last_call["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"cap:save:{user_id}"

    async with get_db_session() as session:
        draft = await session.get(TelegramDraftSession, user_id)
        assert draft.status == "awaiting_confirmation"

    # Step 3: User clicks inline button [✅ Gravar no Obsidian]
    cb = {
        "id": "cb_save",
        "data": f"cap:save:{user_id}",
        "message": {"chat": {"id": user_id}, "message_id": 12},
        "from": {"id": user_id},
    }
    await handle_callback_query(cb)

    assert edit_mock.called
    assert "gravada com sucesso" in edit_mock.call_args[1]["text"]

    # Session deleted from DB
    async with get_db_session() as session:
        draft = await session.get(TelegramDraftSession, user_id)
        assert draft is None

    # Check that note was saved into 01_Projects
    projects_dir = tmp_path / "01_Projects"
    saved_files = list(projects_dir.glob("*.md"))
    assert len(saved_files) == 1
    assert "Expansão Hospitalar" in saved_files[0].name


@pytest.mark.asyncio
async def test_conversational_text_confirmation(tmp_path: Path, monkeypatch):
    user_id = 200
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)

    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))

    # Pre-create session in awaiting_confirmation state
    async with get_db_session() as session:
        d = TelegramDraftSession(
            user_id=user_id,
            status="awaiting_confirmation",
            category="idea",
            title="Ideia de Investimento",
            content="# Ideia de Investimento\nConteúdo da ideia.",
            summary="Ideia de investimento",
        )
        await session.merge(d)
        await session.commit()

    # User simply sends "sim"
    confirm_msg = {
        "message_id": 20,
        "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "sim",
    }
    await handle_message(confirm_msg)

    assert send_mock.called
    assert "gravada com sucesso" in send_mock.call_args[1]["text"]

    # Session deleted
    async with get_db_session() as session:
        assert await session.get(TelegramDraftSession, user_id) is None


@pytest.mark.asyncio
async def test_cancellation_via_text(monkeypatch):
    user_id = 300
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)

    async with get_db_session() as session:
        d = TelegramDraftSession(user_id=user_id, status="drafting", title="Nota Descartável")
        await session.merge(d)
        await session.commit()

    cancel_msg = {
        "message_id": 30,
        "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "cancela",
    }
    await handle_message(cancel_msg)

    assert send_mock.called
    assert "Rascunho cancelado" in send_mock.call_args[1]["text"]

    async with get_db_session() as session:
        assert await session.get(TelegramDraftSession, user_id) is None


@pytest.mark.asyncio
async def test_cancellation_via_callback(monkeypatch):
    user_id = 400
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    ans_mock = AsyncMock(return_value=True)
    edit_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.answer_callback_query", ans_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.edit_message_text", edit_mock)

    async with get_db_session() as session:
        d = TelegramDraftSession(user_id=user_id, status="awaiting_confirmation", title="Nota Descartável")
        await session.merge(d)
        await session.commit()

    cb = {
        "id": "cb_cancel",
        "data": f"cap:cancel:{user_id}",
        "message": {"chat": {"id": user_id}, "message_id": 31},
        "from": {"id": user_id},
    }
    await handle_callback_query(cb)

    assert edit_mock.called
    assert "Nota cancelada" in edit_mock.call_args[1]["text"]

    async with get_db_session() as session:
        assert await session.get(TelegramDraftSession, user_id) is None


@pytest.mark.asyncio
async def test_save_callback_ignores_a_non_confirmation_draft(monkeypatch):
    """A stale 'cap:save' button must not write a half-built or placeholder draft."""
    user_id = 401
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    ans_mock = AsyncMock(return_value=True)
    edit_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.answer_callback_query", ans_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.edit_message_text", edit_mock)

    def _boom(*a, **k):
        raise AssertionError("save_quick_capture must not run for a non-confirmation draft")

    monkeypatch.setattr("rysos.telegram.handlers.VaultManager.save_quick_capture", _boom)

    for status in ("drafting", "disambiguating"):
        async with get_db_session() as session:
            d = TelegramDraftSession(user_id=user_id, status=status, category="idea")
            await session.merge(d)
            await session.commit()

        cb = {
            "id": f"cb_save_{status}",
            "data": f"cap:save:{user_id}",
            "message": {"chat": {"id": user_id}, "message_id": 32},
            "from": {"id": user_id},
        }
        await handle_callback_query(cb)

        assert not edit_mock.called
        assert ans_mock.call_args[1].get("show_alert") is True
        async with get_db_session() as session:
            row = await session.get(TelegramDraftSession, user_id)
            assert row is not None
            await session.delete(row)
            await session.commit()


@pytest.mark.asyncio
async def test_send_evening_recap_and_command(tmp_path: Path, monkeypatch):
    user_id = 500
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)

    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)

    # Create cockpit file for today with completed and pending items
    from datetime import date
    today_str = date.today().strftime("%Y-%m-%d")
    cockpit_file = tmp_path / "00_Cockpit" / "Daily" / f"{today_str}.md"
    cockpit_content = (
        "---\ntitle: Daily Cockpit\n---\n"
        "# Cockpit Diário\n\n"
        "## 🎯 Focos de Alta Prioridade do Dia\n"
        "- [x] **Aprovação do Acervo** — Alinhar com diretoria\n"
        "- [ ] **Revisão orçamentária** — Verificar planilha com financeiro\n\n"
        "## 📅 Agenda & Reuniões de Hoje\n"
        "- 10:00 Reunião Estratégica\n"
    )
    vault.write_file_atomic(cockpit_file, cockpit_content)

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)

    # Trigger via /recap command
    msg = {
        "message_id": 50,
        "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "/recap",
    }
    await handle_message(msg)

    assert send_mock.called
    sent_text = send_mock.call_args[1]["text"]
    assert "Balanço Executivo do Dia" in sent_text
    assert "1/2 foco(s) concluído(s)" in sent_text
    assert "Aprovação do Acervo" in sent_text
    assert "Revisão orçamentária" in sent_text


def _write_cockpit_with_gas(vault: VaultManager, tmp_path: Path) -> Path:
    from datetime import date
    today_str = date.today().strftime("%Y-%m-%d")
    cockpit_file = tmp_path / "00_Cockpit" / "Daily" / f"{today_str}.md"
    vault.write_file_atomic(cockpit_file, (
        "---\ntitle: Daily Cockpit\n---\n"
        "# Cockpit Diário\n\n"
        "## 🎯 Focos de Alta Prioridade do Dia\n"
        "- [ ] **💳 Pagar boleto: Gasnorte** (Patrimônio & Finanças) — Valor: R$ 333,86 — Vencimento HOJE\n"
        "- [ ] **Revisão orçamentária** — Conferir planilha com o financeiro\n\n"
        "## 📅 Agenda & Reuniões de Hoje\n*Nenhum compromisso registrado para hoje.*\n"
    ))
    return cockpit_file


@pytest.mark.asyncio
async def test_natural_focus_completion_checks_cockpit_item(tmp_path: Path, monkeypatch):
    user_id = 700
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)

    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)
    cockpit_file = _write_cockpit_with_gas(vault, tmp_path)

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))

    async with get_db_session() as session:
        d = await session.get(TelegramDraftSession, user_id)
        if d:
            await session.delete(d)
            await session.commit()

    await handle_message({
        "message_id": 70,
        "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "Boleto Gasnorte pago hoje",
    })

    assert send_mock.called
    assert "concluído" in send_mock.call_args[1]["text"]
    assert "Gasnorte" in send_mock.call_args[1]["text"]

    content = cockpit_file.read_text(encoding="utf-8")
    assert "- [x] **💳 Pagar boleto: Gasnorte**" in content
    assert "- [ ] **Revisão orçamentária**" in content

    # A completion report must never leave a lingering note draft
    async with get_db_session() as session:
        assert await session.get(TelegramDraftSession, user_id) is None


@pytest.mark.asyncio
async def test_focus_completion_escapes_stale_note_draft(tmp_path: Path, monkeypatch):
    user_id = 701
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)

    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)
    cockpit_file = _write_cockpit_with_gas(vault, tmp_path)

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))

    # Simulate the reported trap: bot already opened a note draft asking "para qual dia?"
    async with get_db_session() as session:
        d = TelegramDraftSession(
            user_id=user_id, status="drafting", category="today",
            title="Boleto Gasnorte", history_json="[]",
        )
        await session.merge(d)
        await session.commit()

    await handle_message({
        "message_id": 71,
        "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "O boleto da Gasnorte foi pago hoje",
    })

    assert "concluído" in send_mock.call_args[1]["text"]
    assert "- [x] **💳 Pagar boleto: Gasnorte**" in cockpit_file.read_text(encoding="utf-8")
    async with get_db_session() as session:
        assert await session.get(TelegramDraftSession, user_id) is None


@pytest.mark.asyncio
async def test_bulk_completion_escapes_stale_note_draft(tmp_path: Path, monkeypatch):
    """"Todos os boletos estão pagos" mid-draft must break out and bulk-tick, not be
    absorbed back into the note draft as a short reply."""
    user_id = 702
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)

    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)
    cockpit_file = _write_cockpit_with_gas(vault, tmp_path)

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))

    async with get_db_session() as session:
        await session.merge(TelegramDraftSession(
            user_id=user_id, status="drafting", category="today",
            title="Boleto", history_json="[]",
        ))
        await session.commit()

    await handle_message({
        "message_id": 72,
        "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "Todos os boletos e faturas estão pagos",
    })

    assert "concluído" in send_mock.call_args[1]["text"]
    assert "- [x] **💳 Pagar boleto: Gasnorte**" in cockpit_file.read_text(encoding="utf-8")
    assert "- [ ] **Revisão orçamentária**" in cockpit_file.read_text(encoding="utf-8")
    async with get_db_session() as session:
        assert await session.get(TelegramDraftSession, user_id) is None


@pytest.mark.asyncio
async def test_natural_query_cockpit_pendencias_and_greeting(tmp_path: Path, monkeypatch):
    user_id = 600
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)

    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)

    from datetime import date
    today_str = date.today().strftime("%Y-%m-%d")
    cockpit_file = tmp_path / "00_Cockpit" / "Daily" / f"{today_str}.md"
    cockpit_content = (
        "---\ntitle: Daily Cockpit\n---\n"
        "# Cockpit Diário\n\n"
        "## 🎯 Focos de Alta Prioridade do Dia\n"
        "- [ ] **Contrato Acervo** — Assinar com comitê\n\n"
        "## 📅 Agenda & Reuniões de Hoje\n"
        "### ⏰ 10:00 - 11:00: [[04_Meetings/reuniao.md|Alinhamento Estratégico]]\n\n"
        "## 📬 E-mails Críticos & Ações Necessárias\n"
        "- [ ] **De:** Financeiro | **Ação Sugerida:** Pagar boleto Luzsul\n"
    )
    vault.write_file_atomic(cockpit_file, cockpit_content)

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)

    # 1. Greeting test: "Olá"
    msg_greet = {
        "message_id": 61,
        "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "Olá",
    }
    await handle_message(msg_greet)

    assert send_mock.called
    greet_text = send_mock.call_args[1]["text"]
    assert "Olá, Rafael" in greet_text

    # No draft should be created in DB from greeting
    async with get_db_session() as session:
        assert await session.get(TelegramDraftSession, user_id) is None

    # 2. Pendências query test: "quais as pendencias de hoje?"
    msg_query = {
        "message_id": 62,
        "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "quais as pendencias de hoje?",
    }
    await handle_message(msg_query)

    assert send_mock.called
    summary_text = send_mock.call_args[1]["text"]
    assert "Cockpit Diário" in summary_text
    assert "Contrato Acervo" in summary_text
    assert "Alinhamento Estratégico" in summary_text

    # Still no draft session should be created
    async with get_db_session() as session:
        assert await session.get(TelegramDraftSession, user_id) is None


# --- Draft session lifecycle (rework: intent detection & routing) -------------

def _force_intent(monkeypatch, intent):
    from rysos.ai.classifier import IntentResult
    monkeypatch.setattr(
        "rysos.telegram.handlers.adaptive_classifier.classify",
        AsyncMock(return_value=IntentResult(intent=intent, confidence=0.95, reasoning="forced")),
    )


async def _seed_draft(user_id, *, status="drafting", category="today", title="Nota Executiva",
                      history=None, updated_at=None):
    async with get_db_session() as session:
        existing = await session.get(TelegramDraftSession, user_id)
        if existing:
            await session.delete(existing)
            await session.flush()
        row = TelegramDraftSession(
            user_id=user_id, status=status, category=category, title=title,
            extracted_fields_json="{}", missing_fields_json='["title", "area", "description"]',
            history_json=json.dumps(history or []),
        )
        session.add(row)
        await session.flush()
        if updated_at is not None:
            row.updated_at = updated_at
        await session.commit()


@pytest.mark.asyncio
async def test_mid_draft_query_breaks_out_and_drops_draft(tmp_path: Path, monkeypatch):
    from rysos.ai.classifier import IntentType
    user_id = 710
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)

    from datetime import date
    today_str = date.today().strftime("%Y-%m-%d")
    cockpit_file = tmp_path / "00_Cockpit" / "Daily" / f"{today_str}.md"
    vault.write_file_atomic(cockpit_file, (
        "---\ntitle: Daily Cockpit\n---\n# Cockpit Diário\n\n"
        "## 🎯 Focos de Alta Prioridade do Dia\n- [ ] **Contrato Acervo** — Assinar\n\n"
        "## 📅 Agenda & Reuniões de Hoje\n*Nada.*\n"
    ))

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))
    note_mock = MagicMock()
    monkeypatch.setattr("rysos.telegram.handlers.gemini_ai.process_conversational_note", note_mock)
    _force_intent(monkeypatch, IntentType.QUERY_COCKPIT)

    await _seed_draft(user_id, status="drafting", category="today")

    await handle_message({
        "message_id": 80, "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id}, "text": "e o que temos pra hoje afinal, me diz tudo agora",
    })

    assert "Cockpit Diário" in send_mock.call_args[1]["text"]
    note_mock.assert_not_called()  # never entered the draft loop
    async with get_db_session() as session:
        assert await session.get(TelegramDraftSession, user_id) is None  # draft dropped


@pytest.mark.asyncio
async def test_stale_drafting_session_is_expired(tmp_path: Path, monkeypatch):
    from datetime import datetime, timezone, timedelta, date
    from rysos.ai.classifier import IntentType
    user_id = 711
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)
    today_str = date.today().strftime("%Y-%m-%d")
    vault.write_file_atomic(tmp_path / "00_Cockpit" / "Daily" / f"{today_str}.md", (
        "# Cockpit Diário\n\n## 🎯 Focos de Alta Prioridade do Dia\n- [ ] **Algo** — x\n\n"
        "## 📅 Agenda & Reuniões de Hoje\n*Nada.*\n"
    ))

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))
    note_mock = MagicMock()
    monkeypatch.setattr("rysos.telegram.handlers.gemini_ai.process_conversational_note", note_mock)
    _force_intent(monkeypatch, IntentType.QUERY_COCKPIT)

    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=25)
    await _seed_draft(user_id, status="drafting", updated_at=stale)

    # "sexta" alone would normally be treated as a reply to the draft's pending question
    await handle_message({
        "message_id": 81, "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id}, "text": "sexta",
    })

    note_mock.assert_not_called()               # stale draft did not capture the message
    async with get_db_session() as session:
        assert await session.get(TelegramDraftSession, user_id) is None


@pytest.mark.asyncio
async def test_low_signal_first_message_creates_no_draft(tmp_path: Path, monkeypatch):
    from rysos.ai.classifier import IntentType
    user_id = 712
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))
    monkeypatch.setattr("rysos.telegram.handlers.gemini_ai.process_conversational_note", lambda **kw: {
        "category": "idea", "is_complete": False, "title": "Nota Executiva", "content": "",
        "extracted_fields": {}, "missing_fields": ["overview"],
        "conversational_reply": "Me conta um pouco mais sobre o que quer registrar.",
    })
    _force_intent(monkeypatch, IntentType.CAPTURE_NOTE)

    async with get_db_session() as session:
        d = await session.get(TelegramDraftSession, user_id)
        if d:
            await session.delete(d)
            await session.commit()

    await handle_message({
        "message_id": 82, "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id}, "text": "teste",
    })

    assert send_mock.called
    async with get_db_session() as session:
        assert await session.get(TelegramDraftSession, user_id) is None  # no sticky draft


@pytest.mark.asyncio
async def test_draft_turn_cap_asks_directly(tmp_path: Path, monkeypatch):
    from rysos.ai.classifier import IntentType
    user_id = 713
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)
    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))
    monkeypatch.setattr("rysos.telegram.handlers.gemini_ai.process_conversational_note", lambda **kw: {
        "category": "today", "is_complete": False, "title": "Nota Executiva", "content": "",
        "extracted_fields": {}, "missing_fields": ["title", "area"],
        "conversational_reply": "Qual a ação e a área?",
    })

    # 3 user turns already in history; the 4th short reply trips the cap
    await _seed_draft(user_id, status="drafting", category="today", history=[
        {"role": "user", "text": "algo"}, {"role": "assistant", "text": "qual dia?"},
        {"role": "user", "text": "hoje"}, {"role": "assistant", "text": "qual acao?"},
        {"role": "user", "text": "sei la"}, {"role": "assistant", "text": "qual area?"},
    ])

    await handle_message({
        "message_id": 83, "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id}, "text": "sei la mesmo",
    })

    last = send_mock.call_args[1]["text"].lower()
    assert "cockpit" in last and "nota" in last          # the direct disambiguation question
    async with get_db_session() as session:
        row = await session.get(TelegramDraftSession, user_id)
        assert row is not None and row.status == "disambiguating"


@pytest.mark.asyncio
async def test_multi_item_message_queues_and_confirms_each_note_in_turn(tmp_path: Path, monkeypatch):
    """2026-09-16 incident: a ';'-delimited multi-fact report collapsed into one fused
    note. Each ';'-item must become its own draft, confirmed and saved independently."""
    user_id = 714
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)

    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "rysos.telegram.handlers.gemini_ai.process_conversational_note",
        lambda **kw: {
            "category": "idea", "is_complete": True, "title": kw["user_input"],
            "content": f"# {kw['user_input']}", "summary": kw["user_input"],
            "extracted_fields": {"title": kw["user_input"]}, "missing_fields": [],
            "conversational_reply": "Perfeito!",
        },
    )

    await handle_message({
        "message_id": 90, "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "boa ideia numero um de expansao; boa ideia numero dois de expansao",
    })

    async with get_db_session() as session:
        draft = await session.get(TelegramDraftSession, user_id)
        assert draft is not None
        assert draft.status == "awaiting_confirmation"
        assert draft.title == "boa ideia numero um de expansao"
        assert json.loads(draft.pending_segments_json) == ["boa ideia numero dois de expansao"]

    # Confirm item 1: it saves, then the queue auto-advances into item 2's own draft.
    await handle_message({
        "message_id": 91, "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id}, "text": "sim",
    })

    ideas_dir = tmp_path / "06_Resources" / "Ideias_e_Criatividade"
    assert len(list(ideas_dir.glob("*.md"))) == 1

    async with get_db_session() as session:
        draft = await session.get(TelegramDraftSession, user_id)
        assert draft is not None
        assert draft.status == "awaiting_confirmation"
        assert draft.title == "boa ideia numero dois de expansao"
        assert json.loads(draft.pending_segments_json) == []

    # Confirm item 2: it saves too, and the queue is now empty.
    await handle_message({
        "message_id": 92, "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id}, "text": "sim",
    })

    assert len(list(ideas_dir.glob("*.md"))) == 2
    async with get_db_session() as session:
        assert await session.get(TelegramDraftSession, user_id) is None


@pytest.mark.asyncio
async def test_multi_item_completion_segment_advances_queue_without_a_draft(tmp_path: Path, monkeypatch):
    """A COMPLETE_FOCUS-shaped segment (e.g. 'paguei a Nimbus Cloud') must never spawn a note
    draft, and must not swallow the rest of the batch behind it."""
    user_id = 715
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)

    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "rysos.telegram.handlers.gemini_ai.process_conversational_note",
        lambda **kw: {
            "category": "idea", "is_complete": True, "title": kw["user_input"],
            "content": f"# {kw['user_input']}", "summary": kw["user_input"],
            "extracted_fields": {"title": kw["user_input"]}, "missing_fields": [],
            "conversational_reply": "Perfeito!",
        },
    )

    await handle_message({
        "message_id": 93, "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "boleto da Luzsul pago hoje; ideia nova de expansao internacional",
    })

    # No draft was ever created for the completion segment itself.
    async with get_db_session() as session:
        draft = await session.get(TelegramDraftSession, user_id)
        assert draft is not None
        assert draft.category == "idea"
        assert draft.title == "ideia nova de expansao internacional"
        assert draft.status == "awaiting_confirmation"
        assert json.loads(draft.pending_segments_json) == []


@pytest.mark.asyncio
async def test_multi_item_weak_segment_is_skipped_not_stalled(tmp_path: Path, monkeypatch):
    """A queued segment too vague to extract anything from (is_complete=False, no seed)
    must be skipped and the batch continued — there's no next user turn to answer a
    'me conta mais' follow-up mid-batch, so waiting for one would strand the rest."""
    user_id = 716
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)

    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))

    def fake_process(**kw):
        if "fraco" in kw["user_input"]:
            return {
                "category": "idea", "is_complete": False, "title": "", "content": "",
                "extracted_fields": {}, "missing_fields": ["title"],
                "conversational_reply": "Me conta mais sobre isso.",
            }
        return {
            "category": "idea", "is_complete": True, "title": kw["user_input"],
            "content": f"# {kw['user_input']}", "summary": kw["user_input"],
            "extracted_fields": {"title": kw["user_input"]}, "missing_fields": [],
            "conversational_reply": "Perfeito!",
        }
    monkeypatch.setattr("rysos.telegram.handlers.gemini_ai.process_conversational_note", fake_process)

    await handle_message({
        "message_id": 94, "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "item fraco sem conteudo nenhum; ideia solida de expansao regional",
    })

    assert any("ignorado" in c.kwargs.get("text", "") for c in send_mock.call_args_list)

    async with get_db_session() as session:
        draft = await session.get(TelegramDraftSession, user_id)
        assert draft is not None
        assert draft.title == "ideia solida de expansao regional"
        assert draft.status == "awaiting_confirmation"
        assert json.loads(draft.pending_segments_json) == []


@pytest.mark.asyncio
async def test_batch_abandoned_by_unrelated_completion_reply_still_advances_queue(tmp_path: Path, monkeypatch):
    """2026-09-16 production incident: mid-batch, a reply to the note follow-up question
    also declared an unrelated Cockpit completion ("...esta pendência já foi CONCLUIDA
    hoje"). That reply doesn't match any real Cockpit focus, so it falls through the
    strict-completion escape hatch, gets classified as COMPLETE_FOCUS at the top level,
    and step 8 abandons the in-progress project draft — which silently dropped all 6
    still-queued items along with it, because the generic abandon path didn't know about
    pending_segments_json. This must now advance into the next queued item instead."""
    from rysos.ai.classifier import IntentType
    user_id = 718
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)

    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "rysos.telegram.handlers.gemini_ai.process_conversational_note",
        lambda **kw: {
            "category": "idea", "is_complete": True, "title": kw["user_input"],
            "content": f"# {kw['user_input']}", "summary": kw["user_input"],
            "extracted_fields": {"title": kw["user_input"]}, "missing_fields": [],
            "conversational_reply": "Perfeito!",
        },
    )
    # Only the actual completion-shaped reply must classify as COMPLETE_FOCUS — the
    # queued segments it must NOT swallow should still classify as ordinary captures.
    from rysos.ai.classifier import IntentResult

    async def fake_classify(text, **kw):
        if "CONCLUIDA" in text:
            return IntentResult(intent=IntentType.COMPLETE_FOCUS, confidence=0.95, reasoning="forced")
        return IntentResult(intent=IntentType.CAPTURE_NOTE, confidence=0.95, reasoning="forced")
    monkeypatch.setattr("rysos.telegram.handlers.adaptive_classifier.classify", fake_classify)

    # An in-progress project draft still mid-conversation, holding 2 queued items.
    async with get_db_session() as session:
        row = TelegramDraftSession(
            user_id=user_id, status="drafting", category="project", title="Nota Executiva",
            extracted_fields_json="{}", missing_fields_json='["deadline"]', history_json="[]",
            pending_segments_json=json.dumps([
                "fiz o pagamento da divida da Meridian junto a Nimbus Cloud",
                "ideia nova de expansao regional",
            ]),
        )
        session.add(row)
        await session.commit()

    await handle_message({
        "message_id": 97, "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "o prazo é hoje. esta pendência já foi CONCLUIDA hoje",
    })

    # The abandoned project draft is gone, and the queue moved on to its first item
    # instead of being wiped along with it.
    async with get_db_session() as session:
        draft = await session.get(TelegramDraftSession, user_id)
        assert draft is not None
        assert draft.title == "fiz o pagamento da divida da Meridian junto a Nimbus Cloud"
        assert json.loads(draft.pending_segments_json) == ["ideia nova de expansao regional"]


@pytest.mark.asyncio
async def test_unresolved_streak_disambiguation_advances_queue_instead_of_asking(tmp_path: Path, monkeypatch):
    """After a run of unresolved captures, handle_message stops guessing and asks
    directly — but if the currently-idle draft (awaiting_confirmation) is still holding
    a queued multi-item batch, asking (which wipes the draft) must not swallow it."""
    user_id = 719
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)

    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)

    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "rysos.telegram.handlers.gemini_ai.process_conversational_note",
        lambda **kw: {
            "category": "idea", "is_complete": True, "title": kw["user_input"],
            "content": f"# {kw['user_input']}", "summary": kw["user_input"],
            "extracted_fields": {"title": kw["user_input"]}, "missing_fields": [],
            "conversational_reply": "Perfeito!",
        },
    )
    # The queued segment's own classify() call must not hinge on how a live model
    # reads this placeholder phrase out of context — pin it to CAPTURE_NOTE so the
    # test stays about queue continuation, not intent classification.
    from rysos.ai.classifier import IntentResult, IntentType
    monkeypatch.setattr(
        "rysos.telegram.handlers.adaptive_classifier.classify",
        AsyncMock(return_value=IntentResult(intent=IntentType.CAPTURE_NOTE, confidence=0.95, reasoning="forced")),
    )

    # 3 trailing unconfirmed CAPTURE_NOTE turns trips ASK_AFTER_ROUNDS.
    async with get_db_session() as session:
        for _ in range(3):
            session.add(AdaptiveFeedbackLog(
                user_id=user_id, raw_input="algo confuso", predicted_intent="CAPTURE_NOTE",
                feedback_type="auto",
            ))
        session.add(TelegramDraftSession(
            user_id=user_id, status="awaiting_confirmation", category="idea",
            title="Ideia Solta", extracted_fields_json="{}", missing_fields_json="[]",
            history_json="[]",
            pending_segments_json=json.dumps(["ideia seguinte da fila"]),
        ))
        await session.commit()

    await handle_message({
        "message_id": 98, "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id}, "text": "mais uma coisa confusa",
    })

    # No stray "disambiguating" placeholder — the queue took over instead.
    async with get_db_session() as session:
        draft = await session.get(TelegramDraftSession, user_id)
        assert draft is not None
        assert draft.status == "awaiting_confirmation"
        assert draft.title == "ideia seguinte da fila"
        assert json.loads(draft.pending_segments_json) == []


@pytest.mark.asyncio
async def test_multi_item_cancel_via_callback_advances_queue(tmp_path: Path, monkeypatch):
    user_id = 717
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [user_id])
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", tmp_path)

    vault = VaultManager(vault_path=tmp_path)
    vault.initialize_vault_structure()
    monkeypatch.setattr("rysos.telegram.handlers.VaultManager", lambda: vault)

    send_mock = AsyncMock(return_value={"ok": True})
    ans_mock = AsyncMock(return_value=True)
    edit_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_message", send_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.send_chat_action", AsyncMock(return_value=True))
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.answer_callback_query", ans_mock)
    monkeypatch.setattr("rysos.telegram.handlers.telegram_bot.edit_message_text", edit_mock)
    monkeypatch.setattr(
        "rysos.telegram.handlers.gemini_ai.process_conversational_note",
        lambda **kw: {
            "category": "idea", "is_complete": True, "title": kw["user_input"],
            "content": f"# {kw['user_input']}", "summary": kw["user_input"],
            "extracted_fields": {"title": kw["user_input"]}, "missing_fields": [],
            "conversational_reply": "Perfeito!",
        },
    )

    await handle_message({
        "message_id": 95, "from": {"id": user_id, "first_name": "Rafael"},
        "chat": {"id": user_id},
        "text": "ideia descartavel numero um; ideia que fica numero dois",
    })

    cb = {
        "id": "cb_cancel", "data": f"cap:cancel:{user_id}",
        "message": {"chat": {"id": user_id}, "message_id": 96},
        "from": {"id": user_id},
    }
    await handle_callback_query(cb)

    ideas_dir = tmp_path / "06_Resources" / "Ideias_e_Criatividade"
    assert len(list(ideas_dir.glob("*.md"))) == 0  # the cancelled item was never saved

    async with get_db_session() as session:
        draft = await session.get(TelegramDraftSession, user_id)
        assert draft is not None
        assert draft.title == "ideia que fica numero dois"
        assert draft.status == "awaiting_confirmation"
