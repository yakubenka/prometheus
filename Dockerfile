FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

RUN mkdir -p /app/logs

# Healthcheck: бот жив если пишет в лог
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python -c "import os,time; log='/app/logs/prometheus.log'; \
    assert os.path.exists(log) and (time.time()-os.path.getmtime(log)) < 700" || exit 1

CMD ["python", "-u", "main.py"]
