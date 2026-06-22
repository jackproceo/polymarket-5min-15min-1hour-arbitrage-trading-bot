# =============================================================================
# PTB Bot — Polymarket BTC Auto-Trader
# PTB diff + probability triggers, 5m/15m windows
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

# config.env is NOT baked in (contains API keys).
# You MUST mount it:
#   -v ./config.env:/app/config.env
#
# State and analysis logs persist for restart recovery:
#   -v ./state.json:/app/state.json
#   -v ./trading_analysis.jsonl:/app/trading_analysis.jsonl

EXPOSE 5080

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5080/', timeout=5)" || exit 1

# WEB_HOST defaults to 0.0.0.0, WEB_PORT defaults to 5080.
# Override via config.env or environment variables.
CMD ["python3", "polymarket_auto_trade.py"]
