# ─────────────────────────────────────────────────────────────────────────────
# actualizar.ps1 — actualiza Subtitulam a la última versión publicada.
#
# Pensado para la instalación del estudio: descarga los cambios de GitHub,
# reconstruye las imágenes y reinicia los servicios. Los datos (historial,
# glosario, memoria de traducción) viven en volúmenes Docker y NO se tocan.
#
# Uso (desde la carpeta del proyecto):
#   .\scripts\actualizar.ps1
#
# Requisitos: Git y Docker Desktop instalados; carpeta conectada al repo
# (git remote origin configurado).
# ─────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

# Ir a la raíz del proyecto (la carpeta padre de scripts\)
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host ""
Write-Host "=== Actualizando Subtitulam ===" -ForegroundColor Cyan

Write-Host ""
Write-Host "[1/4] Descargando la ultima version desde GitHub..." -ForegroundColor Yellow
git pull --ff-only
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: git pull ha fallado. Comprueba la conexion a internet." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[2/4] Reconstruyendo las imagenes (puede tardar varios minutos)..." -ForegroundColor Yellow
docker compose build
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: el build ha fallado. Comprueba que Docker Desktop esta en marcha." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[3/4] Reiniciando los servicios..." -ForegroundColor Yellow
docker compose up -d
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: no se pudieron arrancar los servicios." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[4/4] Comprobando el estado (15 segundos)..." -ForegroundColor Yellow
Start-Sleep -Seconds 15
docker compose ps

Write-Host ""
Write-Host "Actualizacion completa." -ForegroundColor Green
Write-Host "La app sigue en http://localhost:8501 - recarga la pestana del navegador con F5."
Write-Host ""
