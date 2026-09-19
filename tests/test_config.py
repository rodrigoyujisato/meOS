"""Tests for rysOS Configuration."""

from pathlib import Path
from rysos.config import Settings


def test_settings_default_paths(tmp_path: Path):
    custom_vault = tmp_path / "Obsidian"
    # _env_file=None: skip loading the real .env so the asserted defaults reflect the
    # class definition, not whatever Rafael currently has configured locally.
    settings = Settings(_env_file=None, OBSIDIAN_VAULT_PATH=custom_vault)

    assert settings.OBSIDIAN_VAULT_PATH == custom_vault
    assert settings.PORT == 8000
    assert settings.GEMINI_MODEL == "gemini-3.5-flash-lite"


def test_ensure_directories(tmp_path: Path):
    vault_dir = tmp_path / "Obsidian"
    vault_dir.mkdir(parents=True)
    settings = Settings(OBSIDIAN_VAULT_PATH=vault_dir, DATA_DIR=tmp_path / "data")

    settings.ensure_directories()

    assert (vault_dir / "00_Cockpit" / "Daily").exists()
    assert (vault_dir / "01_Projects").exists()
    assert (vault_dir / "02_Areas").exists()
    assert (vault_dir / "03_Decisions").exists()
    assert (vault_dir / "04_Meetings").exists()
    assert (vault_dir / "05_People").exists()
    assert (vault_dir / "06_Resources").exists()
