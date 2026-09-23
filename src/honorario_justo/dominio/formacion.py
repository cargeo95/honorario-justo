"""Nivel de formación exigido para un cargo: pregrado + posgrado, pregrado, tecnólogo o técnico.

Se lee del texto de requisitos de ESE cargo (celda de formación, fila del presupuesto o
párrafo del perfil). No se guarda el título en sí (eso ya lo dice el cargo), solo el nivel.
Sin marcas claras devuelve None: mejor sin dato que adivinado.
"""

import re

from honorario_justo.dominio.texto import normal

POSGRADO = 'Pregrado + posgrado'
PREGRADO = 'Pregrado'
TECNOLOGO = 'Tecnólogo'
TECNICO = 'Técnico'

_POSGRADO = re.compile(
    r'especializa(?:cion|do|da)|especialista en|medico especialista|maestria|magister|doctorado|posgrado|postgrado'
)
_TECNOLOGO = re.compile(r'\btecnolog[oa]s?\b')
_TECNICO = re.compile(r'\btecnic[oa]s?\s+(?:profesional|laboral|en)\b')
_PREGRADO = re.compile(
    r'\bprofesional\b|pregrado|ingenier|licenciad|\bmedic[oa]\b|abogad|psicolog|contador|administrador|'
    r'arquitect|economista|trabajador social|trabajo social|sociolog|enfermer|nutricion|fisioterap'
)
# "Diploma de pregrado o acta de grado, diploma de postgrado": es cómo acreditar estudios,
# no un requisito del cargo (Guapotá, Estudios Previos p.36). Se quita antes de clasificar.
_ACREDITACION = re.compile(r'diploma[^.;•\n]*')
# "Experiencia profesional mínima de 12 meses" habla de experiencia, no de formación.
_EXPERIENCIA_PROFESIONAL = re.compile(r'experiencia\s+profesional')


def nivel_formacion(texto):
    """Nivel mínimo exigido según el texto de requisitos del cargo, o None."""
    t = _EXPERIENCIA_PROFESIONAL.sub('experiencia', _ACREDITACION.sub(' ', normal(texto or '')))
    if _POSGRADO.search(t):
        return POSGRADO
    # "Profesional o tecnólogo": el mínimo exigido es tecnólogo.
    if _TECNOLOGO.search(t):
        return TECNOLOGO
    if _TECNICO.search(t):
        return TECNICO
    if _PREGRADO.search(t):
        return PREGRADO
    return None
