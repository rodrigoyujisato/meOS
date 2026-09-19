"""Configuration settings for rysOS."""

from pathlib import Path
from typing import Annotated, Any, Optional
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from pydantic import field_validator

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Base directories
    BASE_DIR: Path = BASE_DIR
    DATA_DIR: Path = BASE_DIR / "data"

    # Obsidian vault folder (create it, or point this at an existing vault; `rysos init-vault`
    # builds the PARA structure inside it).
    OBSIDIAN_VAULT_PATH: Path = Path.home() / "Obsidian" / "rysos-vault"

    # Google Gemini AI
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"
    # Cheaper tier for deterministic/operational calls (temp-0 classification, fixed-schema
    # extraction): intent classification, event field extraction, meeting briefs, quick
    # capture refinement. Everything quality-sensitive (transcription, image/document
    # reading, transcript/diary summarization, conversational drafting, open chat) stays on
    # GEMINI_MODEL. analyze_emails/extract_decisions also stay on GEMINI_MODEL for now,
    # pending an A/B check before moving them to this tier too.
    GEMINI_MODEL_LITE: str = "gemini-3.1-flash-lite"

    # Who the assistant works for. USER_EMAILS: every Google account the owner uses (comma-separated
    # in .env). It is used to recognise the owner among meeting attendees.
    USER_NAME: str = ""
    USER_ROLE: str = "executivo"
    USER_EMAILS: Annotated[list[str], NoDecode] = []
    # Accounts on these domains are treated as "pessoal" (personal); any other domain is
    # "corporativo". Personal accounts also get the bills/invoices e-mail rule.
    PERSONAL_EMAIL_DOMAINS: Annotated[list[str], NoDecode] = [
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "icloud.com", "yahoo.com",
    ]

    # Gmail query filter (Ignores social, promo, spam, updates and automated senders)
    GMAIL_QUERY_FILTER: str = (
        "category:primary (is:unread OR is:starred) "
        "-from:(noreply OR no-reply OR notifications OR notification OR alert OR alerts OR newsletter OR billing OR news OR automated OR mailer-daemon OR digest OR promo) "
        "newer_than:3d"
    )

    # Google Workspace OAuth
    GOOGLE_CREDENTIALS_PATH: Path = BASE_DIR / "credentials.json"
    GOOGLE_TOKEN_PATH: Path = BASE_DIR / "token.json"
    GOOGLE_SCOPES: list[str] = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/calendar.events",
    ]

    # IANA timezone used to build Google Calendar event bodies (RFC3339 dateTime + explicit
    # timeZone, never a naive "Z"-suffixed value — the box runs America/Sao_Paulo, not UTC).
    TIMEZONE: str = "America/Sao_Paulo"

    # Web & PWA Server
    # The web UI has no authentication: keep it on localhost unless you put your own access
    # control (VPN, tunnel with auth) in front of it.
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # Scheduled Routines
    MORNING_BRIEF_TIME: str = "07:00"
    EVENING_RECAP_TIME: str = "19:00"

    # Meeting transcript ingestion (watched folder in 07_Inbox_Agent/Transcricoes)
    TRANSCRIPT_SCAN_INTERVAL_MIN: int = 15
    # One Gemini pass covers essentially every real meeting; flash-lite has ~1M token
    # context, so 60k chars (~20k tokens) was needlessly conservative. Truncation above
    # this stays as a safety net.
    TRANSCRIPT_MAX_CHARS: int = 200000
    # The rich 17-key minutes schema produces far more output than the old 6-key one;
    # nothing in gemini.py set an output cap before, so a long meeting could silently
    # truncate the JSON mid-object. flash-lite bills only tokens actually generated, so
    # this is a safety bound, not a target — keep it generous.
    TRANSCRIPT_MAX_OUTPUT_TOKENS: int = 16384

    # Spoken-diary ingestion (watched folder in 07_Inbox_Agent/Diario). Scanned by the
    # same job as transcripts (no separate interval). A spoken entry can be a lot
    # shorter than a meeting, so the "too short to be real" floor is lower.
    DIARY_MIN_CHARS: int = 120
    DIARY_MAX_CHARS: int = 200000
    DIARY_MAX_OUTPUT_TOKENS: int = 16384

    # A meeting note is created eagerly on calendar sync (prep brief, attendee links)
    # before any transcript exists. If no ata ever lands and the owner never wrote anything
    # by hand, it's dead weight — pruned to 08_Archive/Meetings/ this many hours after
    # the meeting's own start time (not the note's creation time).
    MEETING_PRUNE_HOURS: int = 24

    # Telegram Bot Integration
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_ALLOWED_USER_IDS: Annotated[list[int], NoDecode] = []
    TELEGRAM_CHAT_ID: Optional[int] = None
    TELEGRAM_ENABLED: bool = True

    # SQLite Database URL
    DATABASE_URL: str = f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'rysos.db'}"

    @field_validator("USER_EMAILS", "PERSONAL_EMAIL_DOMAINS", mode="before")
    @classmethod
    def parse_csv_list(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return list(v)

    @field_validator("TELEGRAM_ALLOWED_USER_IDS", mode="before")
    @classmethod
    def parse_allowed_user_ids(cls, v: Any) -> list[int]:
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",") if p.strip()]
            return [int(p) for p in parts if p.isdigit() or (p.startswith("-") and p[1:].isdigit())]
        if isinstance(v, (int, float)):
            return [int(v)]
        if isinstance(v, list):
            return [int(x) for x in v if str(x).lstrip("-").isdigit()]
        return []

    @field_validator("TELEGRAM_CHAT_ID", mode="before")
    @classmethod
    def empty_chat_id_is_none(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("OBSIDIAN_VAULT_PATH", mode="after")
    @classmethod
    def expand_vault_path(cls, v: Path) -> Path:
        return v.expanduser()

    @field_validator("GOOGLE_TOKEN_PATH", "GOOGLE_CREDENTIALS_PATH", "DATA_DIR", mode="after")
    @classmethod
    def resolve_paths(cls, v: Path) -> Path:
        if not v.is_absolute():
            return BASE_DIR / v
        return v

    def ensure_directories(self) -> None:
        """Ensure required local directories exist."""
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if self.OBSIDIAN_VAULT_PATH.exists():
            for folder in [
                "00_Cockpit/Daily",
                "00_Cockpit/Weekly",
                "01_Projects",
                "02_Areas",
                "03_Decisions",
                "04_Meetings",
                "05_People",
                "06_Resources/Ideias_e_Criatividade",
                "06_Resources/Leituras_e_Pesquisas",
                "06_Resources/Frameworks_e_Metodos",
                "07_Inbox_Agent",
                "07_Inbox_Agent/Transcricoes",
                "07_Inbox_Agent/Transcricoes/Processado",
                "07_Inbox_Agent/Diario",
                "07_Inbox_Agent/Diario/Processado",
                "07_Inbox_Agent/Triagem",
                "08_Archive",
                "09_Journal",
                "_templates",
            ]:
                (self.OBSIDIAN_VAULT_PATH / folder).mkdir(parents=True, exist_ok=True)


settings = Settings()
