import subprocess
import sys


def test_reproducible_smoke_script() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.smoke_test"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PASS:" in completed.stdout
