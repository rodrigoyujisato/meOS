"""FastAPI routes and API endpoints for rysOS."""

from datetime import datetime, date, timezone
from typing import Any, Optional
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, BackgroundTasks

from rysos.config import settings
from rysos.core import rysos_core
from rysos.db import get_db_session, DecisionRecord, InboxAgentItem
from rysos.vault.manager import VaultManager
from rysos.auth.google_auth import google_auth
from rysos.ai.gemini import gemini_ai
from rysos.observability import get_token_usage_stats

router = APIRouter(prefix="/api")


class DecisionCreateRequest(BaseModel):
    title: str
    context: str = ""
    assumptions: str = ""
    outcome: str = ""
    priority: str = "alta"  # 'alta' or 'baixa'
    area_file: str = "Negocios_e_Governanca"
    area_title: str = "Negócios & Governança"
    related_projects: list[str] = []
    review_date: Optional[str] = None
    success_metric: str = ""


class ProjectCreateRequest(BaseModel):
    title: str
    outcome_description: str = ""
    front: str = "estrategia_ma"  # 'estrategia_ma', 'operacoes_clientes', 'pessoas_cultura'
    priority: str = "alta"  # 'alta' or 'baixa'
    deadline: str = "TBD"
    area_file: str = "Negocios_e_Governanca"
    area_title: str = "Negócios & Governança"


class QuickCaptureRequest(BaseModel):
    text: str
    category: str = "idea"  # 'idea', 'task', 'note', 'today', etc.
    custom_title: Optional[str] = None
    custom_content: Optional[str] = None


class QuickCaptureRefineRequest(BaseModel):
    text: str
    category: str = "idea"


class ChatRequest(BaseModel):
    message: str


@router.get("/status")
async def get_status() -> dict[str, Any]:
    """Returns current system health, auth status, configuration, and token observability."""
    token_stats = get_token_usage_stats()
    return {
        "status": "online",
        "vault_path": str(settings.OBSIDIAN_VAULT_PATH),
        "vault_exists": settings.OBSIDIAN_VAULT_PATH.exists(),
        "google_authenticated": google_auth.is_authenticated(),
        "gemini_ready": gemini_ai.is_available(),
        "gemini_model": settings.GEMINI_MODEL,
        "morning_brief_time": settings.MORNING_BRIEF_TIME,
        "evening_recap_time": settings.EVENING_RECAP_TIME,
        "tokens_today": token_stats.get("today", {}),
        "tokens_total": token_stats.get("total", {}),
    }


@router.get("/metrics/tokens")
async def get_token_metrics(days: int = 30) -> dict[str, Any]:
    """Returns detailed real-time token metrics, breakdown by operation/model, and costs."""
    try:
        return get_token_usage_stats(days=days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao obter métricas de tokens: {str(e)}")


@router.get("/dashboard")
async def get_dashboard() -> dict[str, Any]:
    """Retrieves full dashboard state."""
    try:
        return await rysos_core.get_dashboard_summary()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync")
async def trigger_sync() -> dict[str, Any]:
    """Manually triggers full Google Workspace and Obsidian sync."""
    try:
        res = await rysos_core.run_daily_sync()
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao sincronizar: {str(e)}")


@router.post("/decisions")
async def create_decision(req: DecisionCreateRequest) -> dict[str, Any]:
    """Creates a new decision record in DB and Obsidian Vault."""
    try:
        # Generate unique decision ID
        now = datetime.now(timezone.utc)
        year = now.strftime("%Y")
        count = now.strftime("%d%H%M%S")
        dec_id = f"DEC-{year}-{count[-5:]}"

        # 1. Create Obsidian note
        note_path = rysos_core.vault.create_decision_note(
            decision_id=dec_id,
            title=req.title,
            context=req.context,
            assumptions=req.assumptions,
            outcome=req.outcome,
            priority=req.priority,
            status="em_analise",
            area_file=req.area_file,
            area_title=req.area_title,
            related_projects=req.related_projects,
            review_date=req.review_date,
            success_metric=req.success_metric,
        )

        # 2. Persist in DB
        async with get_db_session() as session:
            record = DecisionRecord(
                id=dec_id,
                title=req.title,
                context=req.context,
                status="em_analise",
                priority=req.priority,
                area=req.area_file,
                related_projects=",".join(req.related_projects),
                note_path=str(note_path),
            )
            session.add(record)

        return {
            "status": "created",
            "decision_id": dec_id,
            "note_path": str(note_path),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects")
async def create_project(req: ProjectCreateRequest) -> dict[str, Any]:
    """Creates a new flat project note in 01_Projects/."""
    try:
        note_path = rysos_core.vault.create_project_note(
            title=req.title,
            outcome_description=req.outcome_description,
            area_file=req.area_file,
            area_title=req.area_title,
            front=req.front,
            priority=req.priority,
            deadline=req.deadline,
        )
        return {
            "status": "created",
            "title": req.title,
            "note_path": str(note_path),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/quick-capture/refine")
async def refine_quick_capture_endpoint(req: QuickCaptureRefineRequest) -> dict[str, Any]:
    """Refines raw capture using Gemini and returns structured markdown for user review."""
    try:
        res = gemini_ai.refine_quick_capture(req.text, req.category)
        return {
            "status": "refined",
            "title": res.get("title", ""),
            "content": res.get("content", ""),
            "summary": res.get("summary", ""),
            "category": req.category,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/quick-capture")
async def quick_capture(req: QuickCaptureRequest) -> dict[str, Any]:
    """Captures a quick item across Obsidian Vault destinations."""
    try:
        now = datetime.now(timezone.utc)
        today_date = date.today()
        timestamp_str = now.strftime("%Y%m%d_%H%M%S")
        date_str = now.strftime("%Y-%m-%d %H:%M")
        today_str = today_date.strftime("%Y-%m-%d")

        text = req.text.strip()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        first_line = req.custom_title.strip() if req.custom_title else (lines[0] if lines else "Item sem título")
        safe_title = rysos_core.vault._sanitize_filename(first_line)
        rest_body = "\n\n".join(lines[1:]) if len(lines) > 1 else ""

        if req.category == "today":
            # Add directly as high priority focus / action checkbox in today's Daily Cockpit
            cockpit_dir = settings.OBSIDIAN_VAULT_PATH / "00_Cockpit" / "Daily"
            cockpit_dir.mkdir(parents=True, exist_ok=True)
            cockpit_file = cockpit_dir / f"{today_str}.md"

            checkbox_line = req.custom_content if req.custom_content else f"- [ ] {text}"
            if cockpit_file.exists():
                content = cockpit_file.read_text(encoding="utf-8")
                target_header = "## 🎯 Focos de Alta Prioridade do Dia"
                if target_header in content:
                    parts = content.split(target_header, 1)
                    updated = parts[0] + target_header + "\n\n" + checkbox_line + parts[1]
                else:
                    updated = content + f"\n\n{target_header}\n\n{checkbox_line}\n"
                rysos_core.vault.write_file_atomic(cockpit_file, updated)
            else:
                rysos_core.vault.create_daily_cockpit(
                    target_date=today_date,
                    high_priority_foci=[{"focus": text}],
                )
            return {"status": "saved", "target": "00_Cockpit", "path": str(cockpit_file)}

        elif req.category == "idea":
            filename = f"{safe_title}.md" if req.custom_title else f"Ideia_{timestamp_str}.md"
            target_path = rysos_core.vault.vault_path / "06_Resources" / "Ideias_e_Criatividade" / filename
            content = req.custom_content if req.custom_content else f"""---
id: IDEA-{timestamp_str}
title: "{first_line}"
type: resource
category: ideia
tags:
  - type/resource
  - ideia
created_at: {date_str}
---

# 💡 {first_line}

{text}
"""
            rysos_core.vault.write_file_atomic(target_path, content)
            return {"status": "saved", "target": "06_Resources/Ideias_e_Criatividade", "path": str(target_path)}

        elif req.category == "framework":
            filename = f"{safe_title}.md" if req.custom_title else f"Framework_{timestamp_str}.md"
            target_path = rysos_core.vault.vault_path / "06_Resources" / "Frameworks_e_Metodos" / filename
            content = req.custom_content if req.custom_content else f"""---
id: FRAMEWORK-{timestamp_str}
title: "{first_line}"
type: resource
category: framework
tags:
  - type/resource
  - framework
created_at: {date_str}
---

# 📚 {first_line}

{text}
"""
            rysos_core.vault.write_file_atomic(target_path, content)
            return {"status": "saved", "target": "06_Resources/Frameworks_e_Metodos", "path": str(target_path)}

        elif req.category == "project":
            if req.custom_content:
                target_path = rysos_core.vault.vault_path / "01_Projects" / f"{safe_title}.md"
                rysos_core.vault.write_file_atomic(target_path, req.custom_content)
                return {"status": "saved", "target": "01_Projects", "path": str(target_path)}
            else:
                proj_title = first_line
                outcome = rest_body or text
                note_path = rysos_core.vault.create_project_note(
                    title=proj_title,
                    outcome_description=outcome,
                )
                return {"status": "saved", "target": "01_Projects", "path": str(note_path)}

        elif req.category == "decision":
            dec_year = now.strftime("%Y")
            count_str = now.strftime("%d%H%M%S")[-5:]
            dec_id = f"DEC-{dec_year}-{count_str}"
            dec_title = req.custom_title or first_line
            if req.custom_content:
                target_path = rysos_core.vault.vault_path / "03_Decisions" / f"{dec_id}.md"
                rysos_core.vault.write_file_atomic(target_path, req.custom_content)
                note_path = target_path
            else:
                dec_context = rest_body or text
                note_path = rysos_core.vault.create_decision_note(
                    decision_id=dec_id,
                    title=dec_title,
                    context=dec_context,
                    status="em_analise",
                    priority="alta",
                )
            async with get_db_session() as session:
                rec = DecisionRecord(
                    id=dec_id,
                    title=dec_title,
                    context=req.custom_content or text,
                    status="em_analise",
                    priority="alta",
                    area="Negocios_e_Governanca",
                    related_projects="",
                    note_path=str(note_path),
                )
                session.add(rec)
            return {"status": "saved", "target": "03_Decisions", "path": str(note_path)}

        elif req.category == "person":
            if req.custom_content:
                target_path = rysos_core.vault.vault_path / "05_People" / f"{safe_title}.md"
                rysos_core.vault.write_file_atomic(target_path, req.custom_content)
                return {"status": "saved", "target": "05_People", "path": str(target_path)}
            else:
                person_name = first_line
                role_info = rest_body or ""
                note_path = rysos_core.vault.create_person_note(
                    name=person_name,
                    role=role_info,
                )
                return {"status": "saved", "target": "05_People", "path": str(note_path)}

        else:  # "inbox" default
            async with get_db_session() as session:
                item = InboxAgentItem(
                    title=(req.custom_title or first_line)[:80],
                    category="quick_capture",
                    priority="alta",
                    description=text,
                    source_reference="quick_capture",
                )
                session.add(item)

            filename = f"{safe_title}.md" if req.custom_title else f"Inbox_{timestamp_str}.md"
            inbox_file = rysos_core.vault.vault_path / "07_Inbox_Agent" / filename
            inbox_content = req.custom_content if req.custom_content else f"""---
id: INBOX-{timestamp_str}
title: "{first_line}"
type: inbox
status: pending_triage
created_at: {date_str}
tags:
  - type/inbox
  - triage
---

# 📥 Item de Entrada: {first_line}

{text}
"""
            rysos_core.vault.write_file_atomic(inbox_file, inbox_content)
            return {"status": "saved", "target": "07_Inbox_Agent", "path": str(inbox_file)}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat")
async def chat_with_agent(req: ChatRequest) -> dict[str, Any]:
    """Converses with the executive Gemini agent with live vault context."""
    try:
        summary = await rysos_core.get_dashboard_summary()
        context_lines = [
            f"Hoje é: {summary['today']}",
            f"Projetos Ativos: {len(summary['projects'])}",
            f"Decisões Registradas: {len(summary['decisions'])}",
        ]
        context = "\n".join(context_lines)
        response = gemini_ai.chat_with_agent(req.message, context=context)
        return {"response": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/auth/google/start")
def start_google_auth(background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Starts Google OAuth flow in the background."""
    try:
        background_tasks.add_task(google_auth.run_interactive_auth)
        return {"status": "auth_started", "message": "Navegador aberto para autorização com Google Workspace."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
