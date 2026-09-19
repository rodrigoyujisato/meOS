"""Tests for Database Models and SQLite storage."""

import pytest
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
try:
    from sqlalchemy.ext.asyncio import async_sessionmaker
except ImportError:
    from sqlalchemy.orm import sessionmaker as async_sessionmaker
from rysos.db.models import Base, ProcessedEmail, DecisionRecord, SyncLog, InboxAgentItem


@pytest.fixture
async def test_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_and_query_processed_email(test_session: AsyncSession):
    email = ProcessedEmail(
        id="msg_123",
        thread_id="thread_456",
        subject="Proposta de M&A Alfa",
        sender="investor@example.com",
        date_received=datetime.now(timezone.utc),
        priority="alta",
        summary="Resumo da proposta",
        suggested_action="Aprovar e agendar call",
    )
    test_session.add(email)
    await test_session.commit()

    queried = await test_session.get(ProcessedEmail, "msg_123")
    assert queried is not None
    assert queried.subject == "Proposta de M&A Alfa"
    assert queried.priority == "alta"


@pytest.mark.asyncio
async def test_create_decision_record(test_session: AsyncSession):
    decision = DecisionRecord(
        id="DEC-2026-001",
        title="Aprovação de Nova Estratégia",
        status="em_analise",
        priority="alta",
        area="Negocios_e_Governanca",
        related_projects="Projeto Alpha",
    )
    test_session.add(decision)
    await test_session.commit()

    stmt = select(DecisionRecord).where(DecisionRecord.id == "DEC-2026-001")
    res = (await test_session.execute(stmt)).scalar_one_or_none()
    assert res is not None
    assert res.status == "em_analise"
    assert res.priority == "alta"
