"""Honorario Justo: observatorio de mínima cuantía en SECOP II.

Capas del paquete (las dependencias van de arriba hacia abajo):

- ``cli``: interfaz de línea de comandos.
- ``servicios``: casos de uso (analizar un proceso, corte diario, hallazgos, aprendizaje).
- ``reportes``: salidas (indicadores por día, tablero, imágenes, expediente de investigación).
- ``fuentes`` y ``almacenamiento``: sistemas externos (SECOP II, PDF/OCR) y SQLite.
- ``dominio``: reglas del negocio (qué es un cargo, un pago, el SMLV) sin dependencias de las capas anteriores.
"""

__version__ = '0.1.0'
