# ─────────────────────────────────────────────────────────────────────────────
# backup.ps1 — copia de seguridad nocturna de los datos de Subtitulam.
#
# Copia las tres piezas de datos que viven en volúmenes Docker (hallazgo G2
# de la auditoría: la TM de ~2.000 películas/año no tenía ninguna copia):
#
#   1) SQLite (jobs, glosario, traducciones) — backup ONLINE con la API
#      sqlite3.backup(): copia consistente aunque haya escrituras en curso.
#      La BD está en modo WAL, copiar el archivo en caliente daría una
#      copia potencialmente corrupta.
#   2) Qdrant (memoria de traducción vectorizada) — snapshot COMPLETO del
#      storage vía su API HTTP.
#   3) outputs/ (SRTs archivados) — copia directa con docker cp. data/ es
#      un volumen Docker nombrado (app_data), no un bind mount al repo,
#      así que docker cp es la vía; si algún día pasa a bind mount, sería
#      más eficiente robocopy /MIR desde la carpeta local.
#
# Estructura del destino:  <DestinoBase>\AAAA-MM-DD\{sqlite,qdrant,outputs}
# Retención: se borran carpetas con más de $DiasRetencion días (se parsea
# la fecha del NOMBRE de la carpeta, no la del filesystem).
# Registro: resumen de cada ejecución en <DestinoBase>\backup.log.
#
# Uso (manual, o desde la tarea 'Subtitulam-Backup-Nocturno' que registra
# scripts/instalar_tareas.ps1):
#   .\scripts\backup.ps1
#   .\scripts\backup.ps1 -DestinoBase "\\NAS\backups\subtitulam" -DiasRetencion 60
#
# Código de salida 0 SOLO si el SQLite se copió y verificó bien (es el dato
# crítico); los fallos de Qdrant u outputs avisan pero no abortan el resto.
# ─────────────────────────────────────────────────────────────────────────────

param(
    # El estudio cambiará esto a la ruta de su NAS (admite rutas UNC).
    [string]$DestinoBase = "$env:USERPROFILE\SubtitulamBackups",
    [int]$DiasRetencion = 30
)

$ErrorActionPreference = "Stop"

# Crear la carpeta base ya, para que el log exista aunque falle todo lo demás.
New-Item -ItemType Directory -Force -Path $DestinoBase | Out-Null
$log = Join-Path $DestinoBase "backup.log"

# Escribe en pantalla y en el log a la vez (la tarea programada corre con
# ventana oculta: sin el log no habría rastro de qué pasó cada noche).
function Registrar($mensaje, $color = "Gray") {
    Write-Host $mensaje -ForegroundColor $color
    Add-Content -Path $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $mensaje"
}

# Tamaño legible de un archivo o carpeta ya copiados al destino.
function TamanoMB($ruta) {
    if (-not (Test-Path $ruta)) { return "0.0 MB" }
    $bytes = (Get-ChildItem $ruta -Recurse -File | Measure-Object Length -Sum).Sum
    if ($null -eq $bytes) { $bytes = 0 }
    return "{0:N1} MB" -f ($bytes / 1MB)
}

$fecha         = Get-Date -Format "yyyy-MM-dd"    # nombre de la carpeta del día
$fechaCompacta = Get-Date -Format "yyyyMMdd"      # sufijo del archivo .db
$destino       = Join-Path $DestinoBase $fecha

Write-Host ""
Write-Host "=== Backup de Subtitulam ===" -ForegroundColor Cyan
Registrar "--- Backup $fecha iniciado (destino: $destino) ---" Cyan

# Esperar a que Docker Desktop esté listo (hasta 5 min). Necesario cuando
# la tarea corre nada más encender el portátil (StartWhenAvailable recupera
# la ejecución de las 03:30 si el equipo estaba apagado).
Write-Host ""
Write-Host "[0/4] Esperando a Docker..." -ForegroundColor Yellow
$intentos = 0
while ($true) {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) { break }
    $intentos++
    if ($intentos -ge 30) {
        Registrar "ERROR: Docker no ha arrancado en 5 minutos. Backup abortado." Red
        exit 1
    }
    Start-Sleep -Seconds 10
}
Write-Host "Docker listo." -ForegroundColor Green

# Carpetas del día. OJO: outputs NO se pre-crea — si existiera, docker cp
# copiaría el directorio DENTRO y quedaría outputs\outputs.
New-Item -ItemType Directory -Force -Path (Join-Path $destino "sqlite") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $destino "qdrant") | Out-Null

# ── [1/4] SQLite: backup online → docker cp al destino ──────────────────────
Write-Host ""
Write-Host "[1/4] Copiando la base de datos SQLite..." -ForegroundColor Yellow
$sqliteOk  = $false
$totalJobs = "?"
$archivoDb = "subtitulam-$fechaCompacta.db"

# El código Python va en una sola línea con comillas SIMPLES internas: así la
# cadena PowerShell (comillas dobles) llega intacta a docker exec. Solo se
# interpola $archivoDb; no hay backticks ni otros $.
$codigoPython = "import sqlite3, os; os.makedirs('/app/data/backups', exist_ok=True); src = sqlite3.connect('/app/data/subtitulam.db'); dst = sqlite3.connect('/app/data/backups/$archivoDb'); src.backup(dst); dst.close(); src.close()"
docker exec subtitulam-backend python -c $codigoPython
if ($LASTEXITCODE -eq 0) {
    # Verificar que la copia se abre y tiene datos ANTES de traerla al host.
    $totalJobs = docker exec subtitulam-backend python -c "import sqlite3; print(sqlite3.connect('/app/data/backups/$archivoDb').execute('SELECT COUNT(*) FROM jobs').fetchone()[0])"
    docker cp "subtitulam-backend:/app/data/backups/$archivoDb" "$destino\sqlite\$archivoDb"
    if ($LASTEXITCODE -eq 0 -and (Test-Path "$destino\sqlite\$archivoDb") -and (Get-Item "$destino\sqlite\$archivoDb").Length -gt 0) {
        # Limpiar la copia intermedia del contenedor (un .db por día acumularía).
        docker exec subtitulam-backend sh -c "rm -f /app/data/backups/$archivoDb"
        $sqliteOk = $true
        Registrar "SQLite OK: $archivoDb ($(TamanoMB "$destino\sqlite\$archivoDb"), $totalJobs jobs)." Green
    } else {
        Registrar "ERROR: el backup SQLite se creo pero docker cp al destino fallo." Red
    }
} else {
    Registrar "ERROR: fallo el backup online de SQLite dentro del contenedor." Red
}

# ── [2/4] Qdrant: snapshot completo vía API → docker cp al destino ──────────
Write-Host ""
Write-Host "[2/4] Creando snapshot de Qdrant..." -ForegroundColor Yellow
$qdrantOk = $false
try {
    # POST /snapshots crea un snapshot COMPLETO del storage (todas las
    # colecciones + metadatos). Qdrant lo escribe en /qdrant/snapshots/
    # dentro del contenedor (path por defecto de storage.snapshots_path,
    # verificado con: docker exec subtitulam-qdrant ls /qdrant/snapshots).
    $respuesta  = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:6333/snapshots" -TimeoutSec 600
    $nombreSnap = $respuesta.result.name
    docker cp "subtitulam-qdrant:/qdrant/snapshots/$nombreSnap" "$destino\qdrant\$nombreSnap"
    if ($LASTEXITCODE -eq 0 -and (Test-Path "$destino\qdrant\$nombreSnap") -and
        (Get-Item "$destino\qdrant\$nombreSnap").Length -eq $respuesta.result.size) {
        # Borrar el snapshot del contenedor: ocupa tanto como el storage entero
        # y ya está a salvo en el destino.
        Invoke-RestMethod -Method Delete -Uri "http://127.0.0.1:6333/snapshots/$nombreSnap" -TimeoutSec 60 | Out-Null
        # Re-ejecución del mismo día: quedarse solo con el snapshot recién
        # verificado (los nombres llevan la hora, se acumularían, y cada uno
        # pesa tanto como el storage entero).
        Get-ChildItem "$destino\qdrant" -File |
            Where-Object { $_.Name -ne $nombreSnap } |
            Remove-Item -Force -Confirm:$false
        $qdrantOk = $true
        Registrar "Qdrant OK: $nombreSnap ($(TamanoMB "$destino\qdrant\$nombreSnap"))." Green
    } else {
        Registrar "AVISO: el snapshot de Qdrant se creo pero la copia al destino fallo o el tamano no coincide." Yellow
    }
} catch {
    Registrar "AVISO: fallo el snapshot de Qdrant ($($_.Exception.Message)). Continuo con el resto." Yellow
}

# ── [3/4] outputs: SRTs archivados → docker cp al destino ───────────────────
Write-Host ""
Write-Host "[3/4] Copiando los SRT archivados (outputs)..." -ForegroundColor Yellow
$outputsOk = $false
# Si ya existe (re-ejecución del mismo día), se borra antes: con el destino
# existente docker cp copiaría DENTRO y quedaría outputs\outputs.
if (Test-Path "$destino\outputs") { Remove-Item "$destino\outputs" -Recurse -Force -Confirm:$false }
docker cp "subtitulam-backend:/app/data/outputs" "$destino\outputs"
if ($LASTEXITCODE -eq 0) {
    $outputsOk = $true
    $numSrt = (Get-ChildItem "$destino\outputs" -Recurse -File | Measure-Object).Count
    Registrar "Outputs OK: $numSrt archivos ($(TamanoMB "$destino\outputs"))." Green
} else {
    Registrar "AVISO: fallo la copia de outputs. Continuo con el resto." Yellow
}

# ── [4/4] Retención: borrar backups con más de $DiasRetencion días ──────────
Write-Host ""
Write-Host "[4/4] Aplicando retencion ($DiasRetencion dias)..." -ForegroundColor Yellow
$limite = (Get-Date).Date.AddDays(-$DiasRetencion)
Get-ChildItem -Path $DestinoBase -Directory |
    Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}$' } |
    ForEach-Object {
        try {
            # La fecha se saca del NOMBRE de la carpeta: la del filesystem
            # cambia con cualquier copia/migración del destino y no es fiable.
            $fechaCarpeta = [datetime]::ParseExact($_.Name, "yyyy-MM-dd", $null)
            if ($fechaCarpeta -lt $limite) {
                Remove-Item $_.FullName -Recurse -Force -Confirm:$false
                Registrar "Retencion: borrado backup antiguo $($_.Name)." Gray
            }
        } catch {
            Registrar "AVISO: no se pudo evaluar/borrar $($_.Name) ($($_.Exception.Message))." Yellow
        }
    }

# ── Resumen y código de salida ───────────────────────────────────────────────
$estado = "sqlite=$(if ($sqliteOk) {'OK'} else {'ERROR'}) qdrant=$(if ($qdrantOk) {'OK'} else {'ERROR'}) outputs=$(if ($outputsOk) {'OK'} else {'ERROR'})"
Write-Host ""
if ($sqliteOk) {
    Registrar "--- Backup $fecha terminado: $estado ---" Green
    exit 0
} else {
    # Sin la copia del SQLite el backup no vale: exit 1 para que la tarea
    # programada quede marcada como fallida y se vea en el historial.
    Registrar "--- Backup $fecha FALLIDO (SQLite no copiado): $estado ---" Red
    exit 1
}
