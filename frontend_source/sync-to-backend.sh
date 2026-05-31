#!/bin/bash
# Sync built React frontend to FastAPI backend
# Run from medaicareplus-web directory

set -e

echo "Building React frontend..."
npm run build

echo "Copying build to backend..."
mkdir -p "../MedaiCarePlus/static/web"
cp -r dist/* "../MedaiCarePlus/static/web/"

echo "Done! Backend now has the latest frontend build."
echo "Run 'cd ../MedaiCarePlus && docker compose up -d' to serve."
