import zipfile

from scripts.export_source import export_source
from scripts.secret_scan import scan


def test_git_candidate_files_do_not_contain_secrets() -> None:
    assert scan() == []


def test_source_export_contains_only_safe_tracked_files(tmp_path) -> None:
    archive_path = export_source(tmp_path / "ventrix-source.zip")
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    assert "README.md" in names
    assert not any(
        name.startswith(
            (".git/", ".next/", ".venv/", "backups/", "data/", "logs/", "node_modules/")
        )
        or name.endswith((".db", ".sqlite", ".sqlite3", ".session", ".log"))
        or (name.rsplit("/", 1)[-1].startswith(".env") and name != ".env.example")
        for name in names
    )
