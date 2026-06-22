# =============================================================================
# BTC Binary — VWAP & Momentum Bot
# Polymarket BTC Up/Down automated trading bot
# =============================================================================
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── Runtime stage ──────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . .

# Create logs directory (will be overridden by volume mount in production)
RUN mkdir -p /app/logs /app/data

# Default config.json is baked in (with simulation defaults).
# Override via volume mount for real trading:
#   -v ./config.json:/app/config.json
#   -v ./.env:/app/.env
#
# Environment variables (sensitive) should be in .env file or docker-compose env_file.

EXPOSE 8765

# Health check: FastAPI dashboard endpoint
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/state', timeout=5)" || exit 1

CMD ["python3", "main.py"]
