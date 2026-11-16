"""Drie kleine DAGs. Alle logica zit in sh_pipeline.flow, zodat die zonder Airflow te testen is."""
from datetime import datetime, timedelta

from airflow.sdk import dag, task

DEFAULTS = dict(start_date=datetime(2025, 1, 1), catchup=False, max_active_runs=1,
                default_args={"retries": 0}, tags=["selfhealing"])


@dag(dag_id="ingest", schedule=timedelta(minutes=1), **DEFAULTS)
def ingest():
    @task
    def ingest_landing():
        from sh_pipeline import flow
        return flow.ingest_landing()

    ingest_landing()


@dag(dag_id="validate_candidates", schedule=timedelta(minutes=1), **DEFAULTS)
def validate_candidates():
    @task
    def validate():
        from sh_pipeline import flow
        return flow.validate_candidates()

    validate()


@dag(dag_id="release_approved", schedule=timedelta(minutes=1), **DEFAULTS)
def release_approved():
    @task
    def release():
        from sh_pipeline import flow
        return flow.release_approved()

    release()


ingest()
validate_candidates()
release_approved()
