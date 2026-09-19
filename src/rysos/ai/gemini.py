"""Gemini AI Client integration for executive reasoning and synthesis."""

import json
import re
import logging
from datetime import date
from typing import Any, Optional
from google import genai
from google.genai import types

from rysos.config import settings
from rysos.connectors.gmail import EmailData
from rysos.connectors.calendar import CalendarEvent
from rysos.observability import record_ai_token_usage
from rysos.ai.prompts import (
    MORNING_BRIEF_SYSTEM_PROMPT,
    CHAT_AGENT_SYSTEM_PROMPT,
    EMAIL_ANALYSIS_PROMPT,
    MEETING_PREP_PROMPT,
    DECISION_EXTRACTION_PROMPT,
    QUICK_CAPTURE_REFINE_PROMPT,
    CONVERSATIONAL_NOTE_PROMPT,
    EVENT_EXTRACTION_PROMPT,
    DEDUP_ASSESSMENT_PROMPT,
    MEETING_TRANSCRIPT_PROMPT,
    MEETING_TRANSCRIPT_PROMPT_V2,
    DIARY_ENTRY_PROMPT,
)

logger = logging.getLogger("rysos.ai")

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The canonical, fully-populated shape returned by summarize_meeting_transcript. Every
# downstream consumer (the ata renderer, the entity resolver, the cockpit push) can rely
# on every key being present with the right type.
_MINUTES_KEYS_SCALAR = ("title", "meeting_type", "date_hint", "language_quality_notes", "para_area", "para_area_rationale")
_MINUTES_KEYS_STRLIST = ("organizations", "open_questions", "risks", "financials", "followups_meetings", "topics_tags", "summary_bullets")


def _s(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _str_list(v: Any) -> list[str]:
    """List of clean strings. A dict item (models sometimes return {what,when} or
    {name,...} even for a flat-list field) is flattened to its joined values."""
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        if isinstance(x, dict):
            joined = " — ".join(_s(val) for val in x.values() if _s(val))
            if joined:
                out.append(joined)
        elif _s(x):
            out.append(_s(x))
    return out


def _parse_minutes_json(raw: str) -> tuple[Optional[dict], str]:
    """Best-effort parse of a model JSON response.

    Returns (data, reason): reason is "ok" | "repaired" | "truncated" | "invalid".
    Handles the two real failure modes seen on noisy transcripts: (a) the model wraps
    the object in a ```json fence or prose, (b) it emits a trailing comma or an
    unquoted key mid-object (observed once in 4 on gemini-3.5-flash-lite).
    """
    if not raw or not raw.strip():
        return None, "invalid"
    text = raw.strip()
    # strip a leading/trailing ```json ... ``` fence and any prose around the object
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]

    try:
        data = json.loads(text)
        return (data if isinstance(data, dict) else None), ("ok" if isinstance(data, dict) else "invalid")
    except json.JSONDecodeError:
        pass

    repaired = re.sub(r",(\s*[}\]])", r"\1", text)          # trailing commas
    repaired = re.sub(r",\s*,", ",", repaired)               # doubled commas
    repaired = re.sub(r'([{,]\s*)([A-Za-z_][\w-]*)(\s*):', r'\1"\2"\3:', repaired)  # bare keys
    try:
        data = json.loads(repaired)
        return (data if isinstance(data, dict) else None), ("repaired" if isinstance(data, dict) else "invalid")
    except json.JSONDecodeError:
        pass

    if repaired.count("{") > repaired.count("}") or repaired.count("[") > repaired.count("]"):
        return None, "truncated"
    return None, "invalid"


def _coerce_minutes(data: Optional[dict], *, meeting_year: int, fallback_title: str = "") -> dict[str, Any]:
    """Force any partial/dirty extraction dict into the canonical 17-key minutes shape.

    Also the single return path for the skeleton fallback, so a bare
    ``_coerce_minutes({"title": ...})`` yields a valid full structure.
    """
    d = data if isinstance(data, dict) else {}
    lo, hi = meeting_year - 1, meeting_year + 1

    def _clean_date(v: str) -> str:
        v = _s(v)
        if _ISO_DATE_RE.match(v):
            try:
                if lo <= int(v[:4]) <= hi:
                    return v
            except ValueError:
                pass
            return ""  # hallucinated / out-of-range year
        return v  # human phrasing ("28 de setembro") kept for due/when, dropped by caller for date_hint

    out: dict[str, Any] = {k: _s(d.get(k)) for k in _MINUTES_KEYS_SCALAR}
    for k in _MINUTES_KEYS_STRLIST:
        out[k] = _str_list(d.get(k))

    title = re.sub(r'[\\/*?:"<>|]', "", out["title"])[:120].strip()
    out["title"] = title or re.sub(r'[\\/*?:"<>|]', "", fallback_title)[:120].strip() or "Reunião sem título"

    dh = out["date_hint"]
    out["date_hint"] = dh if (_ISO_DATE_RE.match(dh) and _clean_date(dh)) else ""

    people = []
    for p in (d.get("people") or []):
        if isinstance(p, str) and p.strip():
            people.append({"name": p.strip(), "role": "", "org": "", "mentions": ""})
        elif isinstance(p, dict) and _s(p.get("name")):
            people.append({"name": _s(p.get("name")), "role": _s(p.get("role")),
                           "org": _s(p.get("org")), "mentions": _s(p.get("mentions"))})
    out["people"] = people

    projects = []
    for p in (d.get("projects") or []):
        if isinstance(p, str) and p.strip():
            projects.append({"name": p.strip(), "status": "", "summary": ""})
        elif isinstance(p, dict) and _s(p.get("name")):
            projects.append({"name": _s(p.get("name")), "status": _s(p.get("status")), "summary": _s(p.get("summary"))})
    out["projects"] = projects

    decisions = []
    for x in (d.get("decisions") or []):
        if isinstance(x, str) and x.strip():
            decisions.append({"title": x.strip(), "status": "em_analise", "rationale": "", "owner": ""})
        elif isinstance(x, dict) and _s(x.get("title")):
            st = _s(x.get("status")).lower() or "em_analise"
            if st not in ("decidido", "em_analise", "bloqueado"):
                st = "em_analise"
            decisions.append({"title": _s(x.get("title")), "status": st,
                              "rationale": _s(x.get("rationale")), "owner": _s(x.get("owner"))})
    out["decisions"] = decisions

    action_items = []
    for it in (d.get("action_items") or []):
        if isinstance(it, str) and it.strip():
            action_items.append({"text": it.strip(), "owner": "", "due": ""})
        elif isinstance(it, dict) and _s(it.get("text")):
            action_items.append({"text": _s(it.get("text")), "owner": _s(it.get("owner")), "due": _clean_date(_s(it.get("due")))})
    out["action_items"] = action_items

    resources = []
    for r in (d.get("resources") or []):
        if isinstance(r, str) and r.strip():
            resources.append({"type": "", "name": r.strip(), "note": ""})
        elif isinstance(r, dict) and _s(r.get("name")):
            resources.append({"type": _s(r.get("type")), "name": _s(r.get("name")), "note": _s(r.get("note"))})
    out["resources"] = resources

    deadlines = []
    for dl in (d.get("deadlines") or []):
        if isinstance(dl, dict) and (_s(dl.get("what")) or _s(dl.get("when"))):
            deadlines.append({"what": _s(dl.get("what")), "when": _clean_date(_s(dl.get("when")))})
        elif isinstance(dl, str) and dl.strip():
            deadlines.append({"what": dl.strip(), "when": ""})
    out["deadlines"] = deadlines

    glossary = []
    for g in (d.get("glossary") or []):
        if isinstance(g, dict) and _s(g.get("term")):
            glossary.append({"term": _s(g.get("term")), "meaning": _s(g.get("meaning"))})
    out["glossary"] = glossary

    return out


class GeminiAIClient:
    """Interface with Google Gemini 2.5 Flash / Pro."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.model_name = model or settings.GEMINI_MODEL
        # Cheaper tier for deterministic/operational calls (temp-0 classification, fixed-
        # schema extraction) — see GEMINI_MODEL_LITE in config.py for which calls use it.
        self.lite_model_name = settings.GEMINI_MODEL_LITE
        self._client: Optional[genai.Client] = None

    @property
    def client(self) -> Optional[genai.Client]:
        """Lazy-loaded Gemini client."""
        if self._client is None and self.api_key:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def is_available(self) -> bool:
        """Checks if Gemini API is configured."""
        return bool(self.api_key and len(self.api_key) > 5)

    def _record_usage(self, response: Any, operation: str, details: Optional[str] = None,
                       model: Optional[str] = None) -> None:
        """Captures real token metrics from Gemini response."""
        try:
            prompt_tokens = 0
            candidate_tokens = 0
            total_tokens = 0
            cached_tokens = 0

            usage = getattr(response, "usage_metadata", None)
            if usage:
                prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
                candidate_tokens = getattr(usage, "candidates_token_count", 0) or 0
                total_tokens = getattr(usage, "total_token_count", 0) or (prompt_tokens + candidate_tokens)
                cached_tokens = getattr(usage, "cached_content_token_count", 0) or 0

            record_ai_token_usage(
                model=model or self.model_name,
                operation=operation,
                prompt_tokens=prompt_tokens,
                candidate_tokens=candidate_tokens,
                total_tokens=total_tokens,
                cached_tokens=cached_tokens,
                details=details,
            )
        except Exception:
            pass

    def analyze_emails(self, emails: list[EmailData]) -> list[dict[str, Any]]:
        """Classifies emails, extracts actionable items, and assigns binary priority."""
        if not emails:
            return []

        if not self.is_available():
            # Heuristic fallback if Gemini API is not yet configured
            return self._heuristic_email_analysis(emails)

        emails_payload = []
        for e in emails:
            acc_info = f"Conta: {e.account} ({e.account_type})" if getattr(e, "account", None) else f"Tipo: {getattr(e, 'account_type', 'corporativo')}"
            body_sample = (e.body_text[:1200] if getattr(e, "body_text", None) else e.snippet).replace("\n", " ").strip()
            emails_payload.append(
                f"ID: {e.id}\n{acc_info}\nDe: {e.sender}\nAssunto: {e.subject}\nData: {e.date}\nConteúdo: {body_sample}\n---"
            )
        emails_text = "\n".join(emails_payload)

        prompt = EMAIL_ANALYSIS_PROMPT.format(emails_text=emails_text)

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=MORNING_BRIEF_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
            self._record_usage(response, "email_analysis", f"{len(emails)} emails")
            raw_text = response.text or "{}"
            data = json.loads(raw_text)
            return data.get("actionable_emails", [])
        except Exception:
            return self._heuristic_email_analysis(emails)

    @staticmethod
    def _has_agenda(m: CalendarEvent) -> bool:
        """True when the invite carries a real description to summarize. Below this bar
        the brief is generated deterministically — the model is never asked to guess a
        purpose from a bare title (JAMAIS INVENTAR)."""
        return len((m.description or "").strip()) >= 15

    @staticmethod
    def _bare_meeting_brief(m: CalendarEvent) -> dict[str, Any]:
        """Factual, non-speculative brief for an invite with no agenda text."""
        return {
            "id": m.id,
            "context_brief": "Convite sem pauta ou descrição. Sem contexto adicional registrado.",
            "priority": "baixa",
            "suggested_preparation": "",
        }

    def generate_meeting_briefs(self, meetings: list[CalendarEvent]) -> list[dict[str, Any]]:
        """Generates executive preparation briefs for upcoming meetings."""
        if not meetings:
            return []

        bare = [m for m in meetings if not self._has_agenda(m)]
        described = [m for m in meetings if self._has_agenda(m)]
        results: list[dict[str, Any]] = [self._bare_meeting_brief(m) for m in bare]

        if not described:
            return results
        if not self.is_available():
            return results + self._heuristic_meeting_briefs(described)

        meetings_payload = []
        for m in described:
            meetings_payload.append(
                f"ID: {m.id}\nResumo: {m.summary}\nHorário: {m.start_time.strftime('%H:%M')} - {m.end_time.strftime('%H:%M')}\n"
                f"Participantes: {', '.join(m.attendees)}\nDescrição: {m.description}\n---"
            )
        meetings_text = "\n".join(meetings_payload)

        prompt = MEETING_PREP_PROMPT.format(meetings_text=meetings_text)

        try:
            response = self.client.models.generate_content(
                model=self.lite_model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=MORNING_BRIEF_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )
            self._record_usage(response, "meeting_briefs", f"{len(described)} meetings", model=self.lite_model_name)
            raw_text = response.text or "{}"
            data = json.loads(raw_text)
            return results + data.get("meeting_briefs", [])
        except Exception:
            return results + self._heuristic_meeting_briefs(described)

    def extract_decisions(self, text: str) -> list[dict[str, Any]]:
        """Extracts decision items from meeting notes or text."""
        if not text.strip():
            return []

        if not self.is_available():
            return []

        prompt = DECISION_EXTRACTION_PROMPT.format(source_text=text)

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=MORNING_BRIEF_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
            self._record_usage(response, "extract_decisions")
            raw_text = response.text or "{}"
            data = json.loads(raw_text)
            return data.get("decisions", [])
        except Exception:
            return []

    def summarize_meeting_transcript(
        self,
        text: str,
        filename: str = "",
        *,
        mtime_date: Optional[date] = None,
        today: Optional[date] = None,
    ) -> dict[str, Any]:
        """Turns a raw meeting transcript into structured executive minutes.

        Always returns the canonical 17-key shape (see `_coerce_minutes`): title,
        meeting_type, date_hint, language_quality_notes, people[], organizations[],
        projects[], para_area, para_area_rationale, decisions[], open_questions[],
        action_items[], resources[], risks[], financials[], deadlines[],
        followups_meetings[], topics_tags[], glossary[], summary_bullets[].

        Robust to a malformed / truncated model response: one JSON-repair pass, then
        one retry asking for a shorter answer, then a skeleton fallback so the pipeline
        can still file the raw transcript against a meeting note.
        """
        stripped = (text or "").strip()
        today = today or date.today()
        meeting_year = (mtime_date or today).year
        first_line = next((ln.strip() for ln in stripped.splitlines() if ln.strip()), "")
        fallback_title = (filename.rsplit(".", 1)[0] or first_line)

        def _skeleton(note: str, *, degraded: bool = False) -> dict[str, Any]:
            res = _coerce_minutes(
                {"title": fallback_title, "summary_bullets": [note] if note else []},
                meeting_year=meeting_year,
                fallback_title=fallback_title,
            )
            # `degraded` = the API was called and could not produce usable structure
            # (as opposed to "Gemini not configured" / empty input). The caller turns
            # this into a visible warning + candidate row so a thin note never lands
            # silently and stays permanently processed.
            if degraded:
                res["_degraded"] = True
            return res

        if not stripped:
            return _skeleton("Transcrição vazia.")
        if not self.is_available():
            return _skeleton("Ata não estruturada (Gemini indisponível) — ver transcrição bruta anexa.")

        base_prompt = MEETING_TRANSCRIPT_PROMPT_V2.format(
            filename=filename or "(desconhecido)",
            today=today.strftime("%Y-%m-%d"),
            transcript_text=stripped,
        )

        def _call(prompt: str, temperature: float) -> tuple[Optional[dict], str]:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=MORNING_BRIEF_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=temperature,
                    max_output_tokens=settings.TRANSCRIPT_MAX_OUTPUT_TOKENS,
                ),
            )
            self._record_usage(response, "summarize_transcript", filename or None)
            return _parse_minutes_json(response.text or "")

        try:
            data, reason = _call(base_prompt, 0.2)
            if data is None:
                logger.warning("Transcrição %s: JSON %s na 1ª tentativa, refazendo mais conciso.", filename or "?", reason)
                data, reason = _call(
                    base_prompt + "\n\nResponda de forma compacta: no máximo 6 itens por lista, "
                    "frases curtas, e feche todas as chaves e colchetes do JSON.",
                    0.1,
                )
            if data is None:
                logger.error("Transcrição %s: JSON inválido (%s) após retry.", filename or "?", reason)
                return _skeleton("Falha ao estruturar a ata (JSON inválido após retry).", degraded=True)
            if reason == "repaired":
                logger.info("Transcrição %s: JSON reparado antes do parse.", filename or "?")
            return _coerce_minutes(data, meeting_year=meeting_year, fallback_title=fallback_title)
        except Exception as e:
            logger.error(f"Erro ao resumir transcrição de reunião no Gemini: {e}")
            return _skeleton(f"Falha ao estruturar a ata (erro IA: {str(e)[:80]}).", degraded=True)

    def summarize_diary_entry(
        self,
        text: str,
        filename: str = "",
        *,
        entry_date: Optional[date] = None,
        today: Optional[date] = None,
    ) -> dict[str, Any]:
        """Turns a spoken-diary transcript (free first-person narration) into the same
        canonical 17-key structure as `summarize_meeting_transcript`, so every downstream
        consumer (`_coerce_minutes`, `EntityResolver`, the journal-note renderer) is
        reused unchanged. Same robustness ladder: JSON-repair pass, one shorter retry,
        then a skeleton fallback so the raw entry can still be filed.

        Unlike the meeting prompt, `entry_date` IS the reference for relative dates
        ("ontem", "semana que vem") — the model resolves them against it.
        """
        stripped = (text or "").strip()
        today = today or date.today()
        ref_date = entry_date or today
        entry_year = ref_date.year
        first_line = next((ln.strip() for ln in stripped.splitlines() if ln.strip()), "")
        fallback_title = (filename.rsplit(".", 1)[0] or first_line)

        def _skeleton(note: str, *, degraded: bool = False) -> dict[str, Any]:
            res = _coerce_minutes(
                {"title": fallback_title, "summary_bullets": [note] if note else []},
                meeting_year=entry_year,
                fallback_title=fallback_title,
            )
            if degraded:
                res["_degraded"] = True
            return res

        if not stripped:
            return _skeleton("Transcrição vazia.")
        if not self.is_available():
            return _skeleton("Entrada de diário não estruturada (Gemini indisponível) — ver narração bruta anexa.")

        base_prompt = DIARY_ENTRY_PROMPT.format(
            filename=filename or "(desconhecido)",
            entry_date=ref_date.strftime("%Y-%m-%d"),
            today=today.strftime("%Y-%m-%d"),
            transcript_text=stripped,
        )

        def _call(prompt: str, temperature: float) -> tuple[Optional[dict], str]:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=MORNING_BRIEF_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=temperature,
                    max_output_tokens=settings.DIARY_MAX_OUTPUT_TOKENS,
                ),
            )
            self._record_usage(response, "summarize_diary", filename or None)
            return _parse_minutes_json(response.text or "")

        try:
            data, reason = _call(base_prompt, 0.2)
            if data is None:
                logger.warning("Diário %s: JSON %s na 1ª tentativa, refazendo mais conciso.", filename or "?", reason)
                data, reason = _call(
                    base_prompt + "\n\nResponda de forma compacta: no máximo 6 itens por lista, "
                    "frases curtas, e feche todas as chaves e colchetes do JSON.",
                    0.1,
                )
            if data is None:
                logger.error("Diário %s: JSON inválido (%s) após retry.", filename or "?", reason)
                return _skeleton("Falha ao estruturar a entrada (JSON inválido após retry).", degraded=True)
            if reason == "repaired":
                logger.info("Diário %s: JSON reparado antes do parse.", filename or "?")
            return _coerce_minutes(data, meeting_year=entry_year, fallback_title=fallback_title)
        except Exception as e:
            logger.error(f"Erro ao resumir entrada de diário no Gemini: {e}")
            return _skeleton(f"Falha ao estruturar a entrada (erro IA: {str(e)[:80]}).", degraded=True)

    def refine_quick_capture(self, text: str, category: str = "idea") -> dict[str, Any]:
        """Refines a quick raw capture note into an Obsidian executive artifact via Gemini."""
        if not text.strip():
            return {"title": "Sem título", "content": "", "summary": ""}

        if not self.is_available():
            first_line = text.strip().splitlines()[0][:60]
            return {
                "title": first_line,
                "content": text.strip(),
                "summary": "Nota estruturada localmente (Gemini indisponível)",
            }

        prompt = QUICK_CAPTURE_REFINE_PROMPT.format(category=category, raw_text=text.strip())

        try:
            response = self.client.models.generate_content(
                model=self.lite_model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=MORNING_BRIEF_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.3,
                ),
            )
            self._record_usage(response, "refine_quick_capture", f"category: {category}", model=self.lite_model_name)
            raw_text = response.text or "{}"
            data = json.loads(raw_text)
            title = re.sub(r'[\\/*?:"<>|]', "", data.get("title", "")).strip() or "Nota Sem Título"
            content = data.get("content", text.strip())
            summary = data.get("summary", "")
            return {
                "title": title,
                "content": content,
                "summary": summary,
            }
        except Exception as e:
            first_line = text.strip().splitlines()[0][:60]
            return {
                "title": first_line,
                "content": text.strip(),
                "summary": f"Fallback direto (erro IA: {str(e)})",
            }

    def chat_with_agent(self, user_message: str, context: Optional[str] = None) -> str:
        """Executive interactive chat with the rysOS Agent."""
        if not self.is_available():
            return (
                "⚠️ **Chave do Gemini não configurada.**\n"
                "Para conversar com o agente executivo, configure sua chave `GEMINI_API_KEY` no arquivo `.env` ou via interface."
            )

        full_prompt = ""
        if context:
            full_prompt += f"CONTEXTO DO COFRE E ROTINA ATUAL:\n{context}\n\n"
        full_prompt += f"MENSAGEM DO USUÁRIO:\n{user_message}"

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=CHAT_AGENT_SYSTEM_PROMPT,
                    temperature=0.4,
                ),
            )
            self._record_usage(response, "chat_agent")
            return response.text or "Sem resposta gerada."
        except Exception as e:
            return f"❌ Erro ao consultar Gemini: {str(e)}"

    def transcribe_audio(self, audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
        """Transcribes voice notes or audio recordings into Portuguese text using Gemini."""
        if not self.is_available() or not audio_bytes:
            return ""

        audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
        prompt = "Transcreva com máxima fidelidade e precisão a fala deste áudio em Português do Brasil. Retorne apenas o texto transcrito, sem introduções ou comentários adicionais."

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[audio_part, prompt],
                config=types.GenerateContentConfig(temperature=0.1),
            )
            self._record_usage(response, "transcribe_audio")
            return (response.text or "").strip()
        except Exception as e:
            logger.error(f"Erro ao transcrever áudio no Gemini: {e}")
            return ""

    def interpret_image(self, image_bytes: bytes, mime_type: str = "image/jpeg", user_prompt: Optional[str] = None) -> str:
        """Extracts text, notes, whiteboard ideas or document content from an image using Gemini."""
        if not self.is_available() or not image_bytes:
            return ""

        image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        # A caption (`user_prompt`) is additional context, never a replacement for the
        # extraction task — overriding it entirely used to let a caption like "cria esse
        # evento" silently swap out fact-extraction, so the returned text carried no
        # structured data for the classifier/event-extractor downstream to act on.
        base_prompt = (
            "Analise esta imagem (documento, anotação manuscrita, quadro branco, tela ou slide) e extraia todo o texto, "
            "ideias centrais e contexto relevante em Português do Brasil de forma estruturada e objetiva."
        )
        prompt = f"{base_prompt}\n\nInstrução adicional do usuário: {user_prompt}" if user_prompt else base_prompt

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[image_part, prompt],
                config=types.GenerateContentConfig(temperature=0.1),
            )
            self._record_usage(response, "interpret_image")
            return (response.text or "").strip()
        except Exception as e:
            logger.error(f"Erro ao interpretar imagem no Gemini: {e}")
            return ""

    def interpret_document(self, file_bytes: bytes, mime_type: str, user_prompt: Optional[str] = None) -> str:
        """Extracts text and structured content from a document file (PDF, etc.) using Gemini's
        native multimodal document understanding — no separate PDF-parsing library involved."""
        if not self.is_available() or not file_bytes:
            return ""

        doc_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
        # Same "additive, not override" rule as interpret_image — see comment there.
        base_prompt = (
            "Analise este documento e extraia todo o texto, dados estruturados (datas, horários, "
            "nomes, valores) e contexto relevante em Português do Brasil de forma objetiva."
        )
        prompt = f"{base_prompt}\n\nInstrução adicional do usuário: {user_prompt}" if user_prompt else base_prompt

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[doc_part, prompt],
                config=types.GenerateContentConfig(temperature=0.1),
            )
            self._record_usage(response, "interpret_document")
            return (response.text or "").strip()
        except Exception as e:
            logger.error(f"Erro ao interpretar documento no Gemini: {e}")
            return ""

    def process_conversational_note(
        self,
        user_input: str,
        current_draft: Optional[dict[str, Any]] = None,
        history: Optional[list[dict[str, str]]] = None,
    ) -> dict[str, Any]:
        """Conversational loop extracting template fields or asking for missing info at low temperature."""
        if not user_input.strip():
            return {
                "category": "idea",
                "is_complete": False,
                "extracted_fields": {},
                "missing_fields": [],
                "conversational_reply": "Por favor, compartilhe sua ideia, anotação, projeto ou decisão.",
            }

        if not self.is_available():
            first_line = user_input.strip().splitlines()[0][:60]
            return {
                "category": "idea",
                "is_complete": True,
                "title": first_line,
                "content": user_input.strip(),
                "summary": first_line,
                "conversational_reply": "Nota processada localmente (Gemini offline). Deseja gravar?",
                "extracted_fields": {"title": first_line},
                "missing_fields": [],
            }

        draft_json = json.dumps(current_draft or {}, ensure_ascii=False, indent=2)
        history_formatted = "\n".join(
            [f"{h.get('role', 'user')}: {h.get('text', '')}" for h in (history or [])]
        ) if history else "Nenhum histórico prévio."

        today = date.today()
        weekday_pt = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"][today.weekday()]
        current_date_context = f"{today.strftime('%Y-%m-%d')} ({weekday_pt})"

        prompt = (
            CONVERSATIONAL_NOTE_PROMPT
            .replace("{current_date_context}", current_date_context)
            .replace("{current_draft_json}", draft_json)
            .replace("{conversation_history}", history_formatted)
            .replace("{user_input}", user_input.strip())
        )

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=CHAT_AGENT_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            self._record_usage(response, "conversational_note")
            raw_text = response.text or "{}"
            data = json.loads(raw_text)

            if data.get("title"):
                data["title"] = re.sub(r'[\\/*?:"<>|]', "", data["title"]).strip()

            return data
        except Exception as e:
            logger.error(f"Erro no processamento conversacional de nota: {e}")
            first_line = user_input.strip().splitlines()[0][:60]
            cat = current_draft.get("category", "idea") if current_draft else "idea"
            return {
                "category": cat,
                "is_complete": True,
                "title": first_line,
                "content": user_input.strip(),
                "summary": first_line,
                "conversational_reply": f"Estruturei a nota com base no texto. Deseja gravar no Obsidian? (Fallback IA: {str(e)[:50]})",
                "extracted_fields": {},
                "missing_fields": [],
            }

    def extract_event_fields(self, user_input: str) -> dict[str, Any]:
        """Extracts a Google Calendar event's fields from a natural-language request.

        Returns date/time as null (never guessed) when the message doesn't state them —
        the caller must ask the user rather than default to "today" or a fixed hour.
        """
        if not self.is_available() or not user_input.strip():
            return {
                "summary": user_input.strip()[:80] or "Novo evento",
                "date": None, "time": None, "duration_minutes": 60,
                "attendees": [], "location": None, "description": None, "account_hint": None,
            }

        today = date.today()
        weekday_pt = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"][today.weekday()]
        current_date_context = f"{today.strftime('%Y-%m-%d')} ({weekday_pt})"

        prompt = (
            EVENT_EXTRACTION_PROMPT
            .replace("{current_date_context}", current_date_context)
            .replace("{user_input}", user_input.strip())
        )

        try:
            response = self.client.models.generate_content(
                model=self.lite_model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )
            self._record_usage(response, "event_extraction", model=self.lite_model_name)
            data = json.loads(response.text or "{}")
            return {
                "summary": data.get("summary") or user_input.strip()[:80],
                "date": data.get("date"),
                "time": data.get("time"),
                "duration_minutes": int(data.get("duration_minutes") or 60),
                "attendees": data.get("attendees") or [],
                "location": data.get("location"),
                "description": data.get("description"),
                "account_hint": data.get("account_hint"),
            }
        except Exception as e:
            logger.error(f"Erro na extração de evento: {e}")
            return {
                "summary": user_input.strip()[:80] or "Novo evento",
                "date": None, "time": None, "duration_minutes": 60,
                "attendees": [], "location": None, "description": None, "account_hint": None,
            }

    @staticmethod
    def _dedup_context_text(kind: str, context: dict[str, Any]) -> str:
        """Renders a `dedup_triage.build_finding_context` dict into the plain-text block
        DEDUP_ASSESSMENT_PROMPT expects — only real note content/backlinks, nothing
        inferred, so the model has exactly what a human reviewer read this session."""
        if kind == "diary_candidates":
            mentions_text = "\n".join(
                f"  - {m['cockpit_date']} ({m['cockpit_path']}): \"{m['matched_text']}\""
                for m in context["mentions"]
            )
            return (
                f"Nota órfã: {context['orphan_path']} (\"{context['orphan_name']}\")\n"
                f"Conteúdo da nota órfã:\n{context['orphan_content']}\n\n"
                f"Menções em texto livre no Cockpit (sem wikilink ainda):\n{mentions_text}"
            )

        def _fmt_backlinks(backlinks: list[dict[str, Any]]) -> str:
            if not backlinks:
                return "  (nenhum backlink encontrado)"
            return "\n".join(
                f"  - {b['source_path']} (alias usado: \"{b['alias']}\")" for b in backlinks
            )

        return (
            f"Nota A: {context['a_path']} (\"{context['a_name']}\")\n"
            f"Conteúdo A:\n{context['a_content']}\n"
            f"Backlinks para A:\n{_fmt_backlinks(context['a_backlinks'])}\n\n"
            f"Nota B: {context['b_path']} (\"{context['b_name']}\")\n"
            f"Conteúdo B:\n{context['b_content']}\n"
            f"Backlinks para B:\n{_fmt_backlinks(context['b_backlinks'])}\n\n"
            f"Score de similaridade (heurístico, não decisivo): {context.get('score')}"
        )

    def assess_dedup_finding(self, kind: str, context: dict[str, Any]) -> dict[str, Any]:
        """AI recommendation for one `/dedup` finding, given the real note content/backlink
        context from `dedup_triage.build_finding_context`. Never writes anything — this only
        proposes; the owner approves/rejects/adjusts each one individually in Telegram (see
        [[dedup_unitary_approval_required]]). Falls back to "needs_review" (never a silent
        merge) if Gemini is unavailable or its response can't be parsed.
        """
        fallback = {
            "recommendation": "needs_review",
            "rationale": "Avaliação por IA indisponível; requer revisão manual.",
            "survivor_path": None,
            "consolidated_summary": None,
        }
        if not self.is_available():
            return fallback

        prompt = DEDUP_ASSESSMENT_PROMPT.format(
            kind=kind, context_text=self._dedup_context_text(kind, context)
        )

        try:
            response = self.client.models.generate_content(
                model=self.lite_model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )
            self._record_usage(response, "dedup_assessment", f"kind: {kind}", model=self.lite_model_name)
            data = json.loads(response.text or "{}")
            recommendation = data.get("recommendation")
            if recommendation not in ("merge", "keep_separate", "link_diary_mention", "needs_review"):
                recommendation = "needs_review"
            return {
                "recommendation": recommendation,
                "rationale": data.get("rationale") or "",
                "survivor_path": data.get("survivor_path"),
                "consolidated_summary": data.get("consolidated_summary"),
            }
        except Exception as e:
            logger.error(f"Erro na avaliação de achado de dedup: {e}")
            return fallback

    def _heuristic_email_analysis(self, emails: list[EmailData]) -> list[dict[str, Any]]:
        """Fallback rule-based email prioritization and bill reminder extraction."""
        high_priority_keywords = ["urgente", "aprovação", "proposta", "contrato", "m&a", "decisão", "diretoria", "board", "deadline", "prazo"]
        bill_keywords = [
            "boleto", "fatura", "vencimento", "nfs-e", "conta de luz", "conta de agua",
            "conta de água", "condominio", "condomínio", "iptu", "ipva",
            "das mei", "simples nacional", "fatura disponível", "pagar até"
        ]

        results = []
        for e in emails:
            subject_lower = e.subject.lower()
            snippet_lower = e.snippet.lower()
            text_full = f"{subject_lower} {snippet_lower}"

            is_personal = getattr(e, "account_type", "") == "pessoal"
            is_bill_keyword = any(kw in text_full for kw in bill_keywords)

            is_bill = False
            due_date = None
            amount = None
            beneficiary = None

            if is_bill_keyword:
                is_bill = True
                # Extração de vencimento
                m_due = re.search(r'(?:vencimento|vence|venc\.?|até)[\s:]*(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)', text_full)
                if m_due:
                    raw_d = m_due.group(1).replace("/", "-")
                    parts = raw_d.split("-")
                    if len(parts) == 3:
                        day, month, year = parts[0], parts[1], parts[2]
                        if len(year) == 2:
                            year = f"20{year}"
                        due_date = f"{year.zfill(4)}-{month.zfill(2)}-{day.zfill(2)}"
                    elif len(parts) == 2:
                        from datetime import date
                        due_date = f"{date.today().year}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"

                # Extração de valor monetário
                m_amt = re.search(r'(?:R\$|\$)\s*([\d\.,]+)', e.subject + " " + e.snippet)
                if m_amt:
                    amount = f"R$ {m_amt.group(1).strip()}"

                # Extração de beneficiário
                if not beneficiary:
                    beneficiary = e.sender.split("<")[0].replace('"', '').strip() or "Emissor da Conta"

            if is_bill and is_personal:
                action_text = f"Pagar boleto: {beneficiary} ({amount or 'valor pendente'})"
                if due_date:
                    action_text = f"Pagar até {due_date}: {beneficiary} ({amount or 'a consultar'})"

                results.append({
                    "id": e.id,
                    "sender": e.sender,
                    "subject": e.subject,
                    "priority": "alta",
                    "summary": e.snippet[:120],
                    "suggested_action": action_text,
                    "category": "pagamento",
                    "related_area": "Patrimonio_e_Financas",
                    "is_bill": True,
                    "due_date": due_date,
                    "amount": amount,
                    "beneficiary": beneficiary,
                })
                continue

            is_high = any(kw in text_full for kw in high_priority_keywords) or e.is_starred

            results.append({
                "id": e.id,
                "sender": e.sender,
                "subject": e.subject,
                "priority": "alta" if is_high else "baixa",
                "summary": e.snippet[:120],
                "suggested_action": "Revisar e responder" if is_high else "Arquivar ou leitura posterior",
                "category": "follow_up" if is_high else "informativo",
                "related_area": "Negocios_e_Governanca",
                "is_bill": False,
                "due_date": None,
                "amount": None,
                "beneficiary": None,
            })
        return results

    def _heuristic_meeting_briefs(self, meetings: list[CalendarEvent]) -> list[dict[str, Any]]:
        """Fallback rule-based meeting briefs."""
        results = []
        for m in meetings:
            desc = (m.description or "").strip()
            results.append({
                "id": m.id,
                # Echo the invite's own description verbatim — never a guessed purpose.
                "context_brief": desc or "Convite sem pauta ou descrição. Sem contexto adicional registrado.",
                "priority": "alta" if len(m.attendees) > 2 else "baixa",
                "suggested_preparation": "",
            })
        return results


gemini_ai = GeminiAIClient()
