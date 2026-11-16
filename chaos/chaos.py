"""chaos: koerier tussen outbox/ en landing/.

  run                 levert batches af; met kans CHAOS_PROBABILITY wordt een batch verstoord
  force <type> [var]  de eerstvolgende batch krijgt gegarandeerd deze verstoring

Schrijft wat hij deed naar chaos_log.jsonl. Dat log is de waarheid waarmee achteraf de
diagnose wordt vergeleken; de healer krijgt het niet te zien.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import chaos_agent
from injectors import VARIANTS, inject, read

OUTBOX = Path(os.environ.get("OUTBOX_DIR", "/outbox"))
LANDING = Path(os.environ.get("LANDING_DIR", "/landing"))
STATE = Path(os.environ.get("CHAOS_STATE_DIR", "/state"))
PROBABILITY = float(os.environ.get("CHAOS_PROBABILITY", "0.3"))
MODE = os.environ.get("CHAOS_MODE", "random")
SEED = os.environ.get("CHAOS_SEED")
POLL = int(os.environ.get("CHAOS_POLL_SECONDS", "5"))
WARMUP = int(os.environ.get("CHAOS_WARMUP_BATCHES", "4"))  # eerst een paar schone batches als referentie

LOG = STATE / "chaos_log.jsonl"
FORCE = STATE / "force.json"


def log(msg: str) -> None:
    print(f"[chaos] {msg}", flush=True)


def history() -> list[dict]:
    return [json.loads(line) for line in LOG.read_text().splitlines()] if LOG.exists() else []


def summarize(batch: Path) -> str:
    lines = []
    for f in sorted(batch.glob("*.csv")):
        header, rows = read(batch, f.stem)
        lines.append(f"- {f.stem}: {len(rows)} rijen, kolommen {', '.join(header)}")
    return "\n".join(lines)


def decide(batch: Path, past: list[dict], rng: random.Random) -> list[dict]:
    if FORCE.exists():
        forced = json.loads(FORCE.read_text())
        FORCE.unlink()
        return [forced]
    if len(past) < WARMUP or rng.random() >= PROBABILITY:
        return []
    allowed = sorted(VARIANTS)
    if MODE == "llm":
        recent = [i["issue_type"] for p in past for i in p["injections"]][-8:]
        try:
            return chaos_agent.choose_llm(summarize(batch), recent, allowed)
        except Exception as exc:
            log(f"Claude-keuze mislukt ({exc}); willekeurige keuze")
    return chaos_agent.choose_random(rng, allowed)


def deliver(batch: Path, rng: random.Random) -> dict:
    past = history()
    tmp = LANDING / f".tmp-{batch.name}"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(batch, tmp)
    injections = []
    for pick in decide(tmp, past, rng):
        try:
            done = inject(tmp, pick["issue_type"], rng, pick.get("variant"), pick.get("fraction"))
            if pick.get("reason"):
                done["reason"] = pick["reason"]
            injections.append(done)
        except Exception as exc:  # een verstoring die niet past op deze batch slaan we over
            log(f"{pick} overgeslagen: {exc}")
    entry = {"batch_id": batch.name, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "mode": MODE, "injections": injections}
    with open(LOG, "a") as f:  # eerst loggen, dan afleveren: geen verstoring zonder spoor
        f.write(json.dumps(entry) + "\n")
    tmp.rename(LANDING / batch.name)
    return entry


def run() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    LANDING.mkdir(parents=True, exist_ok=True)
    rng = random.Random(int(SEED)) if SEED else random.Random()
    log(f"mode={MODE} kans={PROBABILITY} warmup={WARMUP}")
    while True:
        done = {e["batch_id"] for e in history()}
        if OUTBOX.exists():
            for batch in sorted(p for p in OUTBOX.iterdir() if p.is_dir() and not p.name.startswith(".")):
                if batch.name not in done:
                    entry = deliver(batch, rng)
                    what = ", ".join(f"{i['issue_type']}/{i['variant']}" for i in entry["injections"]) or "ongewijzigd"
                    log(f"{batch.name}: {what}")
        time.sleep(POLL)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "force":
        if sys.argv[2] not in VARIANTS:
            sys.exit(f"onbekend type; kies uit: {', '.join(sorted(VARIANTS))}")
        STATE.mkdir(parents=True, exist_ok=True)
        forced = {"issue_type": sys.argv[2]}
        if len(sys.argv) > 3:
            forced["variant"] = sys.argv[3]
        FORCE.write_text(json.dumps(forced))
        log(f"eerstvolgende batch krijgt {forced}")
    else:
        run()
