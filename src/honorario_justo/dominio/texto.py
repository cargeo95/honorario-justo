"""Normalización de texto y vocabulario común del dominio."""

import re
import unicodedata

# Palabras que nombran un cargo en los documentos de contratación. Empezó solo con cargos de
# ingeniería; el 18-sep-2026 los de salud mental de Putumayo (psicólogo, psiquiatra,
# facilitador) salían como "profesional" o "especialista" genérico.
CARGOS = (
    'director', 'asesor', 'ingeniero', 'profesional', 'especialista', 'coordinador', 'residente', 'consultor',
    'topografo', 'geotecnista', 'disenador', 'arquitecto', 'interventor', 'supervisor', 'inspector',
    'psicologo', 'psiquiatra', 'medico', 'enfermero', 'nutricionista', 'fisioterapeuta', 'trabajador social',
    'facilitador', 'docente', 'sociologo', 'abogado', 'contador', 'administrador', 'economista',
    'tecnologo', 'tecnico', 'auxiliar',
)  # fmt: skip
ROLE = re.compile(r'\b(' + '|'.join(CARGOS) + r')\b')


def normal(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(text or '')) if not unicodedata.combining(c)).lower()
