# =============================================================================
# Meridian — Polymarket Multi-Asset Crypto Desk
# 4 parallel traders (BTC, ETH, SOL, XRP), late-window entry strategy
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

# Create required directories for runtime data
RUN mkdir -p /app/logs /app/config

# Default config files are NOT baked in (they contain credentials).
# You MUST mount them:
#   -v ./.env:/app/.env
#   -v ./config/config.json:/app/config/config.json
#
# Start with --web flag for the Flask dashboard.
# Default dashboard port: 5050

EXPOSE 5050

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5050/api/health', timeout=5)" || exit 1

# Web dashboard enabled by default, bind to 0.0.0.0 for container access.
# Change --web-port if 5050 conflicts with other services.
CMD ["python3", "src/main.py", "--web", "--web-host", "0.0.0.0", "--web-port", "5050"]
