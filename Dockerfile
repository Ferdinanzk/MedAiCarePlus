# ═══════════════════════════════════════════════════════════════════════════════
# MedAiCarePlus — Multi-Stage Dockerfile
# Stage 1: Build React frontend
# Stage 2: Run FastAPI backend + serve built frontend
# ═══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: Build React Frontend ─────────────────────────────────────────────
FROM node:20-alpine AS frontend-builder

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

# Install PyTorch CPU-only first (separate index to avoid hash conflicts)
RUN pip install --no-cache-dir \
    torch==2.3.1+cpu \
    torchvision==0.18.1+cpu \
    --index-url https://download.pytorch.org/whl/cpu

# Install remaining dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --retries 5 --timeout 120 -r requirements.txt

# Copy backend code
COPY app/ ./app/
COPY sql/ ./sql/

# Copy built frontend from Stage 1
COPY --from=frontend-builder /frontend/dist /app/static/web

# Copy legacy static assets (CSS, JS for Jinja2 templates)
COPY static/css ./static/css
COPY static/js ./static/js

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
