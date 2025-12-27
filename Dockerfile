FROM python:3.11.8-slim

LABEL maintainer="your-email@example.com"
LABEL description="Discord Music Bot - Plays YouTube music with per-guild queues"
LABEL version="1.0.0"

WORKDIR /app

# Install system dependencies (ffmpeg, libopus, libsodium for audio)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       libopus0 \
       libsodium23 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user for security
RUN useradd -m -u 1000 botuser

# Copy application code with proper ownership
COPY --chown=botuser:botuser . .

# Switch to non-root user
USER botuser

# Health check - verify bot can import discord module
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import discord; print('ok')" || exit 1

CMD ["python", "main.py"]

