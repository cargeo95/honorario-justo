"""Local text extraction and conservative, reviewable evidence suggestions."""

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import fitz

from honorario_justo import config
from honorario_justo.dominio.texto import ROLE, normal

OCR_JS = Path(__file__).resolve().parent / 'ocr.cjs'


def tesseract_dirs():
    """Carpetas donde puede estar Tesseract nativo: SECOP_TESSERACT, la ruta tipica y la
    que registra el instalador de UB-Mannheim (puede venir dentro de otra app, p. ej. VectorGPT)."""
    dirs = [os.environ.get('SECOP_TESSERACT'), 'C:/Program Files/Tesseract-OCR']
    if os.name == 'nt':
        with contextlib.suppress(OSError):
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Tesseract-OCR') as key:
                dirs.append(winreg.QueryValueEx(key, 'InstallDir')[0])
    return [Path(d) for d in dirs if d]


def find_tesseract():
    """(ejecutable, carpeta tessdata con spa). Solo sirve si trae el modelo en espanol:
    en ingles el OCR de pliegos sale peor que con Tesseract.js en espanol."""
    candidates = [Path(p) for p in [shutil.which('tesseract')] if p]
    for d in tesseract_dirs():
        candidates += [d / 'tesseract.exe', d / 'bin' / 'tesseract.exe']
    for exe in (c for c in candidates if c.exists()):
        for tessdata in [exe.parent / 'tessdata', exe.parent.parent / 'tessdata']:
            if (tessdata / 'spa.traineddata').exists():
                return str(exe), str(tessdata)
    return None, None


def ocr_status():
    executable, tessdata = find_tesseract()
    modules = Path(
        os.environ.get(
            'SECOP_NODE_MODULES',
            str(Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'),
        )
    )
    js = modules / 'tesseract.js'
    model = config.OCR / 'spa.traineddata.gz'
    return {
        'available': bool(executable or (shutil.which('node') and js.exists() and model.exists())),
        'engine': 'Tesseract' if executable else 'Tesseract.js',
        'executable': executable,
        'tessdata': tessdata,
        'modules': str(modules),
        'model_ready': model.exists(),
    }


def ocr_page(page, directory, number):
    status = ocr_status()
    if not status['available']:
        raise RuntimeError('OCR no disponible: falta Tesseract o el modelo local en espanol')
    # La imagen temporal va fuera de OneDrive: si queda en data/cases/, OneDrive la bloquea
    # al sincronizar y el borrado falla con WinError 32, perdiendo el texto ya reconocido.
    fd, name = tempfile.mkstemp(prefix=f'ocr-{number}-', suffix='.png')
    os.close(fd)
    path = Path(name)
    page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(path)
    try:
        if status['executable']:
            import pytesseract
            from PIL import Image

            pytesseract.pytesseract.tesseract_cmd = status['executable']
            # Por variable y no con --tessdata-dir: pytesseract parte mal rutas con espacios.
            os.environ['TESSDATA_PREFIX'] = status['tessdata']
            with Image.open(path) as image:
                return pytesseract.image_to_string(image, lang='spa', timeout=90)
        result = subprocess.run(
            ['node', str(OCR_JS), str(path), status['modules'], str(config.OCR)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=120,
            creationflags=0x08000000 if os.name == 'nt' else 0,
        )
        if result.returncode:
            raise RuntimeError(result.stderr[-500:])
        return result.stdout
    finally:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


MONEY = re.compile(r'\$\s*\d|\b\d{1,3}(?:[.,]\d{3}){2,}\b')
PAY_WORDS = ('salario', 'honorario', 'valor mes', 'mensual', 'dedicacion', 'costo personal', 'costos de personal')
# Montos de tablas por producto/cotizacion/unidad: el valor es del entregable, no de una persona.
ITEM_LINE = re.compile(r'cotizacion|promedio|\bproducto\b|\bund\b|\bkm\b|\bevento\b|\bactividad\b')
# Encabezados de tabla de personal con columnas separadas por mucho espacio ("valor      mes")
# o rotulos de subtotal (Guadalupe, CO1.REQ.11048302: "subtotal personal profesional").
PAY_REGEX = re.compile(
    r'valor\s+mes\b|personal\s+profesional|factor\s+multiplicador|costos?\s+(?:directos?\s+)?de\s+personal'
)
# Columna de unidad "mes" seguida de la cantidad: "... mes   1   10.200.000 ...".
MES_UNIT = re.compile(r'\bmes(?:es)?\s+\d{1,2}\b')


def personnel_pay(t):
    """True si un monto (que no sea de un entregable) aparece junto a un cargo y a una palabra de remuneracion.

    Mira ~7 lineas alrededor. Las lineas de costos por item/producto se ignoran, pero la misma pagina
    puede traer ademas una tabla de personal valida.
    """
    lines = t.splitlines()
    for i, line in enumerate(lines):
        if MONEY.search(line) and not ITEM_LINE.search(line):
            window = '\n'.join(lines[max(0, i - 3) : i + 4])
            if ROLE.search(window) and (any(w in window for w in PAY_WORDS) or PAY_REGEX.search(window)):
                return True
            # Tabla de cotizaciones con unidad "mes" en la misma fila del monto y el perfil
            # en una celda de varias lineas arriba (Maripi, CO1.REQ.11051829, p. 20).
            if MES_UNIT.search(line) and ROLE.search('\n'.join(lines[max(0, i - 8) : i + 1])):
                return True
    return False


def scores(text):
    t = normal(text)
    role = bool(ROLE.search(t))
    experience = (
        role
        and bool(re.search(r'\bexperiencia\b', t))
        and bool(re.search(r'\b(anos|meses|matricula|tarjeta profesional|titulo)\b', t))
    )
    pay = personnel_pay(t)
    identity = any(w in t for w in ['municipio', 'alcaldia', 'gobernacion', 'entidad', 'instituto', 'empresa']) and any(
        w in t for w in ['objeto', 'invitacion', 'contratar', 'contratacion']
    )
    return {
        'objeto': (3 + int('objeto' in t) + int('minima cuantia' in t)) if identity else 0,
        'experiencia': (4 + int('especifica' in t) + int('equipo' in t)) if experience else 0,
        'presupuesto': (4 + int('dedicacion' in t) + int('mensual' in t)) if pay else 0,
    }


def analyze_pdf(path, folder, config):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cached = folder / f'{digest}.json'
    old = {}
    if cached.exists():
        old = json.loads(cached.read_text(encoding='utf-8'))
        if (old.get('complete') or config.get('cache_only')) and old.get('version') == 1:
            # Re-score cached text when rules change without repeating extraction or OCR.
            for page in old['pages']:
                page['scores'] = scores(page['text'])
            return old
    pages, warnings, ocr_count = [], [], 0
    with fitz.open(path) as doc:
        if doc.needs_pass:
            raise ValueError('PDF protegido con contrasena')
        limit = min(len(doc), config['max_pages'])
        if len(doc) > limit:
            warnings.append(f'PDF limitado a {limit} de {len(doc)} paginas')
        for i in range(limit):
            page = doc[i]
            text = page.get_text(sort=True)
            method = 'texto'
            if len(text.strip()) < 100:
                previous = next(
                    (
                        p
                        for p in old.get('pages', [])
                        if p['page'] == i + 1 and p.get('method') == 'ocr' and len(p.get('text', '').strip()) >= 50
                    ),
                    None,
                )
                if previous:
                    text, method = previous['text'], 'ocr'
                elif ocr_count < config['max_ocr_pages']:
                    try:
                        text = ocr_page(page, folder, i)
                        method = 'ocr'
                    except Exception as exc:
                        warnings.append(f'Pagina {i + 1}: {exc}')
                    ocr_count += 1
                else:
                    warnings.append(f'Pagina {i + 1}: limite de OCR alcanzado')
            pages.append({'page': i + 1, 'text': text, 'method': method, 'scores': scores(text)})
    result = {'version': 1, 'sha256': digest, 'pages': pages, 'warnings': warnings, 'complete': not warnings}
    cached.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return result


def render_page(pdf, page, target, crop=None):
    with fitz.open(pdf) as doc:
        if not 1 <= page <= len(doc):
            raise ValueError('Pagina fuera del documento')
        source = doc[page - 1]
        rect = source.rect
        if crop is not None:
            if not isinstance(crop, list) or len(crop) != 4 or not all(isinstance(v, (int, float)) for v in crop):
                raise ValueError('Recorte invalido')
            x0, y0, x1, y1 = crop
            if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1) or x1 - x0 < 0.03 or y1 - y0 < 0.03:
                raise ValueError('Recorte fuera de pagina o demasiado pequeno')
            rect = fitz.Rect(
                rect.x0 + x0 * rect.width,
                rect.y0 + y0 * rect.height,
                rect.x0 + x1 * rect.width,
                rect.y0 + y1 * rect.height,
            )
        scale = min(2, 3500 / max(rect.width, rect.height))
        source.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect, alpha=False).save(target)


def classification(evidence, warnings, downloaded):
    if all(evidence.get(k) for k in ['objeto', 'experiencia', 'presupuesto']):
        return 'REVISAR' if warnings else 'CANDIDATO'
    if not downloaded:
        return 'SIN_DOCUMENTOS'
    return 'REVISAR' if warnings or any(evidence.values()) else 'SIN_EVIDENCIA'
