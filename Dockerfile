# Production Dockerfile for Render / Containerized Deployment
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

WORKDIR /app

# Ensure non-buffered stdout and clean environment
ENV PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    PYTHONHASHSEED=random \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8000 \
    HOST=0.0.0.0

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ensure Playwright browser dependencies and chromium are installed
RUN playwright install chromium --with-deps

# Copy full application source code
COPY . .

# Expose standard port
EXPOSE 8000

# Start FastAPI application
CMD ["python", "run.py"]

