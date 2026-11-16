"""Het incidentregister: de enige plek waar de status van een incident leeft.

Elke statusovergang is een functie hier; ongeldige overgangen geven een Conflict.
Opslag is één SQLite-bestand dat alleen de console aanraakt.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

OPEN = ("detected", "investigating", "proposed", "validated", "needs_human", "approved")
CLOSED = ("resolved", "discarded")
STATUSES = OPEN + CLOSED
OVERRIDABLE = ("detected", "investigating", "proposed", "validated", "needs_human")


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path, max_attempts: int = 3):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("""create table if not exists incidents (
            id text primary key, batch_id text, status text, created_at text, updated_at text, doc text)""")
        self.lock = threading.Lock()
        self.max_attempts = max_attempts

    # ------------------------------------------------------------ lezen

    def get(self, incident_id: str) -> dict:
        row = self.db.execute("select doc from incidents where id = ?", [incident_id]).fetchone()
        if not row:
            raise NotFound(incident_id)
        return json.loads(row[0])

    def list(self, status: str | None = None) -> list[dict]:
        sql, args = "select doc from incidents", []
        if status:
            sql, args = sql + " where status = ?", [status]
        return [json.loads(r[0]) for r in self.db.execute(sql + " order by created_at desc, id", args)]

    # ------------------------------------------------------------ schrijven

    def _save(self, doc: dict, status: str, note: str) -> dict:
        doc["status"], doc["updated_at"] = status, now()
        doc["timeline"].append({"at": doc["updated_at"], "status": status, "note": note})
        self.db.execute(
            "insert or replace into incidents values (?, ?, ?, ?, ?, ?)",
            [doc["id"], doc["batch_id"], status, doc["created_at"], doc["updated_at"], json.dumps(doc)])
        self.db.commit()
        return doc

    def _expect(self, doc: dict, *allowed: str) -> None:
        if doc["status"] not in allowed:
            raise Conflict(f"incident {doc['id']} heeft status {doc['status']}; verwacht {' of '.join(allowed)}")

    def _retry(self, doc: dict, source: str, text: str) -> dict:
        """Terug naar de healer met feedback, of naar een mens als de pogingen op zijn."""
        doc["feedback"].append({"attempt": doc["attempt"], "source": source, "text": text})
        if doc.get("proposal"):
            doc["previous_attempts"].append({"attempt": doc["attempt"], "proposal": doc.pop("proposal"),
                                             "validation": doc.pop("validation", None)})
        doc.pop("validation", None)
        if doc["attempt"] >= self.max_attempts:
            return self._save(doc, "needs_human", f"{source}: {text} (pogingen op)")
        doc["attempt"] += 1
        return self._save(doc, "detected", f"{source}: {text} (poging {doc['attempt']})")

    def create(self, payload: dict) -> dict:
        with self.lock:
            row = self.db.execute(
                f"select doc from incidents where batch_id = ? and status in {OPEN}", [payload["batch_id"]]).fetchone()
            if row:
                return json.loads(row[0])
            doc = {**payload, "id": "inc-" + uuid.uuid4().hex[:8], "created_at": now(), "attempt": 1,
                   "max_attempts": self.max_attempts, "feedback": [], "previous_attempts": [], "timeline": []}
            return self._save(doc, "detected", f"{len(payload['findings'])} bevindingen in {payload['batch_id']}")

    def claim(self, incident_id: str) -> dict:
        with self.lock:
            doc = self.get(incident_id)
            self._expect(doc, "detected")
            return self._save(doc, "investigating", "healer onderzoekt")

    def fail(self, incident_id: str, detail: str) -> dict:
        with self.lock:
            doc = self.get(incident_id)
            self._expect(doc, "investigating")
            return self._retry(doc, "healer", f"onderzoek mislukt: {detail}")

    def propose(self, incident_id: str, proposal: dict) -> dict:
        with self.lock:
            doc = self.get(incident_id)
            self._expect(doc, "investigating")
            doc["proposal"] = proposal
            return self._save(doc, "proposed", f"voorstel: {proposal['action']}")

    def validate(self, incident_id: str, validation: dict) -> dict:
        with self.lock:
            doc = self.get(incident_id)
            self._expect(doc, "proposed")
            doc["validation"] = validation
            if validation["passed"]:
                return self._save(doc, "validated", "kandidaat doorstaat alle controles")
            remaining = "; ".join(f"{f['issue_type']} op {f['table_name']}.{f.get('column_name') or '*'} "
                                  f"({f['check_name']}, {f['n_affected']} rijen)"
                                  for f in validation["remaining_findings"])
            return self._retry(doc, "validatie", validation.get("error") or f"kandidaat faalt nog: {remaining}")

    def decide(self, incident_id: str, decision: dict) -> dict:
        with self.lock:
            doc = self.get(incident_id)
            kind, comment = decision["decision"], decision.get("comment", "").strip()
            who = decision.get("decided_by") or "onbekend"
            doc["decision"] = {**decision, "at": now()}
            if kind == "approve":
                self._expect(doc, "validated")
                doc["final_action"] = doc["proposal"]["action"]
                return self._save(doc, "approved", f"goedgekeurd door {who}" + (f": {comment}" if comment else ""))
            if kind == "reject":
                self._expect(doc, "validated")
                return self._retry(doc, "mens", comment or "afgekeurd zonder toelichting")
            self._expect(doc, *OVERRIDABLE)
            doc["final_action"] = kind
            label = "weggooien" if kind == "discard" else "origineel vrijgeven"
            return self._save(doc, "approved", f"{label} door {who}" + (f": {comment}" if comment else ""))

    def resolve(self, incident_id: str, resolution: dict) -> dict:
        with self.lock:
            doc = self.get(incident_id)
            self._expect(doc, "approved")
            doc["resolution"] = resolution
            detail = resolution.get("detail", "")
            if resolution["outcome"] == "failed":
                doc["feedback"].append({"attempt": doc["attempt"], "source": "pipeline", "text": detail})
                return self._save(doc, "needs_human", f"vrijgeven mislukt: {detail}")
            status = "discarded" if resolution["outcome"] == "discarded" else "resolved"
            return self._save(doc, status, detail)
