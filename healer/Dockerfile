FROM python:3.12-slim
WORKDIR /app
COPY healer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY healer/*.py healer/routing.yml ./
COPY healer/prompts ./prompts
CMD ["python", "worker.py"]
