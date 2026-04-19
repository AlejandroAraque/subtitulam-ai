import os
import re
from typing import Dict
from openai import AsyncOpenAI
from dotenv import load_dotenv  # <-- Añadimos esto
from app.utils.text_utils import ajustar_cpl_optimo

# Forzamos a Python a leer el archivo .env antes de seguir
load_dotenv()
client = AsyncOpenAI()

SYSTEM_PROMPT = """
Eres un traductor profesional experto en subtítulos de cine y televisión del inglés al español de España.
Tu objetivo es producir subtítulos naturales, concisos y fáciles de leer.

INSTRUCCIONES OBLIGATORIAS:
1. Economía del lenguaje: Sé conciso, elimina relleno.
2. Naturalidad: "I guess we should get going" → "Supongo que deberíamos ir tirando".
3. Adaptación cultural: No traduzcas literal. "He hit the nail on the head" → "Ha dado en el clavo".
4. Segmentación: Máximo 2 líneas, divididas por pausas naturales.
5. Elipsis del verbo copulativo (CRÍTICO): En español SIEMPRE debe aparecer el verbo "ser" o "estar". "Not necessary" → "No es necesario".
6. Formato ESTRICTO:
   - Inicia cada línea con el número recibido seguido de dos puntos.
   - Ejemplo:
     5: Texto de la primera línea
     Segunda línea del mismo subtítulo
     6: Siguiente subtítulo
"""

def parsear_traducciones(traduccion_bruta: str) -> Dict[int, str]:
    """Extrae el texto traducido y lo asocia a su índice original."""
    traducciones = {}
    idx_actual = None
    
    for linea in traduccion_bruta.splitlines():
        m = re.match(r'^\s*(\d+)\s*:\s*(.*)$', linea)
        if m:
            idx_actual = int(m.group(1))
            traducciones[idx_actual] = m.group(2).strip()
        else:
            if idx_actual is not None and linea.strip():
                traducciones[idx_actual] += "\n" + linea.strip()
                
    return traducciones

async def translate_texts(texts_dict: Dict[int, str], chunk_size: int = 5) -> Dict[int, str]:
    """
    Recibe un diccionario {index: "texto original"} y devuelve {index: "texto traducido ajustado a CPL"}.
    Usa OpenAI asíncrono y procesa en bloques.
    """
    items = list(texts_dict.items())
    translated_dict = {}

    # Procesar en bloques (Chunking)
    for i in range(0, len(items), chunk_size):
        bloque = items[i:i + chunk_size]
        texto_prompt = "\n\n".join([f"{idx}: {texto}" for idx, texto in bloque])
        
        try:
            response = await client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": texto_prompt}
                ],
                temperature=0.3,
                max_tokens=800
            )
            
            traduccion_bruta = response.choices[0].message.content.strip()
            traducciones_parseadas = parsear_traducciones(traduccion_bruta)
            
            # Aplicar tu genial ajuste de CPL a cada traducción
            for idx, texto_trad in traducciones_parseadas.items():
                translated_dict[idx] = ajustar_cpl_optimo(texto_trad, max_cpl=38)
                
        except Exception as e:
            # En producción, usaríamos el logger aquí
            print(f"Error en bloque {bloque[0][0]}-{bloque[-1][0]}: {str(e)}")
            # Si falla, devolvemos el original para no romper el archivo entero
            for idx, texto in bloque:
                translated_dict[idx] = f"[ERROR] {texto}"

    return translated_dict