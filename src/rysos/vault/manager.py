"""Obsidian Vault Manager: atomic UTF-8 file I/O and structure governance."""

import calendar
import difflib
import re
import tempfile
import unicodedata
import uuid
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from jinja2 import Template

from rysos.config import settings
from rysos.identity import user_name
from rysos.vault.templates import (
    DAILY_COCKPIT_TEMPLATE,
    WEEKLY_REVIEW_TEMPLATE,
    PROJECT_TEMPLATE,
    AREA_NEGOCIOS_MOC_TEMPLATE,
    AREA_GENERIC_TEMPLATE,
    DECISION_TEMPLATE,
    MEETING_TEMPLATE,
    PERSON_TEMPLATE,
    IDEA_TEMPLATE,
    FRAMEWORK_TEMPLATE,
    RESOURCE_TEMPLATE,
    JOURNAL_TEMPLATE,
)

# Canonical MECE areas: (frontmatter file slug, human title). The AI returns free-form
# labels ("Patrimônio & Finanças", "Patrimonio_e_Financas"); _resolve_area folds both.
AREAS: dict[str, tuple[str, str]] = {
    "negocios": ("Negocios_e_Governanca", "Negócios & Governança"),
    "saude": ("Saude_e_Vitalidade", "Saúde & Vitalidade"),
    "patrimonio": ("Patrimonio_e_Financas", "Patrimônio & Finanças"),
    "familia": ("Familia_e_Relacionamentos", "Família & Relacionamentos"),
}


class VaultManager:
    """Manages the Obsidian Vault on Google Drive with atomic UTF-8 operations."""

    def __init__(self, vault_path: Optional[Path] = None):
        self.vault_path = vault_path or settings.OBSIDIAN_VAULT_PATH

    def initialize_vault_structure(self) -> dict[str, Any]:
        """Creates the PARA structure and foundational MOC notes if they do not exist."""
        self.vault_path.mkdir(parents=True, exist_ok=True)

        folders = [
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
        ]

        created_folders = []
        for folder in folders:
            folder_path = self.vault_path / folder
            if not folder_path.exists():
                folder_path.mkdir(parents=True, exist_ok=True)
                created_folders.append(folder)

        # Create 4 MECE Area Notes
        self._ensure_area_notes()

        return {
            "vault_path": str(self.vault_path),
            "created_folders": created_folders,
            "status": "initialized",
        }

    def _ensure_area_notes(self) -> None:
        """Ensure foundational 4 MECE Area notes exist."""
        areas_dir = self.vault_path / "02_Areas"
        areas_dir.mkdir(parents=True, exist_ok=True)

        # 1. Negócios & Governança (MOC)
        negocios_file = areas_dir / "Negocios_e_Governanca.md"
        if not negocios_file.exists():
            self.write_file_atomic(negocios_file, AREA_NEGOCIOS_MOC_TEMPLATE)

        # 2. Saúde & Vitalidade
        saude_file = areas_dir / "Saude_e_Vitalidade.md"
        if not saude_file.exists():
            tmpl = Template(AREA_GENERIC_TEMPLATE)
            content = tmpl.render(
                area_id="SAUDE-VITALIDADE",
                title="Saúde & Vitalidade",
                pillar="pessoal",
                tag="saude",
                standards_description="Padrões de sono, nutrição, treino físico, saúde mental e check-ups preventivos.",
            )
            self.write_file_atomic(saude_file, content)

        # 3. Patrimônio & Finanças
        patrimonio_file = areas_dir / "Patrimonio_e_Financas.md"
        if not patrimonio_file.exists():
            tmpl = Template(AREA_GENERIC_TEMPLATE)
            content = tmpl.render(
                area_id="PATRIMONIO-FINANCAS",
                title="Patrimônio & Finanças",
                pillar="pessoal",
                tag="financas",
                standards_description="Gestão de fluxo de caixa pessoal, alocação de ativos, patrimônio e investimentos.",
            )
            self.write_file_atomic(patrimonio_file, content)

        # 4. Família & Relacionamentos
        familia_file = areas_dir / "Familia_e_Relacionamentos.md"
        if not familia_file.exists():
            tmpl = Template(AREA_GENERIC_TEMPLATE)
            content = tmpl.render(
                area_id="FAMILIA-RELACIONAMENTOS",
                title="Família & Relacionamentos",
                pillar="pessoal",
                tag="familia",
                standards_description="Tempo de qualidade com família, amizades estratégicas e deveres pessoais.",
            )
            self.write_file_atomic(familia_file, content)

    def _ensure_template_files(self) -> None:
        """Writes raw template files to _templates directory."""
        tmpl_dir = self.vault_path / "_templates"
        tmpl_dir.mkdir(parents=True, exist_ok=True)

        templates_map = {
            "Daily Cockpit Template.md": DAILY_COCKPIT_TEMPLATE,
            "Weekly Review Template.md": WEEKLY_REVIEW_TEMPLATE,
            "Project Template.md": PROJECT_TEMPLATE,
            "Decision Template.md": DECISION_TEMPLATE,
            "Meeting Template.md": MEETING_TEMPLATE,
            "Person Template.md": PERSON_TEMPLATE,
        }

        for fname, content in templates_map.items():
            fpath = tmpl_dir / fname
            if not fpath.exists():
                self.write_file_atomic(fpath, content)

    def write_file_atomic(self, file_path: Path, content: str) -> None:
        """Writes content atomically to a file using UTF-8 encoding."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=file_path.parent,
            delete=False,
            suffix=".tmp",
        )
        try:
            temp_file.write(content)
            temp_file.flush()
            temp_file.close()
            Path(temp_file.name).replace(file_path)
        except Exception:
            if Path(temp_file.name).exists():
                Path(temp_file.name).unlink()
            raise

    def read_file(self, file_path: Path) -> Optional[str]:
        """Reads content from a markdown note in UTF-8."""
        if not file_path.exists():
            return None
        return file_path.read_text(encoding="utf-8")

    def create_daily_cockpit(
        self,
        target_date: Optional[date] = None,
        calendar_events: Optional[list[dict[str, Any]]] = None,
        actionable_emails: Optional[list[dict[str, Any]]] = None,
        pending_decisions: Optional[list[dict[str, Any]]] = None,
        high_priority_foci: Optional[list[dict[str, Any]]] = None,
    ) -> Path:
        """Generates or updates the Daily Cockpit note for the specified date."""
        target_date = target_date or date.today()
        date_str = target_date.strftime("%Y-%m-%d")
        date_formatted = target_date.strftime("%d/%m/%Y")

        cockpit_file = self.vault_path / "00_Cockpit" / "Daily" / f"{date_str}.md"

        tmpl = Template(DAILY_COCKPIT_TEMPLATE)
        rendered = tmpl.render(
            date_str=date_str,
            date_formatted=date_formatted,
            calendar_events=calendar_events or [],
            actionable_emails=actionable_emails or [],
            pending_decisions=pending_decisions or [],
            high_priority_foci=high_priority_foci or [],
        )

        # A re-sync regenerates the whole file from the template. Two guards keep the
        # Daily note's completion state deterministic across syncs:
        #   1. carry every focus from the old note into the regenerated Focos section
        #      (email-derived ones re-render anyway; conversationally captured ones would
        #      otherwise vanish), preserving each line's [ ] / [x] state;
        #   2. re-apply [x] to any other section's line that reappears in the new render.
        if cockpit_file.exists():
            old_content = self.read_file(cockpit_file) or ""
            rendered = self._merge_foci_section(old_content, rendered, target_date)
            rendered = self._preserve_completed_checkboxes(old_content, rendered)
            rendered = self._preserve_free_notes(old_content, rendered)
        elif target_date <= date.today():
            # Only for a real day being generated (today, or a backfilled past date) —
            # never for a forward-dated placeholder note (`schedule_bill_reminder`,
            # future-due `add_cockpit_focus`), which must hold only its own scheduled
            # item until that day actually arrives.
            rendered = self._carry_forward_unfinished_foci(rendered, target_date)

        self.write_file_atomic(cockpit_file, rendered)
        return cockpit_file

    FOCI_HEADER = "## 🎯 Focos de Alta Prioridade do Dia"
    FOCI_WEEK_HEADER = "### 📆 Foco de Atenção para a Semana"
    FOCI_LATER_HEADER = "### 🗂️ Foco para Acompanhamento"
    _FOCI_SECTION_RE = re.compile(
        r"(## 🎯 Focos de Alta Prioridade do Dia[ \t]*\n)(.*?)(?=\n---|\n## |\Z)", re.DOTALL
    )
    _CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.+?)\s*$")
    # The single visible, user-editable due-date token (Obsidian Tasks-plugin convention):
    # "📅 YYYY-MM-DD" anywhere in the line. Canonical going forward — this is what a human
    # edits directly in Obsidian to change a date, no hidden marker to keep in sync.
    _DUE_TOKEN_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
    # Legacy invisible marker, kept readable (not written anymore) for any note the one-time
    # migration (`migrate_all_daily_cockpit_due_tokens`) hasn't reached yet. NOT anchored to
    # end-of-string: `_carry_forward_unfinished_foci` appends the visible "⏳ (pendente desde
    # ...)" tag AFTER this marker, so an end anchor would silently stop matching the day
    # after a dated item carries over (2026-09-15 bucketing incident).
    _FOCUS_DUE_RE = re.compile(r"<!--\s*due:(\d{4}-\d{2}-\d{2})\s*-->")
    # Visible "(prazo: X)" text, read only as a best-effort fallback for bucketing when
    # neither the 📅 token nor the legacy marker is present — never used for file routing.
    _PRAZO_TEXT_RE = re.compile(r"\(prazo:\s*([^)]+)\)", re.IGNORECASE)

    _WEEKDAYS_PT = {
        "segunda": 0, "segunda feira": 0,
        "terca": 1, "terca feira": 1,
        "quarta": 2, "quarta feira": 2,
        "quinta": 3, "quinta feira": 3,
        "sexta": 4, "sexta feira": 4,
        "sabado": 5,
        "domingo": 6,
    }
    _MONTHS_PT = {
        "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
        "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    }

    @classmethod
    def _focus_due_date(cls, text: str) -> Optional[date]:
        """Reads the line's due date: the visible `📅 YYYY-MM-DD` token first, else the
        legacy invisible `<!-- due: -->` marker (pre-migration notes only)."""
        m = cls._DUE_TOKEN_RE.search(text) or cls._FOCUS_DUE_RE.search(text)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            return None

    @classmethod
    def _best_effort_prazo_date(cls, prazo_text: str, reference: date) -> Optional[date]:
        """Best-effort read of a vague visible '(prazo: X)' phrase into a date, used for
        Cockpit bucketing and (via `_migrate_due_token`) to backfill the visible `📅` token
        onto an already-resolvable line — never used to pick which day's FILE an item lives
        in (that stays on `_resolve_relative_due` in core.py, deliberately conservative).
        Returns None rather than guess on anything unclear, so an unresolvable line is never
        migrated and never bucketed anywhere but Acompanhamento."""
        raw = prazo_text.strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue

        n = cls._normalize_for_match(prazo_text)
        if not n:
            return None
        # "ate"/"até" is a deadline CAP ("by tomorrow", "by Sunday") — for display bucketing
        # only, treated the same as the bare expression it qualifies.
        if n.startswith("ate "):
            n = n[4:]
        if n == "hoje":
            return reference
        if n.startswith("amanh"):
            return reference + timedelta(days=1)

        for name, weekday in cls._WEEKDAYS_PT.items():
            if n == name or n.startswith(name + " "):
                days_ahead = (weekday - reference.weekday()) % 7
                return reference + timedelta(days=days_ahead)

        if "semana que vem" in n or "proxima semana" in n:
            return reference + timedelta(days=7)

        if "final" in n or "fim" in n:
            for month_name, month_num in cls._MONTHS_PT.items():
                if month_name in n:
                    year = reference.year if month_num >= reference.month else reference.year + 1
                    last_day = calendar.monthrange(year, month_num)[1]
                    return date(year, month_num, last_day)
            if "mes" in n:
                last_day = calendar.monthrange(reference.year, reference.month)[1]
                return date(reference.year, reference.month, last_day)

        if "inicio" in n or "comeco" in n:
            for month_name, month_num in cls._MONTHS_PT.items():
                if month_name in n:
                    year = reference.year if month_num >= reference.month else reference.year + 1
                    return date(year, month_num, 1)

        return None

    @classmethod
    def _focus_effective_date(cls, text: str, reference: date) -> Optional[date]:
        """The date used for bucketing/sorting: the explicit marker when present, else a
        best-effort read of the visible '(prazo: X)' text. Display-only."""
        marker_date = cls._focus_due_date(text)
        if marker_date is not None:
            return marker_date
        m = cls._PRAZO_TEXT_RE.search(text)
        if not m:
            return None
        return cls._best_effort_prazo_date(m.group(1), reference)

    @classmethod
    def _focus_bucket(cls, text: str, reference: date) -> str:
        """Which Cockpit subsection a focus line belongs in, relative to `reference`
        (the note's own date — NEVER `date.today()`, or a forward-dated note's items would
        all read as overdue): 'today' (due today or overdue), 'week' (due within 7 days),
        or 'later' (no resolvable date, or more than 7 days out)."""
        d = cls._focus_effective_date(text, reference)
        if d is None:
            return "later"
        if d <= reference:
            return "today"
        if d <= reference + timedelta(days=7):
            return "week"
        return "later"

    @classmethod
    def _sort_foci_items(cls, items: list[tuple[bool, str]], reference: date) -> list[tuple[bool, str]]:
        """Dated items first (earliest / most overdue on top), undated items last —
        stable within each group so an unrelated re-sync doesn't reshuffle same-day
        items with no signal to sort by."""
        dated: list[tuple[date, int, bool, str]] = []
        undated: list[tuple[int, bool, str]] = []
        for idx, (done, text) in enumerate(items):
            d = cls._focus_effective_date(text, reference)
            if d is not None:
                dated.append((d, idx, done, text))
            else:
                undated.append((idx, done, text))
        dated.sort(key=lambda x: (x[0], x[1]))
        return [(done, text) for _, _, done, text in dated] + [(done, text) for _, done, text in undated]

    @classmethod
    def _render_foci_body(cls, items: list[tuple[bool, str]], reference: date) -> str:
        """Renders the full Foci section body split into three tiers by due-date proximity
        to `reference`: today/overdue stay directly under the main header (no subheader,
        matching the pre-existing layout); 'Semana' (<=7 days) and 'Acompanhamento' (no
        date or >7 days) render as `###` subsections and are omitted entirely when empty,
        so a quiet week doesn't leave two permanent blank headers in the note."""
        buckets: dict[str, list[tuple[bool, str]]] = {"today": [], "week": [], "later": []}
        for done, text in items:
            buckets[cls._focus_bucket(text, reference)].append((done, text))
        for key in buckets:
            buckets[key] = cls._sort_foci_items(buckets[key], reference)

        lines: list[str] = []
        if buckets["today"]:
            lines += [f"- [{'x' if d else ' '}] {t}" for d, t in buckets["today"]]
        elif not items:
            lines.append(f"- [ ] {cls._FOCI_PLACEHOLDER_TEXT}")
        else:
            lines.append("*Nenhum item de alta prioridade para hoje.*")

        if buckets["week"]:
            lines += ["", cls.FOCI_WEEK_HEADER]
            lines += [f"- [{'x' if d else ' '}] {t}" for d, t in buckets["week"]]
        if buckets["later"]:
            lines += ["", cls.FOCI_LATER_HEADER]
            lines += [f"- [{'x' if d else ' '}] {t}" for d, t in buckets["later"]]

        return "\n".join(lines)

    def rebucket_daily_focus(self, target_date: Optional[date] = None) -> bool:
        """Re-renders the Foci section's three tiers against `target_date` without touching
        anything else in the Cockpit note — lets a hand-edited '(prazo: X)' or a fresh
        `📅 YYYY-MM-DD` token move an item into the right tier between full syncs, instead
        of waiting for the 07:00 morning job or a manual /sync. Returns True if the section
        actually changed (idempotent no-op otherwise, safe to run on a frequent tick)."""
        target_date = target_date or date.today()
        cockpit_file = self.vault_path / "00_Cockpit" / "Daily" / f"{target_date.strftime('%Y-%m-%d')}.md"
        if not cockpit_file.exists():
            return False
        content = self.read_file(cockpit_file) or ""
        m = self._FOCI_SECTION_RE.search(content)
        if not m:
            return False
        items = self._extract_foci_items(m.group(2))
        if not items:
            return False
        body = self._render_foci_body(items, target_date)
        new_content = content[: m.start(2)] + body + "\n" + content[m.end(2):]
        if new_content == content:
            return False
        self.write_file_atomic(cockpit_file, new_content)
        return True

    @classmethod
    def _migrate_due_token(cls, text: str, reference: date) -> tuple[str, bool]:
        """Rewrites one focus line to carry the single visible `📅 YYYY-MM-DD` token in
        place of the legacy invisible `<!-- due: -->` marker, WITHOUT touching the original
        human phrasing (the '(prazo: quinta-feira)' text, if any, is kept as-is for
        context). Only acts when a date can already be resolved — a line with no
        resolvable date is returned unchanged, never guessed into one. Returns
        (possibly-rewritten text, whether it changed)."""
        if cls._DUE_TOKEN_RE.search(text):
            return text, False  # already on the new format
        d = cls._focus_effective_date(text, reference)
        if d is None:
            return text, False
        stripped = cls._FOCUS_DUE_RE.sub("", text)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        return f"{stripped} 📅 {d.strftime('%Y-%m-%d')}", True

    def migrate_daily_cockpit_due_tokens(self, target_date: Optional[date] = None) -> int:
        """One-off migration for a single Daily Cockpit note: converts every Foci line with
        a resolvable due date to the visible `📅` token (see `_migrate_due_token`) and
        re-renders the section. Returns how many lines were rewritten (0 = already
        migrated / nothing resolvable, note left untouched)."""
        target_date = target_date or date.today()
        cockpit_file = self.vault_path / "00_Cockpit" / "Daily" / f"{target_date.strftime('%Y-%m-%d')}.md"
        if not cockpit_file.exists():
            return 0
        content = self.read_file(cockpit_file) or ""
        m = self._FOCI_SECTION_RE.search(content)
        if not m:
            return 0
        items = self._extract_foci_items(m.group(2))
        if not items:
            return 0

        migrated_items: list[tuple[bool, str]] = []
        changed_count = 0
        for done, text in items:
            new_text, did_change = self._migrate_due_token(text, target_date)
            if did_change:
                changed_count += 1
            migrated_items.append((done, new_text))
        if not changed_count:
            return 0

        body = self._render_foci_body(migrated_items, target_date)
        new_content = content[: m.start(2)] + body + "\n" + content[m.end(2):]
        self.write_file_atomic(cockpit_file, new_content)
        return changed_count

    def migrate_all_daily_cockpit_due_tokens(self) -> dict[str, int]:
        """Runs `migrate_daily_cockpit_due_tokens` over every existing Daily Cockpit note
        (past and forward-dated). Returns {'files_changed': N, 'items_migrated': N}."""
        daily_dir = self.vault_path / "00_Cockpit" / "Daily"
        files_changed = 0
        items_migrated = 0
        for f in sorted(daily_dir.glob("20[0-9][0-9]-[0-1][0-9]-[0-3][0-9].md")):
            try:
                d = datetime.strptime(f.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            changed = self.migrate_daily_cockpit_due_tokens(d)
            if changed:
                files_changed += 1
                items_migrated += changed
        return {"files_changed": files_changed, "items_migrated": items_migrated}

    @classmethod
    def _insert_sorted_focus(cls, content: str, task_text: str, reference: date) -> str:
        """Inserts one new open (`- [ ]`) focus line into the Foci section, re-bucketing and
        re-sorting the whole section. `task_text` excludes the leading `- [ ]`. `reference`
        must be the note's OWN date (not `date.today()`), since bucketing is relative to it."""
        content = content.replace("- [ ] *Defina os 3 focos principais do dia*\n", "")
        content = content.replace("- [ ] *Defina os 3 focos principais do dia*", "")
        m = cls._FOCI_SECTION_RE.search(content)
        if not m:
            header = cls.FOCI_HEADER
            if header in content:
                return content.replace(header, f"{header}\n- [ ] {task_text}", 1)
            return content + f"\n\n{header}\n- [ ] {task_text}\n"
        items = cls._extract_foci_items(m.group(2))
        items.append((False, task_text))
        body = cls._render_foci_body(items, reference)
        return content[: m.start(2)] + body + "\n" + content[m.end(2) :]

    _FOCI_PLACEHOLDER_TEXT = "*Defina os 3 focos principais do dia*"

    @classmethod
    def _extract_foci_items(cls, section_body: str) -> list[tuple[bool, str]]:
        """Returns (is_done, text) for each real checkbox line, skipping the *Defina...* placeholder."""
        items: list[tuple[bool, str]] = []
        for line in section_body.splitlines():
            m = cls._CHECKBOX_RE.match(line)
            if not m:
                continue
            text = m.group(2).strip()
            if text == cls._FOCI_PLACEHOLDER_TEXT:
                continue
            items.append((m.group(1).lower() == "x", text))
        return items

    @classmethod
    def _merge_foci_section(cls, old_content: str, new_content: str, reference: date) -> str:
        """Merges the old Daily note's Focos section into the freshly rendered one: keeps
        every prior focus, preserving [ ] / [x] state, and appends any that the template no
        longer emits.

        Matching is by identity key (`_stable_line_key`) when the line has one — so a bill
        whose amount or due-date phrasing changed between renders is still recognised as the
        same focus — and by normalized text otherwise (hand-written foci). `reference` must
        be the note's OWN date, for bucketing."""
        old_m = cls._FOCI_SECTION_RE.search(old_content)
        new_m = cls._FOCI_SECTION_RE.search(new_content)
        if not old_m or not new_m:
            return new_content

        old_items = cls._extract_foci_items(old_m.group(2))
        if not old_items:
            return new_content

        old_done_text = {cls._normalize_for_match(t) for done, t in old_items if done}
        old_done_ids = {k for done, t in old_items if done and (k := cls._stable_line_key(t))}
        new_items = cls._extract_foci_items(new_m.group(2))
        new_text_keys = {cls._normalize_for_match(t) for _, t in new_items}
        new_ids = {k for _, t in new_items if (k := cls._stable_line_key(t))}

        def _carried_done(t: str) -> bool:
            sid = cls._stable_line_key(t)
            return cls._normalize_for_match(t) in old_done_text or (sid is not None and sid in old_done_ids)

        def _superseded(t: str) -> bool:
            sid = cls._stable_line_key(t)
            return cls._normalize_for_match(t) in new_text_keys or (sid is not None and sid in new_ids)

        merged: list[tuple[bool, str]] = [(done or _carried_done(t), t) for done, t in new_items]
        merged += [(done, t) for done, t in old_items if not _superseded(t)]

        body = cls._render_foci_body(merged, reference)
        return new_content[: new_m.start(2)] + body + "\n" + new_content[new_m.end(2) :]

    @staticmethod
    def _preserve_completed_checkboxes(old_content: str, new_content: str) -> str:
        """Re-applies `- [x]` state from an existing Daily note onto a freshly rendered one.

        A completed line is matched to its regenerated counterpart by identity key
        (`_stable_line_key`) when it has one, else by normalized text. The identity key is
        what keeps a paid bill ticked after a re-sync whose render changed the line's
        amount, due-date phrasing or e-mail subject."""
        done_line_re = re.compile(r"^(\s*[-*]\s+)\[[xX]\]\s+(.+?)\s*$")
        open_line_re = re.compile(r"^(\s*[-*]\s+)\[ \]\s+(.+?)\s*$")

        completed_text: set[str] = set()
        completed_ids: set[str] = set()
        for line in old_content.splitlines():
            m = done_line_re.match(line)
            if not m or not m.group(2).strip():
                continue
            completed_text.add(VaultManager._normalize_for_match(m.group(2)))
            sid = VaultManager._stable_line_key(m.group(2))
            if sid:
                completed_ids.add(sid)
        if not completed_text and not completed_ids:
            return new_content

        out = []
        for line in new_content.splitlines():
            m = open_line_re.match(line)
            if m and m.group(2).strip():
                sid = VaultManager._stable_line_key(m.group(2))
                if VaultManager._normalize_for_match(m.group(2)) in completed_text or (
                    sid is not None and sid in completed_ids
                ):
                    out.append(f"{m.group(1)}[x] {m.group(2)}")
                    continue
            out.append(line)
        result = "\n".join(out)
        if new_content.endswith("\n") and not result.endswith("\n"):
            result += "\n"
        return result

    _FREE_NOTES_RE = re.compile(
        r"(<!-- notas-livres:start -->)(.*?)(<!-- notas-livres:end -->)", re.DOTALL
    )

    @classmethod
    def _preserve_free_notes(cls, old_content: str, new_content: str) -> str:
        """Carries whatever free text was written between the notas-livres markers across
        a same-day cockpit re-render, which otherwise regenerates the whole template
        blank — there is no "Focos"-style structure here to merge, just raw prose to keep."""
        old_m = cls._FREE_NOTES_RE.search(old_content)
        if not old_m or not old_m.group(2).strip():
            return new_content
        new_m = cls._FREE_NOTES_RE.search(new_content)
        if not new_m:
            return new_content
        return new_content[: new_m.start(2)] + old_m.group(2) + new_content[new_m.end(2):]

    def extract_free_notes(self, content: str) -> str:
        """Returns the stripped free text between the notas-livres markers, or ''."""
        m = self._FREE_NOTES_RE.search(content)
        return m.group(2).strip() if m else ""

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        """Accent/emoji/markdown-insensitive normalization for matching free-text against
        Cockpit checklist lines (e.g. 'Gasnorte' vs '💳 **Gasnorte**')."""
        text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
        text = text.lower()
        text = re.sub(r"[*_`#>\[\]()]", " ", text)
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    # Generic filler tokens that carry no discriminating signal when matching a
    # completion phrase ("boleto da gasnorte pago hoje") against a focus line.
    _MATCH_STOPWORDS = frozenset({
        "o", "a", "os", "as", "de", "da", "do", "das", "dos", "e", "em", "no", "na",
        "um", "uma", "para", "pra", "com", "por", "ao", "aos", "the", "of",
        "pagar", "pago", "paga", "pagos", "pagas", "paguei", "quitar", "quitado", "quitei",
        "boleto", "conta", "fatura", "pagamento",
        "feito", "feita", "concluir", "concluido", "concluida", "conclui",
        "finalizar", "finalizado", "finalizada", "finalizei", "terminei", "resolvi",
        "resolvido", "resolvida", "hoje", "ja", "foi", "era", "esta", "debito",
        "automatico", "marcar", "marca", "como", "pode", "item", "done", "paid",
        "valor", "vencimento", "venc", "ref",
    })

    # Bulk-completion cues. "todos os boletos estão pagos" / "todas as contas quitadas" /
    # bare "tudo pago" report a whole class as done at once — per-item similarity can't
    # resolve them (the quantifier is the only surviving token), so the completion path
    # scopes by class instead.
    _BULK_QUANTIFIERS = frozenset({"todos", "todas", "tudo", "todo", "ambos", "ambas"})
    _BULK_COMPLETION_CUES = (
        "pago", "paga", "pagos", "pagas", "paguei", "quit", "conclu", "finaliz",
        "resolv", "feito", "feita", "ja fiz", "baix", "pronto", "em dia", "quitad",
    )

    @staticmethod
    def _stable_line_key(text: str) -> Optional[str]:
        """Identity key for a machine-generated Cockpit line, stable across re-renders
        even when the volatile tail (amount, due-date phrasing, e-mail subject) changes.

        Covers the two generated shapes — the bill focus (`💳 **Pagar boleto: X** …`) and
        the critical-e-mail line (`**De:** X | **Assunto:** …`). Returns None for
        hand-written foci, which keep matching by normalized text.
        """
        plain = re.sub(r"\*+", "", text or "")
        m = re.search(r"pagar\s+boleto:\s*(.+?)\s*(?:\(|—|–|--|$)", plain, re.IGNORECASE)
        if m and m.group(1).strip():
            return "bill:" + VaultManager._normalize_for_match(m.group(1))
        m = re.match(r"\s*de:?\s*(.+?)\s*\|\s*assunto", plain, re.IGNORECASE)
        if m and m.group(1).strip():
            return "mail:" + VaultManager._normalize_for_match(m.group(1))
        return None

    _CARRIED_MARKER_RE = re.compile(r"\s*⏳\s*\*\(pendente desde \d{2}/\d{2}/\d{4}\)\*\s*$")

    def _carry_forward_unfinished_foci(self, content: str, target_date: date) -> str:
        """On first creation of a new day's Cockpit, pulls forward any Focos-do-Dia
        checkbox still unchecked in the most recent prior Daily note, so a pendência
        doesn't silently disappear just because a new day's note was generated.

        Looks back to the latest existing prior file (not strictly yesterday) so a
        missed sync day doesn't drop anything. Tags each carried line with a visible
        "pendente desde" marker, but leaves it alone if the line already carries one —
        that keeps the original open date stable across a multi-day chain instead of
        resetting it every rollover.
        """
        daily_dir = self.vault_path / "00_Cockpit" / "Daily"
        prev_file = None
        prev_date = None
        for f in sorted(daily_dir.glob("20[0-9][0-9]-[0-1][0-9]-[0-3][0-9].md"), reverse=True):
            try:
                d = datetime.strptime(f.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < target_date:
                prev_file, prev_date = f, d
                break
        if not prev_file:
            return content

        prev_content = self.read_file(prev_file) or ""
        prev_m = self._FOCI_SECTION_RE.search(prev_content)
        if not prev_m:
            return content
        unfinished = [t for done, t in self._extract_foci_items(prev_m.group(2)) if not done]
        if not unfinished:
            return content

        existing_keys: set[str] = set()
        new_m = self._FOCI_SECTION_RE.search(content)
        if new_m:
            for _, t in self._extract_foci_items(new_m.group(2)):
                existing_keys.add(self._normalize_for_match(t))
                sid = self._stable_line_key(t)
                if sid:
                    existing_keys.add(sid)

        for text in unfinished:
            key = self._normalize_for_match(text)
            sid = self._stable_line_key(text)
            if key in existing_keys or (sid and sid in existing_keys):
                continue
            if self._CARRIED_MARKER_RE.search(text):
                tagged = text
            else:
                tagged = f"{text} ⏳ *(pendente desde {prev_date.strftime('%d/%m/%Y')})*"
            content = self._insert_sorted_focus(content, tagged, target_date)
            existing_keys.add(key)
            if sid:
                existing_keys.add(sid)
        return content

    def _tick_payment_checkboxes(self, cockpit_file: Path) -> list[str]:
        """Flips every open payment-class checkbox (`- [ ]` -> `- [x]`) in one Daily note.

        Used by the bulk-completion path over today's cockpit and each forward-dated
        reminder note. Returns the labels ticked (empty if the file is missing or had
        none). Placeholder `*...*` lines are skipped.
        """
        if not cockpit_file.exists():
            return []
        content = self.read_file(cockpit_file) or ""
        if not content:
            return []
        box_re = re.compile(r"^(\s*[-*]\s+)\[ \]\s+(.+?)\s*$")
        lines = content.splitlines()
        ticked: list[str] = []
        for i, line in enumerate(lines):
            m = box_re.match(line)
            if not m:
                continue
            text = m.group(2).strip()
            if text == self._FOCI_PLACEHOLDER_TEXT:
                continue
            if self._is_payment_focus(self._focus_label(text)):
                lines[i] = f"{m.group(1)}[x] {text}"
                ticked.append(self._focus_label(text))
        if ticked:
            trailing_nl = "\n" if content.endswith("\n") else ""
            self.write_file_atomic(cockpit_file, "\n".join(lines) + trailing_nl)
        return ticked

    @staticmethod
    def _is_payment_focus(label: str) -> bool:
        """True when a focus/checklist label is a bill or a bill-tagged critical e-mail.

        Bill-related actionable e-mails carry the `[Pessoal - Financeiro]` tag that
        `core.py` stamps on them (`is_personal and is_bill`), so that phrase is a reliable
        second signal alongside the "Pagar boleto:" focus prefix. Kept deliberately narrow
        so a bulk "boletos pagos" never sweeps an unrelated focus that merely mentions
        "faturamento" or "prestação de contas".
        """
        n = VaultManager._normalize_for_match(label)
        if "pagar boleto" in n or "pessoal financeiro" in n:
            return True
        return bool({"boleto", "boletos", "fatura", "faturas"} & set(n.split()))

    def _rank_focus_matches(self, query: str, items: list[tuple[int, str]]) -> list[tuple[float, int, str]]:
        """Scores each candidate focus *label* against the query; returns (score, index, label) desc.

        Score is `max(string ratio, geometric mean of the two token-coverage ratios)`. The
        geometric mean matters: a single query token that also fills the whole focus label
        ("gasnorte" vs "Pagar boleto: Gasnorte") scores ~1.0, but a single token that is only an
        incidental word in a longer label ("jurídico" vs "Revisar minuta do contrato com o
        jurídico") is held down, so it never looks confidently matched.
        """
        q_norm = self._normalize_for_match(query)
        q_tokens = {t for t in q_norm.split() if t not in self._MATCH_STOPWORDS and len(t) > 1}

        scored: list[tuple[float, int, str]] = []
        for idx, label in items:
            it_norm = self._normalize_for_match(label)
            it_tokens = {t for t in it_norm.split() if t not in self._MATCH_STOPWORDS and len(t) > 1}
            shared = len(q_tokens & it_tokens)
            oq = shared / len(q_tokens) if q_tokens else 0.0
            oit = shared / len(it_tokens) if it_tokens else 0.0
            token_score = (oq * oit) ** 0.5
            ratio = difflib.SequenceMatcher(None, q_norm, it_norm).ratio()
            # `ratio` alone is noisy on short labels ("conta de luz" vs "Contrato"): only let
            # it carry a match when it is very high; otherwise a shared token must back it.
            score = max(token_score, ratio if ratio >= 0.72 else 0.0)
            scored.append((score, idx, label))
        scored.sort(key=lambda t: t[0], reverse=True)
        return scored

    @staticmethod
    def _focus_label(text: str) -> str:
        """Short human label for a checklist line, stripped of markdown and trailing metadata."""
        label = re.sub(r"\*\*(.+?)\*\*", r"\1", text).strip()
        cuts = [label.find(sep) for sep in (" — ", " – ", " -- ", " (")]
        cuts = [c for c in cuts if c > 0]
        if cuts:
            label = label[: min(cuts)].strip()
        return label.strip(" *_—–-:") or text.strip()

    def complete_cockpit_focus(
        self,
        query: str,
        target_date: Optional[date] = None,
        apply: bool = True,
        strict: bool = False,
    ) -> dict[str, Any]:
        """Marks the Daily Cockpit focus that best matches `query` as done (`- [ ]` -> `- [x]`).

        Returns a status dict: `completed` | `already_done` | `ambiguous` | `not_found`
        | `no_pending` | `no_cockpit`. With `apply=False` it resolves the match without
        writing. `strict=True` only ever returns `completed`/`already_done` on a strong,
        unambiguous match — used by the mid-draft escape hatch, which must not divert a
        note draft on a loose resemblance.
        """
        target_date = target_date or date.today()
        date_str = target_date.strftime("%Y-%m-%d")
        cockpit_file = self.vault_path / "00_Cockpit" / "Daily" / f"{date_str}.md"

        if not cockpit_file.exists():
            return {"status": "no_cockpit", "date": date_str}

        content = self.read_file(cockpit_file) or ""
        lines = content.splitlines()

        line_re = re.compile(r"^(\s*[-*]\s+)\[( |x|X)\]\s+(.+?)\s*$")
        open_items: list[tuple[int, str]] = []
        done_items: list[tuple[int, str]] = []
        for idx, line in enumerate(lines):
            m = line_re.match(line)
            if not m:
                continue
            text = m.group(3).strip()
            if text == self._FOCI_PLACEHOLDER_TEXT:
                continue  # placeholder line, not a real focus
            bucket = done_items if m.group(2).lower() == "x" else open_items
            bucket.append((idx, self._focus_label(text)))

        # Bulk completion: "todos os boletos estão pagos", "todas as contas quitadas",
        # bare "tudo pago". A quantifier + completion report can't be resolved item by
        # item, so tick every open payment-class checkbox at once — in today's cockpit AND
        # in the forward-dated Daily notes that hold scheduled bill reminders, since
        # "todos" means every bill the cockpit surfaced, due today or later. Disabled in
        # strict mode — the mid-draft escape hatch must never divert a note draft on this.
        q_norm = self._normalize_for_match(query)
        is_bulk = bool(set(q_norm.split()) & self._BULK_QUANTIFIERS) and any(
            cue in q_norm for cue in self._BULK_COMPLETION_CUES
        )
        if is_bulk and not strict:
            open_pay_today = [lbl for _, lbl in open_items if self._is_payment_focus(lbl)]
            future_ticked: dict[str, list[str]] = {}
            if apply:
                today_ticked = self._tick_payment_checkboxes(cockpit_file)
                for f in sorted(cockpit_file.parent.glob("20[0-9][0-9]-[0-1][0-9]-[0-3][0-9].md")):
                    try:
                        d = datetime.strptime(f.stem, "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if not (target_date < d <= target_date + timedelta(days=400)):
                        continue
                    got = self._tick_payment_checkboxes(f)
                    if got:
                        future_ticked[f.stem] = got
            else:
                today_ticked = open_pay_today

            if today_ticked or future_ticked:
                return {
                    "status": "completed_bulk",
                    "items": today_ticked[:12],
                    "future": future_ticked,
                    "date": date_str,
                }
            done_pay = [lbl for _, lbl in done_items if self._is_payment_focus(lbl)]
            if done_pay:
                return {"status": "already_done_bulk", "items": done_pay[:12], "date": date_str}
            return {"status": "no_pending", "date": date_str}

        STRONG, WEAK, MIN_GAP = 0.60, 0.34, 0.15

        def best_done() -> Optional[tuple[float, int, str]]:
            ranked_done = self._rank_focus_matches(query, done_items) if done_items else []
            return ranked_done[0] if ranked_done and ranked_done[0][0] >= STRONG else None

        if not open_items:
            hit = best_done()
            if hit:
                return {"status": "already_done", "item": hit[2], "date": date_str}
            return {"status": "no_pending", "date": date_str}

        ranked = self._rank_focus_matches(query, open_items)
        plausible = [t for t in ranked if t[0] >= WEAK]
        top = ranked[0]

        # Nothing open matches strongly, but something already ticked does.
        if top[0] < STRONG:
            hit = best_done()
            if hit and hit[0] > top[0]:
                return {"status": "already_done", "item": hit[2], "date": date_str}

        if not plausible:
            return {"status": "not_found", "pending": [lbl for _, lbl in open_items][:6], "date": date_str}

        strong_hits = [t for t in plausible if t[0] >= STRONG]
        # Genuine ambiguity: the runner-up is itself a real match (strong) or near-tied
        # with the top. Two near-identical bills ("Gasnorte" / "Gasnorte Residencial") land here.
        if len(plausible) >= 2 and (
            plausible[1][0] >= STRONG or (plausible[0][0] - plausible[1][0]) < MIN_GAP
        ):
            pool = strong_hits if len(strong_hits) >= 2 else plausible
            return {"status": "ambiguous", "candidates": [lbl for _, _, lbl in pool][:4], "date": date_str}

        # A weak, non-strong single guess is not enough to divert a draft.
        if strict and not strong_hits:
            return {"status": "not_found", "pending": [lbl for _, lbl in open_items][:6], "date": date_str}

        _, top_idx, label = plausible[0]
        if apply:
            prefix = line_re.match(lines[top_idx]).group(1)
            lines[top_idx] = lines[top_idx].replace(f"{prefix}[ ]", f"{prefix}[x]", 1)
            trailing_nl = "\n" if content.endswith("\n") else ""
            self.write_file_atomic(cockpit_file, "\n".join(lines) + trailing_nl)
        return {"status": "completed", "item": label, "date": date_str}

    # `Pagar boleto: <beneficiary>** (<amount>) (Ref: <sender>)` — amount and ref optional.
    _BILL_LINE_RE = re.compile(
        r"Pagar boleto:\s*(?P<ben>.+?)\*\*"
        r"(?:\s*\((?P<amt>[^)]*)\))?"
        r"(?:\s*\(Ref:\s*(?P<ref>[^)]*)\))?",
        re.IGNORECASE,
    )

    @staticmethod
    def _sender_addr(text: Optional[str]) -> str:
        """The bare `foo@bar` address out of a `"Name" <foo@bar>` sender string."""
        m = re.search(r"<([^>]+@[^>]+)>", text or "")
        raw = m.group(1) if m else (text or "")
        return raw.strip().lower()

    @classmethod
    def _bill_already_scheduled(
        cls, content: str, beneficiary: str, amount: Optional[str], sender: Optional[str]
    ) -> bool:
        """Deterministic dedup for bill reminders. The beneficiary label is LLM-authored
        and drifts between sync runs (`Construtora Modelo e Imobiliária Ltda` /
        `Construtora Modelo / Condomínio Bosque Verde` / `Construtora Modelo e
        Imobiliária`), so an exact-string guard lets every re-send through. A bill is
        treated as already listed for the date when an existing `Pagar boleto` line
        agrees on any of: the normalized beneficiary, the sender address, or the leading
        beneficiary token (unless the two carry conflicting amounts)."""
        nb = cls._normalize_for_match(beneficiary)
        nb_head = nb.split()[0] if nb else ""
        amt = re.sub(r"\D", "", amount or "")
        addr = cls._sender_addr(sender)
        for line in content.splitlines():
            if "Pagar boleto:" not in line:
                continue
            m = cls._BILL_LINE_RE.search(line)
            if not m:
                continue
            eb = cls._normalize_for_match(m.group("ben") or "")
            eamt = re.sub(r"\D", "", m.group("amt") or "")
            eaddr = cls._sender_addr(m.group("ref") or "")
            if eb and eb == nb:
                return True
            if addr and eaddr and addr == eaddr:
                return True
            conflicting_amounts = bool(amt and eamt and amt != eamt)
            if nb_head and len(nb_head) >= 4 and eb.split()[:1] == [nb_head] and not conflicting_amounts:
                return True
        return False

    def schedule_bill_reminder(
        self,
        due_date: date,
        beneficiary: str,
        amount: Optional[str] = None,
        sender: Optional[str] = None,
    ) -> Path:
        """Schedules a bill payment reminder task in the Daily Cockpit for the specified due date."""
        date_str = due_date.strftime("%Y-%m-%d")
        cockpit_file = self.vault_path / "00_Cockpit" / "Daily" / f"{date_str}.md"

        if not cockpit_file.exists():
            self.create_daily_cockpit(target_date=due_date)

        content = self.read_file(cockpit_file) or ""
        if self._bill_already_scheduled(content, beneficiary, amount, sender):
            return cockpit_file

        amount_str = f" ({amount})" if amount else ""
        sender_ref = f" (Ref: {sender})" if sender else ""
        task_text = f"💳 **Pagar boleto: {beneficiary}**{amount_str}{sender_ref} 📅 {date_str}"

        content = self._insert_sorted_focus(content, task_text, due_date)
        self.write_file_atomic(cockpit_file, content)
        return cockpit_file

    def add_cockpit_focus(
        self,
        target_date: date,
        title: str,
        area: str = "Follow-up",
        description: str = "",
        dedupe_key: Optional[str] = None,
        due_date: Optional[date] = None,
    ) -> Path:
        """Inserts a single `- [ ] **title** (area) — description` task under the Focos
        header of a Daily Cockpit, creating the note if needed. Idempotent: skips when
        `dedupe_key` (default: the title) already appears in the note.

        `due_date`, when known, is stamped as a visible `📅 YYYY-MM-DD` token so the section
        can bucket/sort dated items (today/semana/acompanhamento, earliest first within each)
        — it does not change which day's cockpit the item lands in (callers route that
        themselves)."""
        date_str = target_date.strftime("%Y-%m-%d")
        cockpit_file = self.vault_path / "00_Cockpit" / "Daily" / f"{date_str}.md"
        desc = self._sanitize_inline_description(description)
        title_clean = self._sanitize_inline_description(title)
        task_text = f"**{title_clean}** ({area})" + (f" — {desc}" if desc else "")
        if due_date:
            task_text += f" 📅 {due_date.strftime('%Y-%m-%d')}"
        key = (dedupe_key or title_clean).strip()

        if not cockpit_file.exists():
            self.create_daily_cockpit(target_date=target_date)

        content = self.read_file(cockpit_file) or ""
        # Dedupe against checkbox lines only, matched on the normalised focus label, so a
        # stray substring elsewhere in the note (an e-mail subject, a brief) can't suppress
        # the task and two distinct items where one label prefixes the other stay separate.
        if key:
            key_norm = self._normalize_for_match(key)
            for ln in content.splitlines():
                if ln.lstrip().startswith(("- [ ]", "- [x]", "- [X]")):
                    if self._normalize_for_match(self._focus_label(ln)) == key_norm:
                        return cockpit_file

        content = self._insert_sorted_focus(content, task_text, target_date)
        self.write_file_atomic(cockpit_file, content)
        return cockpit_file

    # The heading that separates the machine-owned top of a meeting note (frontmatter,
    # header blockquote, rysOS Brief) from everything the human owns below it.
    _MEETING_HUMAN_MARKER = "## 📝 Notas & Discussão"

    @staticmethod
    def _meeting_note_event_id(path: Path) -> Optional[str]:
        """Reads the `event_id` frontmatter field of an existing meeting note, or None."""
        try:
            head = path.read_text(encoding="utf-8")[:1000]
        except (OSError, ValueError):  # missing, unreadable, or non-UTF-8 note
            return None
        m = re.search(r'^event_id:\s*"?([^"\n]*)"?\s*$', head, re.MULTILINE)
        return (m.group(1).strip() or None) if m else None

    @staticmethod
    def _meeting_note_title(path: Path) -> str:
        """Reads the `title` frontmatter field of an existing meeting note, or ''."""
        try:
            head = path.read_text(encoding="utf-8")[:1000]
        except (OSError, ValueError):
            return ""
        m = re.search(r'^title:\s*"?([^"\n]*)"?\s*$', head, re.MULTILINE)
        return m.group(1).strip() if m else ""

    # Leading date prefix on a transcript filename stem, e.g. "2026-09-15 " or
    # "2026-09-15 - " before the meeting title proper.
    _FILENAME_DATE_PREFIX_RE = re.compile(r"^\s*\d{4}[-_.]\d{2}[-_.]\d{2}\s*-?\s*")

    @classmethod
    def _title_from_filename_stem(cls, stem: str) -> str:
        return cls._FILENAME_DATE_PREFIX_RE.sub("", stem or "").strip()

    @classmethod
    def _meeting_note_signature(cls, path: Path) -> tuple[str, frozenset]:
        """The (start "HH:MM", normalized attendee-name set) of an existing meeting note.
        Used to recognize the *same* real meeting arriving under a different calendar
        `event_id` (e.g. the same invite duplicated on a Google account, or organizer +
        attendee copies) so it merges into one note instead of spawning `... (2).md`."""
        try:
            head = path.read_text(encoding="utf-8")[:2000]
        except (OSError, ValueError):
            return "", frozenset()
        mt = re.search(r'^time:\s*"?(\d{1,2}:\d{2})', head, re.MULTILINE)
        start = mt.group(1) if mt else ""
        names: set[str] = set()
        mblock = re.search(r"^attendees:\s*\n((?:[ \t]+-.*\n?)+)", head, re.MULTILINE)
        if mblock:
            for line in mblock.group(1).splitlines():
                item = line.split("-", 1)[1].strip() if "-" in line else ""
                wl = re.search(r"\[\[[^|\]]*\|([^\]]+)\]\]", item)
                raw = wl.group(1) if wl else item.strip('"').strip("[]")
                norm = cls._normalize_for_match(raw)
                if norm:
                    names.add(norm)
        return start, frozenset(names)

    def _resolve_meeting_path(
        self,
        date_str: str,
        slug: str,
        event_id: Optional[str],
        *,
        start_hhmm: str = "",
        attendees: Optional[list[str]] = None,
    ) -> Path:
        """Picks the file for this event, disambiguating a slug collision between two
        distinct events on the same day (`... (2).md`, `(3).md`, ...). An existing note
        with no/blank `event_id` (legacy) or a matching one is adopted for merge; so is
        one whose start time and attendee set are identical (same meeting, different
        calendar `event_id`)."""
        base = self.vault_path / "04_Meetings"
        want_set = frozenset(
            n for n in (self._normalize_for_match(a) for a in (attendees or [])) if n
        )
        cand = base / f"{date_str} - {slug}.md"
        n = 2
        while cand.exists():
            existing = self._meeting_note_event_id(cand)
            if not event_id or not existing or existing == event_id:
                return cand
            sig_start, sig_set = self._meeting_note_signature(cand)
            if start_hhmm and sig_start == start_hhmm and want_set == sig_set:
                return cand
            cand = base / f"{date_str} - {slug} ({n}).md"
            n += 1
            if n > 30:
                return cand
        return cand

    def find_matching_calendar_note(
        self,
        date_str: str,
        attendee_names: list[str],
        people_index: Optional[list[dict[str, Any]]] = None,
        filename_stem: str = "",
    ) -> Optional[Path]:
        """Looks for a same-day calendar-synced meeting note that a manually-dropped
        transcript should be filed into.

        Filename match runs first and wins outright: the owner's own convention is to name
        the dropped `.txt` after the note's exact title ("2026-09-15 Terapia com
        Fabiana.txt" → "2026-09-15 - Terapia com Fabiana.md"), which is deterministic and
        user-controlled — it should outrank any content-derived heuristic.

        Falling back to attendee overlap rather than the AI-derived title itself — a
        transcript's title is a content summary ("Alinhamento Estratégico Meridian -
        Billing Nimbus Cloud...") and almost never matches the calendar invite's own title
        ("Meridian [Estratégia]"), so slug-only matching in `_resolve_meeting_path` would
        spawn an orphan note instead of filing into the real one (2026-09-15 incident).

        A calendar note's attendees are often bare e-mails (`gil.prado@gmail.com`) while
        the transcript cites real names ("Gilberto Pagano") — `people_index` (from
        `list_people()`) bridges the two via each Person note's `email:` frontmatter, so an
        e-mail attendee counts as a match when it resolves to a cited name. The transcript
        also tends to cite first names or nicknames ("Boris", "Vivi") against the invite's
        full name ("Boris Lemos", "Vanessa Salgado"), so overlap is computed word-by-word
        rather than by exact normalized-string equality (also fixed 2026-09-15).

        Only considers same-day notes that don't already carry a `<!-- transcript: -->`
        marker (not yet claimed by another transcript). Prefers an unambiguous attendee-
        overlap match; if none, falls back to adopting the sole untouched same-day note
        when there is exactly one (the common single-meeting-that-day case) — otherwise
        returns None and the caller falls back to its normal title-slug resolution."""
        base = self.vault_path / "04_Meetings"
        if not base.exists():
            return None

        untouched: list[Path] = []
        for f in base.glob(f"{date_str} - *.md"):
            content = self.read_file(f) or ""
            if "<!-- transcript:" not in content:
                untouched.append(f)
        if not untouched:
            return None

        stem_title = self._normalize_for_match(self._title_from_filename_stem(filename_stem))
        if stem_title:
            exact = [f for f in untouched if self._normalize_for_match(self._meeting_note_title(f)) == stem_title]
            if len(exact) == 1:
                return exact[0]
            if not exact:
                prefix = [
                    f for f in untouched
                    if (t := self._normalize_for_match(self._meeting_note_title(f)))
                    and (stem_title.startswith(t) or t.startswith(stem_title))
                ]
                if len(prefix) == 1:
                    return prefix[0]

        # The account owner attends virtually every meeting, so his own name/e-mail is
        # zero-signal noise for disambiguation — without stripping it, two unrelated
        # same-day meetings both "match" on the owner alone and the tie falls through to
        # the single-untouched-note fallback instead of the real distinguishing overlap.
        self_tokens = {self._normalize_for_match(e) for e in settings.USER_EMAILS}
        self_tokens.add(self._normalize_for_match(user_name()))

        want = frozenset(
            n for n in (self._normalize_for_match(a) for a in attendee_names) if n
        ) - self_tokens
        email_to_name = {
            self._normalize_for_match(p["email"]): self._normalize_for_match(p["name"])
            for p in (people_index or []) if p.get("email") and p.get("name")
        }

        if want:
            # Word-level overlap: a cited "Boris" or "Vivi" must count against the
            # invite's full "boris leite" / "viviane salyna" — exact whole-string
            # equality between a first name and a full name never matches.
            want_words = {w for n in want for w in n.split() if len(w) > 2}
            candidates: list[tuple[int, Path]] = []
            for f in untouched:
                _, sig_set = self._meeting_note_signature(f)
                expanded = set(sig_set) - self_tokens
                for token in sig_set:
                    if token in email_to_name:
                        expanded.add(email_to_name[token])
                expanded -= self_tokens
                overlap = len({
                    name for name in expanded
                    if any(w in want_words for w in name.split() if len(w) > 2)
                })
                if overlap:
                    candidates.append((overlap, f))
            if candidates:
                candidates.sort(key=lambda t: t[0], reverse=True)
                if not (len(candidates) > 1 and candidates[0][0] == candidates[1][0]):
                    return candidates[0][1]

        if len(untouched) == 1:
            return untouched[0]
        return None

    @classmethod
    def _merge_meeting_note(cls, old_content: str, new_content: str) -> str:
        """Splices the freshly rendered machine sections (frontmatter, header, rysOS Brief)
        onto an existing note while keeping every human-owned section below
        `## 📝 Notas & Discussão` (notes, decided actions, anything the user appended)
        byte-for-byte. If the old note doesn't carry the marker (unknown layout, renamed
        heading), it is left entirely untouched rather than risk destroying content."""
        marker = cls._MEETING_HUMAN_MARKER
        old_idx = old_content.find(marker)
        if old_idx == -1:
            return old_content
        new_idx = new_content.find(marker)
        if new_idx == -1:
            return new_content
        return new_content[:new_idx] + old_content[old_idx:]

    def _resolve_attendee_display_names(self, attendees: list[str]) -> list[str]:
        """Maps a bare-email attendee (e.g. "gil.prado@gmail.com", the shape Google
        Calendar hands back when a guest has no shared display name) to the existing
        05_People note's real name when one exists with a matching `email:` — otherwise
        the raw attendee string passes through unchanged.

        Without this, `create_meeting_note`'s attendee wikilink (rendered straight from
        the raw string) targets `05_People/<email>` instead of the real person note, and
        a later Triagem "criar novo" on that phantom link mints a garbage duplicate note
        keyed by the email with a blank `email:` field of its own (see the
        gil.prado@gmail.com dedup, 2026-09-17) — the same class of bug as
        `create_resource_note` lacking an existence check (see `ensure_resource_note`).

        The account owner is a special case: his own 05_People note's `email:`
        frontmatter carries only ONE of his several real addresses (the others are
        prose-only in the note body), but a calendar synced from any of his accounts
        lists him as an attendee under THAT account's own email — which never matches
        the single frontmatter value. Those are matched against `settings.USER_EMAILS`
        directly instead (phantom-linked despite
        the owner's note existing, because its `email:` is a different account of the owner) — mirrors
        the same self-identity handling `_resolve_meeting_path` already does below."""
        email_to_name = {
            self._normalize_for_match(p["email"]): p["name"]
            for p in self.list_people() if p.get("email")
        }
        self_emails = {self._normalize_for_match(e) for e in settings.USER_EMAILS}
        resolved = []
        for a in attendees:
            if "@" not in a:
                resolved.append(a)
                continue
            key = self._normalize_for_match(a)
            if key in self_emails:
                resolved.append(user_name())
            else:
                resolved.append(email_to_name.get(key, a))
        return resolved

    def create_meeting_note(
        self,
        summary: str,
        start_time: datetime,
        end_time: datetime,
        attendees: list[str],
        meet_link: Optional[str] = None,
        context_brief: str = "",
        priority: str = "baixa",
        related_projects: Optional[list[str]] = None,
        event_id: Optional[str] = None,
        area_link: str = "",
        topics: Optional[list[str]] = None,
        create_only: bool = False,
        override_path: Optional[Path] = None,
    ) -> Path:
        """Creates or refreshes a meeting note linked to attendees and the calendar event.

        On a re-sync the machine-owned top of the note (frontmatter, header, rysOS Brief)
        is regenerated but the human-owned sections below `## 📝 Notas & Discussão` are
        preserved — the daily sync calls this for every event unconditionally, so a plain
        rewrite would wipe hand-typed notes and follow-ups (see workbench gap G19).

        `create_only=True` returns the resolved path untouched when a note already exists:
        the transcript pipeline passes `event_id=None`, which makes `_resolve_meeting_path`
        adopt a same-day/same-slug calendar note — re-rendering it would wipe that note's
        real `attendees`, `meet_link` and AI Brief (the transcript render carries none of
        those). The ata block is spliced in below the human marker by
        `append_transcript_minutes`, which never touches the frontmatter.

        `override_path`, when given, skips slug/event_id path resolution entirely and
        targets that exact note instead — used by the transcript pipeline when it has
        already matched this transcript to a specific same-day calendar note by attendee
        overlap (a content-derived AI title almost never matches the calendar's own title,
        so slug matching alone would spawn an orphan note; see 2026-09-15 incident)."""
        date_str = start_time.strftime("%Y-%m-%d")
        slug = self._slugify(summary)
        meeting_path = override_path or self._resolve_meeting_path(
            date_str, slug, event_id,
            start_hhmm=start_time.strftime("%H:%M"), attendees=attendees,
        )
        if create_only and meeting_path.exists():
            return meeting_path

        attendees = self._resolve_attendee_display_names(attendees)

        tmpl = Template(MEETING_TEMPLATE)
        rendered = tmpl.render(
            date_str=date_str,
            slug=slug,
            summary=summary,
            start_time_str=start_time.strftime("%H:%M"),
            end_time_str=end_time.strftime("%H:%M"),
            priority=priority,
            attendees=attendees,
            meet_link=meet_link,
            context_brief=context_brief or "Reunião sincronizada via rysOS.",
            related_projects=related_projects or [],
            topics=topics or [],
            area_link=area_link or "",
            event_id=event_id or "",
        )

        if meeting_path.exists():
            old = self.read_file(meeting_path) or ""
            merged = self._merge_meeting_note(old, rendered)
            if merged != old:
                self.write_file_atomic(meeting_path, merged)
        else:
            self.write_file_atomic(meeting_path, rendered)
        return meeting_path

    # Lines the fresh MEETING_TEMPLATE leaves under `_MEETING_HUMAN_MARKER` by default —
    # anything else there means the owner actually typed something, so the note is kept
    # regardless of age.
    _MEETING_HUMAN_PLACEHOLDER_LINES = {
        "", "-", "---", "## ✅ Ações & Follow-ups Decididos", "- [ ]",
    }

    _MEETING_TIME_RE = re.compile(r'^time:\s*"?(\d{2}:\d{2})\s*-\s*\d{2}:\d{2}"?\s*$', re.MULTILINE)

    def find_stale_meeting_notes(self, hours: int) -> list[Path]:
        """Meeting notes whose own start time (frontmatter `date` + `time`, not the
        note's creation time) is more than `hours` in the past, with no transcript ata
        and no hand-typed content below the human marker — dead weight from the eager
        per-event note creation on calendar sync. Returns candidates; does not touch
        the filesystem (see `archive_stale_meeting_notes`)."""
        meetings_dir = self.vault_path / "04_Meetings"
        if not meetings_dir.is_dir():
            return []
        cutoff = datetime.now() - timedelta(hours=hours)
        stale: list[Path] = []
        for path in meetings_dir.glob("*.md"):
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, ValueError):
                continue
            date_match = re.search(r'^date:\s*(\d{4}-\d{2}-\d{2})\s*$', content, re.MULTILINE)
            if not date_match:
                continue
            time_match = self._MEETING_TIME_RE.search(content)
            try:
                if time_match:
                    start_dt = datetime.strptime(
                        f"{date_match.group(1)} {time_match.group(1)}", "%Y-%m-%d %H:%M"
                    )
                else:
                    # no parseable start time (e.g. all-day event) — treat the whole
                    # day as the window, same as the old date-only behavior
                    start_dt = datetime.strptime(date_match.group(1), "%Y-%m-%d")
            except ValueError:
                continue
            if start_dt > cutoff:
                continue
            if "<!-- transcript:" in content:
                continue  # real ata landed — never prune
            marker_idx = content.find(self._MEETING_HUMAN_MARKER)
            if marker_idx == -1:
                continue  # unrecognized layout — don't risk touching it
            human_section = content[marker_idx + len(self._MEETING_HUMAN_MARKER):]
            lines = [ln.strip() for ln in human_section.splitlines()]
            if any(ln not in self._MEETING_HUMAN_PLACEHOLDER_LINES for ln in lines):
                continue  # the owner wrote something by hand — keep
            stale.append(path)
        return stale

    def archive_stale_meeting_notes(self, hours: int) -> list[Path]:
        """Moves stale meeting notes (see `find_stale_meeting_notes`) to
        08_Archive/Meetings/, preserving the filename, and repoints that day's Cockpit
        Agenda wikilink (which points at `04_Meetings/{filename}` — see
        `templates.py`'s `event.meeting_note_name`) at the new path. Without this, the
        note is still safe (moved, not deleted) but every Cockpit Daily note generated
        before the prune permanently loses a working link to it."""
        dest_dir = self.vault_path / "08_Archive" / "Meetings"
        dest_dir.mkdir(parents=True, exist_ok=True)
        archived = []
        for path in self.find_stale_meeting_notes(hours):
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, ValueError):
                content = ""
            date_m = re.search(r'^date:\s*(\d{4}-\d{2}-\d{2})\s*$', content, re.MULTILINE) if content else None
            dest = dest_dir / path.name
            try:
                path.replace(dest)
            except OSError:
                continue
            archived.append(dest)
            if date_m:
                self._repoint_cockpit_agenda_link(date_m.group(1), path.name)
        return archived

    def _repoint_cockpit_agenda_link(self, date_str: str, meeting_filename: str) -> None:
        """After a meeting note moves out of 04_Meetings/ (pruned as stale), fixes that
        day's Cockpit Agenda wikilink so it keeps resolving instead of going dead."""
        cockpit_path = self.vault_path / "00_Cockpit" / "Daily" / f"{date_str}.md"
        content = self.read_file(cockpit_path)
        if not content:
            return
        old_ref = f"[[04_Meetings/{meeting_filename}"
        new_ref = f"[[08_Archive/Meetings/{meeting_filename}"
        if old_ref in content:
            self.write_file_atomic(cockpit_path, content.replace(old_ref, new_ref))

    _DECISION_ARCHIVE_FOOTER = "_Arquivada em "

    def archive_garbage_decisions(self) -> list[tuple[str, Path]]:
        """Moves every 03_Decisions note with `status: descartado` into
        08_Archive/Decisions/, stamping a dated footer first. Idempotent — a note
        already carrying the footer is skipped, so a second run never re-archives or
        double-stamps. Returns (decision id, archived path) for each note moved, so the
        caller can drop the matching DecisionRecord DB row.

        Does not rewrite any other note's wikilink pointing at the archived decision —
        notes eligible for this status are expected to be validation mistakes or
        duplicate re-extractions with no legitimate inbound references.
        """
        decisions_dir = self.vault_path / "03_Decisions"
        if not decisions_dir.is_dir():
            return []
        dest_dir = self.vault_path / "08_Archive" / "Decisions"
        id_re = re.compile(r'^id:\s*"?([^"\n]+?)"?\s*$', re.MULTILINE)
        status_re = re.compile(r'^status:\s*"?([^"\n#]+?)"?\s*(?:#.*)?$', re.MULTILINE)
        archived: list[tuple[str, Path]] = []
        for path in decisions_dir.glob("*.md"):
            content = self.read_file(path)
            if not content:
                continue
            status_m = status_re.search(content)
            if not status_m or status_m.group(1).strip() != "descartado":
                continue
            if self._DECISION_ARCHIVE_FOOTER in content:
                continue  # already stamped — shouldn't still be here, but never re-archive
            id_m = id_re.search(content)
            dec_id = id_m.group(1).strip() if id_m else None

            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / path.name
            stamp = date.today().strftime("%Y-%m-%d")
            final = content.rstrip() + f"\n\n---\n\n{self._DECISION_ARCHIVE_FOOTER}{stamp}._\n"
            self.write_file_atomic(dest, final)
            if path.resolve() != dest.resolve():
                path.unlink(missing_ok=True)
            if dec_id:
                archived.append((dec_id, dest))
        return archived

    _DEDUP_MERGE_KIND_DIRS = {
        "people": "05_People", "projects": "01_Projects", "resources": "06_Resources",
        "decisions": "03_Decisions",
    }

    def repoint_backlinks(
        self, old_rel_path: str, new_rel_path: str, backlinks: Optional[list[dict[str, Any]]] = None,
    ) -> list[str]:
        """Rewrites every real backlink to `old_rel_path` so it points at `new_rel_path`
        instead — the repoint step of a `/dedup` merge (`merge_notes`). Pass a precomputed
        `backlinks` (`rysos.vault.dedup.find_backlinks`) to avoid a fresh vault scan when
        the caller already has one from the triage pass; omitted, this scans fresh.

        Idempotent by construction: with `backlinks` omitted, `find_backlinks` only
        reports sources still pointing at `old_rel_path`, and `rewrite_wikilinks` only
        rewrites a link that still resolves to `old_rel_path`'s stem — so re-running this
        after a partial failure (an rclone `[Errno 5]` mid-merge is a known real failure
        mode here) converges instead of double-rewriting or corrupting anything already
        fixed. Returns the list of source paths actually touched.
        """
        from rysos.vault.dedup import find_backlinks, rewrite_wikilinks

        if backlinks is None:
            backlinks = find_backlinks(self, old_rel_path)
        old_stem = Path(old_rel_path).stem
        new_stem = Path(new_rel_path).stem
        touched: list[str] = []
        for source in sorted({b["source_path"] for b in backlinks}):
            path = self.vault_path / source
            content = self.read_file(path)
            if not content:
                continue
            new_content = rewrite_wikilinks(content, old_stem, new_stem)
            if new_content != content:
                self.write_file_atomic(path, new_content)
                touched.append(source)
        return touched

    def merge_notes(
        self, kind: str, survivor_path: str, loser_path: str, consolidated_summary: Optional[str] = None,
    ) -> dict[str, Any]:
        """Applies one approved `/dedup` merge for the people/projects/resources/decisions
        note-pair shape (a diary-mention link is a different operation shape — a sentence
        edit, not a note-pair merge — see `wikify_diary_mention`). the owner (via the
        DedupSuggestion triage flow) has already picked which note survives; this never
        decides that. This module never touches the database — for `kind="decisions"`,
        the CALLER is responsible for deleting the loser's `DecisionRecord` row (this
        method only merges the two vault notes), same as `archive_garbage_decisions`.

        Order is deliberate for crash safety — an rclone `[Errno 5]` mid-tick is a known
        real failure mode here:
          1. fold the AI's `consolidated_summary` into the survivor (additive, guarded so
             a retry never double-appends)
          2. repoint every backlink at the loser onto the survivor (idempotent — see
             `repoint_backlinks`)
          3. archive the loser to `08_Archive/{kind}/` LAST, only after every other note
             that could reference it has been fixed up

        A step that already happened on a prior partial run is a no-op on retry, so
        calling this again after a crash converges rather than corrupting anything.
        Returns `{"survivor_path", "archived_loser_path", "repointed_sources"}`.
        """
        if kind not in self._DEDUP_MERGE_KIND_DIRS:
            raise ValueError(f"merge_notes: unsupported kind {kind!r}")
        if survivor_path == loser_path:
            raise ValueError("merge_notes: survivor and loser are the same note")

        survivor_abs = self.vault_path / survivor_path
        loser_abs = self.vault_path / loser_path
        survivor_content = self.read_file(survivor_abs)
        if survivor_content is None:
            raise FileNotFoundError(f"merge_notes: survivor note not found: {survivor_path}")

        if consolidated_summary:
            stamp = date.today().strftime("%Y-%m-%d")
            loser_name = Path(loser_path).stem
            footer = f'\n\n---\n_Fundido com "{loser_name}" em {stamp} (dedup):_ {consolidated_summary}\n'
            if footer not in survivor_content:
                self.write_file_atomic(survivor_abs, survivor_content.rstrip() + "\n" + footer)

        repointed = self.repoint_backlinks(loser_path, survivor_path)

        dest_dir = self.vault_path / "08_Archive" / self._DEDUP_MERGE_KIND_DIRS[kind].split("_", 1)[1]
        archived_path: Optional[Path] = None
        if loser_abs.exists():
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / loser_abs.name
            loser_abs.replace(dest)
            archived_path = dest

        return {
            "survivor_path": survivor_path,
            "archived_loser_path": str(archived_path) if archived_path else None,
            "repointed_sources": repointed,
        }

    def wikify_diary_mention(self, orphan_path: str, mentions: list[dict[str, Any]]) -> list[str]:
        """Applies one approved `/dedup` diary-mention link (`rysos.vault.dedup.
        find_diary_backlink_candidates` output for one orphan note): turns the plain-text
        name in each listed Cockpit Daily's free-text block into a real wikilink. Exact
        substring replace of that mention's own `matched_text` sample, never fuzzy — if it
        isn't found verbatim any more (the day's note changed since detection), that day
        is skipped rather than guessed at.

        Touches only the block between the `notas-livres` markers, never the rest of the
        Cockpit note. Idempotent: a day whose block already links this note (checked
        before writing) is skipped, so re-running after a partial failure never double-
        wraps a mention or corrupts a link. Returns the Cockpit note paths actually
        changed.
        """
        orphan_stem = Path(orphan_path).stem
        link_target = orphan_path[:-3] if orphan_path.lower().endswith(".md") else orphan_path
        touched: list[str] = []
        for mention in mentions:
            cockpit_path = mention.get("cockpit_path")
            matched_text = mention.get("matched_text")
            if not cockpit_path or not matched_text:
                continue
            path = self.vault_path / cockpit_path
            content = self.read_file(path)
            if not content:
                continue
            span = self._FREE_NOTES_RE.search(content)
            if not span:
                continue
            block = content[span.start(2):span.end(2)]
            if f"[[{link_target}" in block or f"[[{orphan_stem}" in block:
                continue  # already linked here — idempotent, nothing left to do
            idx = block.find(matched_text)
            if idx == -1:
                continue  # detector's sample text isn't there verbatim any more — skip
            new_block = block[:idx] + f"[[{link_target}|{matched_text}]]" + block[idx + len(matched_text):]
            new_content = content[:span.start(2)] + new_block + content[span.end(2):]
            self.write_file_atomic(path, new_content)
            touched.append(cockpit_path)
        return touched

    @staticmethod
    def _transcript_marker(digest: str) -> str:
        return f"<!-- transcript:{digest[:12]} -->"

    # Plain-text tags left on unresolved entity lines in the ata block; patch_transcript_entity
    # (Phase 2) finds and rewrites these once the user validates the entity.
    _ATA_UNCONFIRMED = "⚠️ não confirmado"
    _ATA_NEEDS_VALIDATION = "⚠️ requer validação"

    def _rich_minutes_lines(
        self,
        rich: dict[str, Any],
        *,
        resolved_names: Optional[dict[str, str]] = None,
        unconfirmed_names: Optional[set[str]] = None,
    ) -> list[str]:
        """Render the canonical 17-key minutes dict into markdown section lines.

        Shared verbatim by the meeting ata (`append_transcript_minutes`) and the spoken
        diary (`append_diary_entry`): every dimension the model captured, people/projects
        as wikilinks when in `resolved_names` else bold + `⚠️ não confirmado` when in
        `unconfirmed_names` (all unresolved when that is None). Pure string building.
        """
        resolved = resolved_names or {}
        out: list[str] = []

        def _link_or_plain(name: str, folder: str, *, bold_plain: bool) -> str:
            path = resolved.get(self._normalize_for_match(name))
            if path:
                return f"[[{folder}/{Path(path).stem}|{name}]]"
            return f"**{name}**" if bold_plain else name

        def _person_needs_tag(name: str) -> bool:
            n = self._normalize_for_match(name)
            if n in resolved:
                return False
            return True if unconfirmed_names is None else n in unconfirmed_names

        def _section(header: str, body_lines: list[str]) -> None:
            if body_lines:
                out.append(header)
                out.extend(body_lines)
                out.append("")

        people_lines = []
        for p in rich.get("people", []):
            tagged = _person_needs_tag(p["name"])
            nm = _link_or_plain(p["name"], "05_People", bold_plain=tagged)
            tail = ", ".join(x for x in (p.get("role", ""), p.get("org", "")) if x)
            line = f"- {nm}" + (f" — {tail}" if tail else "")
            if tagged:
                line += f"  ·  {self._ATA_UNCONFIRMED}"
            people_lines.append(line)
        _section("**Participantes citados:**", people_lines)

        if rich.get("para_area"):
            area_file, area_title = self._resolve_area(rich["para_area"])
            rationale = rich.get("para_area_rationale", "")
            _section("**Área sugerida:**", [f"- {area_title}" + (f" — {rationale}" if rationale else "")])

        proj_lines = []
        for pr in rich.get("projects", []):
            nm = _link_or_plain(pr["name"], "01_Projects", bold_plain=False)
            bits = nm + (f" ({pr['status']})" if pr.get("status") else "") + (f" — {pr['summary']}" if pr.get("summary") else "")
            if self._normalize_for_match(pr["name"]) not in resolved:
                bits += f"  ·  {self._ATA_UNCONFIRMED}"
            proj_lines.append(f"- {bits}")
        _section("**Projetos citados:**", proj_lines)

        _section("**Organizações citadas:**", [f"- {o}" for o in rich.get("organizations", [])])
        _section("**Resumo:**", [f"- {b}" for b in rich.get("summary_bullets", [])])

        dec_lines = []
        for dc in rich.get("decisions", []):
            extra = ", ".join(x for x in (dc.get("rationale", ""), (f"resp. {dc['owner']}" if dc.get("owner") else "")) if x)
            dec_lines.append(f"- {dc['title']} [{dc.get('status', 'em_analise')}]"
                             + (f" — {extra}" if extra else "")
                             + f"  ·  {self._ATA_NEEDS_VALIDATION}")
        _section("**Decisões discutidas:**", dec_lines)

        act_lines = []
        for it in rich.get("action_items", []):
            tail = (f" — {it['owner']}" if it.get("owner") else "") + (f" (prazo: {it['due']})" if it.get("due") else "")
            act_lines.append(f"- [ ] {it['text']}{tail}")
        _section("**Ações & Follow-ups:**", act_lines)

        _section("**Recursos & documentos citados:**",
                 [f"- {(r['type'] + ': ') if r.get('type') else ''}{r['name']}" + (f" — {r['note']}" if r.get("note") else "")
                  for r in rich.get("resources", [])])
        _section("**Riscos & atenção:**", [f"- {r}" for r in rich.get("risks", [])])
        _section("**Financeiro citado:**", [f"- {f}" for f in rich.get("financials", [])])
        _section("**Prazos:**", [f"- {d['what']}: {d['when']}" if d.get("when") else f"- {d['what']}" for d in rich.get("deadlines", [])])
        _section("**Perguntas em aberto:**", [f"- {q}" for q in rich.get("open_questions", [])])
        _section("**Próximas reuniões:**", [f"- {m}" for m in rich.get("followups_meetings", [])])
        _section("**Glossário:**", [f"- {g['term']}: {g['meaning']}" if g.get("meaning") else f"- {g['term']}" for g in rich.get("glossary", [])])
        return out

    def append_transcript_minutes(
        self,
        meeting_path: Path,
        digest: str,
        *,
        title: str,
        rich: Optional[dict[str, Any]] = None,
        resolved_names: Optional[dict[str, str]] = None,
        unconfirmed_names: Optional[set[str]] = None,
        summary_bullets: Optional[list[str]] = None,
        decisions: Optional[list[str]] = None,
        action_items: Optional[list[dict[str, str]]] = None,
        source_link: str = "",
        meeting_date: str = "",
    ) -> bool:
        """Appends a structured minutes block to an existing meeting note, below every
        human-owned section, guarded by a `<!-- transcript:<sha> -->` marker.

        When `rich` (the canonical 17-key minutes dict) is given, every captured dimension
        is rendered. Otherwise the legacy summary/decisions/action_items layout is used.
        `resolved_names` maps a normalised person/project name to its vault path for the
        entities confidently matched to an existing note — those render as wikilinks.
        `unconfirmed_names` is the set of normalised person names still awaiting
        validation — those render bold + `⚠️ não confirmado`. Any other name (a one-shot
        mention that never became a candidate) renders as plain text, no tag. When
        `unconfirmed_names` is None, every unresolved name is tagged (legacy behaviour).

        The marker (not the DB row) is the idempotency source of truth: the vault is a
        Drive mount edited from Obsidian too. Returns True when newly written, False when
        the marker was already present or the note is missing.
        """
        existing = self.read_file(meeting_path)
        if existing is None:
            return False
        marker = self._transcript_marker(digest)
        if marker in existing:
            return False

        lines = ["", "---", "", marker, f"### 📄 Ata da transcrição: {title}".rstrip()]
        meta_bits = [b for b in (meeting_date, source_link) if b]
        if rich and rich.get("meeting_type"):
            meta_bits.append(f"tipo: {rich['meeting_type']}")
        if meta_bits:
            lines.append(f"_{' · '.join(meta_bits)}_")
        if rich and rich.get("language_quality_notes"):
            lines.append(f"_{rich['language_quality_notes']}_")
        lines.append("")

        def _section(header: str, body_lines: list[str]) -> None:
            if body_lines:
                lines.append(header)
                lines.extend(body_lines)
                lines.append("")

        if rich is not None:
            lines.extend(self._rich_minutes_lines(
                rich, resolved_names=resolved_names, unconfirmed_names=unconfirmed_names,
            ))
        else:
            _section("**Resumo:**", [f"- {b.strip()}" for b in (summary_bullets or []) if b.strip()])
            _section("**Decisões registradas:**", [f"- {d.strip()}" for d in (decisions or []) if d.strip()])
            act_lines = []
            for it in (action_items or []):
                text = (it.get("text") or "").strip()
                if not text:
                    continue
                tail = (f" — {it['owner'].strip()}" if it.get("owner") else "") + (f" (prazo: {it['due'].strip()})" if it.get("due") else "")
                act_lines.append(f"- [ ] {text}{tail}")
            _section("**Ações & Follow-ups da transcrição:**", act_lines)

        block = "\n".join(lines).rstrip() + "\n"
        bullets = (rich.get("summary_bullets") if rich is not None else summary_bullets) or []
        existing = self._fill_empty_notes_section(existing, bullets)
        self.write_file_atomic(meeting_path, existing.rstrip() + "\n" + block)
        return True

    def _fill_empty_notes_section(self, content: str, bullets: list[str]) -> str:
        """Puts the ata's summary bullets under `## 📝 Notas & Discussão` when that section
        still holds only the template placeholder. A section the user typed in is never
        touched, and a second transcript never overwrites the first one's summary (the
        section is no longer empty by then)."""
        items = [f"- {b.strip()}" for b in bullets if isinstance(b, str) and b.strip()]
        if not items:
            return content
        lines = content.split("\n")
        start = next((i for i, ln in enumerate(lines) if ln.strip() == self._MEETING_HUMAN_MARKER), None)
        if start is None:
            return content
        end = start + 1
        while end < len(lines) and not lines[end].startswith("## ") and lines[end].strip() != "---":
            end += 1
        if any(ln.strip() not in ("", "-") for ln in lines[start + 1:end]):
            return content
        lines[start + 1:end] = items + [""]
        return "\n".join(lines)

    def ensure_journal_note(self, entry_date: date) -> Path:
        """The day's spoken-diary note at ``09_Journal/<YYYY-MM-DD>.md``, created from
        JOURNAL_TEMPLATE if missing. Create-only — never rewrites an existing note
        (multiple entries in a day are appended as blocks by ``append_diary_entry``).
        ``create_daily_cockpit`` never touches ``09_Journal`` so there is no collision.
        """
        date_str = entry_date.strftime("%Y-%m-%d")
        path = self.vault_path / "09_Journal" / f"{date_str}.md"
        if not path.exists():
            rendered = Template(JOURNAL_TEMPLATE).render(
                date_str=date_str,
                date_formatted=entry_date.strftime("%d/%m/%Y"),
            )
            self.write_file_atomic(path, rendered)
        return path

    def append_diary_entry(
        self,
        journal_path: Path,
        digest: str,
        *,
        title: str,
        rich: Optional[dict[str, Any]] = None,
        resolved_names: Optional[dict[str, str]] = None,
        unconfirmed_names: Optional[set[str]] = None,
        raw_narration: str = "",
        source_link: str = "",
        entry_date: str = "",
        tone: str = "",
    ) -> bool:
        """Append one spoken-diary entry block to the day's journal note.

        Below a ``<!-- transcript:<sha12> -->`` marker (**deliberately the same marker
        as the meeting ata** — ``patch_transcript_entity`` / ``entity_apply`` /
        ``apply_pending_triagem`` all hard-code it, so a diary person/project resolved
        via the Triagem note still gets its wikilink upgrade for free):

        * the raw narration **verbatim**, blockquote-prefixed (never sanitised — "jamais
          inventar"; the ``> `` prefix also shields a stray ``#``/``---`` in the
          transcript from breaking note structure);
        * the structured extraction, rendered by the shared ``_rich_minutes_lines``.

        The marker (not the DB row) is the idempotency source of truth. Returns True when
        newly written, False when the marker is already present or the note is missing.
        """
        existing = self.read_file(journal_path)
        if existing is None:
            return False
        marker = self._transcript_marker(digest)
        if marker in existing:
            return False

        lines = ["", "---", "", marker, f"### 📓 Entrada: {title}".rstrip()]
        meta_bits = [b for b in (entry_date, source_link) if b]
        if tone:
            meta_bits.append(f"tom: {tone}")
        if meta_bits:
            lines.append(f"_{' · '.join(meta_bits)}_")
        if rich and rich.get("language_quality_notes"):
            lines.append(f"_{rich['language_quality_notes']}_")
        lines.append("")

        narration = (raw_narration or "").strip()
        if narration:
            lines.append("#### 🎙️ Narração (verbatim)")
            lines.append("")
            lines.extend(f"> {ln}" if ln.strip() else ">" for ln in narration.splitlines())
            lines.append("")

        if rich is not None:
            body = self._rich_minutes_lines(
                rich, resolved_names=resolved_names, unconfirmed_names=unconfirmed_names,
            )
            if body:
                lines.append("#### 🧩 Extração estruturada")
                lines.append("")
                lines.extend(body)

        block = "\n".join(lines).rstrip() + "\n"
        self.write_file_atomic(journal_path, existing.rstrip() + "\n" + block)
        return True

    def patch_transcript_entity(
        self,
        meeting_path: Path,
        digest: str,
        *,
        kind: str,
        surface_form: str,
        link_target: str,
        display_name: Optional[str] = None,
    ) -> bool:
        """Upgrade a plain-text person/project mention in a transcript ata block to a
        wikilink and drop its `⚠️ não confirmado` tag, once the user has validated it.

        Operates ONLY on the slice from this transcript's ``<!-- transcript:<sha> -->``
        marker to EOF, so the note frontmatter (regenerated by ``_merge_meeting_note`` on
        every calendar re-sync) and any other transcript's block in the same note are never
        touched. Matches the exact rendered line prefix (``- **Name**`` for a person,
        ``- Name`` for a project) with a trailing word boundary, so "Nilton" never patches
        "Nilton Zeich". Idempotent: a line already linked (tag gone) yields no write.
        Returns True when the file changed.
        """
        content = self.read_file(meeting_path)
        if content is None:
            return False
        marker = self._transcript_marker(digest)
        idx = content.find(marker)
        if idx == -1:
            return False
        head, block = content[:idx], content[idx:]

        folder = "05_People" if kind == "person" else "01_Projects"
        stem = Path(link_target).stem
        wikilink = f"[[{folder}/{stem}|{display_name or surface_form}]]"
        tag = f"  ·  {self._ATA_UNCONFIRMED}"
        old_prefix = f"- **{surface_form}**" if kind == "person" else f"- {surface_form}"
        new_prefix = f"- {wikilink}"

        out, changed = [], False
        for line in block.split("\n"):
            if line.startswith(old_prefix) and (
                len(line) == len(old_prefix) or line[len(old_prefix)] == " "
            ):
                rest = line[len(old_prefix):]
                if rest.endswith(tag):
                    rest = rest[: -len(tag)]
                new_line = new_prefix + rest
                if new_line != line:
                    out.append(new_line)
                    changed = True
                    continue
            out.append(line)
        if not changed:
            return False
        self.write_file_atomic(meeting_path, head + "\n".join(out))
        return True

    _MEETING_GENERATED_HEADING = "## 🔗 Itens Gerados desta Reunião"

    def append_meeting_generated_link(
        self, meeting_path: Path, link_target: str, display_name: str,
    ) -> bool:
        """Appends a `[[link_target|display_name]]` wikilink to `meeting_path` under a
        dedicated heading, so a Resource or Decision created FROM this meeting is
        reachable from it too — not just the other way around.

        Person/Project mentions get a real Meeting->Entity link via
        `patch_transcript_entity` upgrading their own rendered "ata" bullet line in
        place; Resources and Decisions have no such line to upgrade (see
        `entity_apply._apply_new`'s resource/decision branches), so without this call
        the meeting note is only ever reachable FROM the resource/decision it produced
        (which cites its source), never the other way — every resource note in the
        vault showed up as a graph orphan until this was added (2026-09-17 /dedup
        investigation). `link_target` is a vault-relative path with no extension, e.g.
        ``"06_Resources/Leituras_e_Pesquisas/Nome"``. Idempotent: a link to the same
        target is never duplicated. Returns True when the file changed.
        """
        return self._append_link_under_heading(
            meeting_path, self._MEETING_GENERATED_HEADING, link_target, display_name,
        )

    _DATAVIEW_FENCE_RE = re.compile(r"```dataview\n.*?\n```\n?", re.DOTALL)

    def _append_link_under_heading(
        self, path: Path, heading: str, link_target: str, display_name: str,
        *, drop_dataview: bool = False,
    ) -> bool:
        """Appends `- [[link_target|display_name]]` as the last item of the section under
        `heading` (creating the heading at EOF when absent). Idempotent: any existing
        link to `link_target` — whatever its alias — counts as already present. With
        `drop_dataview`, a ```dataview fence inside that section is removed first (the
        vault has no Dataview plugin, so it only ever rendered as raw code). Returns True
        when the file changed."""
        content = self.read_file(path)
        if content is None:
            return False
        wikilink = f"[[{link_target}|{display_name}]]"
        if wikilink in content or f"[[{link_target}]]" in content or f"[[{link_target}|" in content:
            return False

        lines = content.split("\n")
        start = next((i for i, ln in enumerate(lines) if ln.strip() == heading), None)
        if start is None:
            sep = "" if content.endswith("\n") else "\n"
            new_content = f"{content}{sep}\n---\n\n{heading}\n- {wikilink}\n"
        else:
            end = start + 1
            while end < len(lines) and not lines[end].startswith("## "):
                end += 1
            if drop_dataview:
                body = self._DATAVIEW_FENCE_RE.sub("", "\n".join(lines[start + 1:end])).split("\n")
                lines[start + 1:end] = body
                end = start + 1 + len(body)
            # land after the section's last content line, not after its trailing `---`
            ins = end
            while ins > start + 1 and lines[ins - 1].strip() in ("", "---"):
                ins -= 1
            lines.insert(ins, f"- {wikilink}")
            new_content = "\n".join(lines)

        self.write_file_atomic(path, new_content)
        return True

    _PROJECT_SECTION_HEADINGS = {
        "meeting": "## 🧠 Registro de Contexto & Reuniões",
        "decision": "## ⚖️ Decisões Vinculadas",
        "resource": "## 📚 Recursos & Referências",
    }

    def append_project_link(
        self, project_path: Path, kind: str, link_target: str, display_name: str,
    ) -> bool:
        """Project -> child half of the project graph edge: lists a meeting / decision /
        resource (`kind` = meeting | decision | resource) under its section of the
        project note. `link_target` is vault-relative with no extension. Idempotent."""
        return self._append_link_under_heading(
            project_path, self._PROJECT_SECTION_HEADINGS[kind], link_target, display_name,
            drop_dataview=True,
        )

    def related_project_stems(self, note_path: Path) -> list[str]:
        """Project note stems listed in a note's `related_projects:` frontmatter (block or
        inline list), in order. Empty when the key is absent/blank or there is no frontmatter."""
        content = self.read_file(note_path)
        m = re.match(r"---\n(.*?)\n---(?:\n|$)", content or "", re.DOTALL)
        if not m:
            return []
        lines = m.group(1).split("\n")
        idx = next((i for i, ln in enumerate(lines) if ln.startswith("related_projects:")), None)
        if idx is None:
            return []
        block = [lines[idx]]
        for ln in lines[idx + 1:]:
            if ln.strip() and not re.match(r"\s*-\s|\s+\S", ln):
                break
            block.append(ln)
        stems = re.findall(r"\[\[01_Projects/([^\]|#]+)", "\n".join(block))
        return list(dict.fromkeys(s.strip() for s in stems))

    def remove_project_link(self, project_path: Path, link_target: str) -> bool:
        """Undo of `append_project_link`: drops every `- [[link_target...]]` list item from
        the project note. Returns True when the file changed."""
        content = self.read_file(project_path)
        if content is None:
            return False
        item = re.compile(rf"^- \[\[{re.escape(link_target)}(?:\|[^\]]*)?\]\][ \t]*\n?", re.MULTILINE)
        new_content = item.sub("", content)
        if new_content == content:
            return False
        self.write_file_atomic(project_path, new_content)
        return True

    def remove_related_project(self, note_path: Path, project_stem: str) -> bool:
        """Undo of `add_related_project`: drops the `01_Projects/<stem>` item from the
        `related_projects:` frontmatter list (whatever its alias). Returns True on change."""
        content = self.read_file(note_path)
        if content is None:
            return False
        m = re.match(r"---\n(.*?)\n---(?:\n|$)", content, re.DOTALL)
        if not m:
            return False
        item = re.compile(rf'^\s*-\s*"?\[\[01_Projects/{re.escape(project_stem)}(?:\|[^\]]*)?\]\]"?\s*$')
        lines = m.group(1).split("\n")
        kept = [ln for ln in lines if not item.match(ln)]
        if len(kept) == len(lines):
            return False
        self.write_file_atomic(note_path, content[:m.start(1)] + "\n".join(kept) + content[m.end(1):])
        return True

    def add_related_project(self, note_path: Path, project_stem: str) -> bool:
        """Child -> project half of the edge: adds a `01_Projects/<stem>` wikilink to the
        `related_projects:` frontmatter list of a decision or resource note (adding the key
        when the note predates it). Never used on meeting notes — their frontmatter is
        re-rendered on every calendar sync (`_merge_meeting_note`), so a link written there
        would be silently dropped; a meeting's project edge lives in its ata bullet.
        Idempotent; a `related_projects` of unrecognised shape is left untouched."""
        content = self.read_file(note_path)
        if content is None:
            return False
        m = re.match(r"---\n(.*?)\n---(?:\n|$)", content, re.DOTALL)
        if not m:
            return False
        fm = m.group(1)
        if re.search(rf"\[\[01_Projects/{re.escape(project_stem)}(?:\||\]\])", fm):
            return False
        item = f'  - "[[01_Projects/{project_stem}|{project_stem}]]"'
        lines = fm.split("\n")
        idx = next(
            (i for i, ln in enumerate(lines) if re.match(r"related_projects:\s*(\[\s*\])?\s*$", ln)),
            None,
        )
        if idx is None:
            if any(ln.startswith("related_projects:") for ln in lines):
                return False
            lines += ["related_projects:", item]
        else:
            lines[idx] = "related_projects:"
            last, j = idx, idx + 1
            while j < len(lines) and (not lines[j].strip() or re.match(r"\s*-\s", lines[j])):
                if lines[j].strip():
                    last = j
                j += 1
            lines.insert(last + 1, item)
        self.write_file_atomic(note_path, content[:m.start(1)] + "\n".join(lines) + content[m.end(1):])
        return True

    _MEETING_STEM_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(.*)$", re.DOTALL)

    def rename_meeting_note_wikilink_safe(self, old_path: Path, new_date_str: str) -> Optional[Path]:
        """Rename a meeting note to carry `new_date_str` as its leading date, rewriting
        every `[[04_Meetings/<old stem>...]]` / `[[<old stem>...]]` backlink across the
        vault and the note's own `date:` frontmatter + `MEET-<date>-...` id.

        Returns the new path, `old_path` when the date is already correct, or None when
        the note is missing, the stem has no leading date, or the target name is taken
        (caller decides what to tell the user). DB rows are the caller's job.
        """
        if not old_path.exists():
            return None
        m = self._MEETING_STEM_DATE_RE.match(old_path.stem)
        if not m:
            return None
        old_stem, old_date, rest = old_path.stem, m.group(1), m.group(2)
        if old_date == new_date_str:
            return old_path
        new_stem = f"{new_date_str}{rest}"
        new_path = old_path.with_name(f"{new_stem}.md")
        if new_path.exists():
            return None

        content = self.read_file(old_path) or ""
        content = re.sub(r"^(date:\s*).*$", rf"\g<1>{new_date_str}", content, count=1, flags=re.MULTILINE)
        content = content.replace(f"MEET-{old_date}-", f"MEET-{new_date_str}-")
        self.write_file_atomic(new_path, content)
        old_path.unlink()

        swaps = [
            (f"[[04_Meetings/{old_stem}]]", f"[[04_Meetings/{new_stem}]]"),
            (f"[[04_Meetings/{old_stem}|", f"[[04_Meetings/{new_stem}|"),
            (f"[[{old_stem}]]", f"[[{new_stem}]]"),
            (f"[[{old_stem}|", f"[[{new_stem}|"),
        ]
        for md in self.vault_path.rglob("*.md"):
            if md == new_path:
                continue
            t = self.read_file(md)
            if not t or old_stem not in t:
                continue
            nt = t
            for a, b in swaps:
                nt = nt.replace(a, b)
            if nt != t:
                self.write_file_atomic(md, nt)
        return new_path

    def enrich_person_note(
        self,
        name: str,
        *,
        role: str = "",
        org: str = "",
        email: str = "",
        overwrite: bool = False,
    ) -> bool:
        """Fills the `role`, `organization`, `email` frontmatter fields of an existing
        Person note. Never touches the body, no-op if the note is missing.

        Default (`overwrite=False`) only fills a field that is currently blank (`""`) —
        the safe choice for any silent/automatic caller (including the single-tap
        Telegram "vincular" button and the bulk "aceitar sugestões fortes" macro).
        `overwrite=True` is for the one deliberate, hand-typed Triagem `=<nome exato>`
        resolution — the user explicitly wrote a name to link to, so replacing a stale
        role/organization with the freshly heard one is expected, not a surprise.
        Returns True when the file changed."""
        path = self.vault_path / "05_People" / f"{self._sanitize_filename(name)}.md"
        content = self.read_file(path)
        if content is None:
            return False
        new = content
        value_pattern = r'"[^"]*"[ \t]*' if overwrite else r'""[ \t]*'
        for field, value in (("role", role), ("organization", org), ("email", email)):
            if not value:
                continue
            new = re.sub(
                rf'^({re.escape(field)}:\s*){value_pattern}$',
                lambda m, v=value: f'{m.group(1)}"{v}"',
                new, count=1, flags=re.MULTILINE,
            )
        if new == content:
            return False
        self.write_file_atomic(path, new)
        return True

    def scan_decision_statuses(self) -> dict[str, str]:
        """Reads the (`id`, `status`) frontmatter pair of every note in 03_Decisions.

        The vault file is the source of truth for status once a decision exists — a human
        edits it there (Obsidian), never through the bot. This lets callers reconcile the
        DecisionRecord DB row, which is otherwise insert-only and goes stale forever."""
        statuses: dict[str, str] = {}
        decisions_dir = self.vault_path / "03_Decisions"
        if not decisions_dir.is_dir():
            return statuses
        for path in decisions_dir.glob("*.md"):
            try:
                head = path.read_text(encoding="utf-8")[:1000]
            except (OSError, ValueError):
                continue
            id_match = re.search(r'^id:\s*"?([^"\n]+?)"?\s*$', head, re.MULTILINE)
            status_match = re.search(r'^status:\s*"?([^"\n#]+?)"?\s*(?:#.*)?$', head, re.MULTILINE)
            if id_match and status_match:
                statuses[id_match.group(1).strip()] = status_match.group(1).strip()
        return statuses

    def scan_decision_note_names(self) -> dict[str, str]:
        """Maps decision `id` -> real filename stem (no `.md`) for every note in
        03_Decisions. Notes are filed as `{id} - {title}.md`, never `{id}.md` alone, so
        any wikilink built from just the id (`[[03_Decisions/{{ dec.id }}]]`) resolves to
        nothing and Obsidian creates a blank note on click — this is the fix: read the
        real filename off disk instead of assuming or trusting a possibly-stale DB
        `note_path`."""
        names: dict[str, str] = {}
        decisions_dir = self.vault_path / "03_Decisions"
        if not decisions_dir.is_dir():
            return names
        for path in decisions_dir.glob("*.md"):
            try:
                head = path.read_text(encoding="utf-8")[:1000]
            except (OSError, ValueError):
                continue
            id_match = re.search(r'^id:\s*"?([^"\n]+?)"?\s*$', head, re.MULTILINE)
            if id_match:
                names[id_match.group(1).strip()] = path.stem
        return names

    def create_decision_note(
        self,
        decision_id: str,
        title: str,
        context: str = "",
        assumptions: str = "",
        outcome: str = "",
        priority: str = "alta",
        status: str = "em_analise",
        area_file: str = "Negocios_e_Governanca",
        area_title: str = "Negócios & Governança",
        related_projects: Optional[list[str]] = None,
        review_date: Optional[str] = None,
        success_metric: str = "",
        file_stem: Optional[str] = None,
    ) -> Path:
        """Creates a structured decision record in 03_Decisions.

        `file_stem` overrides the filename (default `{decision_id}.md`); the frontmatter
        `id` always stays the clean `decision_id`.
        """
        stem = self._sanitize_filename(file_stem or decision_id)
        decision_path = self.vault_path / "03_Decisions" / f"{stem}.md"
        today_str = date.today().strftime("%Y-%m-%d")

        tmpl = Template(DECISION_TEMPLATE)
        rendered = tmpl.render(
            decision_id=decision_id,
            title=title,
            status=status,
            priority=priority,
            area_file=area_file,
            area_title=area_title,
            related_projects=related_projects or [],
            decision_date=today_str,
            context=context,
            assumptions=assumptions,
            outcome=outcome,
            review_date=review_date or today_str,
            success_metric=success_metric,
        )

        self.write_file_atomic(decision_path, rendered)
        return decision_path

    def create_project_note(
        self,
        title: str,
        proj_id: Optional[str] = None,
        outcome_description: str = "",
        area_file: str = "Negocios_e_Governanca",
        area_title: str = "Negócios & Governança",
        front: str = "estrategia_ma",
        priority: str = "alta",
        status: str = "active",
        deadline: str = "TBD",
        stakeholders: Optional[list[str]] = None,
        related_decisions: Optional[list[str]] = None,
    ) -> Path:
        """Creates a flat project note in 01_Projects."""
        safe_title = self._sanitize_filename(title)
        project_path = self.vault_path / "01_Projects" / f"{safe_title}.md"
        now_utc = datetime.now(timezone.utc)
        proj_id = proj_id or f"{now_utc.strftime('%Y%m%d%H%M')}"

        # Notes are filed as `{id} - {title}.md`, never `{id}.md` alone — a wikilink built
        # from just the id resolves to nothing (see scan_decision_note_names). Read the
        # real filename off disk so `related_decisions` links actually resolve.
        decision_names = self.scan_decision_note_names() if related_decisions else {}
        related_decisions_resolved = [decision_names.get(d, d) for d in (related_decisions or [])]

        tmpl = Template(PROJECT_TEMPLATE)
        rendered = tmpl.render(
            proj_id=proj_id,
            title=title,
            area_file=area_file,
            area_title=area_title,
            front=front,
            priority=priority,
            status=status,
            deadline=deadline,
            stakeholders=stakeholders or [],
            related_decisions=related_decisions_resolved,
            created_at=now_utc.strftime("%Y-%m-%dT%H:%M:%S"),
            outcome_description=outcome_description or f"Objetivo principal do projeto {title}.",
        )

        self.write_file_atomic(project_path, rendered)
        return project_path

    def list_projects(self) -> list[dict[str, Any]]:
        """Lists all project notes in 01_Projects."""
        projects_dir = self.vault_path / "01_Projects"
        if not projects_dir.exists():
            return []

        results = []
        for file in sorted(projects_dir.glob("*.md")):
            content = self.read_file(file)
            priority = "baixa"
            status = "active"
            if content:
                m_prio = re.search(r"^priority:\s*(\w+)", content, re.MULTILINE)
                if m_prio:
                    priority = m_prio.group(1)
                m_status = re.search(r"^status:\s*(\w+)", content, re.MULTILINE)
                if m_status:
                    status = m_status.group(1)

            results.append({
                "title": file.stem,
                "file_name": file.name,
                "priority": priority,
                "status": status,
                "path": str(file),
            })
        return results

    def create_person_note(
        self,
        name: str,
        organization: str = "",
        role: str = "",
        email: str = "",
        slug: Optional[str] = None,
    ) -> Path:
        """Creates or updates a structured Person note in 05_People."""
        safe_name = self._sanitize_filename(name)
        person_path = self.vault_path / "05_People" / f"{safe_name}.md"
        person_slug = slug or self._slugify(name).lower().replace(" ", "-")

        tmpl = Template(PERSON_TEMPLATE)
        rendered = tmpl.render(
            slug=person_slug,
            name=name,
            organization=organization,
            role=role,
            email=email,
        )

        self.write_file_atomic(person_path, rendered)
        return person_path

    def create_idea_note(
        self,
        title: str,
        overview: str = "",
        rationale: str = "",
        next_steps: Optional[Any] = None,
    ) -> Path:
        """Creates a structured idea note in 06_Resources/Ideias_e_Criatividade."""
        path = self.vault_path / "06_Resources" / "Ideias_e_Criatividade" / f"{self._sanitize_filename(title)}.md"
        now_utc = datetime.now(timezone.utc)
        rendered = Template(IDEA_TEMPLATE).render(
            idea_id=now_utc.strftime("%Y%m%d%H%M"),
            title=title,
            overview=overview or "",
            rationale=rationale or "",
            next_steps=self._as_list(next_steps),
            created_at=now_utc.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        self.write_file_atomic(path, rendered)
        return path

    def create_framework_note(
        self,
        title: str,
        description: str = "",
        components: Optional[Any] = None,
    ) -> Path:
        """Creates a structured framework/method note in 06_Resources/Frameworks_e_Metodos."""
        path = self.vault_path / "06_Resources" / "Frameworks_e_Metodos" / f"{self._sanitize_filename(title)}.md"
        now_utc = datetime.now(timezone.utc)
        rendered = Template(FRAMEWORK_TEMPLATE).render(
            fw_id=now_utc.strftime("%Y%m%d%H%M"),
            title=title,
            description=description or "",
            components=self._as_list(components),
            created_at=now_utc.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        self.write_file_atomic(path, rendered)
        return path

    def create_resource_note(
        self,
        title: str,
        summary: str = "",
        source: str = "",
        area_link: str = "",
        tags: Optional[Any] = None,
    ) -> Path:
        """Creates a generic resource/reference note in 06_Resources/Leituras_e_Pesquisas —
        for anything that isn't specifically an Idea or a Framework."""
        path = self.vault_path / "06_Resources" / "Leituras_e_Pesquisas" / f"{self._sanitize_filename(title)}.md"
        now_utc = datetime.now(timezone.utc)
        # Minute-resolution alone collides when several resources are created in the same
        # transcript run (all land in the same wall-clock minute) — same class of bug as the
        # decision-id collision incident (see entity_apply.py's dec_id fix). Second-resolution
        # + a random suffix makes same-tick collisions astronomically unlikely.
        resource_id = f"{now_utc.strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:3]}"
        rendered = Template(RESOURCE_TEMPLATE).render(
            resource_id=resource_id,
            title=title,
            summary=summary or "",
            source=source or "",
            area_link=area_link or "",
            related_projects=[],
            tags=self._as_list(tags),
            created_at=now_utc.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        self.write_file_atomic(path, rendered)
        return path

    def ensure_resource_note(
        self,
        title: str,
        summary: str = "",
        source: str = "",
        area_link: str = "",
        tags: Optional[Any] = None,
    ) -> Path:
        """Reuses an existing 06_Resources note whose title normalizes identically to `title`
        before creating a new one — `create_resource_note` alone had no existence check at
        all (unlike `ensure_person_note`/`ensure_project_note`), which let the same real-world
        resource mentioned across several meetings mint a new note per mention (see the XYZ
        contract / Meridian platform dedup, 2026-09-17)."""
        exact = self._exact_resource_match(title)
        if exact:
            return Path(exact["path"])
        return self.create_resource_note(
            title=title, summary=summary, source=source, area_link=area_link, tags=tags,
        )

    def ensure_person_note(
        self,
        name: str,
        organization: str = "",
        role: str = "",
        email: str = "",
    ) -> Path:
        """Ensures a person note exists in 05_People without overwriting existing notes."""
        safe_name = self._sanitize_filename(name)
        person_path = self.vault_path / "05_People" / f"{safe_name}.md"
        if not person_path.exists():
            return self.create_person_note(
                name=name,
                organization=organization,
                role=role,
                email=email,
            )
        return person_path

    def ensure_project_note(self, title: str, outcome_description: str = "") -> Path:
        """Ensures a project note exists in 01_Projects without overwriting an existing
        one — `create_project_note` always overwrites, and a real project note carries
        stakeholders / deadline / outcome that must never be clobbered by a transcript
        resolution (the entity resolver's 'no exact match' and 'same sanitized filename'
        are not the same predicate, and a note may appear between the run and the tap)."""
        project_path = self.vault_path / "01_Projects" / f"{self._sanitize_filename(title)}.md"
        if not project_path.exists():
            return self.create_project_note(title=title, outcome_description=outcome_description)
        return project_path

    def list_people(self) -> list[dict[str, Any]]:
        """Lists all person notes in 05_People."""
        people_dir = self.vault_path / "05_People"
        if not people_dir.exists():
            return []

        results = []
        for file in sorted(people_dir.glob("*.md")):
            content = self.read_file(file)
            org = ""
            role = ""
            email = ""
            if content:
                m_org = re.search(r'^organization:\s*"?(.*?)"?$', content, re.MULTILINE)
                if m_org:
                    org = m_org.group(1).strip('"')
                m_role = re.search(r'^role:\s*"?(.*?)"?$', content, re.MULTILINE)
                if m_role:
                    role = m_role.group(1).strip('"')
                m_email = re.search(r'^email:\s*"?(.*?)"?$', content, re.MULTILINE)
                if m_email:
                    email = m_email.group(1).strip('"')

            results.append({
                "name": file.stem,
                "file_name": file.name,
                "organization": org,
                "role": role,
                "email": email,
                "path": str(file),
            })
        return results

    @staticmethod
    def _phonetic_key(name: str) -> str:
        """Normalizes a name for loose phonetic comparison (accents, case, common PT-BR sound swaps)."""
        text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
        text = re.sub(r"[^a-z]", "", text)
        # Collapse sounds that are frequently confused in PT-BR transcription (voice/typo)
        text = text.replace("ss", "s").replace("j", "g").replace("ge", "ge").replace("gi", "gi")
        text = re.sub(r"(.)\1+", r"\1", text)  # collapse doubled letters
        return text

    # Leading tokens that are titles, not given names — "Dr. Jorge" and "Dr. the owner" must
    # never collide on "Dr." being treated as a first name (2026-09-17 /dedup false-positive
    # incident: every "Dr. X" person in the vault scored 1.00 against every other "Dr. Y").
    _HONORIFICS = frozenset({"dr", "dra", "sr", "sra", "prof", "profa", "eng", "engo", "enga"})
    # PT-BR name particles, stripped from surname tokens before comparing them — "de
    # Oliveira Soares" vs "Santos" must still match on the discriminating token.
    _NAME_PARTICLES = frozenset({"de", "da", "do", "dos", "das", "e"})

    @staticmethod
    def _jaro_winkler(s1: str, s2: str) -> float:
        """Standard Jaro-Winkler string similarity (Winkler prefix bonus, scaling 0.1, max
        prefix 4) — cross-checked bit-for-bit against jellyfish 1.2.1 on this module's own
        test cases (see test_dedup.py's golden-value test). Hand-rolled rather than a new
        dependency: this box is a Raspberry Pi (aarch64) and jellyfish 1.x ships as a
        compiled Rust extension — one Python version bump away from needing a Rust
        toolchain here, for what is otherwise a ~40-line, fully-specified algorithm."""
        if s1 == s2:
            return 1.0
        len1, len2 = len(s1), len(s2)
        if len1 == 0 or len2 == 0:
            return 0.0
        match_distance = max(0, max(len1, len2) // 2 - 1)
        s1_matches = [False] * len1
        s2_matches = [False] * len2
        matches = 0
        for i in range(len1):
            start = max(0, i - match_distance)
            end = min(i + match_distance + 1, len2)
            for j in range(start, end):
                if s2_matches[j] or s1[i] != s2[j]:
                    continue
                s1_matches[i] = True
                s2_matches[j] = True
                matches += 1
                break
        if matches == 0:
            return 0.0
        transpositions = 0
        k = 0
        for i in range(len1):
            if not s1_matches[i]:
                continue
            while not s2_matches[k]:
                k += 1
            if s1[i] != s2[k]:
                transpositions += 1
            k += 1
        transpositions //= 2
        jaro = (matches / len1 + matches / len2 + (matches - transpositions) / matches) / 3.0
        prefix = 0
        for i in range(min(4, len1, len2)):
            if s1[i] != s2[i]:
                break
            prefix += 1
        return jaro + prefix * 0.1 * (1 - jaro)

    @classmethod
    def _given_surname_tokens(cls, name: str) -> tuple[str, list[str]]:
        """Splits a person name into (given_name_token, [surname_tokens]) — strips a
        parenthetical aside ("Rafael Mendes (Rafa)" -> "Rafael Mendes"), a leading
        honorific, and PT-BR name particles from the surname tokens. Empty surname_tokens
        means `name` is a bare single-token name (e.g. "Marta")."""
        text = re.sub(r"\([^)]*\)", " ", name)
        raw_tokens = [t for t in re.split(r"[\s-]+", text.strip()) if t]

        def _key(t: str) -> str:
            return re.sub(
                r"[^a-z]", "", unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii").lower()
            )

        tokens = []
        for t in raw_tokens:
            if not tokens and _key(t) in cls._HONORIFICS:
                continue  # drop a leading honorific, never treat it as the given name
            tokens.append(t)
        if not tokens:
            return "", []
        given = tokens[0]
        surname_tokens = [t for t in tokens[1:] if _key(t) not in cls._NAME_PARTICLES]
        return given, surname_tokens

    @classmethod
    def _person_pair_score(
        cls, name_a: str, name_b: str,
        given_cutoff: float = 0.85, surname_cutoff: float = 0.80, bare_cutoff: float = 0.85,
    ) -> float:
        """0.0 unless name_a/name_b are plausibly the same person. A shared given name
        alone is NOT enough — "Gabriela Cortez"/"Gabriela Ferraz"/"Gabriela Guerra" are
        three different people who happen to share a common first name. When both names
        carry a surname, BOTH the given name AND at least one surname token must clear
        their own cutoff (AND, not max/OR — the bug this replaces let a common first name
        alone drive the score to 1.00, e.g. two people both titled "Dr." colliding on
        that title being mistaken for a first name). When at least one side is a bare
        single-token name (no surname — e.g. "Marta"), it's compared only against the
        other name's given token, at its own higher bar — deliberately a weaker
        "possibly the same, one record incomplete" signal, not full identity (this is the
        real Marta/Marta Duarte case from 2026-09-17, which must keep matching)."""
        ga, sa = cls._given_surname_tokens(name_a)
        gb, sb = cls._given_surname_tokens(name_b)
        if not ga or not gb:
            return 0.0
        if sa and sb:
            given_score = cls._jaro_winkler(cls._phonetic_key(ga), cls._phonetic_key(gb))
            surname_score = max(
                cls._jaro_winkler(cls._phonetic_key(x), cls._phonetic_key(y))
                for x in sa for y in sb
            )
            if given_score >= given_cutoff and surname_score >= surname_cutoff:
                return max(given_score, surname_score)
            return 0.0
        score = cls._jaro_winkler(cls._phonetic_key(ga), cls._phonetic_key(gb))
        return score if score >= bare_cutoff else 0.0

    def find_similar_people(
        self, name: str,
        given_cutoff: float = 0.85, surname_cutoff: float = 0.80, bare_cutoff: float = 0.85,
    ) -> list[str]:
        """Finds existing 05_People notes plausibly the same person as `name` — see
        `_person_pair_score` for what counts as a match."""
        existing = self.list_people()
        if not existing:
            return []

        matches = []
        for person in existing:
            existing_name = person["name"]
            if existing_name.strip().lower() == name.strip().lower():
                continue  # exact match, not a "similar" duplicate concern
            if self._person_pair_score(name, existing_name, given_cutoff, surname_cutoff, bare_cutoff) > 0.0:
                matches.append(existing_name)

        return matches

    def _exact_person_match(self, surface: str) -> Optional[dict[str, Any]]:
        """The existing 05_People note whose name normalises identically to `surface`, or None.
        Kept separate from find_similar_people, which skips exact matches by design — never
        infer 'this is an exact existing entity' from that method's output."""
        n = self._normalize_for_match(surface)
        if not n:
            return None
        for person in self.list_people():
            if self._normalize_for_match(person["name"]) == n:
                return person
        return None

    def similar_people_scored(
        self, name: str,
        given_cutoff: float = 0.85, surname_cutoff: float = 0.80, bare_cutoff: float = 0.85,
    ) -> list[dict[str, Any]]:
        """Near-matches for a person name as [{'name','path','score'}], score desc, top 5 —
        see `_person_pair_score` for what counts as a match. Excludes an exact normalised
        match (that is handled by _exact_person_match)."""
        n = self._normalize_for_match(name)
        out: list[dict[str, Any]] = []
        for person in self.list_people():
            if self._normalize_for_match(person["name"]) == n:
                continue
            score = self._person_pair_score(name, person["name"], given_cutoff, surname_cutoff, bare_cutoff)
            if score > 0.0:
                out.append({"name": person["name"], "path": person["path"], "score": round(score, 3)})
        return sorted(out, key=lambda m: m["score"], reverse=True)[:5]

    def _exact_project_match(self, title: str) -> Optional[dict[str, Any]]:
        """The existing 01_Projects note whose title normalises identically to `title`, or None."""
        n = self._normalize_for_match(title)
        if not n:
            return None
        for proj in self.list_projects():
            if self._normalize_for_match(proj["title"]) == n:
                return proj
        return None

    def list_resources(self) -> list[dict[str, Any]]:
        """Lists all resource notes across the 06_Resources subfolders (Ideias_e_Criatividade,
        Leituras_e_Pesquisas, Frameworks_e_Metodos)."""
        results = []
        for sub in ("Ideias_e_Criatividade", "Leituras_e_Pesquisas", "Frameworks_e_Metodos"):
            res_dir = self.vault_path / "06_Resources" / sub
            if not res_dir.exists():
                continue
            for file in sorted(res_dir.glob("*.md")):
                results.append({"name": file.stem, "path": str(file)})
        return results

    def _exact_resource_match(self, title: str) -> Optional[dict[str, Any]]:
        """The existing 06_Resources note (any subfolder) whose title normalises identically
        to `title`, or None."""
        n = self._normalize_for_match(title)
        if not n:
            return None
        for res in self.list_resources():
            if self._normalize_for_match(res["name"]) == n:
                return res
        return None

    def _exact_decision_match(self, title: str) -> Optional[dict[str, Any]]:
        """The existing 03_Decisions note whose `title:` frontmatter normalises identically
        to `title`, or None. Unlike people/projects/resources, a decision note's FILENAME
        is `{id} - {title}.md` (see `scan_decision_note_names`), never just the title — so
        matching against the filename stem (like `_exact_project_match` does) would never
        hit, and every Triagem `=<nome exato>` link attempt on a decision would report
        "não encontrado" even for an exact title match. Match on the `title:` frontmatter
        field instead."""
        n = self._normalize_for_match(title)
        if not n:
            return None
        decisions_dir = self.vault_path / "03_Decisions"
        if not decisions_dir.is_dir():
            return None
        for path in sorted(decisions_dir.glob("*.md")):
            content = self.read_file(path)
            if not content:
                continue
            m = re.search(r'^title:\s*"?([^"\n]+?)"?\s*$', content[:1000], re.MULTILINE)
            if m and self._normalize_for_match(m.group(1)) == n:
                return {"title": m.group(1).strip(), "path": str(path)}
        return None

    # Generic title words that carry no discriminating signal on their own — "Plataforma
    # Meridian" vs "Plataforma iHealth" must not match just because both are a "Plataforma"
    # (2026-09-17 /dedup false-positive incident), the resource/project analogue of the
    # honorific-only collision fixed in _person_pair_score.
    _GENERIC_PROJECT_TOKENS = frozenset({
        "projeto", "programa", "plano", "iniciativa", "de", "da", "do", "e", "em", "para", "com", "na", "no",
    })
    _GENERIC_RESOURCE_TOKENS = frozenset({
        "minuta", "minutas", "contrato", "contratual", "plataforma", "proposta", "termo", "termos",
        "instrumento", "instrumentos", "acordo", "apresentacao", "institucional", "documento",
        "carta", "intencao", "intencoes", "de", "da", "do", "e", "em", "para", "com",
    })

    @classmethod
    def _distinctive_token_gate(
        cls, title_a: str, title_b: str, stoplist: frozenset[str], token_cutoff: float = 0.90,
    ) -> bool:
        """True iff title_a/title_b share at least one non-generic token (fuzzy match, not
        exact set intersection — a typo like "Meridian"/"Meridiam" must still pass). Required
        alongside the whole-string ratio cutoff in find_similar_projects/find_similar_resources
        so two titles don't match purely because they share a generic word — the same
        principle as _person_pair_score requiring more than a shared honorific/first name."""
        tokens_a = [t for t in cls._normalize_for_match(title_a).split() if t not in stoplist]
        tokens_b = [t for t in cls._normalize_for_match(title_b).split() if t not in stoplist]
        if not tokens_a or not tokens_b:
            return False
        return any(cls._jaro_winkler(x, y) >= token_cutoff for x in tokens_a for y in tokens_b)

    def find_similar_projects(self, title: str, cutoff: float = 0.75) -> list[dict[str, Any]]:
        """Fuzzy title near-matches for a project as [{'title','path','score'}], score desc,
        top 5. Requires both a whole-string ratio >= cutoff AND a shared distinctive token
        (see `_distinctive_token_gate`) — a shared generic word alone isn't enough."""
        target = self._normalize_for_match(title)
        if not target:
            return []
        out: list[dict[str, Any]] = []
        for proj in self.list_projects():
            pn = self._normalize_for_match(proj["title"])
            if pn == target:
                continue
            score = difflib.SequenceMatcher(None, target, pn).ratio()
            if score >= cutoff and self._distinctive_token_gate(title, proj["title"], self._GENERIC_PROJECT_TOKENS):
                out.append({"title": proj["title"], "path": proj["path"], "score": round(score, 3)})
        return sorted(out, key=lambda m: m["score"], reverse=True)[:5]

    def find_similar_resources(self, title: str, cutoff: float = 0.7) -> list[dict[str, Any]]:
        """Fuzzy title near-matches for a resource as [{'title','path','score'}], score desc,
        top 5. Lower default cutoff than `find_similar_projects` — resource titles vary more
        per mention (e.g. "Contrato XYZ" vs "Minuta Contratual XYZ" for the same document,
        score ~0.67) — detection-only, surfaced via `/dedup` for the owner to confirm, never
        auto-merged (see `resource_id_collision_fixed` / `decision_id_collision_incident`).
        Requires both the ratio cutoff AND a shared distinctive token (see
        `_distinctive_token_gate`) — a shared generic word ("Plataforma X" vs "Plataforma Y")
        alone isn't enough."""
        target = self._normalize_for_match(title)
        if not target:
            return []
        out: list[dict[str, Any]] = []
        for res in self.list_resources():
            rn = self._normalize_for_match(res["name"])
            if rn == target:
                continue
            score = difflib.SequenceMatcher(None, target, rn).ratio()
            if score >= cutoff and self._distinctive_token_gate(title, res["name"], self._GENERIC_RESOURCE_TOKENS):
                out.append({"title": res["name"], "path": res["path"], "score": round(score, 3)})
        return sorted(out, key=lambda m: m["score"], reverse=True)[:5]

    def match_person(self, surface: str, *, org: str = "", role: str = "", email: str = "",
                     index: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        """Robust person resolution (see `rysos.connectors.entity_match.match_against`).
        Pass `index` (a `list_people()` result) to reuse a hoisted index."""
        from rysos.connectors.entity_match import match_against
        return match_against(surface, index if index is not None else self.list_people(),
                             kind="person", org=org, role=role, email=email)

    def match_project(self, surface: str, *,
                      index: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        from rysos.connectors.entity_match import match_against
        return match_against(surface, index if index is not None else self.list_projects(),
                             kind="project")

    # extracted_fields keys that carry the *body* of each structured template. If none is
    # populated (e.g. the offline AI fallback returns only a title), the capture is written
    # as a freeform note rather than a hollow templated shell.
    _BODY_FIELDS: dict[str, tuple[str, ...]] = {
        "project": ("outcome_description", "stakeholders"),
        "decision": ("context", "outcome", "assumptions"),
        "idea": ("overview", "rationale", "next_steps"),
        "framework": ("description", "components"),
    }

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        """Coerces an AI field (list, delimited string, or None) into a clean list of strings."""
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            return [p.strip() for p in re.split(r"[,;\n]", value) if p.strip()]
        return []

    @staticmethod
    def _nonblank(value: Any) -> bool:
        if isinstance(value, (list, tuple)):
            return any(str(v).strip() for v in value)
        return bool(str(value or "").strip())

    @classmethod
    def _resolve_area(cls, label: Optional[str]) -> tuple[str, str]:
        """Maps any free-form area label ('Patrimônio & Finanças', 'Patrimonio_e_Financas')
        to the canonical (frontmatter file slug, human title) pair; defaults to Negócios."""
        n = cls._normalize_for_match(label or "")
        if "saude" in n or "vitalidade" in n:
            return AREAS["saude"]
        if "patrimonio" in n or "financ" in n:
            return AREAS["patrimonio"]
        if "familia" in n or "relacionament" in n:
            return AREAS["familia"]
        return AREAS["negocios"]

    @staticmethod
    def _norm_priority(value: Any) -> str:
        return "baixa" if str(value or "").strip().lower() == "baixa" else "alta"

    def _has_body_fields(self, category: str, fields: dict[str, Any]) -> bool:
        return any(self._nonblank(fields.get(k)) for k in self._BODY_FIELDS.get(category, ()))

    def save_quick_capture(
        self,
        category: str,
        title: str,
        content: str,
        target_date: Optional[str] = None,
        fields: Optional[dict[str, Any]] = None,
    ) -> Path:
        """Persists a confirmed quick capture into its Obsidian destination.

        The structured `fields` (the AI's `extracted_fields`) drive the Jinja templates so
        note structure stays deterministic; the LLM prose in `content` is only a fallback
        for degraded/offline captures and for the freeform Inbox. `target_date`
        (YYYY-MM-DD) picks which Daily Cockpit a "today" focus lands in.
        """
        fields = fields or {}
        safe_title = self._sanitize_filename(title)
        now = datetime.now(timezone.utc)

        if category == "today":
            return self._file_today_focus(title, content, fields, target_date)

        if category == "project" and self._has_body_fields("project", fields):
            area_file, area_title = self._resolve_area(fields.get("area"))
            return self.create_project_note(
                title=title,
                outcome_description=str(fields.get("outcome_description") or ""),
                area_file=area_file,
                area_title=area_title,
                front=str(fields.get("front") or "estrategia_ma"),
                priority=self._norm_priority(fields.get("priority")),
                deadline=str(fields.get("deadline") or "TBD"),
                stakeholders=self._as_list(fields.get("stakeholders")),
            )

        if category == "decision" and self._has_body_fields("decision", fields):
            area_file, area_title = self._resolve_area(fields.get("area"))
            dec_id = f"DEC-{now.strftime('%Y')}-{now.strftime('%d%H%M%S')[-5:]}"
            status = str(fields.get("status") or "em_analise")
            return self.create_decision_note(
                decision_id=dec_id,
                title=title,
                context=str(fields.get("context") or ""),
                assumptions=str(fields.get("assumptions") or ""),
                outcome=str(fields.get("outcome") or ""),
                priority=self._norm_priority(fields.get("priority")),
                status=status if status in ("em_analise", "decidido", "bloqueado") else "em_analise",
                area_file=area_file,
                area_title=area_title,
                review_date=str(fields.get("review_date")) if fields.get("review_date") else None,
                success_metric=str(fields.get("success_metric") or ""),
                file_stem=f"{dec_id} - {safe_title}",
            )

        if category == "person":
            # Never clobber an existing People note; it may hold accumulated meeting history.
            return self.ensure_person_note(
                name=str(fields.get("name") or title),
                organization=str(fields.get("organization") or ""),
                role=str(fields.get("role") or ""),
                email=str(fields.get("email") or ""),
            )

        if category == "idea" and self._has_body_fields("idea", fields):
            return self.create_idea_note(
                title=title,
                overview=str(fields.get("overview") or ""),
                rationale=str(fields.get("rationale") or ""),
                next_steps=fields.get("next_steps"),
            )

        if category == "framework" and self._has_body_fields("framework", fields):
            return self.create_framework_note(
                title=title,
                description=str(fields.get("description") or ""),
                components=fields.get("components"),
            )

        return self._write_freeform_note(category, safe_title, content)

    def _write_freeform_note(self, category: str, safe_title: str, content: str) -> Path:
        """Fallback path: writes the note body as-is into its category folder. Used for the
        Inbox and for degraded/offline captures that arrive without structured fields."""
        folder = {
            "idea": "06_Resources/Ideias_e_Criatividade",
            "framework": "06_Resources/Frameworks_e_Metodos",
            "project": "01_Projects",
            "decision": "03_Decisions",
        }.get(category, "07_Inbox_Agent")
        if category == "decision":
            now = datetime.now(timezone.utc)
            safe_title = f"DEC-{now.strftime('%Y')}-{now.strftime('%d%H%M%S')[-5:]} - {safe_title}"
        target_path = self.vault_path / folder / f"{safe_title}.md"
        self.write_file_atomic(target_path, content or f"# {safe_title}\n")
        return target_path

    def _file_today_focus(
        self,
        title: str,
        content: str,
        fields: dict[str, Any],
        target_date: Optional[str],
    ) -> Path:
        """Inserts a `- [ ]` focus line into the target day's Daily Cockpit, skipping insertion
        if an equivalent focus is already listed."""
        if target_date:
            try:
                focus_date = datetime.strptime(target_date, "%Y-%m-%d").date()
            except ValueError:
                focus_date = date.today()
        else:
            focus_date = date.today()
        date_str = focus_date.strftime("%Y-%m-%d")
        cockpit_file = self.vault_path / "00_Cockpit" / "Daily" / f"{date_str}.md"

        focus_title = str(fields.get("title") or title).strip()
        description = self._sanitize_inline_description(str(fields.get("description") or "") or content)
        checkbox_line = (
            f"- [ ] **{focus_title}** — {description}" if description else f"- [ ] **{focus_title}**"
        )
        title_key = self._normalize_for_match(focus_title)

        if not cockpit_file.exists():
            self.create_daily_cockpit(
                target_date=focus_date,
                high_priority_foci=[{
                    "title": focus_title,
                    "area": self._resolve_area(fields.get("area"))[1],
                    "description": description,
                }],
            )
            return cockpit_file

        existing = self.read_file(cockpit_file) or ""
        for line in existing.splitlines():
            m = re.match(r"^\s*[-*]\s+\[[ xX]\]\s+(.+?)\s*$", line)
            if not m:
                continue
            norm_line = self._normalize_for_match(m.group(1))
            if title_key and (norm_line == title_key or norm_line.startswith(title_key + " ")):
                return cockpit_file  # equivalent focus already present

        header = "## 🎯 Focos de Alta Prioridade do Dia"
        if header in existing:
            existing = existing.replace("- [ ] *Defina os 3 focos principais do dia*\n", "")
            existing = existing.replace("- [ ] *Defina os 3 focos principais do dia*", "")
            updated = existing.replace(header, f"{header}\n{checkbox_line}", 1)
        else:
            updated = existing + f"\n\n{header}\n{checkbox_line}\n"
        self.write_file_atomic(cockpit_file, updated)
        return cockpit_file


    @staticmethod
    def _slugify(text: str) -> str:
        """Generates a clean file slug."""
        text = re.sub(r'[\\/*?:"<>|]', "", text)
        return text.strip()

    @staticmethod
    def _sanitize_filename(text: str) -> str:
        """Sanitizes filename for cross-platform OS compatibility."""
        return re.sub(r'[\\/*?:"<>|]', "_", text).strip()

    @staticmethod
    def _sanitize_inline_description(text: str) -> str:
        """Collapses AI-generated content to a single safe line, guarding against embedded
        frontmatter/multi-line note bodies corrupting a checkbox line in the Daily Cockpit."""
        if not text:
            return ""
        first_line = text.strip().splitlines()[0].strip()
        # Drop a raw YAML frontmatter delimiter that shouldn't appear inline
        if first_line == "---":
            remaining = [ln.strip() for ln in text.strip().splitlines()[1:] if ln.strip() and ln.strip() != "---"]
            first_line = remaining[0] if remaining else ""
        return first_line[:200]
