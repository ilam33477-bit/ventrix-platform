from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".ini", ".json", ".md", ".mjs", ".py", ".sh", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml"
}
SKIP_PARTS = {".git", ".venv", "node_modules", "data", "backups", "__pycache__"}
PATTERNS = {
    "Telegram bot token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "DeepSeek API key": re.compile(r"\b" + "sk" + r"-[A-Za-z0-9]{20,}\b"),
    "Fernet configuration key": re.compile(r"APP_ENCRYPTION_KEY\s*=\s*[A-Za-z0-9_-]{43}="),
    "owner API token": re.compile(r"OWNER_API_TOKEN\s*=\s*[^\s#]{24,}"),
}


def candidate_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    paths: list[Path] = []
    for name in completed.stdout.splitlines():
        path = ROOT / name
        if path.name == ".env" or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.is_file() and (path.suffix.lower() in TEXT_SUFFIXES or path.name == "Dockerfile"):
            paths.append(path)
    return paths


def scan() -> list[tuple[str, Path]]:
    findings: list[tuple[str, Path]] = []
    for path in candidate_files():
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append((label, path.relative_to(ROOT)))
    return findings


def main() -> None:
    findings = scan()
    if findings:
        for label, path in findings:
            print(f"FAIL: possible {label} in {path}")
        raise SystemExit(1)
    print("PASS: no credential-shaped values found in Git candidate files")


if __name__ == "__main__":
    main()
