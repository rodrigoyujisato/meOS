"""Executive Telegram message & callback handlers for rysOS."""

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo
from sqlalchemy import delete, select

from rysos.config import settings
from rysos.identity import account_email as owner_account_email, user_first_name, user_name
from rysos.vault.manager import VaultManager
from rysos.ai.gemini import gemini_ai
from rysos.ai.classifier import (
    adaptive_classifier,
    IntentType,
    FOCUS_COMPLETION_MARKERS,
    looks_interrogative,
    looks_like_cancel_or_reschedule,
    normalize_text,
)
from rysos.ai.learning import adaptive_kb
from rysos.ai.segmenter import segment_capture_message
from rysos.connectors.calendar import CalendarConnector
from rysos.db import get_db_session, DecisionRecord, DedupSuggestion, TelegramDraftSession
from rysos.telegram.bot import telegram_bot

logger = logging.getLogger("rysos.telegram.handlers")

# Draft session lifecycle guards (see plan wise-snacking-popcorn).
DRAFT_TTL_MIN = 15          # a 'drafting'/'disambiguating' row older than this is abandoned
MAX_DRAFT_TURNS = 4         # dead-end drafting turns before the bot asks directly
ESCALATE_AFTER_ROUNDS = 2   # trailing unresolved CAPTURE_NOTE rounds -> heavier classify pass
ASK_AFTER_ROUNDS = 3        # ... -> stop guessing, ask the owner what he wants

# An event confirmation must expire (unlike a note's 'awaiting_confirmation', which never
# does): "amanhã 15h" confirmed two days later would create the event on the wrong day.
EVENT_DRAFT_TTL_MIN = 15

CANCELLATION_KEYWORDS = {
    "cancela", "cancelar", "esquece", "desistir", "/cancelar",
    "cancela isso", "esquece isso", "desisto", "descartar", "cancel"
}

CONFIRMATION_KEYWORDS = {
    "sim", "confirmo", "confirmar", "gravar", "salvar",
    "pode gravar", "pode salvar", "ok", "perfeito", "grava", "salva"
}

CATEGORY_LABELS = {
    "idea": "💡 Ideia",
    "project": "🚀 Projeto",
    "decision": "⚖️ Decisão",
    "today": "🎯 Foco Hoje",
    "person": "👤 Pessoa",
    "framework": "📐 Framework",
}

# Deterministic commands surfaced in Telegram's native "/" menu (setMyCommands).
# Each maps to a fixed handler in the command-routing block below — no AI in the loop.
# Command names: lowercase latin/digits/underscore only, <=32 chars; description <=256.
NATIVE_BOT_COMMANDS = [
    {"command": "cockpit", "description": "Resumo executivo do dia de hoje"},
    {"command": "agenda", "description": "Compromissos e reuniões de hoje"},
    {"command": "decisoes", "description": "Decisões executivas em aberto"},
    {"command": "triagem", "description": "Cards de triagem das reuniões com itens pendentes"},
    {"command": "recap", "description": "Balanço de encerramento do dia"},
    {"command": "sync", "description": "Sincroniza agenda e e-mail, regenera o cockpit"},
    {"command": "dedup", "description": "Varredura de duplicidades e links quebrados no vault"},
    {"command": "heartbeat", "description": "Consumo e custo de tokens de IA (hoje / 7 dias / total)"},
    {"command": "cancelar", "description": "Descarta a nota ou rascunho em andamento"},
    {"command": "ajuda", "description": "Como conversar com o rysOS"},
]


async def register_bot_commands() -> bool:
    """Pushes NATIVE_BOT_COMMANDS to Telegram so they appear in the client's menu."""
    ok = await telegram_bot.set_my_commands(NATIVE_BOT_COMMANDS)
    logger.info(f"[Telegram] native command menu {'registrado' if ok else 'NÃO registrado'}")
    return ok


@asynccontextmanager
async def _typing_indicator(chat_id: int | str, action: str = "typing"):
    """Keeps Telegram's "digitando..." indicator alive for the duration of a slow call.

    Telegram drops the chat action after ~5s, so a single `send_chat_action` before a
    Gemini call (which can easily take longer) leaves the chat looking idle mid-response —
    exactly the "travou, silenciou ou está processando?" confusion reported in the
    2026-09-16 Telegram thread. Resending every 4s keeps it lit until the `with` block
    exits, success or failure.
    """
    stop = asyncio.Event()

    async def _pulse():
        while not stop.is_set():
            await telegram_bot.send_chat_action(chat_id, action)
            try:
                await asyncio.wait_for(stop.wait(), timeout=4)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(_pulse())
    try:
        yield
    finally:
        stop.set()
        try:
            await task
        except Exception:
            pass


def is_authorized(user_id: int) -> bool:
    """Validates if Telegram user ID is whitelisted. An empty whitelist authorizes nobody:
    the bot reads mail, calendar and notes, so it must never be open by default."""
    return user_id in settings.TELEGRAM_ALLOWED_USER_IDS


_CATEGORY_CHANGE_RE = re.compile(
    r"\b(muda|mudar|troca|trocar|na verdade (e|eh|é)|era pra ser|corrig|"
    r"classifica como|categoriza como|isso e (uma|um))\b",
    re.IGNORECASE,
)


def _asks_category_change(text: str) -> bool:
    return bool(_CATEGORY_CHANGE_RE.search(text or ""))


def _meaningful_fields(fields: dict[str, Any]) -> bool:
    """True if the AI extracted something to hold on to beyond bookkeeping keys."""
    for key, value in (fields or {}).items():
        if key in ("date", "area", "category"):
            continue
        if isinstance(value, (list, tuple, dict)):
            if value:
                return True
        elif str(value or "").strip():
            return True
    return False


def _is_probable_draft_reply(text: str) -> bool:
    """A short free-text answer to the bot's own follow-up question (not a new request)."""
    t = (text or "").strip()
    if not t or looks_interrogative(t):
        return False
    low = t.lower()
    if low in CANCELLATION_KEYWORDS or low in CONFIRMATION_KEYWORDS:
        return False
    if adaptive_classifier.keyword_query_intent(t) is not None:
        return False
    return len(t.split()) <= 8


def _count_trailing_unresolved(recent_turns: list[dict[str, Any]]) -> int:
    """How many of the most recent turns were CAPTURE_NOTE that never got confirmed."""
    n = 0
    for turn in reversed(recent_turns or []):
        if turn.get("intent") == "CAPTURE_NOTE" and turn.get("feedback") == "auto":
            n += 1
        else:
            break
    return n


async def _delete_draft(user_id: int) -> Optional[str]:
    """Deletes the active draft row and returns its pending_segments_json (if any) so the
    caller can continue a multi-item batch instead of silently dropping the rest of it —
    abandoning the note being drafted must never abandon the whole queued report with it
    (2026-09-16 incident: a focus-completion aside mid-batch dropped 6 queued items)."""
    async with get_db_session() as session:
        row = await session.get(TelegramDraftSession, user_id)
        if row:
            pending_json = row.pending_segments_json
            await session.delete(row)
            await session.commit()
            return pending_json
    return None


async def _load_active_draft(user_id: int) -> Optional[TelegramDraftSession]:
    """Loads the user's draft, first expiring a stale 'drafting'/'disambiguating' row.

    An `awaiting_confirmation` draft (a note ready to save) never expires here.
    """
    async with get_db_session() as session:
        row = await session.get(TelegramDraftSession, user_id)
        if not row:
            return None
        ttl = (
            EVENT_DRAFT_TTL_MIN
            if row.status in ("awaiting_event_confirmation", "awaiting_account_choice")
            else DRAFT_TTL_MIN
        )
        if row.status in (
            "drafting", "disambiguating", "awaiting_event_confirmation",
            "awaiting_account_choice", "awaiting_dedup_reply",
        ):
            updated = row.updated_at
            if updated is not None and updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            expired = updated is None or (datetime.now(timezone.utc) - updated) > timedelta(minutes=ttl)
            if expired:
                await session.delete(row)
                await session.commit()
                logger.info(f"[Draft] expired stale {row.status} draft for user={user_id}")
                return None
        # force-load the columns callers read, then detach
        _ = (row.status, row.category, row.title, row.history_json, row.pending_segments_json)
        session.expunge(row)
        return row


async def sweep_stale_drafts() -> int:
    """Startup housekeeping: drop every stale 'drafting'/'disambiguating' row."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=DRAFT_TTL_MIN)
    removed = 0
    event_cutoff = datetime.now(timezone.utc) - timedelta(minutes=EVENT_DRAFT_TTL_MIN)
    async with get_db_session() as session:
        stmt = select(TelegramDraftSession).where(
            TelegramDraftSession.status.in_([
                "drafting", "disambiguating", "awaiting_event_confirmation",
                "awaiting_account_choice", "awaiting_dedup_reply",
            ])
        )
        for row in (await session.execute(stmt)).scalars().all():
            updated = row.updated_at
            if updated is not None and updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            row_cutoff = (
                event_cutoff
                if row.status in ("awaiting_event_confirmation", "awaiting_account_choice")
                else cutoff
            )
            if updated is None or updated < row_cutoff:
                await session.delete(row)
                removed += 1
        if removed:
            await session.commit()
    if removed:
        logger.info(f"[Draft] startup sweep removed {removed} stale draft(s)")
    return removed


async def _ask_disambiguation(chat_id: int | str, user_id: int) -> Optional[str]:
    """Stop guessing after repeated misses: wipe any draft and ask the owner directly.

    The next message is routed by `_route_disambiguation_reply` before classification.
    Returns the wiped draft's pending_segments_json (if any) so a caller with a queued
    multi-item batch still in flight can continue it instead of losing it here.
    """
    pending_json = None
    async with get_db_session() as session:
        row = await session.get(TelegramDraftSession, user_id)
        if row:
            pending_json = row.pending_segments_json
            await session.delete(row)
            await session.flush()
        session.add(TelegramDraftSession(
            user_id=user_id,
            status="disambiguating",
            category="idea",
            extracted_fields_json="{}",
            missing_fields_json="[]",
            history_json="[]",
        ))
        await session.commit()
    await telegram_bot.send_message(
        chat_id=chat_id,
        text=(
            "Não peguei bem o que você quer. Me diz de forma direta: você quer ver o cockpit "
            "do dia, sua agenda, as decisões em aberto, ou registrar uma nota nova?"
        ),
    )
    return pending_json


async def _route_disambiguation_reply(chat_id: int | str, user_id: int, text: str) -> bool:
    """Deterministically routes the reply to the disambiguation question. Consumes the state."""
    pending_json = await _delete_draft(user_id)
    await _advance_capture_queue(chat_id, user_id, pending_json)
    n = normalize_text(text)
    mapped = adaptive_classifier.keyword_query_intent(text)
    if mapped == IntentType.QUERY_AGENDA or any(k in n for k in ("agenda", "reuniao", "compromiss")):
        await send_today_agenda(chat_id)
        return True
    if mapped == IntentType.QUERY_DECISIONS or "decis" in n:
        await send_decisions_summary(chat_id)
        return True
    if any(k in n for k in ("nota", "anota", "anotar", "registr", "ideia", "projeto", "lembr")):
        await process_conversational_turn(chat_id=chat_id, user_id=user_id, user_input=text)
        return True
    if mapped == IntentType.QUERY_COCKPIT or any(k in n for k in ("cockpit", "pendencia", "foco", "dia", "hoje")):
        await send_cockpit_summary(chat_id)
        return True
    return False


async def handle_message(message: dict[str, Any]) -> None:
    """Routes incoming Telegram messages supporting text, voice, photos, and documents."""
    from_user = message.get("from", {})
    user_id = from_user.get("id")
    chat_id = message.get("chat", {}).get("id")

    if not user_id or not chat_id:
        return

    # 1. Authorization check
    if not is_authorized(user_id):
        logger.warning(f"Acesso não autorizado bloqueado: user_id={user_id} (@{from_user.get('username')})")
        await telegram_bot.send_message(
            chat_id=chat_id,
            text=(f"⛔ <b>Acesso Restrito ao rysOS</b>\nEste bot é de uso exclusivo de {user_name()}.\n"
                  f"Seu ID do Telegram: <code>{user_id}</code> (para autorizar, adicione-o a TELEGRAM_ALLOWED_USER_IDS)."),
        )
        return

    if not settings.TELEGRAM_ALLOWED_USER_IDS:
        await telegram_bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ <b>Atenção de Segurança rysOS:</b>\n"
                 f"Seu Telegram ID é <code>{user_id}</code> e seu Chat ID é <code>{chat_id}</code>.\n"
                 f"Configure no seu arquivo <code>.env</code>:\n"
                 f"<code>TELEGRAM_ALLOWED_USER_IDS={user_id}</code>\n"
                 f"<code>TELEGRAM_CHAT_ID={chat_id}</code>",
        )

    # 2. Extract input from text, voice/audio, or photo
    input_text: Optional[str] = None
    media_prefix: str = ""

    # Voice / Audio input
    voice = message.get("voice") or message.get("audio")
    if voice:
        await telegram_bot.send_chat_action(chat_id, "record_voice")
        file_id = voice.get("file_id")
        file_info = await telegram_bot.get_file(file_id) if file_id else None
        if file_info and "file_path" in file_info:
            audio_bytes = await telegram_bot.download_file_bytes(file_info["file_path"])
            if audio_bytes:
                input_text = await asyncio.to_thread(gemini_ai.transcribe_audio, audio_bytes)
                media_prefix = "🎙️ "

        if not input_text:
            await telegram_bot.send_message(
                chat_id=chat_id,
                text="⚠️ Não consegui compreender o áudio. Poderia enviar novamente ou escrever em texto?",
            )
            return

    # Photo / Image input
    elif message.get("photo"):
        await telegram_bot.send_chat_action(chat_id, "upload_photo")
        photos = message["photo"]
        file_id = photos[-1].get("file_id") if photos else None
        caption = (message.get("caption") or "").strip()
        file_info = await telegram_bot.get_file(file_id) if file_id else None
        if file_info and "file_path" in file_info:
            photo_bytes = await telegram_bot.download_file_bytes(file_info["file_path"])
            if photo_bytes:
                extracted = await asyncio.to_thread(
                    gemini_ai.interpret_image, photo_bytes, user_prompt=caption
                )
                # Keep the user's own instruction words in input_text (not just Gemini's
                # extraction of the image) — the intent classifier reads input_text, and a
                # caption like "cria esse evento" carries the CREATE_EVENT cue that a neutral
                # fact-extraction of the image content alone would never produce.
                input_text = f"{caption}\n\n{extracted}".strip() if caption else extracted
                media_prefix = "📸 "

        if not input_text:
            await telegram_bot.send_message(
                chat_id=chat_id,
                text="⚠️ Não consegui processar a imagem. Poderia reenviar com melhor nitidez ou descrever por texto?",
            )
            return

    # Document / File input (PDF, etc.) — Gemini reads it natively, no separate parser.
    elif message.get("document"):
        await telegram_bot.send_chat_action(chat_id, "typing")
        doc = message["document"]
        file_id = doc.get("file_id")
        mime_type = doc.get("mime_type") or "application/octet-stream"
        caption = (message.get("caption") or "").strip()
        file_info = await telegram_bot.get_file(file_id) if file_id else None
        if file_info and "file_path" in file_info:
            doc_bytes = await telegram_bot.download_file_bytes(file_info["file_path"])
            if doc_bytes:
                if mime_type.startswith("image/"):
                    extracted = await asyncio.to_thread(
                        gemini_ai.interpret_image, doc_bytes, mime_type=mime_type, user_prompt=caption
                    )
                else:
                    extracted = await asyncio.to_thread(
                        gemini_ai.interpret_document, doc_bytes, mime_type, user_prompt=caption
                    )
                # Same reasoning as the photo branch above: keep the caption's literal words
                # in input_text so the classifier can see an explicit "cria esse evento"
                # instruction, not just a neutral extraction of the file's contents.
                input_text = f"{caption}\n\n{extracted}".strip() if caption else extracted
                media_prefix = "📄 "

        if not input_text:
            await telegram_bot.send_message(
                chat_id=chat_id,
                text="⚠️ Não consegui processar esse arquivo. Poderia enviar em outro formato ou descrever o conteúdo por texto?",
            )
            return

    # Text input
    else:
        raw_text = (message.get("text") or "").strip()
        if raw_text:
            input_text = raw_text

    if not input_text:
        return

    # 3. Cancellation check
    if input_text.lower().strip() in CANCELLATION_KEYWORDS:
        async with get_db_session() as session:
            stmt = select(TelegramDraftSession).where(TelegramDraftSession.user_id == user_id)
            active_draft = (await session.execute(stmt)).scalar_one_or_none()
            if active_draft:
                await session.delete(active_draft)
                await session.commit()
                await telegram_bot.send_message(
                    chat_id=chat_id,
                    text="❌ <b>Rascunho cancelado.</b> Nenhuma alteração foi salva no Obsidian.",
                )
            else:
                await telegram_bot.send_message(
                    chat_id=chat_id,
                    text="Nenhuma anotação em rascunho no momento.",
                )
        return

    # 3.5. Deterministic confirmation check (bypasses AI classification, mirrors cancellation above)
    if input_text.lower().strip() in CONFIRMATION_KEYWORDS:
        async with get_db_session() as session:
            stmt = select(TelegramDraftSession).where(TelegramDraftSession.user_id == user_id)
            active_draft = (await session.execute(stmt)).scalar_one_or_none()
        if active_draft and active_draft.status == "awaiting_confirmation":
            await process_conversational_turn(chat_id=chat_id, user_id=user_id, user_input=input_text)
            return

    # 4. Command routing
    if input_text.startswith("/"):
        cmd = input_text.split()[0].lower()
        if cmd in ["/start", "/ajuda", "/help"]:
            await send_help_message(chat_id, from_user.get("first_name", user_first_name()))
            return
        elif cmd in ["/cockpit", "/painel"]:
            await send_cockpit_summary(chat_id)
            return
        elif cmd in ["/hoje", "/agenda"]:
            await send_today_agenda(chat_id)
            return
        elif cmd in ["/decisoes", "/decisions"]:
            await send_decisions_summary(chat_id)
            return
        elif cmd in ["/triagem", "/triage"]:
            from rysos.telegram.entity_review import push_pending_reviews
            sent = await push_pending_reviews(chat_id)
            if sent == 0:
                await telegram_bot.send_message(
                    chat_id=chat_id, text="Nenhuma reunião com itens de triagem pendentes.")
            return
        elif cmd in ["/recap", "/fechamento"]:
            await send_evening_recap(chat_id)
            return
        elif cmd in ["/sync", "/sincronizar"]:
            await telegram_bot.send_message(chat_id=chat_id, text="⏳ <i>Sincronizando Workspace com Google Calendar e Gmail...</i>")
            from rysos.core import rysos_core
            await rysos_core.run_daily_sync()
            await send_cockpit_summary(chat_id)
            return
        elif cmd in ["/dedup"]:
            await send_dedup_report(chat_id, user_id)
            return
        elif cmd in ["/heartbeat"]:
            await send_heartbeat_report(chat_id)
            return

    # 4.5. Load any active draft (expiring a stale drafting/disambiguating row first).
    active_draft = await _load_active_draft(user_id)

    # 4.6. Follow-up to the bot's "cockpit, agenda ou nota?" question — routed deterministically.
    if active_draft is not None and active_draft.status == "disambiguating":
        if await _route_disambiguation_reply(chat_id, user_id, input_text):
            return
        active_draft = None  # helper consumed the state; treat as a fresh message

    # 4.65. Reply to a /dedup suggestion in flight — sim/não/ajuste, one at a time.
    if active_draft is not None and active_draft.status == "awaiting_dedup_reply":
        await _route_dedup_reply(chat_id, user_id, input_text, active_draft)
        return

    # 4.7. Mid-draft handling. A drafting session no longer swallows every message: only a
    # genuine short reply to the pending question stays in the loop; anything else is
    # classified and may break out (dropping the draft).
    if active_draft is not None and active_draft.status == "drafting":
        # (a) a completion report ("boleto da Gasnorte pago hoje") escapes the note loop.
        #     A bulk report ("todos os boletos estão pagos") escapes too: the strict probe
        #     can't confirm it item-by-item, so match the phrase shape directly — otherwise
        #     (b) would absorb this 6-word statement back into the note draft.
        if _looks_like_focus_completion(input_text):
            if _looks_like_bulk_completion(input_text):
                pending_json = await _delete_draft(user_id)
                await handle_focus_completion(chat_id, user_id, input_text)
                await _advance_capture_queue(chat_id, user_id, pending_json)
                return
            probe = VaultManager().complete_cockpit_focus(input_text, apply=False, strict=True)
            if probe.get("status") in ("completed", "already_done"):
                pending_json = await _delete_draft(user_id)
                await handle_focus_completion(chat_id, user_id, input_text)
                await _advance_capture_queue(chat_id, user_id, pending_json)
                return
        # (b) a short, non-interrogative answer that isn't itself a query -> continue drafting
        if _is_probable_draft_reply(input_text):
            await process_conversational_turn(chat_id=chat_id, user_id=user_id, user_input=input_text)
            return
        # (c) otherwise fall through to classification below.

    # 5. A message carrying the owner's own explicit multi-item divisor (';') is routed
    # per-segment, each independently classified — bypassing whole-message classification
    # entirely. Classifying the blob first would let one completion marker anywhere in it
    # ("paguei a Nimbus Cloud") swing the WHOLE message into COMPLETE_FOCUS and skip capture of
    # everything else (2026-09-16 incident: a multi-fact report became one fused note).
    # Skipped for anything that reads as a question or matches a deterministic QUERY_*/
    # CREATE_EVENT/GREETING/SYNC cue — those must reach classify()'s own guardrails
    # (a question must never become a note) rather than fall through to CAPTURE_NOTE by
    # default inside the per-segment router.
    if (
        active_draft is None
        and not looks_interrogative(input_text)
        and adaptive_classifier.keyword_query_intent(input_text) is None
    ):
        segments = segment_capture_message(input_text)
        if len(segments) > 1:
            await _route_capture_segment(chat_id, user_id, segments[0], media_prefix, segments[1:])
            return

    # 6. Escalation bookkeeping + semantic classification.
    recent_turns = await adaptive_kb.get_recent_turns(user_id, limit=8)
    unresolved = _count_trailing_unresolved(recent_turns)

    # A run of misclassified captures with no draft to show for it: stop guessing and ask.
    # (An in-progress draft is governed by MAX_DRAFT_TURNS inside the loop instead.)
    if unresolved >= ASK_AFTER_ROUNDS and (active_draft is None or active_draft.status != "drafting"):
        pending = json.loads(active_draft.pending_segments_json or "[]") if active_draft else []
        if pending:
            # Mid-batch: skip the disambiguation ask (there's no next user turn to answer
            # it) and move straight to the next item instead of a stray "disambiguating"
            # row that _advance_capture_queue would otherwise clobber right back out of.
            await _delete_draft(user_id)
            await _advance_capture_queue(chat_id, user_id, json.dumps(pending, ensure_ascii=False))
        else:
            await _ask_disambiguation(chat_id, user_id)
        return

    async with _typing_indicator(chat_id):
        classification = await adaptive_classifier.classify(
            input_text,
            user_id=user_id,
            recent_turns=recent_turns,
            escalate=unresolved >= ESCALATE_AFTER_ROUNDS,
        )
    intent = classification.intent
    logger.info(
        f"[AdaptiveClassifier] user={user_id} intent={intent.value} "
        f"conf={classification.confidence:.2f} escalate={unresolved >= ESCALATE_AFTER_ROUNDS} "
        f"reason={classification.reasoning}"
    )

    # 7. CAPTURE_NOTE is the only intent that keeps or creates a draft.
    if intent == IntentType.CAPTURE_NOTE:
        await process_conversational_turn(
            chat_id=chat_id,
            user_id=user_id,
            user_input=input_text,
            media_prefix=media_prefix,
            suggested_category=classification.suggested_category,
        )
        return

    if intent == IntentType.CREATE_EVENT:
        await handle_create_event(chat_id=chat_id, user_id=user_id, user_input=input_text)
        return

    # 8. Every other intent is a new request: an in-progress draft is abandoned. If it was
    # holding a queued multi-item batch, the batch itself must survive the abandonment —
    # only the specific note being drafted is dropped (2026-09-16 incident: a completion
    # aside mid-batch silently discarded the other 6 queued items along with the draft).
    if active_draft is not None and active_draft.status in ("drafting", "disambiguating"):
        pending_json = await _delete_draft(user_id)
        active_draft = None
        await _advance_capture_queue(chat_id, user_id, pending_json)

    # Route according to classified intent
    if intent == IntentType.GREETING:
        await telegram_bot.send_message(
            chat_id=chat_id,
            text="🧭 <i>[Saudação]</i>\n\n"
                 f"👋 <b>Olá, {user_first_name()}!</b>\n\n"
                 "Estou à disposição no rysOS. Você pode:\n"
                 "• Ver o <b>Cockpit</b> (<i>'quais as pendências de hoje?'</i> ou <code>/cockpit</code>)\n"
                 "• Ver sua <b>Agenda</b> (<i>'qual minha agenda?'</i> ou <code>/hoje</code>)\n"
                 "• Ver <b>Decisões</b> (<i>'quais decisões pendentes?'</i> ou <code>/decisoes</code>)\n"
                 "• Ver a <b>Triagem</b> de reuniões (<code>/triagem</code>)\n"
                 "• Ver o <b>Balanço do Dia</b> (<i>'fechamento de hoje'</i> ou <code>/recap</code>)\n"
                 "• <b>Capturar notas</b> enviando texto, áudio ou fotos para salvar no Obsidian.",
        )
        return

    elif intent == IntentType.QUERY_COCKPIT:
        await send_cockpit_summary(chat_id)
        await _notify_pending_draft(chat_id, user_id, active_draft)
        return

    elif intent == IntentType.QUERY_AGENDA:
        await send_today_agenda(chat_id)
        await _notify_pending_draft(chat_id, user_id, active_draft)
        return

    elif intent == IntentType.QUERY_DECISIONS:
        await send_decisions_summary(chat_id)
        await _notify_pending_draft(chat_id, user_id, active_draft)
        return

    elif intent == IntentType.QUERY_RECAP:
        await send_evening_recap(chat_id)
        return

    elif intent == IntentType.COMPLETE_FOCUS:
        await handle_focus_completion(chat_id, user_id, input_text)
        await _notify_pending_draft(chat_id, user_id, active_draft)
        return

    elif intent == IntentType.SYNC_WORKSPACE:
        await telegram_bot.send_message(
            chat_id=chat_id,
            text="🧭 <i>[Sincronização]</i> ⏳ <i>Sincronizando Workspace com Google Calendar e Gmail...</i>",
        )
        from rysos.core import rysos_core
        await rysos_core.run_daily_sync()
        await send_cockpit_summary(chat_id)
        return

    elif intent == IntentType.QUERY_GENERAL:
        vault = VaultManager()
        today_str = date.today().strftime("%Y-%m-%d")
        cockpit_file = vault.vault_path / "00_Cockpit" / "Daily" / f"{today_str}.md"
        context = vault.read_file(cockpit_file) if cockpit_file.exists() else ""
        async with _typing_indicator(chat_id):
            answer = await asyncio.to_thread(gemini_ai.chat_with_agent, input_text, context=context)
        await telegram_bot.send_message(
            chat_id=chat_id,
            text=f"🧠 <i>[Agente Executivo rysOS]</i>\n\n{answer}",
        )
        await _notify_pending_draft(chat_id, user_id, active_draft)
        return


async def _notify_pending_draft(chat_id: int | str, user_id: int, active_draft: Optional[TelegramDraftSession]) -> None:
    """Resurfaces a pending note or event confirmation after an unrelated query."""
    if not active_draft:
        return
    if active_draft.status == "awaiting_confirmation":
        await send_active_draft_notice(chat_id, user_id, active_draft)
    elif active_draft.status == "awaiting_event_confirmation":
        event_fields = json.loads(active_draft.extracted_fields_json or "{}")
        await send_active_event_draft_notice(chat_id, user_id, active_draft.title, event_fields)
    elif active_draft.status == "awaiting_account_choice":
        await send_account_choice_prompt(chat_id, user_id, active_draft.title)


async def send_active_draft_notice(chat_id: int | str, user_id: int, draft: TelegramDraftSession) -> None:
    """Displays an unobtrusive reminder that a draft note is pending confirmation."""
    title = draft.title or "Nota Executiva"
    cat_label = CATEGORY_LABELS.get(draft.category, f"📁 {draft.category.capitalize()}")
    keyboard = {
        "inline_keyboard": [
            [
                {"text": f"✅ Gravar '{title[:20]}...'", "callback_data": f"cap:save:{user_id}"},
                {"text": "❌ Descartar", "callback_data": f"cap:cancel:{user_id}"},
            ]
        ]
    }
    await telegram_bot.send_message(
        chat_id=chat_id,
        text=f"📌 <i>Lembrete: Você possui um rascunho pausado ({cat_label}: <b>{title}</b>).</i>",
        reply_markup=keyboard,
    )


def _account_hint_email(kind: str) -> str:
    """The owner's configured account of this type (USER_EMAILS), or '' when none is set."""
    return owner_account_email(kind)


def _hint_suffix(kind: str) -> str:
    email = _account_hint_email(kind)
    return f" ({email})" if email else ""


async def handle_create_event(chat_id: int | str, user_id: int, user_input: str) -> None:
    """Extracts a calendar event from natural language and asks for confirmation before creating it.

    Never guesses a missing date or time (see JAMAIS INVENTAR in classifier/prompts): asks
    directly instead. Confirmation is button-only (no free-text 'sim') to keep this isolated
    from the CAPTURE_NOTE text-confirmation path, which owns a different draft shape.
    """
    await telegram_bot.send_chat_action(chat_id, "typing")
    fields = await asyncio.to_thread(gemini_ai.extract_event_fields, user_input)

    if not fields.get("date") or not fields.get("time"):
        await telegram_bot.send_message(
            chat_id=chat_id,
            text=(
                f"Para marcar \"{fields.get('summary') or user_input.strip()}\" eu preciso da "
                "data e do horário. Pode me passar os dois?"
            ),
        )
        return

    try:
        start_dt = datetime.strptime(f"{fields['date']} {fields['time']}", "%Y-%m-%d %H:%M")
    except ValueError:
        await telegram_bot.send_message(
            chat_id=chat_id,
            text="Não consegui entender a data ou o horário. Pode me passar de novo, tipo 'amanhã às 15h'?",
        )
        return

    duration = int(fields.get("duration_minutes") or 60)
    end_dt = start_dt + timedelta(minutes=duration)

    draft_fields = {
        "summary": fields.get("summary"),
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "attendees": fields.get("attendees") or [],
        "location": fields.get("location"),
        "description": fields.get("description"),
        "account_email": None,
    }

    # The target Google Calendar account is always confirmed explicitly with the owner —
    # a model hint is never trusted to pick which real-world calendar gets written to.
    # NOTE: TelegramDraftSession has a one-row-per-user schema, so a CAPTURE_NOTE draft's
    # queued batch (pending_segments_json) has nowhere to live once this row is taken over
    # for an event draft — advancing it here would race the row we're about to insert and
    # corrupt the event flow. This is a real gap (a batch mid-flight loses its remaining
    # items if the owner asks to create an event before it finishes) but fixing it needs a
    # queue that isn't tied to the single active-draft row, not a patch here.
    async with get_db_session() as session:
        existing = await session.get(TelegramDraftSession, user_id)
        if existing:
            await session.delete(existing)
            await session.flush()
        session.add(TelegramDraftSession(
            user_id=user_id,
            status="awaiting_account_choice",
            category="event",
            title=fields.get("summary"),
            extracted_fields_json=json.dumps(draft_fields, ensure_ascii=False),
            missing_fields_json="[]",
            history_json="[]",
        ))
        await session.commit()

    await send_account_choice_prompt(chat_id, user_id, fields.get("summary"))


async def send_account_choice_prompt(chat_id: int | str, user_id: int, title: Optional[str]) -> None:
    """Asks which Google account the event should be created on."""
    keyboard = {
        "inline_keyboard": [
            [
                {"text": f"📱 Pessoal{_hint_suffix('pessoal')}", "callback_data": f"evt:accp:{user_id}"},
                {"text": f"💼 Corporativo{_hint_suffix('corporativo')}", "callback_data": f"evt:accc:{user_id}"},
            ]
        ]
    }
    await telegram_bot.send_message(
        chat_id=chat_id,
        text=f"📅 <b>{title or 'Novo evento'}</b>\n\nEm qual conta devo criar esse evento?",
        reply_markup=keyboard,
    )


async def send_active_event_draft_notice(
    chat_id: int | str, user_id: int, title: Optional[str], fields: dict[str, Any]
) -> None:
    """Shows the event confirmation card with a create/discard inline keyboard."""
    start_dt = datetime.fromisoformat(fields["start"])
    end_dt = datetime.fromisoformat(fields["end"])
    account_email = fields.get("account_email") or _account_hint_email("pessoal")
    lines = [
        f"📅 <b>{title or 'Novo evento'}</b>",
        f"🕒 {start_dt.strftime('%d/%m/%Y %H:%M')} – {end_dt.strftime('%H:%M')}",
        f"👤 Conta: {account_email}",
    ]
    if fields.get("location"):
        lines.append(f"📍 {fields['location']}")
    if fields.get("attendees"):
        lines.append(f"🧑‍🤝‍🧑 {', '.join(fields['attendees'])}")
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Criar evento", "callback_data": f"evt:create:{user_id}"},
                {"text": "❌ Descartar", "callback_data": f"evt:cancel:{user_id}"},
            ]
        ]
    }
    await telegram_bot.send_message(chat_id=chat_id, text="\n".join(lines), reply_markup=keyboard)


def _looks_like_focus_completion(text: str) -> bool:
    """True when a free-text message reads as reporting a finished item (not a new note).

    A cancellation or reschedule ("foi cancelado", "foi para 21/09") is a status change,
    not a completion — must never escape the draft loop into ticking a checkbox done.
    """
    if looks_like_cancel_or_reschedule(text):
        return False
    low = text.lower().strip()
    return any(marker in low for marker in FOCUS_COMPLETION_MARKERS)


# Bound at import (before any test monkeypatch of `VaultManager`) so the mid-draft escape
# hatch mirrors the exact quantifier + cue set that `complete_cockpit_focus` acts on.
_BULK_QUANTIFIERS = frozenset(VaultManager._BULK_QUANTIFIERS)
_BULK_COMPLETION_CUES = tuple(VaultManager._BULK_COMPLETION_CUES)


def _looks_like_bulk_completion(text: str) -> bool:
    """True for a whole-class completion report ("todos os boletos estão pagos").

    Mirrors the quantifier + completion-cue test in `VaultManager.complete_cockpit_focus`
    so the mid-draft escape hatch releases the same phrases the bulk path can act on.
    """
    n = normalize_text(text)
    return bool(set(n.split()) & _BULK_QUANTIFIERS) and any(
        cue in n for cue in _BULK_COMPLETION_CUES
    )


async def handle_focus_completion(chat_id: int | str, user_id: int, user_input: str) -> None:
    """Marks the best-matching focus in today's Daily Cockpit as done and replies in plain chat tone.

    Replies are hardcoded (never model-narrated) so the bot never claims a checkbox was
    ticked when it was not, and stays within the no-dashes / no-bold Telegram style.
    """
    vault = VaultManager()
    result = vault.complete_cockpit_focus(user_input)
    status = result.get("status")

    if status == "completed":
        text = f"Pronto, marquei como concluído no Cockpit de hoje: {result['item']}."
    elif status == "completed_bulk":
        items = list(result.get("items", []))
        future = result.get("future", {})
        rows = [f"• {i}" for i in items]
        for iso in sorted(future):
            y, m, d = iso.split("-")
            for lbl in future[iso]:
                rows.append(f"• {lbl} (lembrete de {d}/{m})")
        total = len(items) + sum(len(v) for v in future.values())
        listed = "\n".join(rows)
        noun = "item de pagamento" if total == 1 else "itens de pagamento"
        done = "concluído" if total == 1 else "concluídos"
        scope = ", incluindo os lembretes futuros" if future else " no Cockpit de hoje"
        text = f"Pronto, marquei {total} {noun} como {done}{scope}:\n{listed}"
    elif status == "already_done_bulk":
        text = "Os itens de pagamento do Cockpit já estavam todos marcados como concluídos."
    elif status == "already_done":
        text = f"Esse item já estava marcado como concluído no Cockpit de hoje: {result['item']}."
    elif status == "ambiguous":
        options = "\n".join(f"• {c}" for c in result.get("candidates", []))
        text = (
            "Tenho mais de um foco em aberto que parece com isso hoje. Qual deles você concluiu?\n"
            f"{options}"
        )
    elif status == "not_found":
        pending = result.get("pending", [])
        if pending:
            options = "\n".join(f"• {p}" for p in pending)
            text = (
                "Não encontrei esse item entre os focos em aberto do Cockpit de hoje. "
                f"Os que ainda estão pendentes são:\n{options}"
            )
        else:
            text = "Não encontrei esse item e não há focos em aberto no Cockpit de hoje."
    elif status == "no_pending":
        text = "Não há focos em aberto no Cockpit de hoje para marcar como concluído."
    else:  # no_cockpit
        text = (
            "O Cockpit de hoje ainda não foi gerado, então não há focos para marcar. "
            "Envie /sync para gerar agora."
        )

    await telegram_bot.send_message(chat_id=chat_id, text=text)


async def process_conversational_turn(
    chat_id: int | str,
    user_id: int,
    user_input: str,
    media_prefix: str = "",
    suggested_category: Optional[str] = None,
    pending_segments: Optional[list[str]] = None,
) -> None:
    """Executes conversational draft loop: state lookup, Gemini extraction, and confirmation/refinement."""
    await telegram_bot.send_chat_action(chat_id, "typing")
    async with get_db_session() as session:
        stmt = select(TelegramDraftSession).where(TelegramDraftSession.user_id == user_id)
        draft = (await session.execute(stmt)).scalar_one_or_none()

        # Check if awaiting confirmation and user confirmed via text
        if draft and draft.status == "awaiting_confirmation":
            if user_input.lower().strip() in CONFIRMATION_KEYWORDS:
                draft_fields = json.loads(draft.extracted_fields_json or "{}")
                vault = VaultManager()
                saved_path = vault.save_quick_capture(
                    category=draft.category,
                    title=draft.title or "Nota Executiva",
                    content=draft.content or "",
                    target_date=draft_fields.get("date"),
                    fields=draft_fields,
                )
                category = draft.category
                title = draft.title or "Nota Executiva"
                pending_json = draft.pending_segments_json
                await session.delete(draft)
                await session.commit()

                # Record positive reinforcement in adaptive memory
                await adaptive_kb.record_feedback(
                    user_id=user_id,
                    raw_input=title,
                    predicted_intent="CAPTURE_NOTE",
                    predicted_category=category,
                    feedback_type="confirmed",
                )

                cat_label = CATEGORY_LABELS.get(category, f"📁 {category.capitalize()}")
                await telegram_bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ <b>Nota gravada com sucesso no Obsidian!</b>\n\n"
                         f"📂 <b>Destino:</b> {cat_label} (<code>{category}</code>)\n"
                         f"📄 <b>Arquivo:</b> <code>{saved_path.name}</code>\n"
                         f"📌 <b>Título:</b> {title}",
                )
                await _advance_capture_queue(chat_id, user_id, pending_json)
                return

        # Prepare current draft and history for AI context
        current_draft_dict: Optional[dict[str, Any]] = None
        history_list: list[dict[str, str]] = []

        if draft:
            current_draft_dict = {
                "category": draft.category,
                "title": draft.title,
                "content": draft.content,
                "summary": draft.summary,
                "extracted_fields": json.loads(draft.extracted_fields_json or "{}"),
                "missing_fields": json.loads(draft.missing_fields_json or "[]"),
            }
            if draft.history_json:
                try:
                    history_list = json.loads(draft.history_json)
                except Exception:
                    history_list = []
        elif suggested_category:
            current_draft_dict = {"category": suggested_category}

        # Call Gemini low-temperature conversational extractor (off the event loop)
        async with _typing_indicator(chat_id):
            result = await asyncio.to_thread(
                gemini_ai.process_conversational_note,
                user_input=user_input,
                current_draft=current_draft_dict,
                history=history_list,
            )

        raw_category = result.get("category", "idea")
        is_complete = result.get("is_complete", False)
        title = result.get("title") or "Nota Executiva"
        content = result.get("content") or ""
        summary = result.get("summary") or ""
        extracted_fields = result.get("extracted_fields", {})
        missing_fields = result.get("missing_fields", [])
        conversational_reply = result.get("conversational_reply", "")

        # Pin the category once a draft has one — a later turn's guess must not silently
        # flip it (a mid-draft "sexta" once re-classified the whole note).
        if draft and draft.category and draft.category != raw_category and not _asks_category_change(user_input):
            category = draft.category
        else:
            category = raw_category

        if category == "today" and result.get("date"):
            extracted_fields["date"] = result["date"]

        # Turn cap: after too many dead-end drafting rounds, stop looping and ask directly.
        prior_user_turns = sum(1 for h in history_list if h.get("role") == "user")
        if draft and draft.status == "drafting" and not is_complete and prior_user_turns + 1 >= MAX_DRAFT_TURNS:
            pending = json.loads(draft.pending_segments_json or "[]")
            await session.delete(draft)
            await session.commit()
            if pending:
                # Mid-batch: there's no next user turn to answer a "cockpit, agenda ou
                # nota?" disambiguation, so skip it and move straight to the next item
                # instead of leaving a stray "disambiguating" row the queue would then
                # clobber back into "awaiting_confirmation".
                await telegram_bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ <i>Item ignorado (não ficou claro o que registrar): \"{user_input}\"</i>",
                )
                await _advance_capture_queue(chat_id, user_id, json.dumps(pending, ensure_ascii=False))
            else:
                await _ask_disambiguation(chat_id, user_id)
            return

        # Turn 1 with nothing concrete to hold on to: reply once, create no sticky draft.
        has_seed = bool(
            is_complete
            or content.strip()
            or (title.strip() and title.strip().lower() != "nota executiva")
            or _meaningful_fields(extracted_fields)
        )
        if not draft and not has_seed:
            await session.rollback()
            if pending_segments:
                # A queued item too vague to hold a draft on its own: skip it rather than
                # stall waiting for a reply that will never come (there's no user turn
                # left in this batch to answer a follow-up), and keep the rest moving.
                await telegram_bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ <i>Item ignorado (pouca informação): \"{user_input}\"</i>",
                )
                await _advance_capture_queue(chat_id, user_id, json.dumps(pending_segments, ensure_ascii=False))
            else:
                await telegram_bot.send_message(
                    chat_id=chat_id,
                    text=conversational_reply or "Me conta um pouco mais sobre o que você quer registrar.",
                )
            return

        # Update or create session. `pending_segments_json` stays NULL for an ordinary
        # (non-batch) capture — only a real batch item gets it set, even to "[]" once the
        # queue is down to its last item — so _advance_capture_queue can tell "never a
        # batch" (NULL, stay silent) from "batch just exhausted" (empty list, say so).
        if not draft:
            draft = TelegramDraftSession(user_id=user_id)
            draft.pending_segments_json = (
                json.dumps(pending_segments, ensure_ascii=False) if pending_segments is not None else None
            )
            session.add(draft)
        elif pending_segments is not None:
            draft.pending_segments_json = json.dumps(pending_segments, ensure_ascii=False)

        draft.category = category
        draft.title = title
        draft.content = content
        draft.summary = summary
        draft.extracted_fields_json = json.dumps(extracted_fields, ensure_ascii=False)
        draft.missing_fields_json = json.dumps(missing_fields, ensure_ascii=False)

        history_list.append({"role": "user", "text": f"{media_prefix}{user_input}".strip()})
        history_list.append({"role": "assistant", "text": conversational_reply})
        draft.history_json = json.dumps(history_list, ensure_ascii=False)

        if not is_complete:
            draft.status = "drafting"
            await session.commit()
            await telegram_bot.send_message(chat_id=chat_id, text=conversational_reply)
        else:
            draft.status = "awaiting_confirmation"
            await session.commit()

            cat_label = CATEGORY_LABELS.get(category, f"📁 {category.capitalize()}")

            similar_warning = ""
            if category == "person":
                similar = VaultManager().find_similar_people(title)
                if similar:
                    similar_list = ", ".join(similar)
                    similar_warning = (
                        f"\n⚠️ <b>Já existe pessoa cadastrada com nome parecido:</b> {similar_list}\n"
                        f"Confirme se não é a mesma pessoa antes de gravar.\n"
                    )

            preview_text = (
                f"📝 <b>Nota Pronta — {cat_label}</b>\n\n"
                f"📌 <b>Título:</b> {title}\n"
                f"💡 <b>Resumo:</b> {summary}\n"
                f"{similar_warning}\n"
                f"<i>{conversational_reply}</i>\n\n"
                f"Confirma a gravação no Obsidian ou deseja alterar algo?"
            )
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Gravar no Obsidian", "callback_data": f"cap:save:{user_id}"},
                        {"text": "❌ Cancelar Nota", "callback_data": f"cap:cancel:{user_id}"},
                    ]
                ]
            }
            await telegram_bot.send_message(chat_id=chat_id, text=preview_text, reply_markup=keyboard)


async def _route_capture_segment(
    chat_id: int | str,
    user_id: int,
    segment: str,
    media_prefix: str,
    pending_segments: list[str],
) -> None:
    """Classifies one segment of a multi-item capture and routes it — each clause gets
    its own intent, not just its own note. A genuine focus completion ("paguei a Nimbus Cloud")
    ticks the Cockpit checkbox instead of becoming a note; a question or other query
    intent gets answered instead of silently absorbed into a note draft (2026-09-16
    Telegram thread feedback: a doubt raised mid-batch got treated as a statement); a
    cancellation/reschedule is already guardrailed back to CAPTURE_NOTE inside classify()
    itself. Anything else falls through to the note draft, the safe default for a clause
    that reads as a fact."""
    async with _typing_indicator(chat_id):
        segment_classification = await adaptive_classifier.classify(segment, user_id=user_id)
    intent = segment_classification.intent
    logger.info(
        f"[AdaptiveClassifier][segment] user={user_id} intent={intent.value} "
        f"conf={segment_classification.confidence:.2f} reason={segment_classification.reasoning}"
    )
    pending_json = json.dumps(pending_segments, ensure_ascii=False)
    if intent == IntentType.COMPLETE_FOCUS:
        await handle_focus_completion(chat_id, user_id, segment)
        await _advance_capture_queue(chat_id, user_id, pending_json)
    elif intent == IntentType.QUERY_COCKPIT:
        await send_cockpit_summary(chat_id)
        await _advance_capture_queue(chat_id, user_id, pending_json)
    elif intent == IntentType.QUERY_AGENDA:
        await send_today_agenda(chat_id)
        await _advance_capture_queue(chat_id, user_id, pending_json)
    elif intent == IntentType.QUERY_DECISIONS:
        await send_decisions_summary(chat_id)
        await _advance_capture_queue(chat_id, user_id, pending_json)
    elif intent == IntentType.QUERY_RECAP:
        await send_evening_recap(chat_id)
        await _advance_capture_queue(chat_id, user_id, pending_json)
    elif intent == IntentType.QUERY_GENERAL:
        vault = VaultManager()
        today_str = date.today().strftime("%Y-%m-%d")
        cockpit_file = vault.vault_path / "00_Cockpit" / "Daily" / f"{today_str}.md"
        context = vault.read_file(cockpit_file) if cockpit_file.exists() else ""
        async with _typing_indicator(chat_id):
            answer = await asyncio.to_thread(gemini_ai.chat_with_agent, segment, context=context)
        await telegram_bot.send_message(chat_id=chat_id, text=f"🧠 <i>[Agente Executivo rysOS]</i>\n\n{answer}")
        await _advance_capture_queue(chat_id, user_id, pending_json)
    else:
        await process_conversational_turn(
            chat_id=chat_id,
            user_id=user_id,
            user_input=segment,
            media_prefix=media_prefix,
            suggested_category=segment_classification.suggested_category,
            pending_segments=pending_segments,
        )


async def _advance_capture_queue(chat_id: int | str, user_id: int, pending_segments_json: Optional[str]) -> None:
    """After a queued multi-item capture's note is saved, cancelled, or skipped, starts
    the next segment as its own draft. NULL means the message that started this draft
    was never multi-item (stay silent); "[]" means a real batch just ran out of items
    (say so — otherwise the flow just goes quiet with no sign it's done, see 2026-09-16
    Telegram thread feedback)."""
    if pending_segments_json is None:
        return
    pending = json.loads(pending_segments_json or "[]")
    if not pending:
        await telegram_bot.send_message(
            chat_id=chat_id,
            text="Terminei de processar os itens dessa mensagem.",
        )
        return
    next_item, rest = pending[0], pending[1:]
    await telegram_bot.send_message(
        chat_id=chat_id,
        text=f"📎 <i>Próximo item da mensagem (restam {len(pending)})...</i>",
    )
    await _route_capture_segment(chat_id, user_id, next_item, media_prefix="", pending_segments=rest)


async def handle_event_callback(callback_query: dict[str, Any], parts: list[str]) -> None:
    """Handles the 'evt:create'/'evt:cancel' confirmation buttons for a pending calendar event."""
    cb_id = callback_query.get("id")
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    from_user = callback_query.get("from", {})
    user_id = from_user.get("id")

    if not is_authorized(user_id):
        await telegram_bot.answer_callback_query(cb_id, text="Acesso não autorizado.", show_alert=True)
        return

    action = parts[1]
    target_user_id = int(parts[2]) if parts[2].isdigit() else user_id

    async with get_db_session() as session:
        draft = await session.get(TelegramDraftSession, target_user_id)

        if action == "create":
            if not draft or draft.status != "awaiting_event_confirmation":
                await telegram_bot.answer_callback_query(
                    cb_id, text="Esse rascunho de evento não existe mais ou expirou.", show_alert=True,
                )
                return

            fields = json.loads(draft.extracted_fields_json or "{}")
            title = draft.title or "Novo evento"

            if not fields.get("account_email"):
                await telegram_bot.answer_callback_query(
                    cb_id, text="Ainda não sei em qual conta criar. Escolha a conta primeiro.", show_alert=True,
                )
                return

            # Delete before calling the API so a double-tap can't find the draft again and
            # fire a second insert.
            await session.delete(draft)
            await session.commit()

            tz = ZoneInfo(settings.TIMEZONE)
            start_dt = datetime.fromisoformat(fields["start"]).replace(tzinfo=tz)
            end_dt = datetime.fromisoformat(fields["end"]).replace(tzinfo=tz)

            try:
                created = await asyncio.to_thread(
                    CalendarConnector().create_event,
                    summary=title,
                    start_time=start_dt,
                    end_time=end_dt,
                    account_email=fields.get("account_email"),
                    description=fields.get("description"),
                    location=fields.get("location"),
                    attendees=fields.get("attendees") or None,
                )
            except Exception as e:
                logger.error(f"[CalendarConnector] falha ao criar evento: {e}")
                await telegram_bot.answer_callback_query(cb_id, text="Erro ao criar o evento.", show_alert=True)
                await telegram_bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"⚠️ <b>Não consegui criar o evento no Google Calendar.</b>\nErro: {str(e)[:200]}",
                )
                return

            await telegram_bot.answer_callback_query(cb_id, text="✅ Evento criado!")
            await telegram_bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"✅ <b>Evento criado no Google Calendar!</b>\n\n"
                    f"📅 <b>{created.summary}</b>\n"
                    f"🕒 {created.start_time.strftime('%d/%m/%Y %H:%M')} – {created.end_time.strftime('%H:%M')}\n"
                    f"👤 Conta: {created.account}"
                ),
            )

        elif action in ("accp", "accc"):
            if not draft or draft.status != "awaiting_account_choice":
                await telegram_bot.answer_callback_query(
                    cb_id, text="Esse rascunho de evento não existe mais ou expirou.", show_alert=True,
                )
                return

            account_email = _account_hint_email("pessoal" if action == "accp" else "corporativo")
            fields = json.loads(draft.extracted_fields_json or "{}")
            fields["account_email"] = account_email
            draft.extracted_fields_json = json.dumps(fields, ensure_ascii=False)
            draft.status = "awaiting_event_confirmation"
            await session.commit()

            await telegram_bot.answer_callback_query(cb_id, text=f"Conta: {account_email}")
            await telegram_bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"📅 <b>{draft.title or 'Novo evento'}</b>\n\nConta escolhida: {account_email}",
            )
            await send_active_event_draft_notice(chat_id, target_user_id, draft.title, fields)

        elif action == "cancel":
            if draft and draft.status in ("awaiting_event_confirmation", "awaiting_account_choice"):
                await session.delete(draft)
                await session.commit()

            await telegram_bot.answer_callback_query(cb_id, text="Cancelado.")
            await telegram_bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="❌ <b>Evento cancelado.</b> Nada foi criado no Google Calendar.",
            )


async def handle_callback_query(callback_query: dict[str, Any]) -> None:
    """Handles inline keyboard approval and cancellation buttons."""
    cb_id = callback_query.get("id")
    data = callback_query.get("data", "")
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    from_user = callback_query.get("from", {})
    user_id = from_user.get("id")

    if not is_authorized(user_id):
        await telegram_bot.answer_callback_query(cb_id, text="Acesso não autorizado.", show_alert=True)
        return

    parts = data.split(":")
    if len(parts) < 3:
        await telegram_bot.answer_callback_query(cb_id)
        return

    # Route entity-validation callbacks out before the draft-session query below —
    # they own their own DB session and have nothing to do with CAPTURE_NOTE drafts.
    if parts[0] == "entcand":
        from rysos.telegram.entity_review import handle_entcand_callback
        await handle_entcand_callback(callback_query, parts)
        return

    if parts[0] == "entgrp":
        from rysos.telegram.entity_review import handle_entgrp_callback
        await handle_entgrp_callback(callback_query, parts)
        return

    if parts[0] == "prjlnk":
        from rysos.telegram.entity_review import handle_prjlnk_callback
        await handle_prjlnk_callback(callback_query, parts)
        return

    if parts[0] == "evt":
        await handle_event_callback(callback_query, parts)
        return

    if parts[0] != "cap":
        await telegram_bot.answer_callback_query(cb_id)
        return

    action = parts[1]
    target_user_id = int(parts[2]) if parts[2].isdigit() else user_id

    async with get_db_session() as session:
        stmt = select(TelegramDraftSession).where(TelegramDraftSession.user_id == target_user_id)
        draft = (await session.execute(stmt)).scalar_one_or_none()

        if action == "save":
            if not draft:
                await telegram_bot.answer_callback_query(cb_id, text="Rascunho não encontrado ou já salvo.", show_alert=True)
                return

            if draft.status != "awaiting_confirmation":
                # Stale inline button from an earlier message: the current draft row is
                # still being built (or is a disambiguation placeholder). Never write a
                # half-formed note to the vault.
                await telegram_bot.answer_callback_query(
                    cb_id,
                    text="Esse rascunho ainda não está pronto para gravar. Me manda de novo o que você quer registrar.",
                    show_alert=True,
                )
                return

            draft_fields = json.loads(draft.extracted_fields_json or "{}")
            vault = VaultManager()
            saved_path = vault.save_quick_capture(
                category=draft.category,
                title=draft.title or "Nota Executiva",
                content=draft.content or "",
                target_date=draft_fields.get("date"),
                fields=draft_fields,
            )
            cat_label = CATEGORY_LABELS.get(draft.category, f"📁 {draft.category.capitalize()}")
            title = draft.title or "Nota Executiva"
            category = draft.category
            pending_json = draft.pending_segments_json

            await session.delete(draft)
            await session.commit()

            await adaptive_kb.record_feedback(
                user_id=target_user_id,
                raw_input=title,
                predicted_intent="CAPTURE_NOTE",
                predicted_category=category,
                feedback_type="confirmed",
            )

            await telegram_bot.answer_callback_query(cb_id, text="✅ Gravado com sucesso no Obsidian!")
            await telegram_bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ <b>Nota gravada com sucesso no Obsidian!</b>\n\n"
                     f"📂 <b>Destino:</b> {cat_label} (<code>{category}</code>)\n"
                     f"📄 <b>Arquivo:</b> <code>{saved_path.name}</code>\n"
                     f"📌 <b>Título:</b> {title}",
            )
            await _advance_capture_queue(chat_id, target_user_id, pending_json)

        elif action == "cancel":
            pending_json = None
            if draft:
                cat_to_log = draft.category
                title_to_log = draft.title or "Cancelado"
                pending_json = draft.pending_segments_json
                await session.delete(draft)
                await session.commit()
                await adaptive_kb.record_feedback(
                    user_id=target_user_id,
                    raw_input=title_to_log,
                    predicted_intent="CAPTURE_NOTE",
                    predicted_category=cat_to_log,
                    feedback_type="cancelled",
                )

            await telegram_bot.answer_callback_query(cb_id, text="Cancelado.")
            await telegram_bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="❌ <b>Nota cancelada.</b> Nenhuma informação foi gravada no Obsidian.",
            )
            await _advance_capture_queue(chat_id, target_user_id, pending_json)


async def send_help_message(chat_id: int | str, name: str) -> None:
    """Sends greeting and executive capabilities overview."""
    msg = (
        f"👋 <b>Olá, {name}! Bem-vindo ao rysOS.</b>\n\n"
        f"Este é o seu canal executivo direto com o seu Segundo Cérebro no Obsidian.\n\n"
        f"<b>Comandos Rápidos</b> (também no menu ☰ do Telegram):\n"
        f"• /cockpit — Resumo executivo do dia de hoje\n"
        f"• /agenda — Compromissos e reuniões de hoje\n"
        f"• /decisoes — Decisões executivas em aberto\n"
        f"• /triagem — Cards de triagem das reuniões com itens pendentes\n"
        f"• /recap — Balanço executivo de encerramento do dia\n"
        f"• /sync — Sincroniza agenda e e-mail e regenera o cockpit\n"
        f"• /dedup — Varredura de duplicidades e links quebrados no vault\n"
        f"• /heartbeat — Consumo e custo de tokens de IA (hoje / 7 dias / total)\n"
        f"• /cancelar — Descarta nota ou rascunho ativo\n\n"
        f"<b>Captura Inteligente Multimodal:</b>\n"
        f"• Envie anotações por <b>texto</b>, <b>áudio</b> ou <b>fotos</b> (quadros, documentos, telas).\n"
        f"• O bot categoriza e extrai os campos automaticamente.\n"
        f"• Se faltar algum dado essencial, o bot pergunta de forma natural.\n"
        f"• Quando tudo estiver pronto, você confirma ou pede edições antes de gravar no Obsidian."
    )
    await telegram_bot.send_message(chat_id=chat_id, text=msg)


async def send_cockpit_summary(chat_id: int | str) -> None:
    """Sends today's Daily Cockpit summary including foci, meetings, emails, and decisions."""
    vault = VaultManager()
    today_str = date.today().strftime("%Y-%m-%d")
    today_formatted = date.today().strftime("%d/%m/%Y")
    cockpit_file = vault.vault_path / "00_Cockpit" / "Daily" / f"{today_str}.md"

    if not cockpit_file.exists():
        await telegram_bot.send_message(
            chat_id=chat_id,
            text=f"📅 <b>Cockpit Diário ({today_formatted})</b> ainda não foi gerado.\nEnvie <code>/sync</code> para sincronizar agora.",
        )
        return

    content = vault.read_file(cockpit_file) or ""

    # 1. Extract Foci / Tasks
    foci_match = re.search(r"## 🎯 Focos de Alta Prioridade do Dia\s*\n(.*?)(?=\n---|\n##(?!#)|$)", content, re.DOTALL)
    foci_raw = foci_match.group(1).strip() if foci_match else ""
    foci_lines = [line.strip() for line in foci_raw.splitlines() if line.strip() and not line.strip().startswith("- [ ] *Defina")]
    clean_foci = "\n".join(foci_lines) if foci_lines else "• Nenhum foco pendente."

    # 2. Extract Meetings
    meetings_match = re.search(r"## 📅 Agenda & Reuniões de Hoje\s*\n(.*?)(?=\n---|\n##(?!#)|$)", content, re.DOTALL)
    meetings_raw = meetings_match.group(1).strip() if meetings_match else ""
    meeting_titles = re.findall(r"###\s*⏰\s*(.*?)(?=\n|$)", meetings_raw)
    if meeting_titles:
        clean_meetings = "\n".join([f"• ⏰ {re.sub(r'\[\[.*?\|(.*?)\]\]', r'\1', m).strip()}" for m in meeting_titles])
    else:
        clean_meetings = "• Sem reuniões registradas para hoje."

    # 3. Extract Actionable Emails / Bills
    emails_match = re.search(r"## 📬 E-mails Críticos & Ações Necessárias\s*\n(.*?)(?=\n---|\n##(?!#)|$)", content, re.DOTALL)
    emails_raw = emails_match.group(1).strip() if emails_match else ""
    email_actions = re.findall(r"Ação Sugerida:\*\*\s*(.*?)(?=\n|$)", emails_raw)
    if email_actions:
        clean_emails = "\n".join([f"• {a.strip()}" for a in email_actions[:4]])
    else:
        clean_emails = "• Nenhuma ação crítica de e-mail pendente."

    # 4. Extract Decisions
    decisions_match = re.search(r"## 💡 Decisões & Pendências a Desbloquear\s*\n(.*?)(?=\n---|\n##(?!#)|$)", content, re.DOTALL)
    decisions_raw = decisions_match.group(1).strip() if decisions_match else ""
    dec_items = re.findall(r"-\s*\[\s*\]\s*\[\[.*?\|(.*?)\]\]", decisions_raw)
    if dec_items:
        clean_decs = "\n".join([f"• ⚖️ {d.strip()}" for d in dec_items[:3]])
    else:
        clean_decs = "• Nenhuma decisão urgente no radar."

    response = (
        f"🚀 <b>Cockpit Diário — {today_formatted}</b>\n\n"
        f"<b>🎯 Focos do Dia:</b>\n{clean_foci}\n\n"
        f"<b>📅 Agenda de Reuniões:</b>\n{clean_meetings}\n\n"
        f"<b>📬 Ações & Contas Críticas:</b>\n{clean_emails}\n\n"
        f"<b>💡 Decisões sob Análise:</b>\n{clean_decs}"
    )
    await telegram_bot.send_message(chat_id=chat_id, text=response)


async def send_today_agenda(chat_id: int | str) -> None:
    """Sends today's meetings and high-priority tasks."""
    vault = VaultManager()
    today_str = date.today().strftime("%Y-%m-%d")
    cockpit_file = vault.vault_path / "00_Cockpit" / "Daily" / f"{today_str}.md"

    if not cockpit_file.exists():
        await telegram_bot.send_message(chat_id=chat_id, text="Nenhum compromisso carregado para hoje.")
        return

    content = vault.read_file(cockpit_file) or ""
    meetings_match = re.search(r"## 📅 Agenda & Reuniões de Hoje\s*\n(.*?)(?=\n---|\n##(?!#)|$)", content, re.DOTALL)
    meetings_text = meetings_match.group(1).strip() if meetings_match else "Sem reuniões."

    await telegram_bot.send_message(
        chat_id=chat_id,
        text=f"📅 <b>Agenda de Hoje ({date.today().strftime('%d/%m/%Y')}):</b>\n\n{meetings_text[:1200]}",
    )


async def send_decisions_summary(chat_id: int | str) -> None:
    """Lists pending decision records from SQLite database."""
    from rysos.core import reconcile_decision_statuses

    try:
        await reconcile_decision_statuses(VaultManager())
    except Exception as e:
        logger.warning(f"Erro ao reconciliar status de decisões: {e}")

    async with get_db_session() as session:
        stmt = select(DecisionRecord).where(DecisionRecord.status == "em_analise").order_by(DecisionRecord.decision_date.desc())
        decisions = (await session.execute(stmt)).scalars().all()

    if not decisions:
        await telegram_bot.send_message(
            chat_id=chat_id,
            text="💡 <b>Nenhuma decisão executiva pendente</b> de análise no momento.",
        )
        return

    lines = []
    for d in decisions:
        prio_tag = "🔴 Alta" if d.priority == "alta" else "🟡 Baixa"
        lines.append(f"• <b>{d.id}:</b> {d.title} ({prio_tag})\n  Área: <i>{d.area}</i>")

    await telegram_bot.send_message(
        chat_id=chat_id,
        text=f"💡 <b>Decisões Executivas Pendentes ({len(decisions)}):</b>\n\n" + "\n\n".join(lines),
    )


_DEDUP_AFFIRMATIVE = {"sim", "confirma", "confirmo", "ok", "pode", "manda", "vai", "isso"}
_DEDUP_NEGATIVE = {"nao", "cancela", "mantem", "deixa", "ignora", "para"}
# Subset safe to match as just the reply's FIRST word (see the n_first check below) —
# "para" excluded: as a lone reply it means "stop" (fine exact-match), but as a sentence's
# first word it's usually the preposition "to/for" ("para mim tanto faz"), so treating it
# as a first-word negative would misfire on ordinary adjustment sentences.
_DEDUP_NEGATIVE_PREFIX = _DEDUP_NEGATIVE - {"para"}
_DEDUP_STATUS_LABEL = {
    "applied": "mesclada(s)", "rejected": "mantida(s) separada(s)",
    "approved": "confirmada(s) como separada(s)", "adjusted": "ajuste(s) registrado(s)",
}


_DEDUP_KIND_LABEL = {"people": "pessoas", "resources": "recursos", "decisions": "decisões"}


async def _build_dedup_suggestions(
    vault: VaultManager, report: Any, decisions_meta: list[dict[str, Any]],
    backlink_index: dict[str, list[dict[str, Any]]], chat_id: int | str,
) -> list[DedupSuggestion]:
    """AI-assesses every actionable finding from a `/dedup` scan — people/resources
    near-duplicate pairs, 2-way possibly-duplicate decision groups, and diary mentions
    missing a wikilink — and persists a `DedupSuggestion` row for each one worth asking
    about. A "needs_review" (or any other) assessment isn't interactive here — it's left
    out, so it stays implicitly covered by the plain-report tail the owner reviews by hand.
    A decision group with more than 2 ids is left out too (no natural single survivor to
    propose) — stays in the plain report. Projects and resource id collisions are
    deliberately out of AI-assessed scope entirely, per the original plan."""
    from rysos.vault.dedup_triage import build_finding_context

    pairs: list[tuple[str, dict[str, Any]]] = (
        [("people", f) for f in report.people] + [("resources", f) for f in report.resources]
    )

    decisions_by_id = {d["id"]: d for d in decisions_meta}
    for group in report.decisions:
        ids = group.get("ids") or []
        if len(ids) != 2:
            continue  # multi-way group — no single natural survivor, leave in the report
        id_a, id_b = ids
        path_a = (decisions_by_id.get(id_a) or {}).get("note_path")
        path_b = (decisions_by_id.get(id_b) or {}).get("note_path")
        if not path_a or not path_b:
            continue  # DB row with no note on file — can't merge, skip
        pairs.append(("decisions", {
            "a": f'{group["title"]} ({id_a})', "a_path": path_a, "a_dec_id": id_a,
            "b": f'{group["title"]} ({id_b})', "b_path": path_b, "b_dec_id": id_b,
            "score": None,
        }))

    rows: list[DedupSuggestion] = []
    for kind, finding in pairs:
        context = await asyncio.to_thread(build_finding_context, vault, kind, finding, backlink_index)
        assessment = await asyncio.to_thread(gemini_ai.assess_dedup_finding, kind, context)
        if assessment["recommendation"] not in ("merge", "keep_separate"):
            continue
        finding_json: dict[str, Any] = {
            "a_path": context["a_path"], "a_name": context["a_name"],
            "b_path": context["b_path"], "b_name": context["b_name"],
            "score": finding.get("score"),
        }
        if kind == "decisions":
            finding_json["a_dec_id"] = finding["a_dec_id"]
            finding_json["b_dec_id"] = finding["b_dec_id"]
        rows.append(DedupSuggestion(
            kind=kind,
            finding_json=json.dumps(finding_json),
            ai_recommendation=assessment["recommendation"],
            ai_rationale=assessment["rationale"],
            proposed_survivor_path=assessment.get("survivor_path"),
            proposed_summary=assessment.get("consolidated_summary"),
            status="pending",
            chat_id=str(chat_id),
        ))

    for diary_finding in report.diary_candidates:
        context = await asyncio.to_thread(
            build_finding_context, vault, "diary_candidates", diary_finding, backlink_index,
        )
        assessment = await asyncio.to_thread(gemini_ai.assess_dedup_finding, "diary_candidates", context)
        if assessment["recommendation"] != "link_diary_mention":
            continue
        rows.append(DedupSuggestion(
            kind="diary_link",
            finding_json=json.dumps({
                "orphan_path": context["orphan_path"], "orphan_name": context["orphan_name"],
                "mentions": diary_finding["mentions"],
            }),
            ai_recommendation=assessment["recommendation"],
            ai_rationale=assessment["rationale"],
            proposed_survivor_path=assessment.get("survivor_path"),
            proposed_summary=None,
            status="pending",
            chat_id=str(chat_id),
        ))

    if rows:
        async with get_db_session() as session:
            session.add_all(rows)
            await session.commit()
            for row in rows:
                await session.refresh(row)
    return rows


def _dedup_suggestion_card(row: DedupSuggestion, position: int, total: int) -> str:
    finding = json.loads(row.finding_json)
    header = f"🔎 <b>Duplicidade {position}/{total}</b>"

    if row.kind == "diary_link":
        orphan_name = finding.get("orphan_name", "?")
        mentions = finding.get("mentions", [])
        dates = ", ".join(m.get("cockpit_date", "?") for m in mentions[:5])
        lines = [
            f"{header} — menção sem link",
            f'"{orphan_name}" citada em texto livre, sem wikilink, no Cockpit de: {dates}.',
            "",
            row.ai_rationale,
            "",
            f'Sugestão: transformar a menção em link para "{orphan_name}".',
            "Responda sim para linkar, não para deixar como está, ou me diga o que ajustar.",
        ]
        return "\n".join(lines)

    a_name, b_name = finding.get("a_name", "?"), finding.get("b_name", "?")
    kind_label = _DEDUP_KIND_LABEL.get(row.kind, row.kind)
    pair = f'"{a_name}" vs "{b_name}"'
    score = finding.get("score")
    if score is not None:
        pair += f" (score {float(score):.2f})"

    lines = [f"{header} — {kind_label}", pair, "", row.ai_rationale, ""]
    if row.ai_recommendation == "merge":
        survivor_path = row.proposed_survivor_path
        if survivor_path == finding.get("a_path"):
            survivor_name = a_name
        elif survivor_path == finding.get("b_path"):
            survivor_name = b_name
        else:
            survivor_name = survivor_path or "?"
        lines.append(f'Sugestão: mesclar, sobrevive "{survivor_name}".')
        lines.append("Responda sim para mesclar, não para manter separado, ou me diga o que ajustar.")
    else:  # keep_separate
        lines.append("Sugestão: manter separado.")
        lines.append("Responda sim para confirmar, não se você acha que são a mesma coisa, ou me diga o que ajustar.")
    return "\n".join(lines)


async def _route_dedup_reply(chat_id: int | str, user_id: int, text: str, draft: TelegramDraftSession) -> None:
    """Applies (sim), rejects (não), or records-without-applying (any other reply) the
    `/dedup` suggestion currently in flight for this user, then advances to the next
    pending one in the same batch. A free-text reply is NEVER auto-interpreted and
    applied — only sim/não trigger a vault write, matching
    [[dedup_unitary_approval_required]]: one suggestion at a time, an adjustment is
    recorded for the owner to resolve by hand, never guessed at."""
    state = json.loads(draft.extracted_fields_json or "{}")
    suggestion_id = state.get("dedup_suggestion_id")
    batch_ids = state.get("dedup_batch_ids") or []

    resolved = False
    outcome_text = "Nada pendente para revisar agora."
    async with get_db_session() as session:
        row = await session.get(DedupSuggestion, suggestion_id) if suggestion_id else None
        if row is None or row.status != "pending":
            stale = await session.get(TelegramDraftSession, user_id)
            if stale:
                await session.delete(stale)
                await session.commit()
            await telegram_bot.send_message(chat_id=chat_id, text=outcome_text)
            return

        n = normalize_text(text)
        # "sim" stays an exact match ONLY — it's the one reply that writes to the vault
        # (a merge), so "sim, mas confere antes" must never be read as a blank confirm.
        # "não" never writes anything (rejects, or at most leaves a suggestion pending),
        # so it's safe to also match on the reply's first word — the owner naturally
        # answers "não, manter separado" (echoing the card's own wording) rather than a
        # bare "não", and that used to fall through to the adjustment bucket instead of
        # closing cleanly (2026-09-18 Telegram thread).
        n_first = n.split(" ", 1)[0] if n else ""
        finding = json.loads(row.finding_json)
        if n in _DEDUP_AFFIRMATIVE:
            if row.ai_recommendation == "merge":
                survivor = row.proposed_survivor_path
                if survivor not in (finding.get("a_path"), finding.get("b_path")):
                    outcome_text = (
                        "Não consegui identificar qual nota deve sobreviver — a sugestão "
                        "continua pendente, roda /dedup de novo."
                    )
                else:
                    loser = finding["b_path"] if survivor == finding["a_path"] else finding["a_path"]
                    survivor_name = finding["a_name"] if survivor == finding["a_path"] else finding["b_name"]
                    loser_name = finding["b_name"] if survivor == finding["a_path"] else finding["a_name"]
                    try:
                        vault = VaultManager()
                        result = await asyncio.to_thread(
                            vault.merge_notes, row.kind, survivor, loser, row.proposed_summary,
                        )
                        if row.kind == "decisions":
                            loser_dec_id = (
                                finding["b_dec_id"] if survivor == finding["a_path"] else finding["a_dec_id"]
                            )
                            await session.execute(delete(DecisionRecord).where(DecisionRecord.id == loser_dec_id))
                        row.status = "applied"
                        resolved = True
                        outcome_text = (
                            f'Mesclado em "{survivor_name}". "{loser_name}" arquivada, '
                            f"{len(result['repointed_sources'])} nota(s) repontada(s)."
                        )
                    except Exception as e:
                        logger.error(f"Erro ao aplicar merge de dedup id={row.id}: {e}")
                        outcome_text = (
                            "Não consegui aplicar a fusão agora — a sugestão continua "
                            "pendente, tenta de novo em instantes."
                        )
            elif row.ai_recommendation == "link_diary_mention":
                try:
                    vault = VaultManager()
                    touched = await asyncio.to_thread(
                        vault.wikify_diary_mention, finding["orphan_path"], finding["mentions"],
                    )
                    row.status = "applied"
                    resolved = True
                    orphan_name = finding.get("orphan_name", "?")
                    outcome_text = (
                        f'Linkado. {len(touched)} nota(s) do Cockpit atualizada(s) com o '
                        f'wikilink para "{orphan_name}".'
                    )
                except Exception as e:
                    logger.error(f"Erro ao aplicar link de diário de dedup id={row.id}: {e}")
                    outcome_text = (
                        "Não consegui aplicar o link agora — a sugestão continua pendente, "
                        "tenta de novo em instantes."
                    )
            else:  # keep_separate confirmed
                row.status = "approved"
                resolved = True
                outcome_text = "Combinado, mantendo separado."
        elif n in _DEDUP_NEGATIVE or n_first in _DEDUP_NEGATIVE_PREFIX:
            row.status = "rejected"
            resolved = True
            if row.ai_recommendation == "merge":
                outcome_text = "Beleza, mantendo os dois separados."
            elif row.ai_recommendation == "link_diary_mention":
                outcome_text = "Entendido, deixei como está — sem wikilink."
            else:
                outcome_text = (
                    "Entendido. Não vou mesclar sozinho sem saber qual nota deve sobreviver — "
                    "se quiser mesclar, roda /dedup de novo e me diga explicitamente."
                )
        else:
            finding["owner_adjustment_note"] = text
            row.finding_json = json.dumps(finding)
            row.status = "adjusted"
            resolved = True
            outcome_text = (
                "Anotado. Não vou aplicar isso sozinho — fica registrado como ajuste "
                "pendente pra você resolver direto no vault."
            )

        if resolved:
            row.resolved_at = datetime.now(timezone.utc)
        await session.commit()

    await telegram_bot.send_message(chat_id=chat_id, text=outcome_text)
    if resolved:
        await _advance_dedup_queue(chat_id, user_id, batch_ids, suggestion_id)


async def _advance_dedup_queue(chat_id: int | str, user_id: int, batch_ids: list[int], just_resolved_id: int) -> None:
    """Sends the next pending `DedupSuggestion` in the batch, or — once none are left — a
    closing summary and clears the draft. Never batches multiple suggestions into one ask."""
    remaining = [i for i in batch_ids if i != just_resolved_id]
    async with get_db_session() as session:
        next_row = None
        for sid in remaining:
            candidate = await session.get(DedupSuggestion, sid)
            if candidate and candidate.status == "pending":
                next_row = candidate
                break

        draft = await session.get(TelegramDraftSession, user_id)
        if next_row is None:
            if draft:
                await session.delete(draft)
            counts: dict[str, int] = {}
            for sid in batch_ids:
                s = await session.get(DedupSuggestion, sid)
                if s:
                    counts[s.status] = counts.get(s.status, 0) + 1
            await session.commit()
            summary = ", ".join(f"{v} {_DEDUP_STATUS_LABEL.get(k, k)}" for k, v in counts.items())
            await telegram_bot.send_message(
                chat_id=chat_id, text=f"Triagem de /dedup concluída: {summary or 'nada a revisar'}.",
            )
            return

        if draft:
            draft.extracted_fields_json = json.dumps({
                "dedup_suggestion_id": next_row.id, "dedup_batch_ids": batch_ids,
            })
        await session.commit()

    position = batch_ids.index(next_row.id) + 1
    await telegram_bot.send_message(
        chat_id=chat_id, text=_dedup_suggestion_card(next_row, position, len(batch_ids)),
    )


async def send_dedup_report(chat_id: int | str, user_id: int) -> None:
    """Runs the vault integrity scan, then kicks off the sequential Telegram triage for
    every actionable finding — people/resources near-duplicate pairs, 2-way decision
    groups, and diary mentions missing a wikilink: one AI-assessed suggestion at a time,
    plain-text sim/não/ajuste reply, never a batch action (see
    [[dedup_unitary_approval_required]]). Nothing merges without an explicit "sim" to a
    specific suggestion. What's left over (projects, resource id collisions, a
    multi-way decision group, orphan notes, phantom links) — no merge primitive for
    those, or no single natural survivor — is reported plainly, same as before.
    """
    from rysos.vault.dedup import scan_vault, collect_vault_files, build_backlink_index

    await telegram_bot.send_message(chat_id=chat_id, text="🔍 <i>Varrendo o vault por duplicidades e links quebrados...</i>")

    async with get_db_session() as session:
        stmt = select(DecisionRecord.id, DecisionRecord.title, DecisionRecord.area, DecisionRecord.note_path)
        rows = (await session.execute(stmt)).all()
    decisions = [{"id": r.id, "title": r.title, "area": r.area, "note_path": r.note_path} for r in rows]

    vault = VaultManager()
    async with _typing_indicator(chat_id):
        files = await asyncio.to_thread(collect_vault_files, vault)
        report = await asyncio.to_thread(scan_vault, vault, decisions, files)
        backlink_index = await asyncio.to_thread(build_backlink_index, files[0])

    if report.is_empty():
        await telegram_bot.send_message(
            chat_id=chat_id,
            text="✅ Nenhuma duplicidade ou link quebrado encontrado no vault.",
        )
        return

    suggestions = await _build_dedup_suggestions(vault, report, decisions, backlink_index, chat_id)

    decisions_covered_ids: set[str] = set()
    diary_covered_paths: set[str] = set()
    for s in suggestions:
        fj = json.loads(s.finding_json)
        if s.kind == "decisions":
            decisions_covered_ids.update({fj.get("a_dec_id"), fj.get("b_dec_id")})
        elif s.kind == "diary_link":
            diary_covered_paths.add(fj.get("orphan_path"))

    remaining_decisions = [d for d in report.decisions if not (set(d["ids"]) & decisions_covered_ids)]
    remaining_diary = [d for d in report.diary_candidates if d["orphan_path"] not in diary_covered_paths]

    lines = ["🔍 <b>Varredura de Integridade do Vault</b>\n"]

    if report.projects:
        lines.append(f"<b>🚀 Projetos parecidos ({len(report.projects)}):</b>")
        for p in report.projects[:10]:
            lines.append(f"• {p['a']} ~ {p['b']} (score {p['score']:.2f})")
        lines.append("")

    if report.resource_id_collisions:
        lines.append(f"<b>📁 Colisões de id em Resources ({len(report.resource_id_collisions)}):</b>")
        for c in report.resource_id_collisions[:10]:
            titles = ", ".join(c["titles"][:4])
            lines.append(f"• {c['id']}: {titles}")
        lines.append("")

    if remaining_decisions:
        lines.append(f"<b>⚖️ Decisões possivelmente duplicadas ({len(remaining_decisions)}):</b>")
        for d in remaining_decisions[:10]:
            lines.append(f"• {d['title']} ({d['area']}): {', '.join(d['ids'])}")
        lines.append("")

    if report.orphan_notes:
        lines.append(f"<b>📄 Notas órfãs, amostra de {len(report.orphan_notes)}:</b>")
        for path in report.orphan_notes[:10]:
            lines.append(f"• {path}")
        lines.append("")

    if report.phantom_links:
        lines.append(f"<b>🔗 Links fantasmas, amostra de {len(report.phantom_links)}:</b>")
        for link in report.phantom_links[:10]:
            lines.append(f"• {link['source']} -> [[{link['target']}]]")
        lines.append("")

    if remaining_diary:
        lines.append(f"<b>📓 Menções em texto livre sem wikilink ({len(remaining_diary)}):</b>")
        for d in remaining_diary[:10]:
            lines.append(f"• {d['orphan_name']} — {len(d['mentions'])} menção(ões)")
        lines.append("")

    plain_report_sent = len(lines) > 1
    if plain_report_sent:
        lines.append("<i>Revise manualmente — ainda sem fusão automática para esses casos.</i>")
        await telegram_bot.send_message(chat_id=chat_id, text="\n".join(lines))

    if not suggestions:
        if not plain_report_sent:
            await telegram_bot.send_message(
                chat_id=chat_id, text="✅ Nenhuma duplicidade ou link quebrado encontrado no vault.",
            )
        return

    async with get_db_session() as session:
        existing = await session.get(TelegramDraftSession, user_id)
        if existing:
            await session.delete(existing)
            await session.flush()
        session.add(TelegramDraftSession(
            user_id=user_id,
            status="awaiting_dedup_reply",
            category="dedup",
            extracted_fields_json=json.dumps({
                "dedup_suggestion_id": suggestions[0].id,
                "dedup_batch_ids": [s.id for s in suggestions],
            }),
            history_json="[]",
        ))
        await session.commit()

    await telegram_bot.send_message(
        chat_id=chat_id,
        text=f"Encontrei {len(suggestions)} possível(is) duplicidade(s) para revisar, uma de cada vez.",
    )
    await telegram_bot.send_message(
        chat_id=chat_id, text=_dedup_suggestion_card(suggestions[0], 1, len(suggestions)),
    )


def _fmt_tokens_line(label: str, bucket: dict) -> str:
    calls = bucket.get("calls_count", 0)
    total = bucket.get("total_tokens", 0)
    prompt = bucket.get("prompt_tokens", 0)
    out = bucket.get("candidate_tokens", 0)
    cost_usd = bucket.get("cost_usd", 0.0)
    cost_brl = bucket.get("cost_brl", 0.0)
    return (
        f"{label} <b>{total:,}</b> tokens (prompt {prompt:,} | output {out:,}) "
        f"em {calls} chamada(s) — ${cost_usd:.4f} (~R$ {cost_brl:.2f})"
    ).replace(",", ".")


async def send_heartbeat_report(chat_id: int | str) -> None:
    """Reports real Gemini token consumption and cost (rysos.observability), mirroring
    the `rysos tokens` CLI command — Telegram's on-demand view onto the same
    `token_usage_logs` data every AI call already records via `_record_usage`.

    On-demand only by design (2026-09-17 scope decision, mirrors /dedup's read-only
    stance): no scheduled push, no budget-threshold alert — those are a deliberate
    follow-up once the owner has a sense for what a normal day costs."""
    from rysos.observability import get_token_usage_stats

    stats = await asyncio.to_thread(get_token_usage_stats)

    today = stats.get("today", {})
    last_7d = stats.get("last_7_days", {})
    total = stats.get("total", {})

    if not total.get("calls_count"):
        await telegram_bot.send_message(
            chat_id=chat_id,
            text="💓 <b>Heartbeat</b>\n\nAinda não há nenhuma chamada de IA registrada.",
        )
        return

    lines = [
        "💓 <b>rysOS Heartbeat — Consumo de Tokens IA</b>\n",
        _fmt_tokens_line("📅 <b>Hoje:</b>", today),
        _fmt_tokens_line("📊 <b>Últimos 7 dias:</b>", last_7d),
        _fmt_tokens_line("🏛️ <b>Total histórico:</b>", total),
        "",
    ]

    by_op = stats.get("by_operation", [])
    if by_op:
        lines.append("<b>🔧 Por operação (acumulado, top 8):</b>")
        for op in by_op[:8]:
            lines.append(
                f"• {op['operation']}: {op['calls_count']}x, "
                f"{op['total_tokens']:,} tokens, ${op['cost_usd']:.4f}".replace(",", ".")
            )
        lines.append("")

    by_model = stats.get("by_model", [])
    if by_model:
        lines.append("<b>🤖 Por modelo (acumulado):</b>")
        for m in by_model:
            lines.append(
                f"• {m['model']}: {m['calls_count']}x, "
                f"{m['total_tokens']:,} tokens, ${m['cost_usd']:.4f}".replace(",", ".")
            )
        lines.append("")

    lines.append(f"<i>Modelo padrão: {stats.get('current_model')} · câmbio usado: R$ {stats.get('usd_to_brl')}/US$</i>")
    await telegram_bot.send_message(chat_id=chat_id, text="\n".join(lines))


async def send_evening_recap(chat_id: int | str) -> str:
    """Generates and sends end-of-day executive recap analyzing completed vs pending tasks."""
    vault = VaultManager()
    today_str = date.today().strftime("%Y-%m-%d")
    today_formatted = date.today().strftime("%d/%m/%Y")
    cockpit_file = vault.vault_path / "00_Cockpit" / "Daily" / f"{today_str}.md"

    if not cockpit_file.exists():
        msg = f"🌙 <b>Balanço do Dia ({today_formatted})</b>\n\nNenhum Cockpit Diário foi gerado para a data de hoje."
        await telegram_bot.send_message(chat_id=chat_id, text=msg)
        return msg

    content = vault.read_file(cockpit_file) or ""

    # Extract High Priority Foci section
    foci_match = re.search(r"## 🎯 Focos de Alta Prioridade do Dia\s*\n(.*?)(?=\n---|\n##(?!#)|$)", content, re.DOTALL)
    foci_text = foci_match.group(1).strip() if foci_match else content

    # Find completed (- [x] ...) and pending (- [ ] ...) items
    completed_items = [re.sub(r"^\*\*(.*?)\*\*", r"\1", item.strip()) for item in re.findall(r"-\s*\[x\]\s*(.*)", foci_text, re.IGNORECASE)]
    pending_items = [re.sub(r"^\*\*(.*?)\*\*", r"\1", item.strip()) for item in re.findall(r"-\s*\[\s\]\s*(.*)", foci_text)]

    total = len(completed_items) + len(pending_items)
    lines = [f"🌙 <b>Balanço Executivo do Dia — {today_formatted}</b>\n"]

    if total == 0:
        lines.append("Nenhum foco de alta prioridade rastreado no Cockpit de hoje.")
    else:
        lines.append(f"📊 <b>Progresso:</b> {len(completed_items)}/{total} foco(s) concluído(s)\n")

        if completed_items:
            lines.append("<b>✅ Concluídos:</b>")
            for item in completed_items[:5]:
                lines.append(f"• {item}")
            lines.append("")

        if pending_items:
            lines.append("<b>⏳ Pendências para amanhã:</b>")
            for item in pending_items[:5]:
                lines.append(f"• {item}")
        else:
            lines.append("🎉 <b>Excelente!</b> Todos os focos previstos para hoje foram concluídos.")

    recap_msg = "\n".join(lines)
    await telegram_bot.send_message(chat_id=chat_id, text=recap_msg)
    return recap_msg


# Register handlers into telegram_bot instance
telegram_bot.set_message_handler(handle_message)
telegram_bot.set_callback_handler(handle_callback_query)
