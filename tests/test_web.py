"""Tests for FastAPI Web Endpoints and PWA assets."""

from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport
from rysos.web.app import app
from rysos.core import rysos_core


@pytest.fixture(autouse=True)
async def initialize_app_state():
    await rysos_core.initialize()


@pytest.mark.asyncio
async def test_get_status():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/status")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "online"
        assert "vault_path" in data


@pytest.mark.asyncio
async def test_get_dashboard():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/dashboard")
        assert res.status_code == 200
        data = res.json()
        assert "today" in data
        assert "projects" in data


@pytest.mark.asyncio
async def test_create_decision_via_api():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "title": "Decisão de Teste Web",
            "context": "Contexto do teste",
            "priority": "alta",
            "outcome": "Aprovado",
        }
        res = await client.post("/api/decisions", json=payload)
        assert res.status_code == 200, f"Error: {res.text}"
        data = res.json()
        assert data["status"] == "created"
        assert data["decision_id"].startswith("DEC-")

        # Cleanup: apagar a nota de decisão de teste criada
        created_file = Path(data["note_path"]) if "note_path" in data else rysos_core.vault.decisions_path / f"{data['decision_id']}.md"
        if created_file.exists():
            created_file.unlink()
        assert not created_file.exists()


@pytest.mark.asyncio
async def test_pwa_manifest_and_sw():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        manifest_res = await client.get("/manifest.json")
        assert manifest_res.status_code == 200
        assert manifest_res.json()["short_name"] == "rysOS"

        sw_res = await client.get("/sw.js")
        assert sw_res.status_code == 200
        assert "rysos-cache" in sw_res.text


@pytest.mark.asyncio
async def test_quick_capture_all_destinations():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Today
        res_today = await client.post("/api/quick-capture", json={
            "text": "Prioridade de Teste para o Cockpit",
            "category": "today"
        })
        assert res_today.status_code == 200
        assert res_today.json()["target"] == "00_Cockpit"

        # 2. Idea
        res_idea = await client.post("/api/quick-capture", json={
            "text": "Ideia Incrível\nDetalhe da ideia genial.",
            "category": "idea"
        })
        assert res_idea.status_code == 200
        assert res_idea.json()["target"] == "06_Resources/Ideias_e_Criatividade"

        # 3. Framework
        res_frame = await client.post("/api/quick-capture", json={
            "text": "Matriz Eisenhower\nUrgente vs Importante.",
            "category": "framework"
        })
        assert res_frame.status_code == 200
        assert res_frame.json()["target"] == "06_Resources/Frameworks_e_Metodos"

        # 4. Project
        res_proj = await client.post("/api/quick-capture", json={
            "text": "Projeto Alfa\nOutcome principal do projeto.",
            "category": "project"
        })
        assert res_proj.status_code == 200
        assert res_proj.json()["target"] == "01_Projects"

        # 5. Decision
        res_dec = await client.post("/api/quick-capture", json={
            "text": "Decisão sobre Stack\nOptar por Python vs Go.",
            "category": "decision"
        })
        assert res_dec.status_code == 200
        assert res_dec.json()["target"] == "03_Decisions"

        # 6. Person
        res_person = await client.post("/api/quick-capture", json={
            "text": "Carlos Silva\nDiretor de Tecnologia",
            "category": "person"
        })
        assert res_person.status_code == 200
        assert res_person.json()["target"] == "05_People"

        # 7. Inbox
        res_inbox = await client.post("/api/quick-capture", json={
            "text": "Nota solta para a IA triar depois",
            "category": "inbox"
        })
        assert res_inbox.status_code == 200
        assert res_inbox.json()["target"] == "07_Inbox_Agent"

        # Cleanup: apagar todas as notas de teste criadas
        created_paths = [
            Path(res_today.json()["path"]),
            Path(res_idea.json()["path"]),
            Path(res_frame.json()["path"]),
            Path(res_proj.json()["path"]),
            Path(res_dec.json()["path"]),
            Path(res_person.json()["path"]),
            Path(res_inbox.json()["path"]),
        ]
        for p in created_paths:
            if p.exists():
                p.unlink()


@pytest.mark.asyncio
async def test_quick_capture_refine_and_approve(monkeypatch):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Mock Gemini refinement if offline/mocked
        from rysos.ai.gemini import gemini_ai
        monkeypatch.setattr(
            gemini_ai,
            "refine_quick_capture",
            lambda text, cat: {
                "title": "Sistema Operacional Cerebral no Raspberry Pi",
                "content": f"# 💡 Sistema Operacional Cerebral\n\nConceito mockado.\n\n{text}",
                "summary": "Proposta de arquitetura de segundo cérebro em host local",
            }
        )

        # 1. Request AI refinement
        refine_res = await client.post("/api/quick-capture/refine", json={
            "text": "eu tive uma ideia de fazer um sistema operacional do meu cerebro, acho que dá pra mockar no raspi",
            "category": "idea",
        })
        assert refine_res.status_code == 200
        data = refine_res.json()
        assert data["status"] == "refined"
        assert data["title"] == "Sistema Operacional Cerebral no Raspberry Pi"
        assert "Sistema Operacional Cerebral" in data["content"]

        # 2. Approve and save
        save_res = await client.post("/api/quick-capture", json={
            "text": "eu tive uma ideia...",
            "category": "idea",
            "custom_title": data["title"],
            "custom_content": data["content"] + "\n\n[Revisado e Aprovado por Rafael Moreira]",
        })
        assert save_res.status_code == 200
        save_data = save_res.json()
        assert save_data["target"] == "06_Resources/Ideias_e_Criatividade"
        saved_path = Path(save_data["path"])
        assert saved_path.exists()
        assert "[Revisado e Aprovado por Rafael Moreira]" in saved_path.read_text(encoding="utf-8")

        # Cleanup: apagar a nota de teste criada
        if saved_path.exists():
            saved_path.unlink()
        assert not saved_path.exists()


