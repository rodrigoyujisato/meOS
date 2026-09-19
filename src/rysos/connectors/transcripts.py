"""Watched-folder connector for manual meeting transcripts.

the owner drops a ``.txt`` (or ``.md``) meeting transcript into
``<vault>/07_Inbox_Agent/Transcricoes/``. This connector scans that folder,
hashes each file for idempotency, guesses the meeting date from the filename,
and — once the pipeline has filed it — moves the raw file into
``Transcricoes/Processado/`` so it is archived but no longer re-scanned.
"""

import hashlib
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("rysos.connectors.transcripts")

_SUPPORTED_SUFFIXES = (".txt", ".md", ".vtt", ".srt")
_INBOX_SUBDIR = ("07_Inbox_Agent", "Transcricoes")
_DIARY_INBOX_SUBDIR = ("07_Inbox_Agent", "Diario")
_PROCESSED_DIRNAME = "Processado"

# Housekeeping files that live in the watch folder but are not transcripts.
_IGNORED_STEMS = frozenset({"leia-me", "leiame", "readme", "read-me", ".gitkeep", "_index", "index"})
# Below this many characters of real content a file is almost certainly a note
# to self / placeholder, not a meeting transcript.
_MIN_TRANSCRIPT_CHARS = 400

# Leading per-line timestamps emitted by speech-to-text / diarizer tools, e.g.
# "[0.00 - 3.74] ", "[00:01:23] ", "[12:04 - 12:09] ", "0:00:07.480 --> ...".
_LEADING_TS_RE = re.compile(
    r"^\s*(?:\[\s*\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*(?:-|–|-->)\s*\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*\]"
    r"|\[\s*\d+(?:\.\d+)?\s*(?:-|–)\s*\d+(?:\.\d+)?\s*\]"
    r"|\[\s*\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*\]"
    r"|\(\s*\d{1,2}:\d{2}(?::\d{2})?\s*\))\s*"
)

# YYYY-MM-DD / YYYY_MM_DD / YYYYMMDD, and DD-MM-YYYY / DD.MM.YYYY
_DATE_PATTERNS = (
    (re.compile(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})"), ("y", "m", "d")),
    (re.compile(r"(\d{2})[-_.](\d{2})[-_.](20\d{2})"), ("d", "m", "y")),
)


@dataclass
class TranscriptFile:
    path: Path
    raw_text: str
    sha256: str
    date_hint: Optional[date] = None
    _cleaned: str = field(default="", repr=False)

    @property
    def filename(self) -> str:
        return self.path.name


def _strip_vtt_srt(text: str) -> str:
    """Normalises raw ASR/caption exports: drops WEBVTT headers, standalone cue
    numbers and ``00:00:00.000 --> ...`` timing lines, and strips the leading
    per-line timestamp that speech-to-text/diarizer dumps put on every line
    (``[0.00 - 3.74] ``, ``[00:01:23] ``). Cuts 15-25% of noise on those exports,
    which matters against the input-size cap on long meetings."""
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s == "WEBVTT" or s.isdigit():
            continue
        if "-->" in s and re.search(r"\d{1,2}:\d{2}", s):
            continue
        cleaned = _LEADING_TS_RE.sub("", ln)
        if cleaned.strip():
            out.append(cleaned)
    return "\n".join(out).strip()


class TranscriptConnector:
    """Finds and archives raw transcripts in a watched Inbox folder.

    Serves both the meeting-transcript pipeline (default folder) and the spoken-diary
    pipeline: pass ``inbox_subdir=_DIARY_INBOX_SUBDIR`` and a lower ``min_chars`` (a
    spoken diary entry is often much shorter than a meeting). Everything else — format
    normalisation, filename date guess, SHA-256, no-clobber archival — is identical.
    """

    def __init__(
        self,
        vault_path: Path,
        *,
        inbox_subdir: tuple = _INBOX_SUBDIR,
        min_chars: int = _MIN_TRANSCRIPT_CHARS,
    ):
        self.vault_path = Path(vault_path)
        self._inbox_subdir = tuple(inbox_subdir)
        self._min_chars = int(min_chars)

    @property
    def inbox_dir(self) -> Path:
        return self.vault_path.joinpath(*self._inbox_subdir)

    @property
    def processed_dir(self) -> Path:
        return self.inbox_dir / _PROCESSED_DIRNAME

    def ensure_dirs(self) -> None:
        self.processed_dir.mkdir(parents=True, exist_ok=True)

    def scan(self) -> list[TranscriptFile]:
        """Returns every unprocessed transcript sitting directly in the Inbox folder.

        The ``Processado/`` archive is never descended into, and files are read
        defensively — an unreadable or empty file is skipped with a warning
        rather than aborting the batch.
        """
        self.ensure_dirs()
        found: list[TranscriptFile] = []
        for entry in sorted(self.inbox_dir.iterdir()):
            if entry.is_dir() or entry.suffix.lower() not in _SUPPORTED_SUFFIXES:
                continue
            if entry.stem.strip().lower() in _IGNORED_STEMS or entry.name.startswith("."):
                continue
            try:
                raw = entry.read_bytes()
            except OSError as e:  # pragma: no cover - filesystem race
                logger.warning(f"Transcrição ilegível ignorada ({entry.name}): {e}")
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1", errors="replace")
            # Normalise every format, not just VTT/SRT: real-world .txt drops from
            # speech-to-text tools carry a leading "[0.00 - 3.74]" timestamp on every line.
            text = _strip_vtt_srt(text)
            if len(text) < self._min_chars:
                logger.warning(
                    f"Ignorada (curta demais para ser transcrição, {len(text)} chars): {entry.name}"
                )
                continue
            found.append(
                TranscriptFile(
                    path=entry,
                    raw_text=text,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    date_hint=self._date_from_name(entry.name),
                )
            )
        return found

    def move_to_processed(self, tf: TranscriptFile) -> Path:
        """Moves the raw file into ``Processado/``; never overwrites an existing archive
        (a re-drop of byte-identical content would otherwise clobber the first copy)."""
        self.ensure_dirs()
        dest = self.processed_dir / tf.path.name
        if dest.exists():
            n = 2
            stem, suffix = tf.path.stem, tf.path.suffix
            dest = self.processed_dir / f"{stem} [{tf.sha256[:8]}]{suffix}"
            while dest.exists():
                dest = self.processed_dir / f"{stem} [{tf.sha256[:8]}] ({n}){suffix}"
                n += 1
        try:
            shutil.move(str(tf.path), str(dest))
        except OSError as e:  # pragma: no cover - filesystem race
            logger.warning(f"Não foi possível arquivar {tf.path.name}: {e}")
            return tf.path
        return dest

    @staticmethod
    def _date_from_name(name: str) -> Optional[date]:
        for rx, order in _DATE_PATTERNS:
            m = rx.search(name)
            if not m:
                continue
            parts = dict(zip(order, m.groups()))
            try:
                d = date(int(parts["y"]), int(parts["m"]), int(parts["d"]))
            except (ValueError, KeyError):
                continue
            if 2000 <= d.year <= datetime.now().year + 1:
                return d
        return None
