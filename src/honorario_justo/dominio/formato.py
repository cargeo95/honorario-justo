"""Formato de cifras y fechas en español para reportes y publicaciones."""

MESES = [
    'enero',
    'febrero',
    'marzo',
    'abril',
    'mayo',
    'junio',
    'julio',
    'agosto',
    'septiembre',
    'octubre',
    'noviembre',
    'diciembre',
]


def pesos(valor):
    return f'{valor:,.0f}'.replace(',', '.')
