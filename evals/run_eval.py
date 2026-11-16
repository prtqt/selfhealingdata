"""Evaluatieharnas: hoe goed herstellen de agents batches waarvan we de waarheid kennen?

Voor elk geval (verstoring x seed) draait de echte keten zonder console:
  schone batch -> injector van chaos -> poort van de pipeline -> healer -> poort op de kandidaat
en wordt de kandidaat vergeleken met de schone batch.

  python evals/run_eval.py                         alle varianten, 1 seed, modellen uit healer/routing.yml
  python evals/run_eval.py --types duplicates schema_drift --seeds 3
  python evals/run_eval.py --routing mijn_routing.yml --label alles-op-haiku
  python evals/run_eval.py --baseline              zonder Claude: vaste regels, als ondergrens en om het harnas te testen

Dit is een integratiehulpmiddel en mag daarom, net als tests/, meerdere componenten importeren.
"""
from __future__ import annotations

import argparse
import csv
import importlib
import json
import os
import random
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for sub in ("source", "chaos", "pipeline", "healer"):
    sys.path.insert(0, str(ROOT / sub))


class BaselineClient:
    """Ondergrens zonder Claude: exacte duplicaten verwijderen, al het andere weggooien."""

    def __init__(self):
        self.n = 0
        self.messages = SimpleNamespace(create=self.create)
        self.beta = SimpleNamespace(messages=self.messages)

    def create(self, **kwargs):
        self.n += 1
        system = kwargs["system"]
        if "submit_diagnosis" in {t["name"] for t in kwargs["tools"]}:
            name, inp = "submit_diagnosis", {"root_cause": "vaste regel, geen onderzoek", "evidence": [],
                                             "confidence": "low", "repair_advice": "n.v.t."}
        elif "sleutels die meer dan één keer" in system:
            tables = sorted(set(__import__("re").findall(r"work\.(\w+): \d+ rijen", kwargs["messages"][0]["content"])))
            name, inp = "submit_fix", {"action": "repair", "rationale": "exacte kopieën verwijderen", "business_summary": "",
                                       "recipe_sql": [f"create or replace table work.{t} as select distinct * from work.{t}" for t in tables]}
        else:
            name, inp = "submit_fix", {"action": "discard", "recipe_sql": [], "rationale": "vaste regel: niet herstellen",
                                       "business_summary": ""}
        block = SimpleNamespace(type="tool_use", id=f"b{self.n}", name=name, input=inp)
        return SimpleNamespace(content=[block], stop_reason="tool_use", usage=SimpleNamespace(input_tokens=0, output_tokens=0))


def rows(path: Path) -> Counter:
    if not path.exists():
        return Counter()
    with open(path, newline="") as f:
        return Counter(tuple(r) for r in list(csv.reader(f))[1:])


def compare(clean: Path, candidate: Path, tables: list[str]) -> dict:
    """Aandeel schone rijen dat in de kandidaat terugkomt, en aandeel kandidaatrijen dat niet in de schone batch zat."""
    want = have = kept = 0
    for t in tables:
        a, b = rows(clean / f"{t}.csv"), rows(candidate / f"{t}.csv")
        want, have, kept = want + sum(a.values()), have + sum(b.values()), kept + sum((a & b).values())
    return {"recovered": round(kept / want, 4) if want else 0.0, "spurious": round((have - kept) / have, 4) if have else 0.0}


def prepare(workdir: Path, n_batches: int = 6) -> list[Path]:
    os.environ.update(CONTRACT_PATH=str(ROOT / "contracts/landing_schema.yml"), SOURCE_WORK_DIR=str(workdir / "src"),
                      OUT_DIR=str(workdir / "clean"), DATASET="synthetic", MAX_BATCHES=str(n_batches),
                      DATA_DIR=str(workdir / "data"), QUARANTINE_DIR=str(workdir / "data/quarantine"),
                      CANDIDATES_DIR=str(workdir / "data/candidates"), CONSOLE_URL="http://127.0.0.1:9")
    src = importlib.import_module("source")
    src.bootstrap()
    while src.deliver_next():
        pass
    return sorted((workdir / "clean").iterdir())


def run_case(case_id: str, issue_type: str, variant: str, seed: int, batches: list[Path], workdir: Path,
             client, routing) -> dict:
    from injectors import inject
    from sh_pipeline import flow, gate
    import worker

    clean = batches[-1]
    broken = workdir / "cases" / case_id / clean.name
    shutil.rmtree(broken.parent, ignore_errors=True)
    shutil.copytree(clean, broken)
    truth = inject(broken, issue_type, random.Random(seed), variant)
    manifest = json.loads((broken / "_manifest.json").read_text())
    history = [(b.name, f.stem, sum(rows(f).values())) for b in batches[:-1] for f in b.glob("*.csv")]
    tables = list(gate.load_contract())

    findings, row_counts = gate.run_gate(broken, manifest, history)
    result = {"case": case_id, "issue_type": issue_type, "variant": variant, "seed": seed,
              "detected": issue_type in {f["issue_type"] for f in findings}, "found_types": sorted({f["issue_type"] for f in findings}),
              "action": None, "passed_gate": False, "recovered": 0.0, "spurious": 0.0, "cost_usd": 0.0, "tokens": 0,
              "seconds": 0.0, "models": [], "error": None, "recoverable_ceiling": compare(clean, broken, tables)["recovered"]}
    if not findings:
        return result

    manifest_q = {**manifest, "batch_id": case_id}
    flow._quarantine(broken, manifest_q, findings, history)
    incident = {"id": case_id, "attempt": 1, "quarantine_path": case_id, "findings": findings, "feedback": []}
    started = time.time()
    try:
        proposal = worker.heal(client, routing, incident)
    except Exception as exc:
        result.update(error=f"{type(exc).__name__}: {exc}"[:300], seconds=round(time.time() - started, 1))
        return result
    usage = [f.get("usage") or {} for f in proposal["fixes"]]
    result.update(action=proposal["action"], seconds=round(time.time() - started, 1),
                  models=sorted({f["model"] for f in proposal["fixes"]}),
                  cost_usd=round(sum(u.get("cost_usd", 0) for u in usage), 4),
                  tokens=sum(u.get("input_tokens", 0) + u.get("output_tokens", 0) for u in usage),
                  root_causes=[f["root_cause"] for f in proposal["fixes"]])
    if proposal["action"] == "repair":
        candidate = Path(os.environ["CANDIDATES_DIR"]) / proposal["candidate_path"]
        remaining, _ = gate.run_gate(candidate, manifest, history)
        result.update(passed_gate=not remaining, remaining=sorted({f["check_name"] for f in remaining}),
                      **compare(clean, candidate, tables))
    return result


def summarize(results: list[dict]) -> list[dict]:
    out = []
    for t in sorted({r["issue_type"] for r in results}):
        rs = [r for r in results if r["issue_type"] == t]
        n = len(rs)
        healed = [r for r in rs if r["passed_gate"]]
        out.append({"issue_type": t, "cases": n, "detected": sum(r["detected"] for r in rs),
                    "healed": len(healed), "discarded": sum(r["action"] == "discard" for r in rs),
                    "failed": sum(1 for r in rs if r["action"] == "repair" and not r["passed_gate"] or r["error"]),
                    "avg_recovered": round(sum(r["recovered"] for r in healed) / len(healed), 3) if healed else None,
                    "cost_usd": round(sum(r["cost_usd"] for r in rs), 4),
                    "models": sorted({m for r in rs for m in r["models"]})})
    return out


def as_markdown(label: str, summary: list[dict], results: list[dict]) -> str:
    lines = [f"# Evaluatie: {label}", "",
             f"{len(results)} gevallen · ${sum(r['cost_usd'] for r in results):.4f} · {sum(r['seconds'] for r in results):.0f}s", "",
             "| Soort fout | Model | Gevallen | Gedetecteerd | Hersteld | Weggegooid | Mislukt | Schone rijen terug | Kosten |",
             "|---|---|---|---|---|---|---|---|---|"]
    for s in summary:
        rec = f"{s['avg_recovered']:.1%}" if s["avg_recovered"] is not None else "–"
        lines.append(f"| {s['issue_type']} | {', '.join(s['models']) or '–'} | {s['cases']} | {s['detected']} | {s['healed']} | "
                     f"{s['discarded']} | {s['failed']} | {rec} | ${s['cost_usd']:.4f} |")
    lines += ["", "Hersteld = de kandidaat doorstaat de poort. Schone rijen terug = aandeel rijen van de onverstoorde batch "
              "dat in herstelde kandidaten terugkomt.", "", "## Per geval", "",
              "| Geval | Uitkomst | Schone rijen terug | Maximaal haalbaar zonder herstel | Vreemde rijen | Kosten |", "|---|---|---|---|---|---|"]
    for r in results:
        outcome = ("fout: " + r["error"]) if r["error"] else "niet gedetecteerd" if not r["detected"] else \
            "weggegooid" if r["action"] == "discard" else "hersteld" if r["passed_gate"] else \
            "faalt poort: " + ", ".join(r.get("remaining", []))
        lines.append(f"| {r['case']} | {outcome} | {r['recovered']:.1%} | {r['recoverable_ceiling']:.1%} | {r['spurious']:.1%} | ${r['cost_usd']:.4f} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> dict:
    saved = dict(os.environ)   # het harnas zet paden in de omgeving; na afloop is alles weer zoals het was
    try:
        return _main(argv)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--types", nargs="*", help="alleen deze soorten fouten")
    ap.add_argument("--seeds", type=int, default=1, help="aantal seeds per variant")
    ap.add_argument("--routing", default=str(ROOT / "healer/routing.yml"))
    ap.add_argument("--label", default=None)
    ap.add_argument("--baseline", action="store_true", help="zonder Claude, met vaste regels")
    ap.add_argument("--out", default=str(ROOT / "evals/results"))
    args = ap.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="selfhealing-eval-"))
    batches = prepare(workdir)
    from injectors import VARIANTS
    import worker
    routing = worker.load_routing(Path(args.routing))
    if args.baseline:
        client, label = BaselineClient(), args.label or "baseline"
    else:
        import anthropic
        client, label = anthropic.Anthropic(), args.label or Path(args.routing).stem

    results = []
    for issue_type, variants in VARIANTS.items():
        if args.types and issue_type not in args.types:
            continue
        for variant in variants:
            for seed in range(1, args.seeds + 1):
                case_id = f"{issue_type}-{variant}-s{seed}"
                r = run_case(case_id, issue_type, variant, seed, batches, workdir, client, routing)
                results.append(r)
                print(f"[eval] {case_id}: {'hersteld' if r['passed_gate'] else r['action'] or r['error'] or 'niet gedetecteerd'} "
                      f"({r['recovered']:.0%} terug, ${r['cost_usd']:.4f})", flush=True)
    summary = summarize(results)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out / f"{stamp}-{label}.json").write_text(json.dumps({"label": label, "summary": summary, "results": results}, indent=2, ensure_ascii=False))
    (out / f"{stamp}-{label}.md").write_text(as_markdown(label, summary, results))
    print(f"[eval] rapport: {out / f'{stamp}-{label}.md'}")
    shutil.rmtree(workdir, ignore_errors=True)
    return {"label": label, "summary": summary, "results": results}


if __name__ == "__main__":
    main()
