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

# Dezelfde gegevens, twee lezers. De product owner krijgt gewone taal en het besluit;
# de developer krijgt controles, SQL, modellen en tokens.
PO_ISSUE_LABELS = {
    "null_spike": "Ontbrekende gegevens", "duplicates": "Dubbele gegevens", "invalid_values": "Onmogelijke waarden",
    "referential_integrity": "Losse verwijzingen", "volume_anomaly": "Onvolledige levering", "schema_drift": "Gewijzigd formaat",
}
PO_GROUPS = {"validated": "wait", "needs_human": "wait", "detected": "busy", "investigating": "busy", "proposed": "busy",
             "approved": "processing", "resolved": "done", "discarded": "done"}
PO_GROUP_LABELS = {"wait": "Wacht op jou", "busy": "Wordt onderzocht", "processing": "Wordt verwerkt", "done": "Afgehandeld"}
TABLES_NL = {"orders": "bestellingen", "order_items": "orderregels", "order_payments": "betalingen",
             "customers": "klanten", "products": "producten"}
MONTHS = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"]


def nl_date(iso: str | None) -> str:
    try:
        y, m, d = (int(x) for x in iso[:10].split("-"))
        return f"{d} {MONTHS[m - 1]} {y}"
    except (TypeError, ValueError, IndexError):
        return iso or ""


def plain_finding(f: dict) -> str:
    """Een bevinding in één zin zonder vaktaal."""
    t, col = TABLES_NL.get(f["table_name"], f["table_name"]), f.get("column_name")
    n, total, check = f["n_affected"], f["n_total"], f["check_name"]
    if check == "required_not_null":
        return f"Bij {n} van de {total} {t} ontbreekt een verplicht gegeven."
    if check == "unique_key":
        return f"{n} {t} zijn dubbel aangeleverd."
    if check == "missing_column":
        return f"Bij de {t} ontbreekt een gegeven dat we verwachten ({col})."
    if check == "unexpected_column":
        return f"Bij de {t} is een onbekend gegeven meegeleverd ({col})."
    if check == "type_mismatch":
        return f"Bij {n} van de {total} {t} staat een waarde in een onleesbaar formaat ({col})."
    if check == "missing_table":
        return f"De {t} zijn helemaal niet aangeleverd."
    if check == "empty_table":
        return f"Er zijn geen {t} aangeleverd."
    if check == "row_count_deviation":
        return f"Het aantal {t} ({n}) wijkt sterk af van wat normaal is."
    if check.startswith("fk_"):
        return f"{n} {t} verwijzen naar gegevens die niet zijn meegeleverd."
    return f"{n} {t} hebben een waarde die niet kan kloppen: {f['details']}."


def money(amount: float | None) -> str:
    """Bedrag in Braziliaanse real, zoals de Olist-data."""
    if amount is None:
        return ""
    return "R$ " + f"{amount:,.0f}".replace(",", ".")


def minutes_between(a: str, b: str) -> float:
    from datetime import datetime
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60


def duration(minutes: float | None) -> str:
    if minutes is None:
        return "–"
    if minutes < 90:
        return f"{round(minutes)} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} uur".replace(".", ",")
    return f"{round(minutes / 1440)} dagen"


SHARED_PAGES = {"rules"}  # pagina's die voor beide lezers gelijk zijn, op een enkel blok na


def current_view(request: Request) -> str:
    """?view= wint en wordt onthouden in een cookie; standaard de product-ownerweergave."""
    chosen = request.query_params.get("view") or request.cookies.get("view")
    return chosen if chosen in ("po", "dev") else "po"


def page(request: Request, name: str, context: dict):
    v = current_view(request)
    template = f"{name}.html" if name in SHARED_PAGES else f"{name}_{v}.html"
    response = templates.TemplateResponse(request, template, {**context, "view": v, "path": request.url.path, "static": False})
    response.set_cookie("view", v, max_age=365 * 24 * 3600, samesite="lax")
    return response


app = FastAPI(title="Self-healing console")
store = Store(STATE / "incidents.sqlite", int(os.environ.get("MAX_ATTEMPTS", "3")))
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(STATUS_LABELS=STATUS_LABELS, ISSUE_LABELS=ISSUE_LABELS, PO_ISSUE_LABELS=PO_ISSUE_LABELS,
                             PO_GROUP_LABELS=PO_GROUP_LABELS, TABLES_NL=TABLES_NL)
templates.env.filters["nl_date"] = nl_date
templates.env.filters["duration"] = duration
templates.env.filters["money"] = money
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


@app.get("/api/rules")
def list_rules():
    return store.rules()


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
    usage = [fx.get("usage") or {} for fx in all_fixes]
    v = doc.get("validation") or {}
    changes = [{"table": TABLES_NL.get(t, t), "before": before, "after": v["row_counts_after"].get(t, 0)}
               for t, before in v.get("row_counts_before", {}).items()] if v.get("row_counts_after") else []
    closed = doc["status"] in CLOSED
    return {**doc, "types": types, "truth": chaos_truth(doc),
            "models": sorted({fx["model"] for fx in fixes}),
            "cost_usd": round(sum(u.get("cost_usd", 0) for u in usage), 4),
            "tokens": sum(u.get("input_tokens", 0) + u.get("output_tokens", 0) for u in usage),
            # voor de product owner
            "group": PO_GROUPS[doc["status"]],
            "week": nl_date(doc.get("window_start")) if doc.get("window_start") else doc["batch_id"],
            "plain_findings": [plain_finding(f) for f in doc["findings"]],
            "changes": changes,
            "rows_removed": sum(max(c["before"] - c["after"], 0) for c in changes),
            "n_orders": (doc.get("impact") or {}).get("orders") or doc.get("row_counts", {}).get("orders"),
            "revenue": (doc.get("impact") or {}).get("revenue"),
            "reports": (doc.get("impact") or {}).get("waiting_reports", []),
            "revenue_sort": (doc.get("impact") or {}).get("revenue") or 0,
            "minutes_open": minutes_between(doc["created_at"], doc["updated_at"] if closed else now())}


def summary(incidents: list[dict]) -> dict:
    counts = {s: 0 for s in STATUSES}
    groups = {g: 0 for g in PO_GROUP_LABELS}
    for i in incidents:
        counts[i["status"]] += 1
        groups[i["group"]] += 1
    scored = [i["truth"] for i in incidents if i["truth"] and not i["truth"]["organic"]]
    done = [i for i in incidents if i["status"] in CLOSED]
    lead = sorted(i["minutes_open"] for i in done)
    return {"counts": counts, "groups": groups, "total": len(incidents),
            "cost_usd": round(sum(i["cost_usd"] for i in incidents), 4),
            "tokens": sum(i["tokens"] for i in incidents),
            "scored": len(scored),
            "detected_ok": sum(1 for t in scored if t["detected_ok"]),
            "diagnosed_ok": sum(1 for t in scored if t["diagnosed_ok"]),
            # voor de product owner
            "repaired": sum(1 for i in done if i.get("final_action") == "repair"),
            "first_time_right": sum(1 for i in done if i.get("final_action") == "repair" and i["attempt"] == 1),
            "median_lead": lead[len(lead) // 2] if lead else None,
            "weeks_missing": [i["week"] for i in incidents if i["status"] not in CLOSED],
            "revenue_missing": sum(i["revenue"] or 0 for i in incidents if i["status"] not in CLOSED)}


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, status: str = "", issue: str = ""):
    everything = [view(d) for d in store.list()]
    shown = [i for i in everything if (not status or i["status"] == status) and (not issue or issue in i["types"])]
    return page(request, "overview", {"incidents": shown, "all": everything, "summary": summary(everything),
                                      "status": status, "issue": issue})


@app.get("/incidents/{incident_id}", response_class=HTMLResponse)
def detail(request: Request, incident_id: str, error: str = ""):
    doc = guarded(store.get, incident_id)
    return page(request, "detail", {"i": view(doc), "error": error})


@app.post("/incidents/{incident_id}/decision")
def decide_form(incident_id: str, decision: str = Form(...), comment: str = Form(""), decided_by: str = Form(""),
                save_as_rule: str = Form("")):
    payload = {"decision": decision, "comment": comment, "decided_by": decided_by or "console",
               "save_as_rule": bool(save_as_rule) and decision == "approve"}
    check("decision", payload)
    try:
        store.decide(incident_id, payload)
    except NotFound:
        raise HTTPException(404, "incident bestaat niet")
    except Conflict as exc:
        return RedirectResponse(f"/incidents/{incident_id}?error={exc}", status_code=303)
    return RedirectResponse(f"/incidents/{incident_id}", status_code=303)


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    return page(request, "rules", {"rules": store.rules()})


@app.post("/rules/{rule_id}/deactivate")
def deactivate_rule(rule_id: str):
    guarded(store.deactivate_rule, rule_id)
    return RedirectResponse("/rules", status_code=303)


@app.get("/export", response_class=HTMLResponse)
def export(request: Request):
    """Eén losstaand HTML-bestand met alle incidenten en oplossingen, zonder knoppen."""
    everything = [view(d) for d in store.list()]
    html = templates.get_template("report.html").render(
        incidents=everything, summary=summary(everything), static=True, generated_at=now())
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "report.html").write_text(html)
    return HTMLResponse(html, headers={"Content-Disposition": 'inline; filename="report.html"'})
