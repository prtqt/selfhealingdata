# Build context: repo-root (zodat contracts/ meegekopieerd kan worden).
FROM python:3.12-slim
WORKDIR /app
COPY source/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY contracts /contracts
COPY source/source.py .
CMD ["python", "source.py", "run"]
