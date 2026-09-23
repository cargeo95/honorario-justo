"""Rutas y registro de la aplicación.

Todo lo que depende del entorno vive aquí: carpetas de datos, resultados, indicadores y
logs (sobrescribibles con variables de entorno) y la configuración del logging. Los demás
módulos leen estas rutas en tiempo de ejecución (``config.DATA``), así las pruebas pueden
apuntarlas a una carpeta temporal.
"""

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get('SECOP_DATA', ROOT / 'data')).resolve()
LOGS = Path(os.environ.get('SECOP_LOGS', ROOT / 'logs')).resolve()
RESULTADOS = Path(os.environ.get('SECOP_RESULTS', ROOT / 'Resultados')).resolve()
INDICADORES = Path(os.environ.get('SECOP_INDICADORES', ROOT / 'Indicadores')).resolve()
# Modelo de Tesseract en español: se descarga una vez con scripts/setup_ocr.py.
OCR = Path(os.environ.get('SECOP_OCR', ROOT / 'data' / 'ocr')).resolve()


def indicadores_folder(root=None):
    folder = Path(root or INDICADORES).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def configurar_logging():
    """logs/observatorio.log: todo (rota a diario, 30 días). logs/errores.log: solo WARNING+,
    para ver de un vistazo qué falló (descargas, CAPTCHA, OCR, PDF ilegibles, excepciones)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    LOGS.mkdir(parents=True, exist_ok=True)
    formato = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    todo = TimedRotatingFileHandler(LOGS / 'observatorio.log', when='midnight', backupCount=30, encoding='utf-8')
    errores = logging.FileHandler(LOGS / 'errores.log', encoding='utf-8')
    errores.setLevel(logging.WARNING)
    consola = logging.StreamHandler()
    for handler in (todo, errores, consola):
        handler.setFormatter(formato)
    logging.basicConfig(level=logging.INFO, force=True, handlers=[todo, errores, consola])
