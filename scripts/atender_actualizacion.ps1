# ─────────────────────────────────────────────────────────────────────────────
# atender_actualizacion.ps1 — vigilante del botón "Actualizar ahora" de la UI.
#
# El botón de la interfaz no puede actualizar directamente (el contenedor no
# puede reconstruirse a sí mismo), así que deja un archivo-señal en el volumen
# de datos. Este script, ejecutado cada pocos minutos por una tarea programada
# del host, comprueba la señal y lanza la actualización si existe.
#
# Lo registra automáticamente scripts/instalar_tareas.ps1 — no hace falta
# ejecutarlo a mano.
# ─────────────────────────────────────────────────────────────────────────────

# rm devuelve 0 SOLO si el archivo existía y se borró (consumir la señal es
# atómico: dos ejecuciones simultáneas no lanzan dos actualizaciones).
docker exec subtitulam-backend sh -c "rm /app/data/update-requested" 2>$null

if ($LASTEXITCODE -eq 0) {
    Write-Host "Señal de actualización detectada — actualizando..."
    & "$PSScriptRoot\actualizar.ps1"
}
# Sin señal: salir en silencio (es el caso normal, cada 5 minutos).
