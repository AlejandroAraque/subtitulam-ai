from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import Response
from app.services import srt_service, translation_service

router = APIRouter()


@router.get("/")
def root():
    return {"message": "API lista para traducir con GPT-4o 🤖"}


@router.post("/translate")
async def translate_subtitle(
    file: UploadFile = File(...),
    context: str = Form(""),
    target_lang: str = Form("es"),
    cpl: int = Form(38),
):
    """Traduce un .srt al idioma destino inyectando el contexto en el system prompt."""
    if not file.filename.endswith('.srt'):
        raise HTTPException(status_code=400, detail="Sube un archivo .srt válido.")

    content = await file.read()

    try:
        text_content = content.decode("utf-8")
        original_subtitles = srt_service.parse_srt(text_content)
        texts_to_translate = {s.index: s.content for s in original_subtitles}

        translated_map = await translation_service.translate_texts(
            texts_to_translate,
            target_lang=target_lang,
            context=context,
            cpl_limit=cpl,
        )

        final_texts = [translated_map.get(s.index, s.content) for s in original_subtitles]
        final_srt_content = srt_service.rebuild_srt(original_subtitles, final_texts)

        return Response(
            content=final_srt_content,
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename=traducido_{file.filename}"},
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en el servidor: {str(e)}")
