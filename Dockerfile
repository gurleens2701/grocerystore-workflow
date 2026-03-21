# ─────────────────────────────────────────────────────────────────────────────
# Gas Station Agent — Dockerfile
# Base: Microsoft Playwright Python image (Jammy) — includes Chromium deps
# ─────────────────────────────────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install timezone data (required for APScheduler America/New_York)
RUN apt-get update -qq && apt-get install -y -qq tzdata && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Install Chromium browser for Playwright (headless NRS login)
RUN playwright install chromium --with-deps

# Copy application source
COPY . .

# Ensure reports output directory exists inside the image
# (also mounted as a volume at runtime, so this is a safe fallback)
RUN mkdir -p /app/reports

# CMD is intentionally omitted — docker-compose specifies the command per service
