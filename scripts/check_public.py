"""Fail closed on non-source files and likely credentials; never print their values."""

import re
import subprocess
import sys
from pathlib import PurePosixPath


def git(*args):
    return subprocess.check_output(["git", *args])


def allowed(name):
    path = PurePosixPath(name)
    exact = {
        ".gitignore",
        ".gitattributes",
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "AUDIT.md",
        "CODEX_LIVE_VALIDATION.md",
        "pyproject.toml",
        "run_windows.bat",
        "build_windows.bat",
        ".githooks/pre-commit",
        ".githooks/pre-push",
    }
    return (
        name in exact
        or (
            path.parts[0] in {"src", "tests", "scripts"}
            and path.suffix == ".py"
            and "__pycache__" not in path.parts
        )
        or (path.parent == PurePosixPath(".github/workflows") and path.suffix == ".yml")
    )


PATTERNS = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(rb"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(
        rb'(?i)(?:client[_ -]?(?:id|secret)|access[_ -]?token|authorization)\s*["\']?\s*[:=]\s*["\'][A-Za-z0-9_./+\-=]{24,}["\']'
    ),
]


def check_entry(name, body):
    if not allowed(name):
        return "not a public source path"
    if len(body) > 512_000 or b"\x00" in body:
        return "binary or oversized file"
    if any(pattern.search(body) for pattern in PATTERNS):
        return "possible credential"
    return None


def main():
    revisions = sys.argv[1:]
    if revisions:
        commits = set()
        for revision in revisions:
            commits.update(git("rev-list", revision).decode().splitlines())
        entries = []
        seen = set()
        for commit in commits:
            for entry in git("ls-tree", "-rz", commit).split(b"\0"):
                if not entry:
                    continue
                info, name = entry.split(b"\t", 1)
                mode, kind, oid = info.split()
                if (name, oid) in seen:
                    continue
                seen.add((name, oid))
                entries.append((name.decode(), mode, oid.decode()))
    else:
        entries = []
        for entry in git("ls-files", "--stage", "-z").split(b"\0"):
            if entry:
                info, name = entry.split(b"\t", 1)
                mode, oid, stage = info.split()
                entries.append((name.decode(), mode, oid.decode()))
    errors = []
    for name, mode, oid in entries:
        reason = (
            "symlink or submodule"
            if mode not in {b"100644", b"100755"}
            else check_entry(name, git("cat-file", "blob", oid))
        )
        if reason:
            errors.append(f"{name}: {reason}")
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Public-code guard passed: {len(entries)} file versions checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
