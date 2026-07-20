FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY services/bauer-twin-api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/bauer-twin-api/app.py ./app.py
COPY services/bauer-twin-api/bauer_twin ./bauer_twin

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "bauer_twin.entrypoint"]

