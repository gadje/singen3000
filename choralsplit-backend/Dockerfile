# ── Stage 1: build Audiveris from source ─────────────────────────────────────
FROM eclipse-temurin:25-jdk-jammy AS audiveris-build

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Clone the latest release tag
ARG AUDIVERIS_TAG=5.10.1
RUN git clone --depth 1 --branch ${AUDIVERIS_TAG} \
    https://github.com/Audiveris/audiveris.git /build/audiveris

WORKDIR /build/audiveris

# Build only the app subproject (skipping tests and packaging)
RUN ./gradlew :app:installDist -x test --no-daemon

# Move whatever installDist created (directory name varies by Gradle version)
RUN mv "$(find /build/audiveris/app/build/install -mindepth 1 -maxdepth 1 -type d | head -1)" /opt/audiveris \
    && chmod +x /opt/audiveris/bin/*


# ── Stage 2: runtime image ───────────────────────────────────────────────────
FROM python:3.11-slim-bookworm

# Copy JRE from the build stage (avoids apt availability issues for openjdk-21)
COPY --from=audiveris-build /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# Tesseract OCR (required by Audiveris)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*

# Copy built Audiveris
COPY --from=audiveris-build /opt/audiveris /opt/audiveris
ENV AUDIVERIS_CMD=/opt/audiveris/bin/Audiveris

# Python deps
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY backend/ .

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]