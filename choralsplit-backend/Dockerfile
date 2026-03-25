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

# Tesseract OCR + MIDI→MP3 tools + ImageMagick for PDF preprocessing
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-deu \
    fluidsynth \
    ffmpeg \
    fluid-soundfont-gm \
    imagemagick \
    ghostscript \
    && rm -rf /var/lib/apt/lists/*

# Use tessdata_best for higher accuracy OCR
RUN mkdir -p /usr/share/tesseract-ocr/5/tessdata_best \
    && curl -L -o /usr/share/tesseract-ocr/5/tessdata_best/eng.traineddata \
       https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata \
    && curl -L -o /usr/share/tesseract-ocr/5/tessdata_best/deu.traineddata \
       https://github.com/tesseract-ocr/tessdata_best/raw/main/deu.traineddata \
    && curl -L -o /usr/share/tesseract-ocr/5/tessdata_best/osd.traineddata \
       https://github.com/tesseract-ocr/tessdata_best/raw/main/osd.traineddata
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata_best

# Allow ImageMagick to process PDFs (disabled by default in policy.xml)
RUN if [ -f /etc/ImageMagick-6/policy.xml ]; then \
      sed -i 's/<policy domain="coder" rights="none" pattern="PDF"/<policy domain="coder" rights="read|write" pattern="PDF"/' /etc/ImageMagick-6/policy.xml; \
    fi

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