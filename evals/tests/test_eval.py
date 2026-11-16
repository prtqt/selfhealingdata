import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evals"))


def test_baseline_run_scores_against_the_clean_batch(tmp_path):
    for name in [m for m in sys.modules if m.startswith("sh_pipeline") or m in ("source", "worker")]:
        del sys.modules[name]
    import run_eval
    report = run_eval.main(["--baseline", "--types", "duplicates", "volume_anomaly", "--out", str(tmp_path)])
    by_case = {r["case"]: r for r in report["results"]}

    dup = by_case["duplicates-duplicate_rows-s1"]
    assert dup["detected"] and dup["passed_gate"] and dup["action"] == "repair"
    assert dup["recovered"] == 1.0 and dup["spurious"] == 0.0     # exact de schone batch terug

    gone = by_case["volume_anomaly-missing_table-s1"]
    assert gone["detected"] and gone["action"] == "discard" and gone["recovered"] == 0.0
    assert 0 < gone["recoverable_ceiling"] < 1

    summary = {s["issue_type"]: s for s in report["summary"]}
    assert summary["duplicates"]["healed"] == 1 and summary["volume_anomaly"]["discarded"] == 2
    md = next(tmp_path.glob("*-baseline.md")).read_text()
    assert "| duplicates |" in md and "weggegooid" in md
