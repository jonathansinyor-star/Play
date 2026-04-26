FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

# Install ffmpeg for audio extraction
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD gunicorn app:app --timeout 600 --workers 2 --bind 0.0.0.0:$PORT
