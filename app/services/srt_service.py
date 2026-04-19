import srt
from typing import List

def parse_srt(file_content: str) -> List[srt.Subtitle]:
    """
    Convierte el texto bruto del archivo SRT en una lista de objetos de subtítulos.
    """
    return list(srt.parse(file_content))

def extract_texts(subtitles: List[srt.Subtitle]) -> List[str]:
    """
    Extrae solo los textos de los subtítulos, ignorando los tiempos.
    Esto es lo que enviaremos a la IA más adelante.
    """
    return [sub.content for sub in subtitles]

def rebuild_srt(original_subtitles: List[srt.Subtitle], translated_texts: List[str]) -> str:
    """
    Toma los tiempos originales y les inyecta los textos traducidos.
    Devuelve el texto final en formato SRT listo para guardar.
    """
    # Creamos una copia para no modificar los originales en memoria
    for i, sub in enumerate(original_subtitles):
        if i < len(translated_texts):
            sub.content = translated_texts[i]
    return srt.compose(original_subtitles)

def mock_translate(texts: List[str]) -> List[str]:
    """
    Simulador de IA. Simplemente añade '[ES]' delante de cada frase.
    Nos sirve para probar que el flujo funciona sin gastar API Keys.
    """
    return [f"[ES] {text}" for text in texts]