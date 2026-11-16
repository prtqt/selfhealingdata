"""console: REST-API voor het incidentregister en het HTML-scherm voor goedkeuring."""
from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from store import CLOSED, STATUSES, Conflict, NotFound, Store, now

CONTRACTS = Path(os.environ.get("CONTRACTS_DIR", "/contracts"))
STATE = Path(os.environ.get("CONSOLE_STATE_DIR", "/state"))
CHAOS_LOG = Path(os.environ.get("CHAOS_LOG_PATH", "/chaos/chaos_log.jsonl"))
REPORT_DIR = Path(os.environ.get("REPORT_DIR", STATE))

STATUS_LABELS = {
    "detected": "Gedetecteerd", "investigating": "In onderzoek", "proposed": "Voorstel wordt gecontroleerd",
    "validated": "Wacht op goedkeuring", "needs_human": "Mens nodig", "approved": "Wordt uitgevoerd",
    "resolved": "Opgelost", "discarded": "Weggegooid",
}
ISSUE_LABELS = {
    "null_spike": "Lege waarden", "duplicates": "Duplicaten", "invalid_values": "Ongeldige waarden",
    "referential_integrity": "Kapotte verwijzingen", "volume_anomaly": "Afwijkend volume", "schema_drift": "Schema drift",
}

app = FastAPI(title="Self-healing console")
store = Store(STATE / "incidents.sqlite", int(os.environ.get("MAX_ATTEMPTS", "3")))
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(STATUS_LABELS=STATUS_LABELS, ISSUE_LABELS=ISSUE_LABELS)
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False)


def check(name: str, payload: dict) -> None:
    schema = json.loads((CONTRACTS / f"{name}.json").read_text())
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        raise HTTPException(422, f"voldoet niet aan contract {name}.json: {exc.message}")


def guarded(fn, *args):
    try:
        return fn(*args)
    except NotFound:
        raise HTTPException(404, "incident bestaat niet")
    except Conflict as exc:
        raise HTTPException(409, str(exc))


# ---------------------------------------------------------------- API

@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/incidents")
def create_incident(payload: dict):
    check("incident", payload)
    return store.create(payload)


@app.get("/api/incidents")
def list_incidents(status: str | None = None):
    return store.list(status)


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str):
    return guarded(store.get, incident_id)


@app.post("/api/incidents/{incident_id}/claim")
def claim(incident_id: str):
    return guarded(store.claim, incident_id)


@app.post("/api/incidents/{incident_id}/failure")
def failure(incident_id: str, payload: dict):
    return guarded(store.fail, incident_id, str(payload.get("detail", ""))[:2000])


@app.post("/api/incidents/{incident_id}/proposal")
def proposal(incident_id: str, payload: dict):
    check("proposal", payload)
    return guarded(store.propose, incident_id, payload)


@app.post("/api/incidents/{incident_id}/validation")
def validation(incident_id: str, payload: dict):
    check("validation", payload)
    return guarded(store.validate, incident_id, payload)


@app.post("/api/incidents/{incident_id}/decision")
def decision(incident_id: str, payload: dict):
    check("decision", payload)
    return guarded(store.decide, incident_id, payload)


@app.post("/api/incidents/{incident_id}/resolution")
def resolution(incident_id: str, payload: dict):
    check("resolution", payload)
    return guarded(store.resolve, incident_id, payload)


# ---------------------------------------------------------------- weergave

def chaos_truth(doc: dict) -> dict | None:
    """Wat de chaos-agent werkelijk deed. Pas zichtbaar als het incident is afgesloten."""
    if doc["status"] not in CLOSED or not CHAOS_LOG.exists():
        return None
    entries = [json.loads(line) for line in CHAOS_LOG.read_text().splitlines() if line.strip()]
    entry = next((e for e in entries if e["batch_id"] == doc["batch_id"]), None)
    if entry is None:
        return None
    truth = {i["issue_type"] for i in entry["injections"]}
    detected = {f["issue_type"] for f in doc["findings"]}
    attempts = [doc] + doc["previous_attempts"]
    diagnosed = {fx["issue_type"] for a in attempts if a.get("proposal") for fx in a["proposal"]["fixes"]}
    return {"injections": entry["injections"], "truth": sorted(truth), "detected_ok": truth <= detected,
            "diagnosed_ok": truth <= diagnosed if diagnosed else None, "organic": not truth}


def view(doc: dict) -> dict:
    types = sorted({f["issue_type"] for f in doc["findings"]})
    fixes = (doc.get("proposal") or {}).get("fixes", [])
    all_fixes = fixes + [fx for a in doc["previous_attempts"] if a.get("proposal") for fx in a["proposal"]["fixes"]]
    return {**doc, "types": types, "truth": chaos_truth(doc),
            "models": sorted({fx["model"] for fx in fixes}),
            "cost_usd": round(sum((fx.get("usage") or {}).get("cost_usd", 0) for fx in all_fixes), 4)}


def summary(incidents: list[dict]) -> dict:
    counts = {s: 0 for s in STATUSES}
    for i in incidents:
        counts[i["status"]] += 1
    scored = [i["truth"] for i in incidents if i["truth"] and not i["truth"]["organic"]]
    return {"counts": counts, "total": len(incidents),
            "cost_usd": round(sum(i["cost_usd"] for i in incidents), 4),
            "scored": len(scored),
            "detected_ok": sum(1 for t in scored if t["detected_ok"]),
            "diagnosed_ok": sum(1 for t in scored if t["diagnosed_ok"])}


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, status: str = "", issue: str = ""):
    everything = [view(d) for d in store.list()]
    shown = [i for i in everything if (not status or i["status"] == status) and (not issue or issue in i["types"])]
    return templates.TemplateResponse(request, "overview.html", {
        "incidents": shown, "summary": summary(everything), "status": status, "issue": issue, "static": False})


@app.get("/incidents/{incident_id}", response_class=HTMLResponse)
def detail(request: Request, incident_id: str, error: str = ""):
    doc = guarded(store.get, incident_id)
    return templates.TemplateResponse(request, "detail.html", {"i": view(doc), "error": error, "static": False})


@app.post("/incidents/{incident_id}/decision")
def decide_form(incident_id: str, decision: str = Form(...), comment: str = Form(""), decided_by: str = Form("")):
    payload = {"decision": decision, "comment": comment, "decided_by": decided_by or "console"}
    check("decision", payload)
    try:
        store.decide(incident_id, payload)
    except NotFound:
        raise HTTPException(404, "incident bestaat niet")
    except Conflict as exc:
        return RedirectResponse(f"/incidents/{incident_id}?error={exc}", status_code=303)
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@app.get("/export", response_class=HTMLResponse)
def export(request: Request):
    """Eén losstaand HTML-bestand met alle incidenten en oplossingen, zonder knoppen."""
    everything = [view(d) for d in store.list()]
    html = templates.get_template("report.html").render(
        incidents=everything, summary=summary(everything), static=True, generated_at=now())
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "report.html").write_text(html)
    return HTMLResponse(html, headers={"Content-Disposition": 'inline; filename="report.html"'})
