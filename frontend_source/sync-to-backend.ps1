# Sync built React frontend to FastAPI backend
# Run from medaicareplus-web directory

Write-Host "Building React frontend..." -ForegroundColor Green
npm run build

Write-Host "Copying build to backend..." -ForegroundColor Green
$backendDir = "..\MedaiCarePlus\static\web"
New-Item -ItemType Directory -Force -Path $backendDir | Out-Null
Copy-Item -Path "dist\*" -Destination $backendDir -Recurse -Force

Write-Host "Done! Backend now has the latest frontend build." -ForegroundColor Green
Write-Host "Run 'cd ..\MedaiCarePlus; docker compose up -d' to serve." -ForegroundColor Cyan
