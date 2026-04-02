FROM python:3.11-slim

# System dependencies for langdetect and plyer
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY templates/ ./templates/
COPY sources.yaml .

# Create data directory for SQLite and Telegram session
RUN mkdir -p /app/data

# Default environment (override via docker-compose or -e flags)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_URL=sqlite+aiosqlite:////app/data/incidents.db \
    TELEGRAM_SESSION_NAME=beirut_monitor \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8080 \
    LOG_LEVEL=INFO

EXPOSE 8080

CMD ["python", "-m", "app.main"]
