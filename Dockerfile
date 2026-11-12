FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Install minimal build tools for native extensions (e.g. tgcrypto, psutil)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    git \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Optimize build caching: copy requirements first
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy application source
COPY . .

EXPOSE 8080

CMD ["python", "-m", "FileStream"]
