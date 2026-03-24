# ── Stage 1: download Audiveris ──────────────────────────────────────────────
FROM debian:bookworm-slim AS audiveris-fetch

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Audiveris 5.3 — check https://github.com/Audiveris/audiveris/releases for latest
ARG AUDIVERIS_VERSION=5.3.1
RUN curl -fsSL \
    "https://github.com/Audiveris/audiveris/releases/download/${AUDIVERIS_VERSION}/Audiveris_${AUDIVERIS_VERSION}.zip" \
    -o /tmp/audiveris.zip \
    && unzip /tmp/audiveris.zip -d /opt \
    && mv /opt/Audiveris* /opt/audiveris \
    && chmod +x /opt/audiveris/bin/Audiveris \
    && rm /tmp/audiveris.zip


# ── Stage 2: runtime image ───────────────────────────────────────────────────
FROM python:3.11-slim-bookworm

# Java runtime (Audiveris needs JRE 11+)
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jre-headless \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Copy Audiveris
COPY --from=audiveris-fetch /opt/audiveris /opt/audiveris
ENV AUDIVERIS_CMD=/opt/audiveris/bin/Audiveris

# Python deps
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY backend/ .

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
