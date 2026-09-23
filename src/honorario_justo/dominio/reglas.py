"""Reglas de lectura aprendidas de los datos.

Cada regla anota la evidencia que la respalda (ver ``Indicadores/aprendizaje/acumulado.md``).
Se aplica solo lo que los datos sostienen con claridad; lo dudoso espera más cortes.
"""

from honorario_justo.dominio.texto import normal

# 2026-09-23: 68 procesos de estos tipos (33 suministros, 27 compraventas, 4 seguros, 4 obras),
# 2.202 páginas de OCR y ninguna cifra de pago por cargo. Compran bienes, pólizas u obra a
# empresas; no desglosan honorarios de personas. Se sigue leyendo su texto digital, que es
# rápido, y el proceso cuenta en el embudo del día: solo se omite el OCR.
TIPOS_SIN_OCR = {'suministros', 'compraventa', 'seguros', 'obra'}


def omitir_ocr(metadata):
    """True si el tipo de contrato del proceso no justifica gastar OCR."""
    return normal(metadata.get('tipo_de_contrato') or '').strip() in TIPOS_SIN_OCR
