export HOST_UID := $(shell id -u)
DIRS := source outbox landing quarantine candidates pipeline console chaos
COMPOSE := docker compose

.PHONY: dirs up core down clean logs chaos report test hooks

dirs:            ## uitwisselmappen aanmaken als de huidige gebruiker
	@mkdir -p $(addprefix data/,$(DIRS))

up: dirs         ## alles starten: source -> chaos -> pipeline, console en healer
	SOURCE_OUT=outbox $(COMPOSE) --profile all up -d --build

core: dirs       ## alleen source en pipeline (geen chaos, console of healer)
	$(COMPOSE) --profile core up -d --build

down:            ## stoppen, data blijft staan
	$(COMPOSE) --profile all down

clean: down      ## stoppen en alle data weggooien
	$(COMPOSE) --profile all down -v
	rm -rf data

logs:
	$(COMPOSE) --profile all logs -f --tail 50

chaos:           ## volgende batch gegarandeerd verstoren: make chaos TYPE=schema_drift [VARIANT=rename_column]
	$(COMPOSE) exec chaos python chaos.py force $(TYPE) $(VARIANT)

report:          ## statisch rapport naar data/console/report.html
	curl -fsS http://localhost:8000/export -o /dev/null && echo "data/console/report.html"

test:            ## alle tests, zonder Docker en zonder Claude
	python -m pytest source/tests chaos/tests pipeline/tests console/tests healer/tests tests -q

hooks:           ## pre-commit hook aanzetten die commits met sleutels of geheimen blokkeert
	git config core.hooksPath .githooks
	@echo "pre-commit hook staat aan"
