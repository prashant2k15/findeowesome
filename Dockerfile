FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m -u 10001 blf \
 && mkdir -p /app/exports /app/imports /app/data \
 && chown -R blf:blf /app
USER blf

EXPOSE 8000

# default = the 24x7 worker; compose overrides this for the api service
CMD ["python", "-m", "app.worker"]
