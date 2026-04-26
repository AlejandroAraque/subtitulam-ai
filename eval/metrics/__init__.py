"""
Cada métrica vive en su propio módulo y expone una función `compute()`
con interfaz uniforme:

    compute(predictions: list[str], references: list[str], **kwargs) -> dict

Esto facilita registrar métricas nuevas (BERTScore en la fase 7) sin
tocar el runner.
"""
