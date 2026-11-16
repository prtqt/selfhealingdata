"""Dunne laag om de Claude API: één aanroep, en het bijhouden van verbruik en kosten."""
from __future__ import annotations

from dataclasses import dataclass, field

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class Route:
    model: str
    effort: str = "medium"
    max_turns: int = 15
    fallbacks: bool = False
    price_per_mtok: tuple[float, float] = (0.0, 0.0)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    cost_usd: float = field(default=0.0)

    def add(self, response, route: Route) -> None:
        self.turns += 1
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        self.cost_usd = round(self.input_tokens / 1e6 * route.price_per_mtok[0]
                              + self.output_tokens / 1e6 * route.price_per_mtok[1], 6)

    def as_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "turns": self.turns, "cost_usd": self.cost_usd}


def create(client, route: Route, system: str, tools: list[dict], messages: list[dict]):
    kwargs = dict(model=route.model, max_tokens=16000, system=system, tools=tools, messages=messages,
                  output_config={"effort": route.effort})
    if route.fallbacks:
        # Bij een weigering door een veiligheidsfilter neemt een ander model het verzoek over.
        return client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
    return client.messages.create(**kwargs)
