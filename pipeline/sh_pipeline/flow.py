"""De drie stappen van de pipeline. De Airflow-DAGs roepen alleen deze functies aan."""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import requests

from . import config, console_client, gate, warehouse

log = logging.getLogger("pipeline")


def _manifest(batch_dir: Path) -> dict:
    return json.loads((batch_dir / "_manifest.json").read_text())


def _quarantine(batch_dir: Path, manifest: dict, findings: list[dict], history: list[tuple]) -> None:
    """Schrijft de quarantainebundel atomair (zie contracts/README.md)."""
    target = config.QUARANTINE_DIR / manifest["batch_id"]
    tmp = config.QUARANTINE_DIR / f".tmp-{manifest['batch_id']}"
    config.QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(batch_dir, tmp)
    (tmp / "findings.json").write_text(json.dumps(findings, indent=2))
    (tmp / "history_stats.csv").write_text(
        "batch_id,table_name,row_count\n" + "".join(f"{b},{t},{n}\n" for b, t, n in history))
    shutil.copy(config.CONTRACT_PATH, tmp / "landing_schema.yml")
    shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)


def _report(con, manifest: dict, findings: list[dict], row_counts: dict, impact: dict | None = None,
            partial: dict | None = None, status: str = "quarantined") -> None:
    """Meldt het incident. Lukt dat niet, dan blijft de batch in quarantaine en volgt een nieuwe poging."""
    try:
        incident_id = console_client.report_incident({
            "batch_id": manifest["batch_id"], "quarantine_path": manifest["batch_id"],
            "window_start": manifest["window_start"], "window_end": manifest["window_end"],
            "row_counts": row_counts, "findings": findings,
            **({"impact": impact} if impact else {}), **({"partial": partial} if partial else {}),
        })
        warehouse.set_status(con, manifest, status, incident_id)
    except requests.RequestException as exc:
        log.warning("console niet bereikbaar, incident voor %s wordt later gemeld: %s", manifest["batch_id"], exc)


ROW_LEVEL = {"null_spike", "duplicates", "invalid_values", "referential_integrity"}


def _partial_release(batch_dir: Path, manifest: dict, findings: list[dict], bad_orders: list[str],
                     n_orders: int, history: list[tuple]) -> dict | None:
    """Laadt de bestellingen zonder bevinding alvast in, als dat deel zelf de poort haalt.

    Geeft {released_orders, held_orders} of None als de batch in zijn geheel moet wachten.
    """
    if not config.PARTIAL_RELEASE or not bad_orders or not n_orders:
        return None
    if {f["issue_type"] for f in findings} - ROW_LEVEL or len(bad_orders) / n_orders > config.PARTIAL_MAX_SHARE:
        return None
    subset = config.STATE_DIR / "partial" / manifest["batch_id"]
    shutil.rmtree(subset, ignore_errors=True)
    try:
        released = gate.write_clean_subset(batch_dir, subset, bad_orders)
        remaining, _ = gate.run_gate(subset, manifest, history)
        if remaining or not released:
            return None
        with warehouse.connect() as con:
            warehouse.load_batch(con, subset, manifest, "partial")
        return {"released_orders": released, "held_orders": len(bad_orders)}
    except Exception as exc:  # deelvrijgave is een extra; bij twijfel wacht de hele batch
        log.warning("deelvrijgave van %s overgeslagen: %s", manifest["batch_id"], exc)
        return None
    finally:
        shutil.rmtree(subset, ignore_errors=True)


def ingest_landing() -> dict:
    """Beoordeelt elke nieuwe batch in landing/: goed naar de marts, fout naar quarantaine."""
    result = {"clean": [], "quarantined": []}
    rebuild = False
    with warehouse.pipeline_lock():
        with warehouse.connect() as con:
            known = warehouse.known_batches(con)
        batches = sorted(p for p in config.LANDING_DIR.glob("*") if p.is_dir() and not p.name.startswith("."))

        # Eerder in quarantaine gezet terwijl de console onbereikbaar was: alsnog melden.
        for batch_id, info in known.items():
            qdir = config.QUARANTINE_DIR / batch_id
            if info["status"] in ("quarantined", "partial") and not info["incident_id"] and qdir.exists():
                findings = json.loads((qdir / "findings.json").read_text())
                with warehouse.connect() as con:
                    _report(con, _manifest(qdir), findings, {}, gate.batch_impact(qdir, findings), status=info["status"])

        for batch_dir in batches:
            manifest = _manifest(batch_dir)
            batch_id = manifest["batch_id"]
            if batch_id in known:
                continue
            with warehouse.connect() as con:
                history = warehouse.history(con, batch_id)
            findings, row_counts, bad_orders = gate.run_gate(batch_dir, manifest, history, with_bad_orders=True)
            partial = None
            if findings:
                partial = _partial_release(batch_dir, manifest, findings, bad_orders, row_counts.get("orders", 0), history)
                rebuild = rebuild or bool(partial)
            with warehouse.connect() as con:
                if findings:
                    status = "partial" if partial else "quarantined"
                    _quarantine(batch_dir, manifest, findings, history)
                    warehouse.set_status(con, manifest, status)
                    _report(con, manifest, findings, row_counts, gate.batch_impact(batch_dir, findings), partial, status)
                    result["quarantined"].append(batch_id)
                else:
                    warehouse.load_batch(con, batch_dir, manifest, "clean")
                    result["clean"].append(batch_id)
            log.info("%s: %s", batch_id, "quarantaine" if findings else "schoon")
        if result["clean"] or rebuild:
            warehouse.build_marts()
    return result


def _fetch(status: str) -> list[dict]:
    try:
        return console_client.incidents(status)
    except requests.RequestException as exc:
        log.warning("console niet bereikbaar: %s", exc)
        return []


def _candidate_dir(incident: dict) -> Path:
    # Alleen de mapnaam telt; zo kan een voorstel nooit buiten candidates/ wijzen.
    return config.CANDIDATES_DIR / Path(incident["proposal"]["candidate_path"]).name


def _check_candidate(incident: dict) -> tuple[list[dict], dict]:
    qdir = config.QUARANTINE_DIR / incident["batch_id"]
    manifest = _manifest(qdir)
    with warehouse.connect() as con:
        history = warehouse.history(con, manifest["batch_id"])
    return gate.run_gate(_candidate_dir(incident), manifest, history)


def validate_candidates() -> list[str]:
    """Draait de poort op elk voorstel van de healer en meldt de uitslag."""
    done = []
    with warehouse.pipeline_lock():
        for incident in _fetch("proposed"):
            before = incident["row_counts"]
            if incident["proposal"]["action"] == "discard":
                payload = {"passed": True, "remaining_findings": [], "row_counts_before": before, "row_counts_after": {}}
            else:
                try:
                    findings, after = _check_candidate(incident)
                    payload = {"passed": not findings, "remaining_findings": findings,
                               "row_counts_before": before, "row_counts_after": after}
                except Exception as exc:  # kapotte kandidaat is een afgekeurde kandidaat
                    payload = {"passed": False, "remaining_findings": [], "row_counts_before": before,
                               "row_counts_after": {}, "error": str(exc)[:2000]}
            console_client.post(incident["id"], "validation", payload)
            done.append(incident["id"])
    return done


def release_approved() -> list[str]:
    """Voert goedgekeurde besluiten uit: kandidaat inlezen, batch weggooien of origineel vrijgeven."""
    done, rebuild = [], False
    with warehouse.pipeline_lock():
        for incident in _fetch("approved"):
            qdir = config.QUARANTINE_DIR / incident["batch_id"]
            manifest = _manifest(qdir)
            action = incident["final_action"]
            try:
                if action == "discard":
                    with warehouse.connect() as con:
                        # Was een deel al vrijgegeven, dan blijft dat staan; alleen de vastgehouden bestellingen vervallen.
                        kept = warehouse.known_batches(con).get(manifest["batch_id"], {}).get("status") == "partial"
                        warehouse.set_status(con, manifest, "partial_kept" if kept else "discarded")
                    outcome = {"outcome": "discarded", "detail": "vastgehouden bestellingen vervallen; de rest stond al in de marts"
                               if kept else "batch niet ingelezen"}
                elif action == "release_original":
                    with warehouse.connect() as con:
                        counts = warehouse.load_batch(con, qdir, manifest, "released_unchecked")
                    outcome, rebuild = {"outcome": "released", "detail": f"origineel ingelezen zonder controle: {counts}"}, True
                else:
                    findings, _ = _check_candidate(incident)  # de poort beslist, ook na goedkeuring
                    if findings:
                        outcome = {"outcome": "failed", "detail": f"kandidaat faalt de controles: {len(findings)} bevindingen"}
                    else:
                        with warehouse.connect() as con:
                            counts = warehouse.load_batch(con, _candidate_dir(incident), manifest, "healed")
                        outcome, rebuild = {"outcome": "released", "detail": f"herstelde batch ingelezen: {counts}"}, True
            except Exception as exc:
                outcome = {"outcome": "failed", "detail": str(exc)[:2000]}
            console_client.post(incident["id"], "resolution", outcome)
            done.append(incident["id"])
        if rebuild:
            warehouse.build_marts()
    return done
