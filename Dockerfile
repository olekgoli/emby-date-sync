FROM python:3.13-alpine

RUN adduser -D -u 1000 app

WORKDIR /app

COPY VERSION .
COPY app/ ./app/

USER app

ENV PYTHONDONTWRITEBYTECODE=1

CMD ["python", "-m", "app.emby_date_sync"]
