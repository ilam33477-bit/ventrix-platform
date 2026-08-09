from __future__ import annotations

import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from .secret_scan import FORBIDDEN_PARTS, FORBIDDEN_SUFFIXES, ROOT, scan

SAFE_TOP_LEVEL_FILES = {
    ".dockerignore",
    ".env.example",
    ".gitignore",
    "Dockerfile",
    "README.md",
    "alembic.ini",
    "docker-compose.yml",
    "eslint.config.mjs",
    "next.config.ts",
    "package-lock.json",
    "package.json",
    "postcss.config.mjs",
    "pyproject.toml",
    "tsconfig.json",
}
SAFE_ROOTS = {"alembic", "app", "docs", "infra", "packages", "scripts", "services", "tests"}


def tracked_source_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    )
    files: list[Path] = []
    for name in completed.stdout.splitlines():
        relative = Path(name)
        if not relative.parts:
            continue
        if relative.name.startswith(".env") and relative.name != ".env.example":
            raise RuntimeError(f"refusing to export secret file: {relative}")
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            if relative.name == ".gitkeep":
                continue
            raise RuntimeError(f"refusing to export runtime path: {relative}")
        if relative.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise RuntimeError(f"refusing to export runtime file: {relative}")
        if relative.parts[0] not in SAFE_ROOTS and relative.as_posix() not in SAFE_TOP_LEVEL_FILES:
            continue
        source = (ROOT / relative).resolve()
        if not source.is_relative_to(ROOT.resolve()) or not source.is_file() or source.is_symlink():
            raise RuntimeError(f"unsafe source path: {relative}")
        files.append(relative)
    return sorted(files)


def export_source(output: Path | None = None) -> Path:
    findings = scan()
    if findings:
        rendered = ", ".join(f"{label}: {path}" for label, path in findings)
        raise RuntimeError(f"secret scan failed before export: {rendered}")
    destination = output or (
        ROOT
        / "dist"
        / f"ventrix-source-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.zip"
    )
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in tracked_source_files():
            archive.write(ROOT / relative, relative.as_posix())
    return destination


def main() -> None:
    print(export_source())


if __name__ == "__main__":
    main()
