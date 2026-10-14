FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN addgroup --system app && adduser --system --ingroup app app
WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=app:app app.py domain.py process.py exceptions.py ./

USER app
ENV PORT=5000 LOG_LEVEL=INFO
EXPOSE 5000

CMD ["sh", "-c", "gunicorn --workers 1 --bind 0.0.0.0:${PORT} --access-logfile - --error-logfile - app:app"]
