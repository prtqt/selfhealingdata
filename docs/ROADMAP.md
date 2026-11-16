# Uitbreidingsplan

Tien uitbreidingen, in de volgorde waarin ze gebouwd worden. Elke stap is los op te leveren, heeft eigen tests en houdt de componentgrenzen intact: geen gedeelde code, alleen contracten en de console-API.

| # | Uitbreiding | Waar | Kern van het ontwerp | Status |
|---|---|---|---|---|
| 1 | Evaluatieharnas | nieuw: `evals/` | Vaste set verstoorde batches met bekende oorzaak. Per geval: detectie juist, kandidaat door de poort, aandeel schone rijen teruggewonnen, kosten. Vergelijkt modellen en prompts. | klaar |
| 2 | Blijvende herstelregels | console, healer | Een goedgekeurd recept kan als regel worden bewaard. Bij een incident met dezelfde bevindingen past de healer het recept toe zonder Claude; de poort controleert, daarna gaat het zonder mens door. Faalt de regel, dan neemt de agent het over. | gepland |
| 3 | Zakelijke impact | pipeline, console | De pipeline rekent bij quarantaine uit hoeveel bestellingen en omzet in de batch zitten en welke rapporten wachten. De product owner sorteert op impact. | gepland |
| 4 | Automatisch goedkeuren binnen grenzen | console | Beleid dat de product owner instelt: welke soorten fouten, minimale zekerheid, maximaal rijverlies. Elk automatisch besluit staat in de tijdlijn. | gepland |
| 5 | Meldingen | console | Webhook (Slack-formaat) zodra iets op een besluit wacht of de healer er niet uitkomt. | gepland |
| 6 | Login en rollen | console, pipeline, healer | Gebruikers met rol product owner of developer; alleen de product owner beslist. Diensten gebruiken een API-token. Uit als er niets is ingesteld. | gepland |
| 7 | Quarantaine per bestelling | pipeline, console | Bestellingen zonder bevinding gaan alvast door als de rest van de batch de poort haalt; alleen de geraakte bestellingen wachten. | gepland |
| 8 | Leren van afkeuringen | console, healer | De console geeft per soort fout eerdere afkeuringen met de opmerking en het uiteindelijk goedgekeurde recept; de healer krijgt die mee als context. | gepland |
| 9 | Meer soorten fouten | chaos, pipeline, healer | Verschuivende verdeling (gemiddelde prijs wijkt af van recente weken) als zevende soort, met dbt-model, injector, prompt en route. | gepland |
| 10 | Bewaking | console | `/metrics` in Prometheus-formaat (incidenten per status, doorlooptijd, kosten, slagingspercentage per model) en healthchecks in compose. | gepland |

## Bewust niet gebouwd

- **Ander warehouse en een berichtenwachtrij.** DuckDB vervangen door Postgres of een cloud-warehouse en het periodiek opvragen door een wachtrij is een herbouw van de pipeline, geen uitbreiding. De componentgrenzen maken het mogelijk (alleen `pipeline/sh_pipeline/warehouse.py` en de dbt-profielen raken het warehouse), maar het hoort niet in een lokale proof of concept.
- **Te late leveringen als soort fout.** Een late batch is niet te herstellen door de batch aan te passen; het is een melding, geen herstelgeval. Dat past beter bij stap 5 dan bij de healer.
