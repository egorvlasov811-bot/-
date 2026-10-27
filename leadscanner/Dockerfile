FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY leadscanner ./leadscanner
COPY config ./config

# Сервис не работает от root: даже при уязвимости в зависимостях доступ к системе ограничен.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data && chown app:app /app/data
USER app

VOLUME ["/app/data"]
CMD ["python", "-m", "leadscanner", "run"]
