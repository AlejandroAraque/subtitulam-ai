# ─────────────────────────────────────────────────────────────────────────────
# instalar_tareas.ps1 — registra las tareas de actualización automática.
#
# Ejecutar UNA vez, como ADMINISTRADOR, en el equipo donde corre Subtitulam:
#   clic derecho en PowerShell → "Ejecutar como administrador" →
#   cd a la carpeta del proyecto → .\scripts\instalar_tareas.ps1
#
# Registra dos tareas:
#   1) Subtitulam-Actualizar-Logon  — actualiza al iniciar sesión (con 3 min
#      de espera para que Docker Desktop arranque). Pensada para portátiles:
#      al encender por la mañana, la app queda al día sola.
#   2) Subtitulam-Boton-Actualizar  — cada 5 minutos comprueba si alguien
#      pulsó "Actualizar ahora" en la interfaz, y actualiza si es el caso.
#
# Ambas pueden correr con batería y se recuperan si el equipo estaba apagado.
# ─────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

# Comprobar permisos de administrador
$esAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $esAdmin) {
    Write-Host "ERROR: este script necesita PowerShell como ADMINISTRADOR." -ForegroundColor Red
    Write-Host "Clic derecho en PowerShell -> 'Ejecutar como administrador' y reintenta."
    exit 1
}

$root = Split-Path $PSScriptRoot -Parent
Write-Host "Proyecto: $root"

$ajustes = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

# ── Tarea 1: actualizar al iniciar sesión ───────────────────────────────────
$accion1 = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\scripts\actualizar.ps1`""
$trigger1 = New-ScheduledTaskTrigger -AtLogOn
$trigger1.Delay = "PT3M"   # 3 min de margen para que Docker Desktop arranque
Register-ScheduledTask -TaskName "Subtitulam-Actualizar-Logon" `
    -Action $accion1 -Trigger $trigger1 -Settings $ajustes -Force | Out-Null
Write-Host "[1/2] Tarea 'Subtitulam-Actualizar-Logon' registrada (al iniciar sesion)." -ForegroundColor Green

# ── Tarea 2: vigilante del botón "Actualizar ahora" (cada 5 min) ────────────
$accion2 = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\scripts\atender_actualizacion.ps1`""
$trigger2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "Subtitulam-Boton-Actualizar" `
    -Action $accion2 -Trigger $trigger2 -Settings $ajustes -Force | Out-Null
Write-Host "[2/2] Tarea 'Subtitulam-Boton-Actualizar' registrada (vigila el boton cada 5 min)." -ForegroundColor Green

Write-Host ""
Write-Host "Listo. La instalacion se actualizara sola al iniciar sesion, y el boton" -ForegroundColor Cyan
Write-Host "'Actualizar ahora' de la interfaz surtira efecto en menos de 5 minutos." -ForegroundColor Cyan
