from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import Response
from app.services import srt_service, translation_service

router = APIRouter()

@router.get("/")
def root():
    return {"message": "API lista para traducir con GPT-4o 🤖"}

@router.post("/translate")
async def translate_subtitle(file: UploadFile = File(...)):
    # 1. Validación básica
    if not file.filename.endswith('.srt'):
        raise HTTPException(status_code=400, detail="Sube un archivo .srt válido.")
    
    content = await file.read()
    
    try:
        # 2. Convertir bytes a texto
        text_content = content.decode("utf-8")
        
        # 3. Parsear el SRT original
        original_subtitles = srt_service.parse_srt(text_content)
        
        # 4. Preparar diccionario {índice: texto} para la IA
        texts_to_translate = {s.index: s.content for s in original_subtitles}
        
        # 5. LLAMADA REAL A LA IA (Asíncrona y por bloques)
        # Esto usará tu lógica de bloques de 5 y ajuste de CPL
        translated_map = await translation_service.translate_texts(texts_to_translate)
        
        # 6. Reconstruir el SRT con los tiempos originales
        # Creamos una lista de textos traducidos manteniendo el orden
        final_texts = [translated_map.get(s.index, s.content) for s in original_subtitles]
        final_srt_content = srt_service.rebuild_srt(original_subtitles, final_texts)
        
        # 7. Devolver el archivo traducido
        return Response(
            content=final_srt_content,
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename=traducido_{file.filename}"}
        )
        
    except Exception as e:
        # Aquí capturamos cualquier error (API Key inválida, fallo de red, etc.)
        raise HTTPException(status_code=500, detail=f"Error en el servidor: {str(e)}")