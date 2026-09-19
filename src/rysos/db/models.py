"""Database models for rysOS local tracking and deduplication."""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Text, DateTime, Boolean, Integer, JSON, Float, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SyncLog(Base):
    """Log of synchronization operations."""
    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(50))  # 'gmail', 'calendar', 'vault', 'all'
    status: Mapped[str] = mapped_column(String(20))  # 'success', 'error', 'running'
    items_count: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ProcessedEmail(Base):
    """Track processed emails to avoid duplicate note creation."""
    __tablename__ = "processed_emails"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)  # Gmail message/thread ID
    thread_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    subject: Mapped[str] = mapped_column(String(500))
    sender: Mapped[str] = mapped_column(String(255))
    date_received: Mapped[datetime] = mapped_column(DateTime)
    priority: Mapped[str] = mapped_column(String(10), default="baixa")  # 'alta' or 'baixa'
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    suggested_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_note_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ProcessedEvent(Base):
    """Track processed calendar events."""
    __tablename__ = "processed_events"

    id: Mapped[str] = mapped_column(String(150), primary_key=True)  # Google Calendar event ID
    summary: Mapped[str] = mapped_column(String(500))
    start_time: Mapped[datetime] = mapped_column(DateTime)
    end_time: Mapped[datetime] = mapped_column(DateTime)
    attendees: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meet_link: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    priority: Mapped[str] = mapped_column(String(10), default="baixa")  # 'alta' or 'baixa'
    created_note_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ProcessedTranscript(Base):
    """Track meeting transcripts already ingested from the watched Inbox folder.

    Keyed by the raw file's SHA-256 so a re-drop (Drive re-sync, manual copy) or a
    crash mid-pipeline never re-appends the same minutes to a meeting note.
    """
    __tablename__ = "processed_transcripts"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_filename: Mapped[str] = mapped_column(String(500))
    title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    meeting_note_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    attendees: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    action_items_count: Mapped[int] = mapped_column(Integer, default=0)
    candidates_count: Mapped[int] = mapped_column(Integer, default=0)
    decisions_count: Mapped[int] = mapped_column(Integer, default=0)
    area: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    projects_linked: Mapped[int] = mapped_column(Integer, default=0)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ProcessedDiaryEntry(Base):
    """Track spoken-diary transcripts already ingested from 07_Inbox_Agent/Diario.

    Same idempotency contract as ProcessedTranscript: keyed by the raw file's SHA-256
    (also mirrored as an in-note `<!-- transcript:<sha12> -->` marker) so a re-drop or a
    crash mid-pipeline never re-appends the same entry to the day's journal note.
    """
    __tablename__ = "processed_diary_entries"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_filename: Mapped[str] = mapped_column(String(500))
    title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    journal_note_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    entry_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD
    date_source: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # filename|date_hint|mtime
    action_items_count: Mapped[int] = mapped_column(Integer, default=0)
    candidates_count: Mapped[int] = mapped_column(Integer, default=0)
    decisions_count: Mapped[int] = mapped_column(Integer, default=0)
    area: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    projects_linked: Mapped[int] = mapped_column(Integer, default=0)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ProcessedCockpitNote(Base):
    """Tracks a Daily Cockpit's 'Notas Livres' free-write block already classified.

    Keyed by the block's own SHA-256 (not a file hash — there is no source file, the
    text lives inline in the Cockpit note) so a same-day resync or an evening-job retry
    never reprocesses unchanged text.
    """
    __tablename__ = "processed_cockpit_notes"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    cockpit_date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    action_items_count: Mapped[int] = mapped_column(Integer, default=0)
    candidates_count: Mapped[int] = mapped_column(Integer, default=0)
    decisions_count: Mapped[int] = mapped_column(Integer, default=0)
    projects_linked: Mapped[int] = mapped_column(Integer, default=0)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class EntityCandidate(Base):
    """A person / project / decision / resource mentioned in a transcript that is NOT an
    exact match to an existing vault note — it must be validated by the user before any
    note is created or linked. Also carries `kind='meeting_date'` flag rows for a meeting
    whose date could only be derived from the file's mtime.
    """
    __tablename__ = "entity_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transcript_sha: Mapped[str] = mapped_column(String(64), index=True)
    meeting_note_path: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(20))  # person | project | decision | resource | meeting_date
    surface_form: Mapped[str] = mapped_column(String(300))
    normalized_form: Mapped[str] = mapped_column(String(300))
    context_sentence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    suggested_matches_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # [{"name","score","path"}]
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | confirmed_new | linked | dismissed | duplicate
    resolved_to: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("transcript_sha", "kind", "normalized_form", name="uq_entcand_sha_kind_norm"),
    )


class DedupSuggestion(Base):
    """A single /dedup finding (a fuzzy-matched pair, a diary-mention orphan) with an
    AI-generated recommendation, awaiting the owner's sequential Telegram approval before
    anything is written to the vault — see rysos.vault.dedup_triage. Standalone rather
    than reusing EntityCandidate, which is tightly coupled to transcript_sha/
    meeting_note_path and doesn't fit non-transcript-derived findings."""
    __tablename__ = "dedup_suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(20))  # people | resources | decisions | diary_link — matches
    # DedupReport's own field names (dedup.py) except diary_link, which is a link-insertion
    # action, not a report field. Only "people"/"resources" are wired up to an interactive
    # Telegram flow so far (handlers.py); "decisions"/"diary_link" have no merge primitive yet.
    finding_json: Mapped[str] = mapped_column(Text)  # the raw scan_vault/diary-candidate entry
    ai_recommendation: Mapped[str] = mapped_column(String(30))  # merge | keep_separate | link_diary_mention
    ai_rationale: Mapped[str] = mapped_column(Text)
    proposed_survivor_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    proposed_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | approved | rejected | adjusted | applied
    chat_id: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class DecisionRecord(Base):
    """Track decisions across projects and executive governance."""
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)  # DEC-2026-001
    title: Mapped[str] = mapped_column(String(300))
    context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="em_analise")  # 'em_analise', 'decidido', 'bloqueado'
    priority: Mapped[str] = mapped_column(String(10), default="alta")  # 'alta' or 'baixa'
    area: Mapped[str] = mapped_column(String(100), default="Negocios_e_Governanca")
    related_projects: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decision_date: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    note_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class InboxAgentItem(Base):
    """Items discovered by AI awaiting human approval/triage."""
    __tablename__ = "inbox_agent_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(50))  # 'email_action', 'decision_needed', 'follow_up', 'new_project'
    priority: Mapped[str] = mapped_column(String(10), default="alta")  # 'alta' or 'baixa'
    description: Mapped[Text] = mapped_column(Text)
    source_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")  # 'pending', 'approved', 'dismissed'
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class TokenUsageLog(Base):
    """Tracks real AI token consumption and cost for observability and executive audit."""
    __tablename__ = "token_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model: Mapped[str] = mapped_column(String(100))
    operation: Mapped[str] = mapped_column(String(100))  # 'email_analysis', 'meeting_briefs', 'extract_decisions', 'chat_agent'
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    candidate_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class TelegramDraftSession(Base):
    """Tracks active conversational note creation and refinement in Telegram."""
    __tablename__ = "telegram_draft_sessions"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(30), default="drafting")  # 'drafting', 'awaiting_confirmation'
    category: Mapped[str] = mapped_column(String(50), default="idea")
    title: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extracted_fields_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    missing_fields_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    history_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pending_segments_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class AdaptiveConcept(Base):
    """Stores learned entities, topics, recurring patterns, and preferred mappings."""
    __tablename__ = "adaptive_concepts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    concept_type: Mapped[str] = mapped_column(String(50), default="entity")  # 'entity', 'topic', 'rule', 'emergent'
    term: Mapped[str] = mapped_column(String(200), index=True)
    preferred_category: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    preferred_area: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    is_emergent: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence_score: Mapped[float] = mapped_column(Float, default=1.0)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class AdaptiveFeedbackLog(Base):
    """Auditable log of classifications, AI reasoning, and user feedback/corrections."""
    __tablename__ = "adaptive_feedback_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    raw_input: Mapped[str] = mapped_column(Text)
    predicted_intent: Mapped[str] = mapped_column(String(50))
    predicted_category: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    corrected_intent: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    corrected_category: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    feedback_type: Mapped[str] = mapped_column(String(30), default="auto")  # 'auto', 'confirmed', 'corrected', 'cancelled'
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

