#!/usr/bin/env python3
"""Validate and build a deterministic FilaScan Import release archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.11+ is required.
    sys.exit("Python 3.11 or newer is required")


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "filascan_import"
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache"}


def fail(message: str) -> None:
    raise SystemExit(f"release validation failed: {message}")


def read_version() -> str:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    version = str(manifest.get("version", ""))
    if not VERSION_PATTERN.fullmatch(version):
        fail(f"invalid manifest version {version!r}")
    if manifest.get("plugin_key") != "filascan_import":
        fail("plugin_key must be filascan_import")
    if manifest.get("page_url") != "/plugin-page/filascan-import":
        fail("unexpected page_url")
    if manifest.get("show_in_nav") is not True:
        fail("show_in_nav must be true")
    if manifest.get("license") != "MIT":
        fail("manifest license must be MIT")

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if pyproject.get("project", {}).get("version") != version:
        fail("pyproject.toml version differs from plugin.json")

    init_text = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    init_match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init_text, re.M)
    if not init_match or init_match.group(1) != version:
        fail("__version__ differs from plugin.json")

    page = (PLUGIN_DIR / "page.html").read_text(encoding="utf-8")
    required_page_markers = (
        "const messages = {",
        "en: {",
        "de: {",
        "/api/v1/filascan-import/config",
        "localStorage.getItem('theme') || 'brand'",
    )
    for marker in required_page_markers:
        if marker not in page:
            fail(f"page.html is missing {marker!r}")
    if 'class="fm-sidebar"' in page or "/api/v1/plugin-nav" in page:
        fail("page.html must not contain the removed duplicate FilaMan sidebar")

    return version


def plugin_files() -> list[Path]:
    files = []
    for path in PLUGIN_DIR.rglob("*"):
        if not path.is_file() or any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        if path.suffix in {".pyc", ".pyo"} or path.name == ".DS_Store":
            continue
        files.append(path)
    return sorted(files)


def write_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def build(output_dir: Path) -> tuple[Path, Path]:
    version = read_version()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"filascan-import-{version}.zip"
    checksum_path = archive_path.with_suffix(".zip.sha256")

    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in plugin_files():
            relative = path.relative_to(ROOT).as_posix()
            write_entry(archive, relative, path.read_bytes())
        write_entry(archive, "filascan_import/LICENSE", (ROOT / "LICENSE").read_bytes())

    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if not names or any(not name.startswith("filascan_import/") for name in names):
            fail("archive must contain only the top-level filascan_import directory")
        if "filascan_import/plugin.json" not in names:
            fail("archive does not contain plugin.json")
        if "filascan_import/LICENSE" not in names:
            fail("archive does not contain the license")
        bad = archive.testzip()
        if bad:
            fail(f"corrupt ZIP member: {bad}")

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    return archive_path, checksum_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist",
        help="directory for the ZIP and SHA-256 file",
    )
    args = parser.parse_args()
    archive, checksum = build(args.output_dir.resolve())
    print(archive)
    print(checksum)


if __name__ == "__main__":
    main()
