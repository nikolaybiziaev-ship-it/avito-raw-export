from pathlib import Path
import subprocess


def test_gitignore_blocks_sensitive_outputs():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".gitignore").read_text(encoding="utf-8")
    for entry in (".env", "exports/", "analysis_ready/", "media/", "logs/"):
        assert entry in text


def test_gitignore_ignores_analysis_ready_outputs():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "check-ignore", "-q", "analysis_ready/chat_synthetic.txt"],
        cwd=root,
    )
    assert result.returncode == 0


def test_exporter_has_no_mutating_messenger_paths():
    root = Path(__file__).resolve().parents[1]
    text = (root / "src" / "avito_raw_export" / "exporter.py").read_text(
        encoding="utf-8"
    )
    forbidden = (
        "/read",
        "/blacklist",
        "/messages/image",
        "/webhook",
        "/ratings/v1/answers",
    )
    assert all(token not in text for token in forbidden)
