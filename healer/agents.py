"""De twee agents per bevinding: de investigator stelt de diagnose, de fixer schrijft het recept."""
from __future__ import annotations

import json
from pathlib import Path

import llm
from workspace import RecipeError, Workspace

PROMPTS = Path(__file__).parent / "prompts"


class AgentError(Exception):
    pass


def tool(name: str, description: str, properties: dict) -> dict:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties,
                             "required": list(properties), "additionalProperties": False}}


STR = {"type": "string"}
STR_LIST = {"type": "array", "items": {"type": "string"}}

RUN_SQL = tool("run_sql", "Voert één SELECT uit op de werkruimte (schema's original en work) en geeft de eerste "
               "40 rijen terug. Gebruik aggregaties om grote tabellen samen te vatten.", {"sql": STR})
SUBMIT_DIAGNOSIS = tool("submit_diagnosis", "Levert de diagnose in. Roep dit aan zodra de oorzaak vaststaat.", {
    "root_cause": {**STR, "description": "Wat er in het bronsysteem of de aanlevering is misgegaan, in één of twee zinnen."},
    "evidence": {**STR_LIST, "description": "De waarnemingen uit je queries waarop de diagnose rust, met getallen."},
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    "repair_advice": {**STR, "description": "Of en hoe de batch te herstellen is zonder gegevens te verzinnen, "
                      "en wat daarbij verloren gaat."},
})
TEST_RECIPE = tool("test_recipe", "Voert het recept uit als proef en draait het daarna terug. Geeft de rijaantallen "
                   "voor en na en de resterende afwijkingen van het contract.", {"recipe_sql": STR_LIST})
SUBMIT_FIX = tool("submit_fix", "Levert het definitieve voorstel in.", {
    "action": {"type": "string", "enum": ["repair", "discard"],
               "description": "repair: het recept herstelt de batch. discard: de batch is niet te redden en moet opnieuw worden aangeleverd."},
    "recipe_sql": {**STR_LIST, "description": "Eén statement per element, alleen op tabellen in work. Leeg bij discard."},
    "rationale": {**STR, "description": "Waarom deze aanpak, en wat de mens die goedkeurt moet weten (bijvoorbeeld hoeveel rijen verdwijnen)."},
})


def run_agent(client, route: llm.Route, system: str, user: str, tools: list[dict], handlers: dict,
              final_tool: str, accept, usage: llm.Usage) -> dict:
    """Agent-lus: het model roept hulpmiddelen aan tot het final_tool gebruikt en accept() dat goedkeurt."""
    messages = [{"role": "user", "content": user}]
    for turn in range(route.max_turns):
        response = llm.create(client, route, system, tools, messages)
        usage.add(response, route)
        if response.stop_reason == "refusal":
            raise AgentError("het model weigerde het verzoek")
        if response.stop_reason == "max_tokens":
            raise AgentError("antwoord afgekapt op max_tokens")
        messages.append({"role": "assistant", "content": response.content})
        calls = [b for b in response.content if b.type == "tool_use"]
        if not calls:
            messages.append({"role": "user", "content": f"Rond af door {final_tool} aan te roepen."})
            continue
        results, final = [], None
        for call in calls:
            try:
                if call.name == final_tool:
                    accept(call.input)
                    final, content = call.input, "ontvangen"
                else:
                    content = handlers[call.name](call.input)
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": content})
            except Exception as exc:  # fout gaat terug naar het model, dat kan bijsturen
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": f"Fout: {exc}", "is_error": True})
        if final is not None:
            return final
        if turn == route.max_turns - 2:
            results.append({"type": "text", "text": f"Dit is je laatste stap: roep nu {final_tool} aan."})
        messages.append({"role": "user", "content": results})
    raise AgentError(f"geen resultaat binnen {route.max_turns} stappen")


def _context(ws: Workspace, issue_type: str, findings: list[dict], all_findings: list[dict],
             feedback: list[dict], done: list[str]) -> str:
    others = sorted({f["issue_type"] for f in all_findings} - {issue_type})
    parts = [
        f"## Bevindingen van de pipeline voor {issue_type}\n{json.dumps(findings, indent=2, ensure_ascii=False)}",
        f"## Contract en werkkopie\n{ws.describe()}",
        "## Historie\nDe tabel original.history_stats bevat de rijaantallen per tabel van eerdere goede batches.",
    ]
    if others:
        parts.append("## Andere bevindingen in deze batch\nWorden door collega-agents behandeld: " + ", ".join(others)
                     + ". Beperk je tot je eigen soort, maar houd er rekening mee.")
    if done:
        parts.append("## Al uitgevoerd op work\nEerdere recepten in deze sessie:\n" + "\n".join(done))
    if feedback:
        parts.append("## Feedback op eerdere pogingen\nEen eerder voorstel is afgewezen. Verwerk dit:\n"
                     + "\n".join(f"- poging {f['attempt']} ({f['source']}): {f['text']}" for f in feedback))
    return "\n\n".join(parts)


def heal_issue(client, route: llm.Route, ws: Workspace, issue_type: str, findings: list[dict],
               all_findings: list[dict], feedback: list[dict], done: list[str]) -> dict:
    """Diagnose en recept voor één soort bevinding. Past het recept bij 'repair' toe op de werkkopie."""
    usage = llm.Usage()
    guidance = (PROMPTS / f"{issue_type}.md").read_text()
    context = _context(ws, issue_type, findings, all_findings, feedback, done)
    read_tools = {"run_sql": lambda a: ws.query(a["sql"])}

    diagnosis = run_agent(
        client, route, (PROMPTS / "investigator.md").read_text() + "\n\n" + guidance, context,
        [RUN_SQL, SUBMIT_DIAGNOSIS], read_tools, "submit_diagnosis", lambda a: None, usage)

    def accept_fix(a: dict) -> None:
        if a["action"] == "repair":
            ws.run_recipe(a["recipe_sql"], commit=False)  # moet foutloos draaien, anders terug naar het model

    fix = run_agent(
        client, route, (PROMPTS / "fixer.md").read_text() + "\n\n" + guidance,
        context + f"\n\n## Diagnose van de investigator\n{json.dumps(diagnosis, indent=2, ensure_ascii=False)}",
        [RUN_SQL, TEST_RECIPE, SUBMIT_FIX],
        {**read_tools, "test_recipe": lambda a: ws.run_recipe(a["recipe_sql"], commit=False)},
        "submit_fix", accept_fix, usage)

    recipe = fix["recipe_sql"] if fix["action"] == "repair" else []
    if recipe:
        ws.run_recipe(recipe, commit=True)
    return {"issue_type": issue_type, "model": route.model, "root_cause": diagnosis["root_cause"],
            "evidence": diagnosis["evidence"], "confidence": diagnosis["confidence"], "action": fix["action"],
            "recipe_sql": recipe, "rationale": fix["rationale"], "usage": usage.as_dict()}
