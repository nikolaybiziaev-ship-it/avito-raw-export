import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "public_guard", ROOT / "scripts/check_public.py"
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.example",
        "exports/raw/account.json",
        "arbitrary_dump.json",
        "src/profile.json",
        "tests/chat.jpg",
        "logs/private.log",
        "voice.opus",
        "src/avito_raw_export.egg-info/PKG-INFO",
        "profiles.json",
    ],
)
def test_real_gitignore_and_guard_reject_data(name):
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", name], cwd=ROOT, capture_output=True
    )
    assert result.returncode == 0
    assert not guard.allowed(name)


def test_secret_scan_without_echoing_value():
    value = b'client_secret = "' + b"a" * 40 + b'"'
    assert guard.check_entry("src/example.py", value) == "possible credential"
    assert guard.check_entry("src/example.py", b'print("hello")') is None
