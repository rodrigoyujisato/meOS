"""Core Orchestration Engine for rysOS."""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from sqlalchemy import select, update, delete

from rysos.config import settings
from rysos.db import (
    get_db_session,
    init_db,
    SyncLog,
    ProcessedEmail,
    ProcessedEvent,
    ProcessedTranscript,
    ProcessedDiaryEntry,
    ProcessedCockpitNote,
    EntityCandidate,
    DecisionRecord,
    InboxAgentItem,
    AdaptiveConcept,
)
from rysos.vault.manager import VaultManager
from rysos.vault.triagem import write_triagem_note, apply_pending_triagem
from rysos.auth.google_auth import GoogleAuthManager, google_auth
from rysos.connectors.gmail import GmailConnector
from rysos.connectors.calendar import CalendarConnector
from rysos.connectors.transcripts import TranscriptConnector, _DIARY_INBOX_SUBDIR
from rysos.connectors.entity_resolver import EntityResolver, ResolvedEntity, AUTO_LINK, CANDIDATE
from rysos.ai.gemini import GeminiAIClient, gemini_ai

logger = logging.getLogger("rysos.core")

_IGNORED_EVENT_SUMMARIES = {"botd", "eotd", "hora do foco"}

_VALID_DECISION_STATUSES = ("em_analise", "decidido", "bloqueado", "descartado")


def _resolve_relative_due(due_text: str, reference: date) -> Optional[date]:
    """Resolves a meeting action item's `due` hint to a concrete date, only when doing
    so is unambiguous: an explicit ISO date, or "hoje"/"amanhã" — the meeting-transcript
    prompt (unlike the diary one) doesn't reliably convert relative dates itself. Vague
    hints ("antes de outubro", "próximas semanas") return None: not safe to route to a
    specific day, so the caller keeps the item on today's Cockpit, sorted last."""
    text = (due_text or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        pass
    low = text.lower()
    if low.startswith("hoje"):
        return reference
    if low.startswith("amanh"):  # covers "amanhã" and the unaccented "amanha"
        return reference + timedelta(days=1)
    return None


async def reconcile_decision_statuses(vault: VaultManager) -> int:
    """Pulls each decision note's `status` frontmatter into the DecisionRecord DB row.

    A human closes a decision by editing the note in 03_Decisions/, never through the bot,
    so DecisionRecord (insert-only otherwise) drifts stale. Called before anything reads
    `DecisionRecord.status` (daily sync, /decisoes) so the DB never lags the vault."""
    note_statuses = vault.scan_decision_statuses()
    if not note_statuses:
        return 0

    updated = 0
    async with get_db_session() as session:
        stmt = select(DecisionRecord.id, DecisionRecord.status)
        db_rows = (await session.execute(stmt)).all()
        for dec_id, db_status in db_rows:
            note_status = note_statuses.get(dec_id)
            if note_status and note_status in _VALID_DECISION_STATUSES and note_status != db_status:
                await session.execute(
                    update(DecisionRecord).where(DecisionRecord.id == dec_id).values(status=note_status)
                )
                updated += 1
    return updated


class RysOSCore:
    """Central engine that coordinates data sources, AI reasoning, and Obsidian vault updates."""

    def __init__(
        self,
        vault_manager: Optional[VaultManager] = None,
        auth_manager: Optional[GoogleAuthManager] = None,
        ai_client: Optional[GeminiAIClient] = None,
    ):
        self.vault = vault_manager or VaultManager()
        self.auth = auth_manager or google_auth
        self.ai = ai_client or gemini_ai
        self.gmail = GmailConnector(self.auth)
        self.calendar = CalendarConnector(self.auth)

    async def initialize(self) -> dict[str, Any]:
        """Initializes database and vault structures."""
        await init_db()
        vault_res = self.vault.initialize_vault_structure()
        return {
            "database": "initialized",
            "vault": vault_res,
            "google_auth": self.auth.is_authenticated(),
            "gemini_ready": self.ai.is_available(),
        }

    async def run_daily_sync(self, target_date: Optional[date] = None) -> dict[str, Any]:
        """Executes full synchronization: Calendar + Gmail -> AI Reasoning -> Daily Cockpit & Notes."""
        await self.initialize()
        target_date = target_date or date.today()
        items_count = 0
        sync_warnings = []

        # 1. Ingest Google Calendar Events
        events = []
        parsed_events_meta = []
        if self.auth.is_authenticated():
            try:
                events = self.calendar.fetch_events_for_date(target_date)
                # Auto-generated calendar noise (workplace-analytics tools routine blocks,
                # daily start/end markers) — never real meetings, so they never get a
                # note, an Agenda line, or a briefing pass.
                events = [
                    ev for ev in events
                    if ev.summary.strip().lower() not in _IGNORED_EVENT_SUMMARIES
                ]
                briefs = self.ai.generate_meeting_briefs(events)
                briefs_map = {b.get("id"): b for b in briefs if isinstance(b, dict)}

                async with get_db_session() as session:
                    for ev in events:
                        brief_info = briefs_map.get(ev.id, {})
                        raw_brief = brief_info.get("context_brief", ev.description or "")
                        if isinstance(raw_brief, list):
                            context_brief = " ".join(str(c) for c in raw_brief)
                        else:
                            context_brief = str(raw_brief)
                        priority = brief_info.get("priority", "baixa")

                        # Create Meeting note in Obsidian
                        meeting_path = self.vault.create_meeting_note(
                            summary=ev.summary,
                            start_time=ev.start_time,
                            end_time=ev.end_time,
                            attendees=ev.attendees,
                            meet_link=ev.meet_link,
                            context_brief=context_brief,
                            priority=priority,
                            event_id=ev.id,
                        )

                        # Persist event record in DB
                        db_event = await session.get(ProcessedEvent, ev.id)
                        if not db_event:
                            db_event = ProcessedEvent(
                                id=ev.id,
                                summary=ev.summary,
                                start_time=ev.start_time,
                                end_time=ev.end_time,
                                attendees=",".join(ev.attendees),
                                meet_link=ev.meet_link,
                                priority=priority,
                                created_note_path=str(meeting_path),
                            )
                            session.add(db_event)
                            items_count += 1

                        parsed_events_meta.append({
                            "summary": ev.summary,
                            "start_time": ev.start_time,
                            "end_time": ev.end_time,
                            "attendees_str": ", ".join(ev.attendees) if ev.attendees else "Apenas você",
                            "meet_link": ev.meet_link,
                            "context_brief": context_brief,
                            "meeting_note_name": meeting_path.name,
                        })
            except Exception as e:
                logger.warning(f"Erro ao buscar Google Calendar: {e}")
                sync_warnings.append(f"Google Calendar: {str(e)}")

        # 2. Ingest Gmail Messages
        actionable_emails = []
        today_bill_foci = []
        if self.auth.is_authenticated():
            try:
                raw_emails = self.gmail.fetch_recent_emails(max_results=35)
                analyzed_emails = self.ai.analyze_emails(raw_emails)
                emails_map = {e.id: e for e in raw_emails}

                # Collapses several emails that describe the *same* bill (a boleto plus its
                # own payment reminder, or copies from different sender relays) down to one
                # reminder + one digest line. Keyed on due date + creditor / sender, never
                # on the email id, which is distinct per message.
                seen_bill_keys: set[str] = set()

                async with get_db_session() as session:
                    for item in analyzed_emails:
                        if not isinstance(item, dict):
                            continue
                        email_id = item.get("id")
                        email_obj = emails_map.get(email_id)
                        if not email_obj:
                            continue

                        # Persist in DB. Gmail's personal-account query is `newer_than:7d`
                        # (not `is:unread`), so the same boleto e-mail keeps being returned
                        # for up to a week after it was first synced. Without this guard it
                        # was re-minted into "Focos do Dia" every morning — mislabeled
                        # "Vencimento HOJE" against its original (now stale) due date — even
                        # after the user paid it and logged that in the correct day's diary.
                        db_email = await session.get(ProcessedEmail, email_id)
                        already_seen = db_email is not None and db_email.processed_at.date() < target_date
                        if not db_email:
                            db_email = ProcessedEmail(
                                id=email_id,
                                thread_id=email_obj.thread_id,
                                subject=email_obj.subject,
                                sender=email_obj.sender,
                                date_received=email_obj.date,
                                priority=item.get("priority", "baixa"),
                                summary=item.get("summary", email_obj.snippet),
                                suggested_action=item.get("suggested_action", ""),
                            )
                            session.add(db_email)
                            items_count += 1
                        if already_seen:
                            continue

                        is_personal = getattr(email_obj, "account_type", "") == "pessoal"
                        is_bill = bool(item.get("is_bill"))

                        if is_personal and is_bill:
                            tag = "[Pessoal - Financeiro]"
                            item["related_area"] = "Patrimonio_e_Financas"
                            item["category"] = "pagamento"
                            item["priority"] = "alta"

                            due_date_str = item.get("due_date")
                            beneficiary = item.get("beneficiary") or email_obj.sender.split("<")[0].replace('"', '').strip() or "Boleto/Conta"
                            amount = item.get("amount")

                            _head = self.vault._normalize_for_match(beneficiary).split()
                            _bkeys = {
                                f"{due_date_str or 'nd'}|h|{_head[0]}" if _head else "",
                                f"{due_date_str or 'nd'}|s|{self.vault._sender_addr(email_obj.sender)}",
                            }
                            _bkeys.discard("")
                            if _bkeys & seen_bill_keys:
                                continue
                            seen_bill_keys |= _bkeys

                            parsed_due_date = None
                            if due_date_str:
                                try:
                                    parsed_due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
                                except Exception:
                                    parsed_due_date = None

                            # Every bill focus must be auditable back to the e-mail that
                            # produced it — "de onde veio isso?" must always have an answer
                            # (see 2026-09-14 incident: a misclassified condo statement
                            # became an untraceable "Pagar boleto: Inter" with no evidence).
                            source_note = (
                                f'Fonte: "{email_obj.subject}" — '
                                f"{email_obj.sender.split('<')[0].strip()}, "
                                f"{email_obj.date.strftime('%d/%m %H:%M')}"
                            )

                            if parsed_due_date:
                                if parsed_due_date == target_date:
                                    today_bill_foci.append({
                                        "title": f"💳 Pagar boleto: {beneficiary}",
                                        "area": "Patrimônio & Finanças",
                                        "description": f"Valor: {amount or 'A consultar'} — Vencimento HOJE ({due_date_str}) — {source_note}",
                                    })
                                    item["suggested_action"] = f"Pagar HOJE ({due_date_str}): {beneficiary} ({amount or 'valor pendente'})"
                                elif parsed_due_date < target_date:
                                    today_bill_foci.append({
                                        "title": f"💳 Pagar boleto: {beneficiary}",
                                        "area": "Patrimônio & Finanças",
                                        "description": f"Valor: {amount or 'A consultar'} — VENCIDO em {due_date_str} — {source_note}",
                                    })
                                    item["suggested_action"] = f"Pagar (VENCIDO em {due_date_str}): {beneficiary} ({amount or 'valor pendente'})"
                                else:
                                    self.vault.schedule_bill_reminder(
                                        due_date=parsed_due_date,
                                        beneficiary=beneficiary,
                                        amount=amount,
                                        sender=email_obj.sender,
                                    )
                                    item["suggested_action"] = f"Agendado lembrete para {parsed_due_date.strftime('%d/%m/%Y')}: {beneficiary} ({amount or 'valor pendente'})"
                            else:
                                today_bill_foci.append({
                                    "title": f"💳 Pagar boleto: {beneficiary}",
                                    "area": "Patrimônio & Finanças",
                                    "description": f"Valor: {amount or 'A consultar'} — Conferir data de vencimento — {source_note}",
                                })
                            # A bill already gets its own Focos-do-Dia entry (or a forward
                            # reminder) above — appending it to actionable_emails too would
                            # just restate the same action under "Ações & Contas Críticas".
                            continue
                        else:
                            tag = "[Corporativo]" if getattr(email_obj, "account_type", "") == "corporativo" else "[Pessoal]"

                        item["sender"] = email_obj.sender
                        item["subject"] = f"{tag} {email_obj.subject}"

                        if item.get("priority") == "alta":
                            actionable_emails.append(item)
            except Exception as e:
                logger.warning(f"Erro ao buscar Gmail: {e}")
                sync_warnings.append(f"Gmail: {str(e)}")

        # 3. Retrieve Pending Decisions from DB
        try:
            await reconcile_decision_statuses(self.vault)
        except Exception as e:
            logger.warning(f"Erro ao reconciliar status de decisões: {e}")
            sync_warnings.append(f"Decisões: {str(e)}")

        pending_decisions = []
        decision_note_names = self.vault.scan_decision_note_names()
        async with get_db_session() as session:
            stmt = select(DecisionRecord).where(DecisionRecord.status.in_(("em_analise", "bloqueado")))
            db_decs = (await session.execute(stmt)).scalars().all()
            for d in db_decs:
                pending_decisions.append({
                    "id": d.id,
                    "title": d.title,
                    "priority": d.priority,
                    "area": d.area,
                    "status": d.status,
                    # The real filename (`{id} - {title}.md`), never just the id — a
                    # wikilink built from the id alone resolves to nothing and Obsidian
                    # creates a blank note on click. Falls back to the id only if the
                    # note vanished between reconciliation and this scan.
                    "note_name": decision_note_names.get(d.id, d.id),
                })

        # 4. Generate Daily Cockpit Note
        cockpit_path = self.vault.create_daily_cockpit(
            target_date=target_date,
            calendar_events=parsed_events_meta,
            actionable_emails=actionable_emails,
            pending_decisions=pending_decisions,
            high_priority_foci=today_bill_foci,
        )

        # 5. Ingest any manually dropped meeting transcripts
        transcripts_result = {"processed": 0}
        try:
            transcripts_result = await self.process_pending_transcripts(target_date=target_date)
        except Exception as e:
            logger.warning(f"Erro ao processar transcrições: {e}")
            sync_warnings.append(f"Transcrições: {str(e)}")

        # 5b. Ingest any spoken-diary entries (07_Inbox_Agent/Diario)
        diary_result = {"processed": 0}
        try:
            diary_result = await self.process_pending_diary(target_date=target_date)
        except Exception as e:
            logger.warning(f"Erro ao processar diário: {e}")
            sync_warnings.append(f"Diário: {str(e)}")

        # 5c. Prune meeting notes that never got a transcript or a hand-typed note
        pruned_meetings: list[Path] = []
        try:
            pruned_meetings = self.vault.archive_stale_meeting_notes(settings.MEETING_PRUNE_HOURS)
            if pruned_meetings:
                logger.info("Poda de reuniões: %d nota(s) sem ata arquivada(s).", len(pruned_meetings))
        except Exception as e:
            logger.warning(f"Erro ao podar notas de reunião: {e}")
            sync_warnings.append(f"Poda de reuniões: {str(e)}")

        # 5d. Archive decisions marked `descartado` and drop their DB row
        try:
            archived_decisions = self.vault.archive_garbage_decisions()
            if archived_decisions:
                async with get_db_session() as session:
                    for dec_id, _dest in archived_decisions:
                        await session.execute(delete(DecisionRecord).where(DecisionRecord.id == dec_id))
                    await session.commit()
                logger.info("Decisões descartadas: %d arquivada(s).", len(archived_decisions))
        except Exception as e:
            logger.warning(f"Erro ao arquivar decisões descartadas: {e}")
            sync_warnings.append(f"Arquivamento de decisões: {str(e)}")

        # 6. Log sync in DB
        async with get_db_session() as session:
            log = SyncLog(
                source="all",
                status="success" if not sync_warnings else "partial_success",
                items_count=items_count,
                details=f"Sincronização executada. Avisos: {'; '.join(sync_warnings)}" if sync_warnings else "Sucesso total.",
            )
            session.add(log)

        return {
            "status": "success",
            "target_date": str(target_date),
            "cockpit_file": str(cockpit_path),
            "events_count": len(events),
            "actionable_emails_count": len(actionable_emails),
            "pending_decisions_count": len(pending_decisions),
            "transcripts_processed": transcripts_result.get("processed", 0),
            "diary_processed": diary_result.get("processed", 0),
            "meetings_pruned": len(pruned_meetings),
            "warnings": sync_warnings,
        }

    @staticmethod
    def _resolve_meeting_date(tf, rich: dict[str, Any], target_date: date) -> tuple[date, str]:
        """Meeting date, best source first: filename → explicit in-transcript date → file mtime.
        Always clamped to not-future. Returns (date, source) where source is
        'filename' | 'date_hint' | 'mtime'.

        A model `date_hint` equal to today or the file's mtime is discarded as a likely
        echo of the prompt's reference date — the mtime fallback lands on the same day
        anyway and reports the honest 'mtime' source (which flags the date for review).
        """
        mt = date.fromtimestamp(tf.path.stat().st_mtime) if tf.path.exists() else target_date
        if tf.date_hint:
            return min(tf.date_hint, target_date), "filename"
        dh = rich.get("date_hint") or ""
        if dh:
            try:
                d = datetime.strptime(dh, "%Y-%m-%d").date()
                if d not in (target_date, mt):
                    return min(d, target_date), "date_hint"
            except ValueError:
                pass
        return min(mt, target_date), "mtime"

    async def _learned_person_links(self) -> dict[str, str]:
        """{normalized surface form -> vault path} for people the user has already
        confirmed via a prior resolution (AdaptiveConcept, concept_type='entity',
        metadata_json.resolved_path). A concept whose target note no longer exists on
        disk is dropped — a stale learned link must never auto-link (see memory rules)."""
        out: dict[str, str] = {}
        async with get_db_session() as session:
            rows = (await session.execute(
                select(AdaptiveConcept).where(AdaptiveConcept.concept_type == "entity")
            )).scalars().all()
        for c in rows:
            try:
                path = (json.loads(c.metadata_json or "{}") or {}).get("resolved_path")
            except (ValueError, TypeError):
                continue
            if path and Path(path).exists():
                out[c.term.strip().lower()] = path
        return out

    async def _persist_candidates(self, session, sha: str, meeting_path: str,
                                  resolved: list, *, date_unconfirmed: bool, date_str: str) -> int:
        """Insert one EntityCandidate per CANDIDATE-disposition entity (idempotent on the
        (sha, kind, normalized_form) unique key) plus one kind='meeting_date' flag row when
        the meeting date could only be derived from the file mtime. Returns rows created."""
        rows = [
            {
                "kind": r.kind,
                "surface_form": r.surface_form,
                "normalized_form": r.normalized_form,
                "context_sentence": r.context_sentence or None,
                "payload_json": json.dumps(r.payload, ensure_ascii=False) if r.payload else None,
                "suggested_matches_json": json.dumps(r.suggested_matches, ensure_ascii=False) if r.suggested_matches else None,
            }
            for r in resolved if r.disposition == CANDIDATE
        ]
        if date_unconfirmed:
            rows.append({
                "kind": "meeting_date", "surface_form": date_str, "normalized_form": date_str,
                "context_sentence": "Data derivada da data do arquivo, não confirmada na transcrição.",
                "payload_json": None, "suggested_matches_json": None,
            })

        created = 0
        for r in rows:
            exists = (await session.execute(
                select(EntityCandidate.id).where(
                    EntityCandidate.transcript_sha == sha,
                    EntityCandidate.kind == r["kind"],
                    EntityCandidate.normalized_form == r["normalized_form"],
                )
            )).first()
            if exists:
                continue
            session.add(EntityCandidate(transcript_sha=sha, meeting_note_path=meeting_path, status="pending", **r))
            created += 1
        return created

    async def process_pending_transcripts(self, target_date: Optional[date] = None) -> dict[str, Any]:
        """Scans 07_Inbox_Agent/Transcricoes for manually dropped meeting transcripts, turns
        each into a rich structured ata via Gemini, files it into the matching 04_Meetings note
        (creating it if the meeting was never on the calendar), and — WITHOUT creating any
        Person/Project/Decision/Resource note — records every non-exact entity as a pending
        EntityCandidate for the user to validate, mirrored into a Triagem note. Action items
        and near-dated deadlines go to the day's Cockpit; the raw file is archived.
        Idempotent on the file SHA-256 (ProcessedTranscript row + in-note transcript marker).
        """
        await init_db()
        target_date = target_date or date.today()
        connector = TranscriptConnector(self.vault.vault_path)

        # First: apply any Triagem notes the owner resolved by hand since the last scan
        # (parse-back). Cheap — only notes still `status: pending` with edited lines.
        try:
            done = await apply_pending_triagem(self.vault)
            if done:
                logger.info("Triagem: %d candidato(s) resolvidos via nota antes do scan.", done)
        except Exception as e:
            logger.warning("Falha ao aplicar resoluções de Triagem: %s", e)

        try:
            pending = connector.scan()
        except FileNotFoundError:
            return {"processed": 0, "notes": []}

        processed: list[dict[str, Any]] = []
        # Built once per batch: nothing in the loop writes AdaptiveConcept rows or vault
        # notes for entities, so the learned map and the match indexes can't change
        # mid-scan, and Path.exists()/list_* on the Drive mount are slow.
        learned_links = await self._learned_person_links()
        people_index = self.vault.list_people()
        projects_index = self.vault.list_projects()
        for tf in pending:
            async with get_db_session() as session:
                if await session.get(ProcessedTranscript, tf.sha256):
                    connector.move_to_processed(tf)  # self-heal: filed but not archived
                    continue

            raw = tf.raw_text
            if len(raw) > settings.TRANSCRIPT_MAX_CHARS:
                raw = raw[: settings.TRANSCRIPT_MAX_CHARS] + "\n\n[...transcrição truncada...]"

            mtime_date = date.fromtimestamp(tf.path.stat().st_mtime) if tf.path.exists() else target_date
            rich = await asyncio.to_thread(
                self.ai.summarize_meeting_transcript, raw, tf.filename,
                mtime_date=mtime_date, today=target_date,
            )

            title = (rich.get("title") or tf.path.stem).strip()[:120] or tf.path.stem
            slug = (self.vault._slugify(title)[:80]).strip() or "Reuniao sem titulo"
            m_date, date_source = self._resolve_meeting_date(tf, rich, target_date)
            date_str = m_date.strftime("%Y-%m-%d")
            date_unconfirmed = date_source == "mtime"

            area_slug, area_title = ("", "")
            if rich.get("para_area"):
                area_slug, area_title = self.vault._resolve_area(rich["para_area"])
            area_link = f"[[02_Areas/{area_slug}|{area_title}]]" if area_slug else ""

            resolved = EntityResolver(self.vault).resolve(
                rich, raw_text=raw,
                people_index=people_index, projects_index=projects_index,
                learned=learned_links,
            )
            resolved_names = {
                r.normalized_form: r.link_target for r in resolved
                if r.disposition == AUTO_LINK and r.link_target
            }
            # names that still need validation get the ⚠️ tag in the ata; one-shot
            # role-less mentions (DROP) render as plain text, no tag.
            pending_person_names = {
                r.normalized_form for r in resolved
                if r.kind == "person" and r.disposition == CANDIDATE
            }
            linked_projects = [r for r in resolved if r.kind == "project" and r.disposition == AUTO_LINK]

            # Extraction was attempted and produced nothing usable: surface it as a
            # pending candidate + a Triagem banner so the near-empty note doesn't land
            # silently and stay processed forever (the raw file is about to be archived
            # and re-dropping it is a no-op on the same SHA).
            degraded = bool(rich.get("_degraded"))
            if degraded:
                resolved.append(ResolvedEntity(
                    kind="extraction_failed",
                    surface_form=title,
                    normalized_form="extraction_failed",
                    context_sentence=(rich.get("summary_bullets") or [""])[0],
                    payload={}, disposition=CANDIDATE,
                ))

            start_dt = datetime.combine(m_date, time(0, 0))
            # Prefer the real same-day calendar note (matched by attendee overlap) over the
            # AI-derived title's slug — a content-derived title almost never matches the
            # calendar invite's own title, so slug-only matching spawns an orphan note
            # instead of filing into the pre-existing one (2026-09-15 incident).
            matched_path = self.vault.find_matching_calendar_note(
                date_str, rich.get("attendees") or [], people_index=people_index,
                filename_stem=tf.path.stem,
            )
            meeting_path = self.vault.create_meeting_note(
                summary=title,
                start_time=start_dt,
                end_time=start_dt,
                attendees=[],  # unresolved names must not become frontmatter wikilinks
                context_brief="Nota criada a partir de transcrição enviada manualmente.",
                priority="baixa",
                related_projects=[Path(r.link_target).stem for r in linked_projects],
                area_link=area_link,
                topics=rich.get("topics_tags") or [],
                create_only=True,  # never re-render a pre-existing calendar note's frontmatter
                override_path=matched_path,
            )

            # Minutes + marker FIRST, then archive, then the DB row: a crash in between is
            # recoverable — the marker short-circuits the re-append and the rest completes.
            rel_archived = connector.processed_dir.joinpath(tf.path.name)
            try:
                source_link = f"[[{rel_archived.relative_to(self.vault.vault_path).as_posix()}]]"
            except ValueError:
                source_link = f"Fonte: {tf.filename}"

            wrote = self.vault.append_transcript_minutes(
                meeting_path, tf.sha256,
                title=title, rich=rich, resolved_names=resolved_names,
                unconfirmed_names=pending_person_names,
                source_link=source_link, meeting_date=date_str,
            )

            async with get_db_session() as session:
                candidates_count = await self._persist_candidates(
                    session, tf.sha256, str(meeting_path), resolved,
                    date_unconfirmed=date_unconfirmed, date_str=date_str,
                )

            connector.move_to_processed(tf)

            action_items = rich.get("action_items") or []
            for it in action_items:
                text = (it.get("text") or "").strip()
                if not text:
                    continue
                resolved_due = _resolve_relative_due(it.get("due") or "", target_date)
                # A future due date belongs on that day's Cockpit, not today's — the
                # Daily Cockpit shows exclusively today's pendências (overdue included).
                # Unresolved/vague due hints stay on today's list, sorted to the end.
                focus_date = resolved_due if (resolved_due and resolved_due > target_date) else target_date
                self.vault.add_cockpit_focus(
                    target_date=focus_date,
                    title=f"🗣️ {text}",
                    area="Follow-up de reunião",
                    description=f"De: {title}"
                    + (f" — resp. {it['owner']}" if it.get("owner") else "")
                    + (f" (prazo: {it['due']})" if it.get("due") else ""),
                    dedupe_key=f"🗣️ {text}",
                    due_date=resolved_due,
                )
            for dl in rich.get("deadlines") or []:
                when, what = (dl.get("when") or "").strip(), (dl.get("what") or "").strip()
                if not what:
                    continue
                try:
                    d = datetime.strptime(when, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if target_date <= d <= target_date + timedelta(days=14):
                    self.vault.add_cockpit_focus(
                        target_date=target_date, title=f"📅 {what}",
                        area="Prazo de reunião", description=f"De: {title} (prazo {when})",
                        dedupe_key=f"📅 {what}",
                    )

            async with get_db_session() as session:
                session.add(ProcessedTranscript(
                    sha256=tf.sha256,
                    source_filename=tf.filename,
                    title=title,
                    meeting_note_path=str(meeting_path),
                    attendees=None,
                    action_items_count=len(action_items),
                    candidates_count=candidates_count,
                    decisions_count=len(rich.get("decisions") or []),
                    area=area_slug or None,
                    projects_linked=len(linked_projects),
                ))

            try:
                write_triagem_note(
                    self.vault, meeting_path, title, date_str, resolved,
                    transcript_sha=tf.sha256, date_unconfirmed=date_unconfirmed,
                    extraction_degraded=degraded,
                )
            except Exception as e:
                logger.warning(f"Falha ao escrever nota de triagem para {meeting_path.name}: {e}")

            processed.append({
                "title": title,
                "sha": tf.sha256,
                "meeting_note": meeting_path.name,
                "candidates": candidates_count,
                "action_items": len(action_items),
                "area": area_slug or None,
                "date_source": date_source,
                "appended": wrote,
            })
            logger.info(
                "Transcrição processada: %s -> %s (%d candidatos, %d ações, área %s, data via %s)",
                tf.filename, meeting_path.name, candidates_count, len(action_items),
                area_slug or "?", date_source,
            )

        return {"processed": len(processed), "notes": processed}

    async def process_pending_diary(self, target_date: Optional[date] = None) -> dict[str, Any]:
        """Scans 07_Inbox_Agent/Diario for spoken-diary transcripts (free first-person
        narration produced by an external speech-to-text step), structures each with Gemini, and
        files it into the day's 09_Journal note — preserving the narration verbatim.
        Auto-fills the unambiguous (links to existing People/Projects, action items and
        near-dated deadlines → the day's Cockpit) and mirrors every non-exact entity into
        a per-entry Triagem note. Never creates a Person/Project/Decision note directly.
        Idempotent on the file SHA-256 (ProcessedDiaryEntry row + in-note transcript
        marker). Runs after `process_pending_transcripts` in the same scan job, which
        already applied any hand-resolved Triagem notes — so this does not re-run
        `apply_pending_triagem`.
        """
        await init_db()
        target_date = target_date or date.today()
        connector = TranscriptConnector(
            self.vault.vault_path,
            inbox_subdir=_DIARY_INBOX_SUBDIR,
            min_chars=settings.DIARY_MIN_CHARS,
        )

        try:
            pending = connector.scan()
        except FileNotFoundError:
            return {"processed": 0, "notes": []}

        processed: list[dict[str, Any]] = []
        learned_links = await self._learned_person_links()
        people_index = self.vault.list_people()
        projects_index = self.vault.list_projects()
        for df in pending:
            async with get_db_session() as session:
                if await session.get(ProcessedDiaryEntry, df.sha256):
                    connector.move_to_processed(df)  # self-heal: filed but not archived
                    continue

            raw = df.raw_text
            if len(raw) > settings.DIARY_MAX_CHARS:
                raw = raw[: settings.DIARY_MAX_CHARS] + "\n\n[...entrada truncada...]"

            mtime_date = date.fromtimestamp(df.path.stat().st_mtime) if df.path.exists() else target_date
            rich = await asyncio.to_thread(
                self.ai.summarize_diary_entry, raw, df.filename,
                entry_date=mtime_date, today=target_date,
            )
            # Temporary instrumentation for the diary-quality gap (Nadia Lobato case,
            # 2026-09-17): a person mentioned in the raw narration can be missing from
            # this structured extraction's own `people[]`, and once that happens the raw
            # Gemini output is gone — there's nothing to inspect after the fact. Logging
            # it here means the next occurrence can actually be diagnosed instead of
            # guessed at. Remove once the gap is confirmed and fixed (or ruled out).
            logger.info(
                "[Diário] extração bruta do Gemini — arquivo=%s sha=%s: %s",
                df.filename, df.sha256[:12], json.dumps(rich, ensure_ascii=False),
            )

            title = (rich.get("title") or df.path.stem).strip()[:120] or df.path.stem
            entry_dt, date_source = self._resolve_meeting_date(df, rich, target_date)
            date_str = entry_dt.strftime("%Y-%m-%d")
            # A per-day journal note must never be renamed, so a genuinely unknown date is
            # surfaced as an open question, NOT as a renamable meeting_date candidate.
            # `_resolve_meeting_date` reports "mtime" both when the model gave no date AND
            # when it gave one equal to today/mtime (discarded as a prompt echo). For a
            # same-day diary that echo IS the real date, so only nag when the model spoke
            # no parseable date of its own.
            model_dh = (rich.get("date_hint") or "").strip()
            model_gave_date = False
            if model_dh:
                try:
                    datetime.strptime(model_dh, "%Y-%m-%d")
                    model_gave_date = True
                except ValueError:
                    pass
            if date_source == "mtime" and not model_gave_date:
                rich.setdefault("open_questions", []).append(
                    f"De que dia é esta entrada? (derivada da data do arquivo: {date_str})"
                )

            area_slug = ""
            if rich.get("para_area"):
                area_slug = self.vault._resolve_area(rich["para_area"])[0]

            resolved = EntityResolver(self.vault).resolve(
                rich, raw_text=raw,
                people_index=people_index, projects_index=projects_index,
                learned=learned_links,
            )
            resolved_names = {
                r.normalized_form: r.link_target for r in resolved
                if r.disposition == AUTO_LINK and r.link_target
            }
            pending_person_names = {
                r.normalized_form for r in resolved
                if r.kind == "person" and r.disposition == CANDIDATE
            }
            linked_projects = [r for r in resolved if r.kind == "project" and r.disposition == AUTO_LINK]

            degraded = bool(rich.get("_degraded"))
            if degraded:
                resolved.append(ResolvedEntity(
                    kind="extraction_failed",
                    surface_form=title,
                    normalized_form="extraction_failed",
                    context_sentence=(rich.get("summary_bullets") or [""])[0],
                    payload={}, disposition=CANDIDATE,
                ))

            journal_path = self.vault.ensure_journal_note(entry_dt)

            rel_archived = connector.processed_dir.joinpath(df.path.name)
            try:
                source_link = f"[[{rel_archived.relative_to(self.vault.vault_path).as_posix()}]]"
            except ValueError:
                source_link = f"Fonte: {df.filename}"

            # Block + marker FIRST, then archive, then the DB row (crash-recoverable).
            wrote = self.vault.append_diary_entry(
                journal_path, df.sha256,
                title=title, rich=rich, resolved_names=resolved_names,
                unconfirmed_names=pending_person_names,
                raw_narration=raw, source_link=source_link, entry_date=date_str,
                tone=rich.get("meeting_type", ""),
            )

            async with get_db_session() as session:
                candidates_count = await self._persist_candidates(
                    session, df.sha256, str(journal_path), resolved,
                    date_unconfirmed=False, date_str=date_str,
                )

            connector.move_to_processed(df)

            action_items = rich.get("action_items") or []
            for it in action_items:
                text = (it.get("text") or "").strip()
                if not text:
                    continue
                self.vault.add_cockpit_focus(
                    target_date=target_date,
                    title=f"📓 {text}",
                    area="Diário",
                    description=f"De: {title}"
                    + (f" — resp. {it['owner']}" if it.get("owner") else "")
                    + (f" (prazo: {it['due']})" if it.get("due") else ""),
                    dedupe_key=f"📓 {text}",
                )
            for dl in rich.get("deadlines") or []:
                when, what = (dl.get("when") or "").strip(), (dl.get("what") or "").strip()
                if not what:
                    continue
                try:
                    d = datetime.strptime(when, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if target_date <= d <= target_date + timedelta(days=14):
                    self.vault.add_cockpit_focus(
                        target_date=target_date, title=f"📅 {what}",
                        area="Prazo (diário)", description=f"De: {title} (prazo {when})",
                        dedupe_key=f"📅 {what}",
                    )

            async with get_db_session() as session:
                session.add(ProcessedDiaryEntry(
                    sha256=df.sha256,
                    source_filename=df.filename,
                    title=title,
                    journal_note_path=str(journal_path),
                    entry_date=date_str,
                    date_source=date_source,
                    action_items_count=len(action_items),
                    candidates_count=candidates_count,
                    decisions_count=len(rich.get("decisions") or []),
                    area=area_slug or None,
                    projects_linked=len(linked_projects),
                ))

            triagem_stem = f"Diario {date_str} [{df.sha256[:8]}]"
            try:
                write_triagem_note(
                    self.vault, journal_path, title, date_str, resolved,
                    transcript_sha=df.sha256, extraction_degraded=degraded,
                    note_stem=triagem_stem, source_folder="09_Journal",
                )
            except Exception as e:
                logger.warning(f"Falha ao escrever nota de triagem do diário para {journal_path.name}: {e}")

            processed.append({
                "title": title,
                "sha": df.sha256,
                "journal_note": journal_path.name,
                "triagem_stem": triagem_stem,
                "candidates": candidates_count,
                "open_questions": len(rich.get("open_questions") or []),
                "action_items": len(action_items),
                "area": area_slug or None,
                "date_source": date_source,
                "appended": wrote,
            })
            logger.info(
                "Entrada de diário processada: %s -> %s (%d candidatos, %d ações, área %s, data via %s)",
                df.filename, journal_path.name, candidates_count, len(action_items),
                area_slug or "?", date_source,
            )

        return {"processed": len(processed), "notes": processed}

    async def process_cockpit_free_notes(self, target_date: Optional[date] = None) -> dict[str, Any]:
        """Reads the '✍️ Notas Livres do Dia' free-write block from a Daily Cockpit note
        and, if it holds unprocessed content, structures it with Gemini exactly like a
        spoken-diary entry (same canonical shape, same EntityResolver/Triagem flow).
        Action items land straight on the Cockpit; decisions/projects/resources always go
        through Triagem for human approval — never created directly. Idempotent on the
        block's own SHA-256 (`ProcessedCockpitNote`), so a same-day resync or an
        evening-job retry never reprocesses unchanged text.
        """
        await init_db()
        target_date = target_date or date.today()
        date_str = target_date.strftime("%Y-%m-%d")
        cockpit_file = self.vault.vault_path / "00_Cockpit" / "Daily" / f"{date_str}.md"
        content = self.vault.read_file(cockpit_file)
        if not content:
            return {"processed": 0}
        raw = self.vault.extract_free_notes(content)
        if not raw:
            return {"processed": 0}

        sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        async with get_db_session() as session:
            if await session.get(ProcessedCockpitNote, sha):
                return {"processed": 0, "already_done": True}

        await apply_pending_triagem(self.vault)  # reconcile before adding new candidates

        rich = await asyncio.to_thread(
            self.ai.summarize_diary_entry, raw, f"Notas Livres {date_str}",
            entry_date=target_date, today=target_date,
        )

        learned_links = await self._learned_person_links()
        resolved = EntityResolver(self.vault).resolve(
            rich, raw_text=raw,
            people_index=self.vault.list_people(), projects_index=self.vault.list_projects(),
            learned=learned_links,
        )
        linked_projects = [r for r in resolved if r.kind == "project" and r.disposition == AUTO_LINK]

        async with get_db_session() as session:
            candidates_count = await self._persist_candidates(
                session, sha, str(cockpit_file), resolved, date_unconfirmed=False, date_str=date_str,
            )

        action_items = rich.get("action_items") or []
        for it in action_items:
            text = (it.get("text") or "").strip()
            if not text:
                continue
            self.vault.add_cockpit_focus(
                target_date=target_date,
                title=f"✍️ {text}",
                area="Notas Livres",
                description=f"De: notas livres de {date_str}"
                + (f" — resp. {it['owner']}" if it.get("owner") else "")
                + (f" (prazo: {it['due']})" if it.get("due") else ""),
                dedupe_key=f"✍️ {text}",
            )
        for dl in rich.get("deadlines") or []:
            when, what = (dl.get("when") or "").strip(), (dl.get("what") or "").strip()
            if not what:
                continue
            try:
                d = datetime.strptime(when, "%Y-%m-%d").date()
            except ValueError:
                continue
            if target_date <= d <= target_date + timedelta(days=14):
                self.vault.add_cockpit_focus(
                    target_date=target_date, title=f"📅 {what}", area="Prazo (notas livres)",
                    description=f"De: notas livres de {date_str} (prazo {when})",
                    dedupe_key=f"📅 {what}",
                )

        triagem_stem = f"Notas Livres {date_str} [{sha[:8]}]"
        try:
            write_triagem_note(
                self.vault, cockpit_file, f"Notas Livres — {date_str}", date_str, resolved,
                transcript_sha=sha, note_stem=triagem_stem, source_folder="00_Cockpit/Daily",
            )
        except Exception as e:
            logger.warning(f"Falha ao escrever nota de triagem das notas livres de {date_str}: {e}")

        async with get_db_session() as session:
            session.add(ProcessedCockpitNote(
                sha256=sha, cockpit_date=date_str,
                action_items_count=len(action_items), candidates_count=candidates_count,
                decisions_count=len(rich.get("decisions") or []),
                projects_linked=len(linked_projects),
            ))

        logger.info(
            "Notas livres do Cockpit processadas: %s (%d candidatos, %d ações)",
            date_str, candidates_count, len(action_items),
        )
        return {
            "processed": 1, "sha": sha, "candidates": candidates_count,
            "action_items": len(action_items), "triagem_stem": triagem_stem,
        }

    async def get_dashboard_summary(self) -> dict[str, Any]:
        """Gathers summary statistics and active items for the web dashboard."""
        await self.initialize()

        today = date.today()
        today_str = today.strftime("%Y-%m-%d")
        daily_cockpit_file = self.vault.vault_path / "00_Cockpit" / "Daily" / f"{today_str}.md"
        cockpit_content = self.vault.read_file(daily_cockpit_file) if daily_cockpit_file.exists() else None

        projects = self.vault.list_projects()

        async with get_db_session() as session:
            # Decisions
            dec_stmt = select(DecisionRecord).order_by(DecisionRecord.decision_date.desc()).limit(10)
            decisions = (await session.execute(dec_stmt)).scalars().all()

            # Inbox items
            inbox_stmt = select(InboxAgentItem).where(InboxAgentItem.status == "pending").limit(10)
            inbox_items = (await session.execute(inbox_stmt)).scalars().all()

        return {
            "today": today_str,
            "vault_path": str(self.vault.vault_path),
            "cockpit_exists": daily_cockpit_file.exists(),
            "cockpit_content": cockpit_content,
            "google_authenticated": self.auth.is_authenticated(),
            "gemini_available": self.ai.is_available(),
            "projects": projects,
            "decisions": [
                {
                    "id": d.id,
                    "title": d.title,
                    "status": d.status,
                    "priority": d.priority,
                    "area": d.area,
                    "date": d.decision_date.strftime("%Y-%m-%d"),
                }
                for d in decisions
            ],
            "inbox_items": [
                {
                    "id": item.id,
                    "title": item.title,
                    "category": item.category,
                    "priority": item.priority,
                    "description": item.description,
                }
                for item in inbox_items
            ],
        }


rysos_core = RysOSCore()
