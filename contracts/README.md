# Contracten

Het enige wat de componenten delen. Geen component importeert code van een ander.

| Bestand | Tussen wie | Inhoud |
|---|---|---|
| `landing_schema.yml` | source → pipeline | Tabellen, kolommen, types en sleutels van een batch |
| `incident.json` | pipeline → console | Batch in quarantaine met de bevindingen |
| `proposal.json` | healer → console | Diagnose en herstelrecept per bevinding |
| `validation.json` | pipeline → console | Uitslag van de controles op de kandidaat |
| `decision.json` | mens → console | Goedkeuren, afkeuren, weggooien of origineel vrijgeven |
| `resolution.json` | pipeline → console | Afloop van het vrijgeven |

## Uitwisselmappen

Elke map heeft één schrijver. Een batch verschijnt altijd atomair: eerst als `.tmp-<naam>`, daarna hernoemd.

| Map | Schrijver | Lezer | Inhoud |
|---|---|---|---|
| `outbox/<batch_id>/` | source | chaos | batch volgens `landing_schema.yml` |
| `landing/<batch_id>/` | chaos (of source) | pipeline | idem |
| `quarantine/<batch_id>/` | pipeline | healer | zie hieronder |
| `candidates/<incident_id>-a<poging>/` | healer | pipeline | herstelde batch + `recipe.sql` |

## Quarantainebundel

```
quarantine/<batch_id>/
  *.csv                 de batch precies zoals aangeleverd
  _manifest.json        batch_id, window_start, window_end
  findings.json         bevindingen (zelfde vorm als in incident.json)
  history_stats.csv     batch_id, table_name, row_count van eerdere goede batches
  history_metrics.csv   batch_id, metric, value van eerdere goede batches (bijv. avg_price)
  landing_schema.yml    het contract waaraan de batch moet voldoen
```

## Blijvende regels

Een goedgekeurd recept kan met `save_as_rule` in het besluit als regel worden bewaard. De console herkent een terugkerende afwijking aan de signatuur van het incident (welke controle faalt op welke kolom) en geeft de passende regel mee in het antwoord op `claim`. De healer past het recept dan toe zonder agent en zet `rule_id` in het voorstel. Doorstaat de kandidaat de poort, dan keurt de console automatisch goed.

## Lessen

Het antwoord op `claim` bevat ook `lessons`: eerdere afkeuringen door een mens bij dezelfde soort fout, met het afgekeurde recept, de opmerking en het recept dat daarna is goedgekeurd.

## Statussen van een incident

`detected → investigating → proposed → validated → approved → resolved`

- Kandidaat faalt de controles of de mens keurt af: terug naar `detected` met feedback, poging +1.
- Na het maximale aantal pogingen: `needs_human`. Een mens kan dan alleen weggooien of het origineel vrijgeven.
- `discarded` is de eindstatus van een weggegooide batch.
