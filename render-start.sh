#!/bin/bash
# Render startup script for 86d-api
# Ensures proper port binding and startup

set -e

echo "[render-start] Starting 86d-api..."
echo "[render-start] PORT=${PORT:-10000}"
echo "[render-start] Python version: $(python --version)"

# Verify critical env vars
if [ -z "$DATABASE_URL" ]; then
    echo "[render-start] WARNING: DATABASE_URL not set"
fi

# Use PORT from Render, fallback to 10000 for local
export UVICORN_PORT=${PORT:-10000}

echo "[render-start] Starting uvicorn on port $UVICORN_PORT..."

# Run uvicorn with explicit settings for Render
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port "$UVICORN_PORT" \
    --workers 1 \
    --timeout-keep-alive 30 \
    --access-log \
    --log-level info
