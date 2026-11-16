"""Elke verstoring van chaos moet door het bijbehorende dbt-model worden gevangen."""
import json
import random
import shutil

import pytest

from injectors import VARIANTS, inject

CASES = [(t, v) for t, vs in VARIANTS.items() for v in vs]


def history_of(batches):
    import duckdb
    rows = []
    for b in batches:
        for f in b.glob("*.csv"):
            n = duckdb.connect().execute(f"select count(*) from read_csv('{f}', all_varchar=true)").fetchone()[0]
            rows.append((b.name, f.stem, n))
    return rows


def test_clean_batches_have_no_findings(env, batches):
    from sh_pipeline import gate
    for b in batches[:2]:
        findings, counts = gate.run_gate(b, json.loads((b / "_manifest.json").read_text()), [])
        assert findings == []
        assert counts["orders"] > 100


@pytest.mark.parametrize("issue_type,variant", CASES)
def test_injector_is_detected_as_its_own_type(env, batches, issue_type, variant):
    from sh_pipeline import gate
    target = env / "landing" / batches[5].name
    shutil.copytree(batches[5], target)
    done = inject(target, issue_type, random.Random(1), variant)
    assert done["variant"] == variant

    manifest = json.loads((target / "_manifest.json").read_text())
    findings, _ = gate.run_gate(target, manifest, history_of(batches[:5]))
    found = {f["issue_type"] for f in findings}
    assert issue_type in found, findings
    # Een verstoring mag hoogstens één ander type als bijwerking opleveren.
    assert len(found - {issue_type}) <= 1, found
