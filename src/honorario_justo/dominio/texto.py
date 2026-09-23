"""Normalización de texto y vocabulario común del dominio."""

import re
import unicodedata

# Palabras que nombran un cargo en los documentos de contratación.
ROLE = re.compile(
    r'\b(director|asesor|ingeniero|profesional|especialista|coordinador|residente|consultor|topografo|geotecnista|disenador|arquitecto)\b'
)


def normal(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(text or '')) if not unicodedata.combining(c)).lower()
