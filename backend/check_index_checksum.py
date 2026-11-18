"""Check a Chroma index directory against its manifest's tree_sha256.

Same algorithm as app/services/index_manifest._tree_sha256, with no app imports,
so it runs with plain python3 before a deploy:

    python3 check_index_checksum.py [index_dir] [manifest_json]
"""
import hashlib
import json
import sys
from pathlib import Path


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not path.name.startswith("chroma.sqlite3")
        and not path.name.endswith(("-wal", "-shm"))
    )
    if not files:
        raise SystemExit(f"Index directory is empty or missing: {root}")
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def main() -> int:
    here = Path(__file__).resolve().parent
    index_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "app" / "openai_chroma_data"
    manifest = Path(sys.argv[2]) if len(sys.argv) > 2 else here / "app" / "index_manifest.json"
    expected = json.loads(manifest.read_text())["tree_sha256"]
    actual = tree_sha256(index_dir)
    if actual != expected:
        print(f"INDEX CHECKSUM MISMATCH in {index_dir}\n  manifest: {expected}\n  actual:   {actual}")
        return 1
    print(f"Index checksum OK ({index_dir.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
