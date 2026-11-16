"""Vangnet: er mogen geen sleutels of geheime bestanden in de repo staan."""
import re
import subprocess

from conftest import ROOT

FORBIDDEN_FILES = re.compile(r"(^|/)(\.env(\.(?!example$).*)?|kaggle\.json)$|\.(pem|key|crt|sqlite|duckdb)$")
SECRET = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}|KGAT_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}"
                    r"|-----BEGIN [A-Z ]*PRIVATE KEY")
KEY_VARS = re.compile(r"^\s*(ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|KAGGLE_KEY|KAGGLE_USERNAME|KAGGLE_API_TOKEN)\s*=\s*(\S+)", re.M)


def tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines() if f]


def test_no_secret_files_are_tracked():
    assert [f for f in tracked() if FORBIDDEN_FILES.search(f)] == []


def test_no_secret_values_in_tracked_files():
    hits = []
    for f in tracked():
        try:
            text = (ROOT / f).read_text()
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        if SECRET.search(text):
            hits.append(f)
        hits += [f"{f}: {m.group(1)}" for m in KEY_VARS.finditer(text)]  # sleutelvariabele met een ingevulde waarde
    assert hits == []


def test_env_is_ignored_by_git_and_docker():
    ignored = subprocess.run(["git", "check-ignore", ".env", "kaggle.json", "data/x"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert ignored == [".env", "kaggle.json", "data/x"]
    assert ".env" in (ROOT / ".dockerignore").read_text().splitlines()
