"""Meldingen naar een webhook in Slack-formaat ({"text": ...}) zodra een mens aan zet is."""
from __future__ import annotations

import json
import threading
import urllib.request

NOTIFY_ON = {"validated": "wacht op je besluit", "needs_human": "heeft je nodig: de automatische oplossing is niet gelukt"}


def message(doc: dict, labels: dict, public_url: str, week: str) -> str | None:
    what = NOTIFY_ON.get(doc["status"])
    if what is None:
        return None
    types = ", ".join(labels[t].lower() for t in sorted({f["issue_type"] for f in doc["findings"]}))
    revenue = (doc.get("impact") or {}).get("revenue")
    money = f" (R$ {revenue:,.0f} omzet)".replace(",", ".") if revenue else ""
    return f"Levering week van {week}{money} {what}. Probleem: {types}. {public_url}/incidents/{doc['id']}"


def send(url: str, text: str) -> None:
    """Verstuurt op de achtergrond; een onbereikbare webhook mag de console nooit ophouden."""
    def post():
        try:
            req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as exc:
            print(f"[console] melding niet verstuurd: {exc}", flush=True)
    threading.Thread(target=post, daemon=True).start()
