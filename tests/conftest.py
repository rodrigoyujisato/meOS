"""Pytest configuration and global fixtures."""

from pathlib import Path
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
try:
    from sqlalchemy.ext.asyncio import async_sessionmaker
except ImportError:
    from sqlalchemy.orm import sessionmaker as async_sessionmaker
from sqlalchemy.pool import NullPool

from rysos.config import settings
from rysos.vault.manager import VaultManager
from rysos.core import rysos_core
from rysos.db.models import Base
import rysos.db.database as db_module


@pytest.fixture(autouse=True)
async def isolate_test_environment(tmp_path: Path, monkeypatch):
    """Ensures test database and vault structure are strictly isolated to tmp_path for every test,
    and cleans up any created files after test execution."""
    test_vault = tmp_path / "vault"
    test_vault.mkdir(parents=True, exist_ok=True)
    test_data = tmp_path / "data"
    test_data.mkdir(parents=True, exist_ok=True)
    test_db_url = f"sqlite+aiosqlite:///{test_data / 'test_rysos.db'}"

    # Isolate settings
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", test_vault)
    monkeypatch.setattr(settings, "DATA_DIR", test_data)
    monkeypatch.setattr(settings, "DATABASE_URL", test_db_url)

    # Fictional owner identity (the app itself has no built-in user)
    monkeypatch.setattr(settings, "USER_NAME", "Rafael Moreira")
    monkeypatch.setattr(settings, "USER_EMAILS", ["owner@acme-corp.example", "owner.pessoal@gmail.com"])

    # Isolate DB engine and session factory
    test_engine = create_async_engine(test_db_url, echo=False, future=True, poolclass=NullPool)
    test_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(db_module, "async_session_factory", test_factory)

    # Initialize isolated DB tables
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Initialize and assign isolated vault manager to rysos_core
    vault = VaultManager(vault_path=test_vault)
    vault.initialize_vault_structure()
    rysos_core.vault = vault

    yield

    # Cleanup: apagar todas as notas e arquivos de teste criados
    for item in test_vault.glob("**/*"):
        if item.is_file():
            item.unlink(missing_ok=True)

    await test_engine.dispose()
