"""Autonomous Scheduler for rysOS daily routines (07:00 Briefing & 19:00 Recap)."""

import asyncio
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from rysos.config import settings
from rysos.identity import user_first_name
from rysos.core import rysos_core


class AgentScheduler:
    """Manages scheduled background automation for rysOS."""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self._is_running = False

    def setup_jobs(self) -> None:
        """Configures morning and evening cron triggers."""
        # Morning Brief (Default: 07:00)
        morning_parts = settings.MORNING_BRIEF_TIME.split(":")
        m_hour = int(morning_parts[0]) if len(morning_parts) > 0 else 7
        m_minute = int(morning_parts[1]) if len(morning_parts) > 1 else 0

        self.scheduler.add_job(
            self.run_morning_job,
            CronTrigger(hour=m_hour, minute=m_minute),
            id="morning_cockpit",
            name="Morning Cockpit Sync",
            replace_existing=True,
        )

        # Evening Recap (Default: 19:00)
        evening_parts = settings.EVENING_RECAP_TIME.split(":")
        e_hour = int(evening_parts[0]) if len(evening_parts) > 0 else 19
        e_minute = int(evening_parts[1]) if len(evening_parts) > 1 else 0

        self.scheduler.add_job(
            self.run_evening_job,
            CronTrigger(hour=e_hour, minute=e_minute),
            id="evening_recap",
            name="Evening Daily Recap",
            replace_existing=True,
            # APScheduler's own default (1s) means any service restart within a
            # second of 23:00 — routine during active dev sessions — drops the run
            # entirely instead of firing late; a generous grace window lets it still
            # run rather than silently skip the day's Notas Livres processing.
            misfire_grace_time=3600,
        )

        # Meeting transcript watcher (default: every 15 min)
        interval_min = max(1, int(getattr(settings, "TRANSCRIPT_SCAN_INTERVAL_MIN", 15)))
        self.scheduler.add_job(
            self.run_transcript_job,
            IntervalTrigger(minutes=interval_min),
            id="transcript_watcher",
            name="Meeting Transcript Ingestion",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    async def run_morning_job(self) -> None:
        """Executes automated morning sync, cockpit generation, and sends Telegram briefing."""
        try:
            await rysos_core.run_daily_sync()
            from rysos.telegram import telegram_bot
            from rysos.telegram.handlers import send_cockpit_summary
            if telegram_bot.is_configured() and settings.TELEGRAM_CHAT_ID:
                await telegram_bot.send_message(
                    chat_id=settings.TELEGRAM_CHAT_ID,
                    text=f"🌅 <b>Bom dia, {user_first_name()}!</b> Seu Cockpit Diário foi sincronizado com sucesso no Obsidian.",
                )
                await send_cockpit_summary(settings.TELEGRAM_CHAT_ID)
        except Exception as e:
            print(f"[Scheduler] Erro no job matinal: {e}")

    async def run_transcript_job(self) -> None:
        """Scans the watched folders for manually dropped meeting transcripts and spoken
        diary entries, files them into 04_Meetings / 09_Journal, and pings Telegram with a
        short digest when anything landed. One job for both so `apply_pending_triagem`
        (which the transcript step runs first) never races itself. Also re-buckets today's
        Cockpit Foci tiers on the same tick (cheap, idempotent, unconditional)."""
        try:
            result = await rysos_core.process_pending_transcripts()
        except Exception as e:
            print(f"[Scheduler] Erro no job de transcrições: {e}")
            result = {"notes": []}

        try:
            diary_result = await rysos_core.process_pending_diary()
        except Exception as e:
            print(f"[Scheduler] Erro no job de diário: {e}")
            diary_result = {"notes": []}

        # Cheap, idempotent re-render of today's Foci tiers (today/semana/acompanhamento)
        # on the same 15-min cadence, so a hand-edited prazo or due marker moves to the
        # right tier without waiting for the 07:00 morning job or a manual /sync.
        try:
            rysos_core.vault.rebucket_daily_focus()
        except Exception as e:
            print(f"[Scheduler] Erro no re-bucketing do Cockpit: {e}")

        # Project <-> meeting/decision/resource wikilinks: idempotent, and it also picks up
        # candidates resolved by hand in the Triagem note since the last tick. Links look
        # back 14 days, so an item that waited on a still-pending project (or a stopped
        # service) is not lost; questions only go out for items resolved in the last day.
        # Older history goes through scripts/backfill_project_links.py (dry-run gated).
        try:
            from rysos.connectors.project_links import sync_project_links
            from rysos.telegram.entity_review import ask_project_link
            await sync_project_links(
                rysos_core.vault, since_days=14, ask=ask_project_link, ask_within_days=1,
            )
        except Exception as e:
            print(f"[Scheduler] Erro na sincronização de vínculos de projeto: {e}")

        notes = result.get("notes") or []
        dnotes = diary_result.get("notes") or []
        if not notes and not dnotes:
            return

        try:
            from rysos.telegram import telegram_bot
            from rysos.telegram.entity_review import push_meeting_review
            if not (telegram_bot.is_configured() and settings.TELEGRAM_CHAT_ID):
                return
            chat_id = settings.TELEGRAM_CHAT_ID

            if notes:
                if len(notes) == 1:
                    n = notes[0]
                    pend = n.get("candidates") or 0
                    extra = f" — {pend} item(ns) na Triagem, card de validação abaixo" if pend else ""
                    text = f"📄 Transcrição arquivada na nota <b>{n['title']}</b>{extra}."
                else:
                    total_pend = sum(n.get("candidates") or 0 for n in notes)
                    text = (f"📄 {len(notes)} transcrições arquivadas, {total_pend} item(ns) na Triagem "
                            f"— um card de validação por reunião abaixo.")
                await telegram_bot.send_message(chat_id=chat_id, text=text)
                for n in notes:
                    if n.get("sha"):
                        await push_meeting_review(n["sha"], chat_id)

            if dnotes:
                if len(dnotes) == 1:
                    n = dnotes[0]
                    bits = []
                    if n.get("candidates"):
                        bits.append(f"{n['candidates']} item(ns) na Triagem")
                    if n.get("open_questions"):
                        bits.append(f"{n['open_questions']} pergunta(s) em aberto")
                    extra = (" — " + ", ".join(bits)) if bits else ""
                    text = f"📓 Entrada de diário registrada em <b>09_Journal/{n['journal_note'][:-3]}</b>{extra}."
                else:
                    total_pend = sum(n.get("candidates") or 0 for n in dnotes)
                    text = f"📓 {len(dnotes)} entradas de diário registradas, {total_pend} item(ns) na Triagem."
                await telegram_bot.send_message(chat_id=chat_id, text=text)
                for n in dnotes:
                    if n.get("sha"):
                        await push_meeting_review(n["sha"], chat_id, note_label=n.get("triagem_stem"))
        except Exception as e:
            print(f"[Scheduler] Falha ao notificar no Telegram: {e}")

    async def run_evening_job(self) -> None:
        """Classifies the Cockpit's freeform 'Notas Livres' block into pendências,
        decisões, projetos and recursos, then sends the evening recap to Telegram."""
        try:
            result = await rysos_core.process_cockpit_free_notes()
        except Exception as e:
            print(f"[Scheduler] Erro ao processar notas livres: {e}")
            result = {"processed": 0}
        try:
            from rysos.telegram import telegram_bot
            from rysos.telegram.handlers import send_evening_recap
            if telegram_bot.is_configured() and settings.TELEGRAM_CHAT_ID:
                chat_id = settings.TELEGRAM_CHAT_ID
                if result.get("processed"):
                    pend = result.get("candidates") or 0
                    extra = f" — {pend} item(ns) na Triagem, confira o card abaixo" if pend else ""
                    await telegram_bot.send_message(
                        chat_id=chat_id, text=f"✍️ Notas livres do dia processadas{extra}."
                    )
                    if result.get("sha"):
                        from rysos.telegram.entity_review import push_meeting_review
                        await push_meeting_review(result["sha"], chat_id, note_label=result.get("triagem_stem"))
                await send_evening_recap(chat_id)
        except Exception as e:
            print(f"[Scheduler] Erro no job noturno: {e}")

    def start(self) -> None:
        """Starts the async scheduler."""
        if not self._is_running:
            self.setup_jobs()
            self.scheduler.start()
            self._is_running = True

    def stop(self) -> None:
        """Stops the scheduler."""
        if self._is_running:
            self.scheduler.shutdown()
            self._is_running = False


agent_scheduler = AgentScheduler()
