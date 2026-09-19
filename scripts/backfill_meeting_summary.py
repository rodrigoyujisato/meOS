"""One-off: backfill `## 📝 Notas & Discussão` with the ata's summary for meeting notes
whose transcript was filed before `VaultManager._fill_empty_notes_section` existed.

  .venv/bin/python scripts/backfill_meeting_summary.py            # dry-run
  .venv/bin/python scripts/backfill_meeting_summary.py --apply    # write

For every 04_Meetings note carrying a `<!-- transcript:... -->` block, the bullets under
that first block's `**Resumo:**` are copied into Notas & Discussão — only while that section
still holds the template placeholder, so anything typed by hand is never touched. Idempotent:
once filled, a note is skipped on the next run.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rysos.vault.manager import VaultManager  # noqa: E402

_MARKER_RE = re.compile(r"<!-- transcript:[0-9a-f]+ -->")


def ata_summary(content: str) -> list[str]:
    """Bullets under the first ata block's `**Resumo:**` heading (empty when absent)."""
    m = _MARKER_RE.search(content)
    if not m:
        return []
    lines = content[m.end():].split("\n")
    nxt = next((i for i, ln in enumerate(lines) if _MARKER_RE.search(ln)), len(lines))
    lines = lines[:nxt]
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "**Resumo:**")
    except StopIteration:
        return []
    out = []
    for ln in lines[start + 1:]:
        if not ln.startswith("- "):
            break
        out.append(ln[2:].strip())
    return out


def main(apply: bool) -> None:
    vault = VaultManager()
    filled = skipped_filled = no_summary = 0
    for path in sorted((vault.vault_path / "04_Meetings").glob("*.md")):
        content = vault.read_file(path) or ""
        if "<!-- transcript:" not in content:
            continue
        bullets = ata_summary(content)
        if not bullets:
            no_summary += 1
            print(f"sem resumo na ata: {path.name}")
            continue
        new = vault._fill_empty_notes_section(content, bullets)
        if new == content:
            skipped_filled += 1
            continue
        filled += 1
        print(f"preenche ({len(bullets)} tópico(s)): {path.name}")
        if apply:
            vault.write_file_atomic(path, new)
    mode = "aplicado" if apply else "dry-run (use --apply para gravar)"
    print(
        f"\n{filled} nota(s) {'preenchidas' if apply else 'seriam preenchidas'}, "
        f"{skipped_filled} já tinham conteúdo em Notas & Discussão, "
        f"{no_summary} sem resumo na ata — {mode}."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    main(parser.parse_args().apply)
