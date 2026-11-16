FROM python:3.12-slim
WORKDIR /app
COPY chaos/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY chaos/*.py ./
CMD ["python", "chaos.py", "run"]
