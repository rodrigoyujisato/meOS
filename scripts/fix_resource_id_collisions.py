"""One-off: reassign unique `id:` values to 06_Resources notes that collide.

  .venv/bin/python scripts/fix_resource_id_collisions.py            # dry-run (default)
  .venv/bin/python scripts/fix_resource_id_collisions.py --apply    # write

Root cause (2026-09-16 Telegram feedback): `VaultManager.create_resource_note`
generated `resource_id` at MINUTE resolution only (`%Y%m%d%H%M`, no seconds, no
random suffix) until commit 6107424 fixed it to second-resolution + a uuid4
suffix — the same class of bug as the 2026-09-14 decision-id collision incident.
Every resource note written before that fix landed shares its `id:` with every
other resource created in the same wall-clock minute: 26 of 27 files in this
vault collide across 3 groups. These are NOT duplicate content — each file is a
distinct, real resource — so this script only rewrites the `id:` frontmatter
line, never merges or deletes anything. No other file in the vault wikilinks a
resource by its `id:` value (only by filename), so this is a pure, isolated
frontmatter fix.

New ids reuse the file's own `created_at` (so the id still reflects when the
note was actually created) at second resolution, with a fresh uuid4 suffix,
matching the current generator's format exactly.
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rysos.config import settings  # noqa: E402

_ID_RE = re.compile(r"^id:\s*(RESOURCE-\S+)\s*$", re.MULTILINE)
_CREATED_RE = re.compile(r"^created_at:\s*(\S+)\s*$", re.MULTILINE)
_SUBFOLDERS = ("Ideias_e_Criatividade", "Leituras_e_Pesquisas", "Frameworks_e_Metodos")


def _new_id(created_at: str) -> str:
    try:
        dt = datetime.fromisoformat(created_at)
        stamp = dt.strftime("%Y%m%d%H%M%S")
    except ValueError:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{stamp}{uuid.uuid4().hex[:3]}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = parser.parse_args()

    vault_path = Path(settings.OBSIDIAN_VAULT_PATH)
    resources_dir = vault_path / "06_Resources"

    by_id: dict[str, list[Path]] = defaultdict(list)
    contents: dict[Path, str] = {}
    for sub in _SUBFOLDERS:
        sub_dir = resources_dir / sub
        if not sub_dir.exists():
            continue
        for file in sorted(sub_dir.glob("*.md")):
            text = file.read_text(encoding="utf-8")
            m = _ID_RE.search(text)
            if not m:
                print(f"  ⚠️  sem id: {file.relative_to(vault_path)}")
                continue
            contents[file] = text
            by_id[m.group(1)].append(file)

    used_ids = set(by_id.keys())
    collisions = {rid: files for rid, files in by_id.items() if len(files) > 1}

    if not collisions:
        print("Nenhuma colisão de id encontrada em 06_Resources.")
        return

    total_renamed = 0
    for rid, files in sorted(collisions.items()):
        print(f"\nColisão em {rid} ({len(files)} arquivos):")
        # Keep the first file's id untouched; reassign the rest.
        for file in files[1:]:
            text = contents[file]
            created_m = _CREATED_RE.search(text)
            created_at = created_m.group(1) if created_m else ""
            new_id = _new_id(created_at)
            while new_id in used_ids:
                new_id = _new_id(created_at)
            used_ids.add(new_id)

            print(f"  {file.relative_to(vault_path)}: {rid} -> RESOURCE-{new_id}")
            if args.apply:
                new_text = _ID_RE.sub(f"id: RESOURCE-{new_id}", text, count=1)
                file.write_text(new_text, encoding="utf-8")
            total_renamed += 1

    mode = "aplicado" if args.apply else "dry-run (use --apply para gravar)"
    print(f"\n{total_renamed} arquivo(s) receberiam novo id — {mode}.")


if __name__ == "__main__":
    main()
