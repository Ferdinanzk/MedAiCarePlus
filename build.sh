#!/bin/bash
# Build script for MedAiCarePlus Docker image with React frontend
# This copies the frontend source into the backend build context, then builds.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR"
FRONTEND_SOURCE="$BACKEND_DIR/../medaicareplus-web"
FRONTEND_BUILD_DIR="$BACKEND_DIR/frontend_source"

SKIP_COPY=false
NO_CACHE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-copy)
            SKIP_COPY=true
            shift
            ;;
        --no-cache)
            NO_CACHE="--no-cache"
            shift
            ;;
        *)
            shift
            ;;
    esac
done

echo "═══════════════════════════════════════════════════"
echo "  MedAiCarePlus Docker Build"
echo "═══════════════════════════════════════════════════"

# ── Step 1: Copy frontend source into backend build context ──────────────────
if [ "$SKIP_COPY" = false ]; then
    echo ""
    echo "[1/3] Copying frontend source..."
    rm -rf "$FRONTEND_BUILD_DIR"
    if [ ! -d "$FRONTEND_SOURCE" ]; then
        echo "ERROR: Frontend source not found at: $FRONTEND_SOURCE"
        exit 1
    fi
    cp -r "$FRONTEND_SOURCE" "$FRONTEND_BUILD_DIR"
    echo "      Copied to frontend_source/"
else
    echo ""
    echo "[1/3] Skipping frontend copy (using existing frontend_source/)"
fi

# ── Step 2: Build Docker image ───────────────────────────────────────────────
echo ""
echo "[2/3] Building Docker image..."
docker compose build $NO_CACHE

# ── Step 3: Clean up ────────────────────────────────────────────────────────
echo ""
echo "[3/3] Cleaning up frontend_source/..."
rm -rf "$FRONTEND_BUILD_DIR"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Build complete!"
echo "  Run: docker compose up -d"
echo "═══════════════════════════════════════════════════"
