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


def experiencia(anos):
    """1 -> '1 año'; 10 -> '10 años'; 0.5 -> '6 meses'; None -> 'sin dato'."""
    if anos is None:
        return 'sin dato'
    if anos < 1:
        return f'{round(anos * 12)} meses'
    n = int(anos) if float(anos).is_integer() else anos
    return f'{n} año' if n == 1 else f'{str(n).replace(".", ",")} años'


def pesos(valor):
    return f'{valor:,.0f}'.replace(',', '.')
