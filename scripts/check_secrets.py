#!/usr/bin/env python3
"""Zoekt sleutels en geheime bestanden in wat git volgt of wat klaarstaat voor een commit.

  check_secrets.py --staged    alleen wat nu gecommit wordt (gebruikt door de pre-commit hook)
  check_secrets.py --tracked   alles wat git volgt (gebruikt door de tests)

Alleen de standaardbibliotheek, zodat de hook zonder virtualenv werkt.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_FILES = re.compile(r"(^|/)(\.env(\.(?!example$).*)?|kaggle\.json)$|\.(pem|key|crt|sqlite|duckdb)$")
SECRET = re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]{20,}|sk-ant-[A-Za-z0-9_-]{8,}|KGAT_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}"
                    r"|-----BEGIN [A-Z ]*PRIVATE KEY")
# Een sleutelvariabele met een ingevulde waarde, zoals in een .env-bestand.
KEY_VARS = re.compile(r"^\s*(ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|KAGGLE_KEY|KAGGLE_USERNAME|KAGGLE_API_TOKEN)"
                      r"[ \t]*=[ \t]*(\S+)", re.M)


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


def files(staged: bool) -> list[str]:
    cmd = ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"] if staged else ["ls-files", "-z"]
    return [f for f in git(*cmd).decode().split("\0") if f]


def content(path: str, staged: bool) -> str | None:
    """De inhoud zoals die gecommit wordt (de index), niet wat toevallig op schijf staat."""
    try:
        raw = git("show", f":{path}") if staged else (ROOT / path).read_bytes()
        return raw.decode()
    except (subprocess.CalledProcessError, UnicodeDecodeError, FileNotFoundError):
        return None


def find(staged: bool) -> list[str]:
    problems = []
    for path in files(staged):
        if FORBIDDEN_FILES.search(path):
            problems.append(f"{path}: dit soort bestand hoort niet in de repo")
            continue
        text = content(path, staged)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if SECRET.search(line):
                problems.append(f"{path}:{n}: lijkt op een sleutel of private key")
            if m := KEY_VARS.search(line):
                problems.append(f"{path}:{n}: {m.group(1)} heeft een ingevulde waarde")
    return problems


if __name__ == "__main__":
    found = find(staged="--tracked" not in sys.argv)
    if found:
        print("Commit geblokkeerd: mogelijke geheimen gevonden.\n", file=sys.stderr)
        print("\n".join(f"  {p}" for p in found), file=sys.stderr)
        print("\nHaal ze uit de commit (git restore --staged <bestand>) en zet waarden in .env.", file=sys.stderr)
        sys.exit(1)
