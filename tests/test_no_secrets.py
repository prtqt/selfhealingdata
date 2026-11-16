"""Vangnet: er mogen geen sleutels of geheime bestanden in de repo staan.

De regels zelf staan in scripts/check_secrets.py, dat ook door de pre-commit hook wordt gebruikt.
"""
import subprocess
import sys

from conftest import ROOT

sys.path.insert(0, str(ROOT / "scripts"))
import check_secrets  # noqa: E402


def test_nothing_secret_is_tracked():
    assert check_secrets.find(staged=False) == []


def test_rules_catch_keys_and_secret_files():
    assert check_secrets.SECRET.search("key = sk-ant-" + "a1b2c3d4e5f6")
    assert check_secrets.KEY_VARS.search("KAGGLE_KEY=abc123")
    assert not check_secrets.KEY_VARS.search("KAGGLE_KEY=\nSTART_DATE=2017-02-06")   # leeg in .env.example mag
    for bad in (".env", ".env.local", "kaggle.json", "certs/server.pem", "data/x.duckdb"):
        assert check_secrets.FORBIDDEN_FILES.search(bad), bad
    assert not check_secrets.FORBIDDEN_FILES.search(".env.example")


def test_env_is_ignored_by_git_and_docker():
    ignored = subprocess.run(["git", "check-ignore", ".env", "kaggle.json", "data/x"], cwd=ROOT,
                             capture_output=True, text=True).stdout.split()
    assert ignored == [".env", "kaggle.json", "data/x"]
    assert ".env" in (ROOT / ".dockerignore").read_text().splitlines()


def test_hook_is_executable_and_runs_the_checker():
    hook = ROOT / ".githooks/pre-commit"
    assert hook.stat().st_mode & 0o111 and "check_secrets.py" in hook.read_text()
