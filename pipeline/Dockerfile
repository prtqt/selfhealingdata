# Build context: repo-root.
FROM apache/airflow:3.4.0-python3.12

# dbt in een eigen venv, zodat zijn afhankelijkheden niet botsen met die van Airflow.
USER root
RUN python -m venv /opt/dbt-venv \
 && /opt/dbt-venv/bin/pip install --no-cache-dir dbt-duckdb==1.11.1 duckdb==2.0.0 \
 && ln -s /opt/dbt-venv/bin/dbt /usr/local/bin/dbt
USER airflow
RUN pip install --no-cache-dir duckdb==2.0.0 pyyaml requests

COPY contracts /contracts
COPY pipeline/sh_pipeline /opt/pipeline/sh_pipeline
COPY pipeline/dags /opt/pipeline/dags
COPY pipeline/dbt /opt/pipeline/dbt
ENV PYTHONPATH=/opt/pipeline \
    DBT_DIR=/opt/pipeline/dbt \
    AIRFLOW__CORE__DAGS_FOLDER=/opt/pipeline/dags
CMD ["standalone"]
