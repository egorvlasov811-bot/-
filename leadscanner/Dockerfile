FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY leadscanner ./leadscanner
COPY config ./config

VOLUME ["/app/data"]
CMD ["python", "-m", "leadscanner", "run"]
