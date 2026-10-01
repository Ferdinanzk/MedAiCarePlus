# ═══════════════════════════════════════════════════════════════════════════════
# MedAiCarePlus — Multi-Stage Dockerfile
# Stage 1: Build React frontend
# Stage 2: Run FastAPI backend + serve built frontend
# ═══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: Build React Frontend ─────────────────────────────────────────────
FROM node:22-alpine AS frontend-builder

WORKDIR /frontend

# Copy frontend source
COPY frontend_source/package*.json ./
RUN npm ci

COPY frontend_source/ .
RUN npm run build

# ── Stage 2: FastAPI Backend ──────────────────────────────────────────────────
FROM python:3.11-slim

# System dependencies for OpenCV + OpenVINO
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgl1 \
    libgomp1 \
    libgstreamer1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install remaining dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --retries 5 --timeout 120 -r requirements.txt

# Copy backend code
COPY app/ ./app/
COPY sql/ ./sql/
COPY models/face_recognition/ /models/face_recognition/
COPY models/emotion_seed43/ /models/emotion_seed43/

# Copy built frontend from Stage 1
COPY --from=frontend-builder /frontend/dist /app/static/web

# Copy legacy static assets (CSS, JS for Jinja2 templates)
COPY static/css ./static/css
COPY static/js ./static/js

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# Let Docker report readiness after the FastAPI lifespan has loaded the models
# and initialized the database connection.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

EXPOSE 8001
CMD ["python", "-m", "app.serve"]
