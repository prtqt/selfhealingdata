"""healer: haalt gedetecteerde incidenten op bij de console en levert een voorstel in.

Kent alleen de console-API, de quarantainebundel en zijn eigen kandidatenmap.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests
import yaml

import agents
import llm
from workspace import Workspace

CONSOLE_URL = os.environ.get("CONSOLE_URL", "http://console:8000").rstrip("/")
QUARANTINE = Path(os.environ.get("QUARANTINE_DIR", "/quarantine"))
CANDIDATES = Path(os.environ.get("CANDIDATES_DIR", "/candidates"))
POLL = int(os.environ.get("HEALER_POLL_SECONDS", "10"))
ROUTING = Path(os.environ.get("ROUTING_PATH", Path(__file__).parent / "routing.yml"))

# Schema eerst rechtzetten: de andere controles gaan uit van de kolommen van het contract.
ORDER = ["schema_drift", "volume_anomaly", "duplicates", "null_spike", "invalid_values", "referential_integrity"]


def log(msg: str) -> None:
    print(f"[healer] {msg}", flush=True)


def load_routing(path: Path = ROUTING) -> dict[str, llm.Route]:
    cfg = yaml.safe_load(path.read_text())
    prices = cfg["prices_per_mtok"]
    return {issue: llm.Route(model=r["model"], effort=r.get("effort", "medium"), max_turns=r.get("max_turns", 15),
                             fallbacks=r.get("fallbacks", False), price_per_mtok=tuple(prices[r["model"]]))
            for issue, r in cfg["routes"].items()}


def heal(client, routing: dict[str, llm.Route], incident: dict) -> dict:
    """Maakt het voorstel voor één incident en schrijft de kandidaat-batch."""
    ws = Workspace(QUARANTINE / Path(incident["quarantine_path"]).name)
    by_type: dict[str, list[dict]] = {}
    for f in incident["findings"]:
        by_type.setdefault(f["issue_type"], []).append(f)
    fixes, done = [], []
    if rule := incident.get("rule"):
        if proposal := apply_rule(ws, rule, incident, sorted(by_type, key=ORDER.index)[0]):
            return proposal
        ws = Workspace(QUARANTINE / Path(incident["quarantine_path"]).name)  # regel paste niet: schone werkkopie voor de agents
    for issue_type in sorted(by_type, key=ORDER.index):
        log(f"{incident['id']}: {issue_type} -> {routing[issue_type].model}")
        fix = agents.heal_issue(client, routing[issue_type], ws, issue_type, by_type[issue_type],
                                incident["findings"], incident.get("feedback", []), done,
                                incident.get("lessons"))
        fixes.append(fix)
        if fix["action"] == "discard":
            return {"action": "discard", "candidate_path": None, "fixes": fixes,
                    "summary": f"Niet te herstellen ({issue_type}): {fix['rationale']}"}
        done += fix["recipe_sql"]
    name = f"{incident['id']}-a{incident['attempt']}"
    ws.export(CANDIDATES / name)
    (CANDIDATES / name / "recipe.sql").write_text(";\n\n".join(done) + ";\n")
    regels = "1 recept-regel" if len(done) == 1 else f"{len(done)} recept-regels"
    return {"action": "repair", "candidate_path": name, "fixes": fixes,
            "summary": f"Herstelde batch klaar voor controle: {regels} uitgevoerd op de werkkopie."}


def apply_rule(ws: Workspace, rule: dict, incident: dict, issue_type: str) -> dict | None:
    """Past een eerder goedgekeurd recept toe zonder Claude. Lukt dat niet, dan nemen de agents het over."""
    try:
        ws.run_recipe(rule["recipe_sql"], commit=True)
    except Exception as exc:
        log(f"{incident['id']}: {rule['id']} niet toepasbaar ({exc}); agents nemen het over")
        return None
    name = f"{incident['id']}-a{incident['attempt']}"
    ws.export(CANDIDATES / name)
    (CANDIDATES / name / "recipe.sql").write_text(";\n\n".join(rule["recipe_sql"]) + ";\n")
    log(f"{incident['id']}: {rule['id']} toegepast")
    return {"action": "repair", "candidate_path": name, "rule_id": rule["id"],
            "summary": f"Bekende afwijking: recept van {rule['id']} toegepast, zonder agent.",
            "fixes": [{"issue_type": issue_type, "model": rule["id"], "confidence": "high", "action": "repair",
                       "root_cause": f"Dezelfde afwijking als bij {rule['created_from']}, waarvoor {rule['created_by']} een blijvende regel heeft goedgekeurd.",
                       "evidence": ["De bevindingen komen exact overeen met de signatuur van de regel."],
                       "recipe_sql": rule["recipe_sql"], "rationale": "Eerder goedgekeurd recept opnieuw toegepast.",
                       "business_summary": rule.get("summary", ""),
                       "usage": {"input_tokens": 0, "output_tokens": 0, "turns": 0, "cost_usd": 0.0}}]}


def api(method: str, path: str, **kwargs):
    token = os.environ.get("CONSOLE_API_TOKEN")  # alleen nodig als de console login aan heeft staan
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.request(method, f"{CONSOLE_URL}/api/incidents{path}", timeout=15, headers=headers, **kwargs)
    r.raise_for_status()
    return r.json()


def tick(client, routing) -> int:
    handled = 0
    for incident in reversed(api("get", "", params={"status": "detected"})):  # oudste eerst
        try:
            incident = api("post", f"/{incident['id']}/claim")
        except requests.HTTPError:
            continue  # iemand anders was eerder, of een mens greep in
        try:
            proposal = heal(client, routing, incident)
            api("post", f"/{incident['id']}/proposal", json=proposal)
            log(f"{incident['id']}: voorstel {proposal['action']}")
        except requests.HTTPError as exc:
            log(f"{incident['id']}: voorstel niet aangenomen ({exc}); mogelijk greep een mens in")
        except Exception as exc:
            log(f"{incident['id']}: mislukt: {exc}")
            try:
                api("post", f"/{incident['id']}/failure", json={"detail": f"{type(exc).__name__}: {exc}"})
            except requests.RequestException:
                pass
        handled += 1
    return handled


def main() -> None:
    import anthropic

    routing = load_routing()
    client = None
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        client = anthropic.Anthropic()
    else:
        log("geen ANTHROPIC_API_KEY gevonden; zet die in .env. De healer wacht en doet niets.")
    log("routing: " + ", ".join(f"{k}={v.model}" for k, v in routing.items()))
    while True:
        if client is not None:
            try:
                tick(client, routing)
            except requests.RequestException as exc:
                log(f"console niet bereikbaar: {exc}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
