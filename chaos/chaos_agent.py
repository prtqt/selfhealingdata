"""Kiest welke verstoring een batch krijgt: willekeurig, of door Claude (CHAOS_MODE=llm)."""
from __future__ import annotations

import os
import random

from injectors import VARIANTS

MODEL = os.environ.get("CHAOS_MODEL", "claude-haiku-5-5")

SYSTEM = """Je bent de chaos-agent in een testopstelling voor een zelfherstellende data pipeline.
Je taak is realistische datafouten kiezen om het herstel te testen, zoals ze bij een echt bronsysteem
voorkomen: een mislukte export, een release die een kolom hernoemt, een job die twee keer draait.
Kies per batch één verstoring, of twee als ze samen een geloofwaardig incident vormen.
Wissel af met eerdere keuzes zodat alle soorten fouten aan bod komen.
Roep voor elke verstoring het hulpmiddel inject aan en geef een korte reden."""

TOOL = {
    "name": "inject",
    "description": "Brengt één verstoring aan in de batch die nu wordt afgeleverd.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "issue_type": {"type": "string", "enum": sorted(VARIANTS)},
            "variant": {"type": "string", "enum": sorted(v for vs in VARIANTS.values() for v in vs),
                        "description": "Moet bij issue_type horen."},
            "fraction": {"type": "number", "description": "Aandeel getroffen rijen, tussen 0.03 en 0.5."},
            "reason": {"type": "string"},
        },
        "required": ["issue_type", "variant", "fraction", "reason"],
        "additionalProperties": False,
    },
}


def choose_random(rng: random.Random, allowed: list[str]) -> list[dict]:
    return [{"issue_type": rng.choice(allowed)}]


def choose_llm(batch_summary: str, recent: list[str], allowed: list[str], client=None) -> list[dict]:
    import anthropic

    client = client or anthropic.Anthropic()
    catalog = "\n".join(f"- {t}: {', '.join(sorted(VARIANTS[t]))}" for t in allowed)
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        tools=[TOOL],
        output_config={"effort": "low"},
        messages=[{"role": "user", "content":
                   f"Batch:\n{batch_summary}\n\nBeschikbare verstoringen (issue_type: varianten):\n{catalog}\n\n"
                   f"Eerder gekozen, nieuwste laatst: {', '.join(recent) or 'nog niets'}\n\n"
                   "Kies de verstoring voor deze batch met inject."}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model weigerde het verzoek")
    picks = []
    for block in response.content:
        if block.type == "tool_use" and block.name == "inject":
            p = dict(block.input)
            if p.get("issue_type") in allowed and p.get("variant") in VARIANTS[p["issue_type"]]:
                picks.append(p)
    if not picks:
        raise RuntimeError("model koos geen geldige verstoring")
    return picks[:2]
