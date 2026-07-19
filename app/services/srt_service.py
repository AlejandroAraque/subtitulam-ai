from typing import List

import srt


def parse_srt(file_content: str) -> List[srt.Subtitle]:
    """
    Convierte el texto bruto del archivo SRT en una lista de objetos de subtítulos.
    """
    return list(srt.parse(file_content))


def parse_srt_normalizado(file_content: str) -> List[srt.Subtitle]:
    """Parsea Y normaliza: ordena por tiempo y renumera 1..N.

    La librería `srt` acepta cues con index=None o duplicado; el pipeline
    usa el índice como clave de dict, así que un SRT con índices rotos
    colapsaba cues (traducciones cruzadas) o reventaba al persistir
    (cue_idx NULL). Renumerar server-side es inocuo — el número de cue
    es un contador, ningún reproductor depende de él.
    """
    return list(srt.sort_and_reindex(srt.parse(file_content)))

def compose_srt(subtitles: List[srt.Subtitle]) -> str:
    """Serializa la lista de subtítulos de vuelta a texto SRT."""
    return srt.compose(subtitles)


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
