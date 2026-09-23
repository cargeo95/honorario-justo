"""Extraccion determinista (sin IA) de cargo/experiencia/pago desde el texto y las
tablas ya cacheadas por fuentes/documentos.py, con un respaldo de Ollama local solo para lo
que la regla no resuelve. Cada resultado queda marcado con el metodo que lo genero
y debe pasar filtros de plausibilidad antes de contar como hallazgo.

No descarga nada nuevo: opera sobre data/cases/<hash>/documents y /text ya existentes.
"""

import hashlib
import json
import re
import urllib.request

import pdfplumber

from honorario_justo import config
from honorario_justo.dominio.texto import ROLE, normal

EXPERIENCE = re.compile(r'\((\d{1,2})\)\s*(anos|ano|meses|mes)\b')
# "diez (10) anos" o "minimo 3 anos": el numero entre parentesis es el que manda.
YEARS_ANY = re.compile(r'(?:\((\d{1,2})\)|\b(\d{1,2}))\s*anos?\b')
# Cargo completo: la palabra de rol (ROLE de dominio/texto.py) mas su especialidad,
# p. ej. "ingeniero residente de interventoria" o "especialista en geotecnia".
ROLE_LABEL = re.compile(
    r'\b(?:ingeniero\s+)?(?:director|asesor|ingeniero|profesional|especialista|coordinador|'
    r'residente|consultor|topografo|geotecnista|disenador|arquitecto)'
    r'(?:\s+(?:de|del|en|la|el|y|[a-z]{3,})\b){0,4}'
)  # \b: "el" no es "el|ectricista"
ROLE_STOP = {
    'con',
    'que',
    'cuya',
    'quien',
    'debe',
    'debera',
    'minimo',
    'minima',
    'experiencia',
    'titulo',
    'anos',
    'contados',
    'mensual',
    'salario',
    'valor',
    'dedicacion',
    'para',
    'por',
    'sera',
    'como',
    'fecha',
    'area',
    'areas',
    'publicas',
    'privadas',
    'seleccion',
    'acreditar',
    'contrato',
    'contratos',
    'general',
    'especifica',
    'y',
    'o',
    'cual',
    'incluye',
    'item',
}
# Palabras que no distinguen un perfil de otro al cruzar requisitos con presupuesto.
GENERIC_TOKENS = {
    'de',
    'del',
    'en',
    'la',
    'el',
    'y',
    'ingeniero',
    'ingeniera',
    'profesional',
    'civil',
    'general',
    'especifica',
    'con',
    'o',
    'los',
    'las',
}

# SMLV por ano de publicacion. 2026: Decreto 1469/2025, suspendido en febrero (el 0159/2026
# fijo el mismo valor de forma transitoria) y vuelto a regir cuando el Consejo de Estado
# revoco la suspension (17-jul-2026). El proceso de nulidad sigue: revisar si hay sentencia.
SMLV = {2023: 1_160_000, 2024: 1_300_000, 2025: 1_423_500, 2026: 1_750_905}
SMLV_FUENTE = {
    2023: 'Decreto 2613/2022',
    2024: 'Decreto 2292/2023',
    2025: 'Decreto 1572/2024',
    2026: 'Decreto 1469/2025',
}
# Por encima de esto un "pago mensual" de una persona es sospechoso (filas de tabla
# pegadas, valor total leido como mensual): no se descarta, pero baja la confianza.
SMLV_SOSPECHOSO = 15
SMLV_DIRECT = re.compile(r'([\d]+[.,]?\d*)\s*(?:veces el )?salarios?\s*minimos?\s*mensuales?\s*vigentes')
MONEY_INLINE = re.compile(r'\$\s*([\d]{1,3}(?:[.,]\d{3})+(?:[.,]\d{2})?)')
# "salario base" y "vr. unitario" (Villa de Leyva: tabla de personal con columna de meses),
# "al mes"/"por mes" (Sutamarchan: "... al mes por $X").
# "costos directos de personal / personal profesional ... factor multiplicador": formato estandar
# de interventoria/consultoria con columnas numeradas (Guapota: $2.800.000, 20%, 3 meses, parcial).
MONTHLY_HINT = re.compile(
    r'mensual|valor\s*mes|h/mes|costo\s*mes|salario\s*base|v(?:alo)?r\.?\s*unitario'
    r'|costos?\s+directos?\s+de\s+personal|personal\s+profesional|factor\s+multiplicador'
    r'|\bal\s+mes\b|\bpor\s+mes\b'
)
# Monto que incluye prestaciones ("salario base mas factor prestacional del 72,81% al mes por
# $7.795.717"): no es el pago base comparable; el documento suele dar despues el valor "sin el factor".
CON_FACTOR = re.compile(r'(?:mas|con|incluye)\b[^.$]{0,60}factor\s+prestacional')

# Piso de plausibilidad: muy por debajo de cualquier SMLV reciente a proposito
# (solo filtra lo absurdo: porcentajes o lineas sueltas mal leidas como sueldo).
# Confirmar el SMLV vigente exacto antes de usar esta cifra para cualquier calculo real.
MIN_PLAUSIBLE_PAY = 1_300_000
MAX_PLAUSIBLE_PAY = 60_000_000  # por si una tabla mal leida junta varias filas en una.

OLLAMA_URL = 'http://localhost:11434/api/generate'
OLLAMA_MODEL = 'gemma3:4b'
OLLAMA_PROMPT = '''Eres un extractor de datos de estudios previos de contratacion publica colombiana.
Del siguiente fragmento, identifica cargos/perfiles profesionales y su pago MENSUAL
(salario, honorario o valor mes), si aparece explicitamente. No inventes cifras que no
esten en el texto. No confundas el valor TOTAL de un contrato con el pago mensual de
una persona. Responde SOLO con JSON valido, sin explicacion, con esta forma exacta:
{"cargos": [{"cargo": "...", "pago_mensual_cop": 0, "dedicacion": "..."}]}
Si no hay ningun pago mensual explicito en el texto, responde {"cargos": []}.

TEXTO:
"""%s"""'''


def es_rol_valido(texto):
    return bool(ROLE.search(normal(texto or '')))


def parse_money_co(texto):
    """Convierte '6.829.554,00' o '3,700,000.00' o '5.000.000' a float de pesos."""
    s = re.sub(r'[^\d.,]', '', texto or '')
    if not s:
        return None
    decimal = re.match(r'^(.*?)[.,](\d{2})$', s)
    entero = re.sub(r'[.,]', '', decimal.group(1) if decimal else s)
    return float(entero) if entero else None


def pago_plausible(valor):
    return isinstance(valor, (int, float)) and MIN_PLAUSIBLE_PAY <= valor <= MAX_PLAUSIBLE_PAY


def case_folder(case_id):
    data = config.DATA
    return data / 'cases' / hashlib.sha256(case_id.encode()).hexdigest()[:24]


def smlv_de(fecha):
    """SMLV del ano de publicacion ('2026-09-21T..' -> 1750905). None si el ano no esta en la tabla."""
    try:
        return SMLV.get(int(str(fecha)[:4]))
    except ValueError:
        return None


def en_smlv(pago, fecha):
    base = smlv_de(fecha)
    return round(pago / base, 2) if pago and base else None


def _cached_text(case_id, filename):
    doc_path = case_folder(case_id) / 'documents' / filename
    if not doc_path.exists():
        return {}
    digest = hashlib.sha256(doc_path.read_bytes()).hexdigest()
    text_file = case_folder(case_id) / 'text' / f'{digest}.json'
    if not text_file.exists():
        return {}
    return json.loads(text_file.read_text(encoding='utf-8'))


def cached_pages(case_id, filename):
    return {p['page']: p['text'] for p in _cached_text(case_id, filename).get('pages', [])}


def page_method(case_id, filename, page):
    """'texto' u 'ocr', segun como fuentes/documentos.py obtuvo el texto de esa pagina."""
    for p in _cached_text(case_id, filename).get('pages', []):
        if p['page'] == page:
            return p.get('method', 'texto')
    return 'texto'


def role_label(texto):
    """Primer cargo completo en el texto, recortado antes de palabras que ya no son
    parte del nombre ('director de interventoria con experiencia...' -> 'director de interventoria')."""
    m = ROLE_LABEL.search(normal(texto or ''))
    if not m:
        return ''
    words = []
    for w in m.group(0).split():
        if w in ROLE_STOP:
            break
        words.append(w)
    while words and words[-1] in {'de', 'del', 'en', 'la', 'el', 'y'}:
        words.pop()
    return ' '.join(words)


def role_tokens(cargo):
    return {w for w in re.findall(r'[a-z]+', normal(cargo)) if w not in GENERIC_TOKENS and len(w) > 2}


def confianza(item, method):
    """alta: tabla vectorial; media: regex sobre texto digital; baja: OCR, IA o pago sospechoso."""
    if item.get('pago_smlv') and item['pago_smlv'] > SMLV_SOSPECHOSO:
        return 'baja'
    if item['metodo'] == 'tabla_pdfplumber':
        return 'alta'
    if item['metodo'] == 'regex_texto' and method == 'texto':
        return 'media'
    return 'baja'


# Contexto (no juicio): de donde sale el precio. Citar una tabla oficial NO implica pagar
# bien: Villa de Leyva usa la Tabla de Precios de la Gobernacion de Boyaca y paga 2,58 SMLV
# a un disenador con 15+ anos; Maripi paga 5,34 SMLV por 5 anos con precios de cotizacion.
REFERENCIA_TARIFA = re.compile(
    r'(tabla\s+(?:oficial\s+)?de\s+(?:precios|tarifas|honorarios)[^\n.;]{0,90}'
    r'|manual\s+de\s+tarifas[^\n.;]{0,90}'
    r'|tarifas?\s+(?:de\s+(?:la\s+)?)?(?:sociedad\s+colombiana\s+de\s+(?:ingenieros|arquitectos)|sci\b)[^\n.;]{0,60}'
    r'|resolucion\s+(?:no\.?\s*)?\d+[^\n.;]{0,40}(?:precios|tarifas|honorarios)[^\n.;]{0,60})'
)


def referencia_tarifa(case_id):
    """Primera mencion a una tabla oficial de tarifas en cualquier documento del caso
    (texto normalizado, recortado), o '' si no hay."""
    text_dir = case_folder(case_id) / 'text'
    for text_file in sorted(text_dir.glob('*.json')) if text_dir.exists() else []:
        try:
            pages = json.loads(text_file.read_text(encoding='utf-8')).get('pages', [])
        except (OSError, ValueError):
            continue
        for p in pages:
            m = REFERENCIA_TARIFA.search(normal(p.get('text', '')))
            if m:
                return re.sub(r'\s+', ' ', m.group(0)).strip()[:140]
    return ''


def page_window(case_id, filename, page, radius=1):
    pages = cached_pages(case_id, filename)
    return '\n'.join(pages[p] for p in range(page - radius, page + radius + 1) if p in pages)


def extract_experience_years(case_id, filename, page):
    text = page_window(case_id, filename, page)
    t = normal(text)
    hits = [int(m.group(1)) for m in EXPERIENCE.finditer(t) if m.group(2).startswith('ano')]
    return sorted(set(hits), reverse=True)


def parse_requisitos_texto(texto):
    """Requisitos por cargo desde texto plano: recorre cargos y anos en orden de
    aparicion y le asigna a cada cargo el PRIMER numero de anos que aparece despues
    de el y antes del siguiente cargo (en los estudios previos ese primero es la
    experiencia general; la especifica viene despues). Cargo sin anos -> no se reporta."""
    t = normal(texto)
    events = [(m.start(), 'rol', m) for m in ROLE_LABEL.finditer(t)]
    events += [(m.start(), 'anos', m) for m in YEARS_ANY.finditer(t)]
    events.sort(key=lambda e: e[0])
    requisitos, actual = [], None
    for _, kind, m in events:
        if kind == 'rol':
            label = role_label(m.group(0))
            # "profesional" o "ingeniero" solos dentro de una frase de requisito
            # ("experiencia profesional general") no abren un cargo nuevo.
            if not role_tokens(label) and actual is not None and actual.get('anos') is None:
                continue
            actual = {'cargo': label, 'anos': None}
            requisitos.append(actual)
        elif actual is not None and actual['anos'] is None:
            n = int(m.group(1) or m.group(2))
            if 1 <= n <= 40:
                actual['anos'] = n
    return [r for r in requisitos if r['anos'] is not None and r['cargo']]


def parse_tabla_requisitos(page):
    """Igual que parse_tabla_personal pero para la tabla de perfiles: columna de
    cargo + columna de experiencia; toma los anos de la celda de experiencia."""
    requisitos = []
    for table in page.extract_tables():
        if len(table) < 2:
            continue
        labels = _header_labels(table)
        col_cargo = _find_column(labels, HEADER_KEYWORDS['cargo'])
        col_exp = _find_column(labels, ('experiencia',), exclude=('especifica',))
        if col_exp is None:
            col_exp = _find_column(labels, ('experiencia',))
        if col_exp is None:
            # Villa de Leyva: "CARGO | PERFIL Y/O FORMACION" con los anos dentro del perfil.
            col_exp = _find_column(labels, ('perfil', 'formacion', 'requisito'))
        if col_cargo is None or col_exp is None or col_cargo == col_exp:
            continue
        for row in table[1:]:
            if max(col_cargo, col_exp) >= len(row) or not row[col_cargo]:
                continue
            cargo = role_label(row[col_cargo]) or ''
            m = YEARS_ANY.search(normal(row[col_exp] or ''))
            if cargo and m:
                requisitos.append({'cargo': cargo, 'anos': int(m.group(1) or m.group(2))})
    return requisitos


def extract_requirements(case_id, filename, page_number):
    """Requisitos (cargo, anos) de una pagina candidata de experiencia: tabla si
    pdfplumber la encuentra; si no, texto de la pagina y la siguiente (la tabla de
    perfiles se corta entre paginas)."""
    doc_path = case_folder(case_id) / 'documents' / filename
    if doc_path.exists():
        try:
            with pdfplumber.open(doc_path) as pdf:
                if 1 <= page_number <= len(pdf.pages):
                    found = parse_tabla_requisitos(pdf.pages[page_number - 1])
                    if found:
                        return [dict(r, metodo='tabla_pdfplumber') for r in found]
        except Exception:
            pass
    pages = cached_pages(case_id, filename)
    text = '\n'.join(pages[p] for p in (page_number, page_number + 1) if p in pages)
    return [dict(r, metodo='texto') for r in parse_requisitos_texto(text)]


def match_requirement(cargo, requisitos):
    """Anos exigidos para ESE cargo: el requisito que comparte mas palabras
    distintivas (director, residente, interventoria, geotecnia...). Empate entre
    requisitos con anos distintos -> None: mejor sin dato que con el de otro perfil."""
    tokens = role_tokens(cargo)
    if not tokens:
        return None
    scored = [(len(tokens & role_tokens(r['cargo'])), r) for r in requisitos]
    best = max((s for s, _ in scored), default=0)
    if best == 0:
        return None
    ganadores = {r['anos'] for s, r in scored if s == best}
    return ganadores.pop() if len(ganadores) == 1 else None


HEADER_KEYWORDS = {
    'cargo': ('cargo', 'perfil', 'oficio'),
    'pago': ('salario', 'honorario', 'mensual'),
    'dedicacion': ('dedicacion',),
    'duracion': ('duracion', 'tiempo', 'h/mes'),
}
PAGO_EXCLUDE = ('total', 'parcial', 'unitario')


def _header_labels(table):
    """Junta hasta 2 filas de encabezado (las tablas de SECOP suelen partir
    'VALOR\\nMENSUAL' en dos filas fisicas por celdas combinadas)."""
    rows = table[:2]
    labels = []
    for col in range(max(len(r) for r in rows)):
        parts = [normal(r[col]) for r in rows if col < len(r) and r[col]]
        labels.append(' '.join(parts))
    return labels


def _find_column(labels, keywords, exclude=()):
    for i, label in enumerate(labels):
        if any(k in label for k in keywords) and not any(e in label for e in exclude):
            return i
    return None


def parse_tabla_personal(page):
    """Busca, entre las tablas que pdfplumber detecta en la pagina, la tabla de
    personal profesional (encabezado con CARGO/PERFIL y SALARIO/MENSUAL) y devuelve
    las filas cuyo cargo matchea un rol profesional real y cuyo pago es plausible."""
    hallazgos = []
    for table in page.extract_tables():
        if len(table) < 2:
            continue
        labels = _header_labels(table)
        col_cargo = _find_column(labels, HEADER_KEYWORDS['cargo'])
        col_pago = _find_column(labels, HEADER_KEYWORDS['pago'], exclude=PAGO_EXCLUDE)
        if col_cargo is None or col_pago is None:
            continue
        col_dedic = _find_column(labels, HEADER_KEYWORDS['dedicacion'])
        col_dur = _find_column(labels, HEADER_KEYWORDS['duracion'])
        for row in table[2:]:
            if col_cargo >= len(row) or col_pago >= len(row):
                continue
            cargo = row[col_cargo]
            if not cargo or not es_rol_valido(cargo):
                continue
            pago = parse_money_co(row[col_pago])
            if not pago_plausible(pago):
                continue
            hallazgos.append(
                {
                    'cargo': re.sub(r'\s+', ' ', cargo).strip(),
                    'pago_mensual_cop': pago,
                    'dedicacion': (row[col_dedic] or '').strip()
                    if col_dedic is not None and col_dedic < len(row)
                    else '',
                    'duracion': (row[col_dur] or '').strip() if col_dur is not None and col_dur < len(row) else '',
                    'metodo': 'tabla_pdfplumber',
                }
            )
    return hallazgos


def extract_pay_table(case_id, filename, page_number):
    doc_path = case_folder(case_id) / 'documents' / filename
    if not doc_path.exists():
        return []
    try:
        with pdfplumber.open(doc_path) as pdf:
            if not 1 <= page_number <= len(pdf.pages):
                return []
            return parse_tabla_personal(pdf.pages[page_number - 1])
    except Exception:
        return []


def extract_pay_regex(case_id, filename, page_number):
    """Respaldo de texto plano cuando no hay tabla parseable: monto junto a 'mensual'
    y a un cargo real (mismo criterio que evidence.personnel_pay), en una ventana de
    +/-3 lineas. Un monto sin cargo cercano no se devuelve: no sirve para comparar
    experiencia-vs-pago por perfil, y queda para que lo intente el respaldo de IA.

    En paginas escaneadas el OCR desalinea columnas, asi que ahi la ventana se
    reduce a +/-1 linea: un cargo tres lineas arriba casi nunca es el de ese monto."""
    text = page_window(case_id, filename, page_number)
    t = normal(text)
    lines = t.splitlines()
    radius = 1 if page_method(case_id, filename, page_number) == 'ocr' else 3
    hallazgos = []
    for i, line in enumerate(lines):
        montos = list(MONEY_INLINE.finditer(line))
        # Varias columnas de dinero en la fila (vr. unitario, vr. parcial, total): el pago
        # por persona es la primera; las siguientes son productos de ella.
        for m in montos[:1]:
            previo = ' '.join(lines[max(0, i - 3) : i]) + ' ' + line[: m.start()]
            previo = previo[previo.rfind('. ') + 1 :] if '. ' in previo else previo
            if CON_FACTOR.search(previo) and 'sin el factor' not in previo:
                continue
            # "total costos de personal $7.910.878": suma de cargos, no el pago de uno.
            if re.search(r'\b(?:sub)?total\b', line[: m.start()]):
                continue
            # El cargo mas cercano al monto: misma linea primero, luego hacia arriba.
            cercanas = [line] + [lines[j] for d in range(1, radius + 1) for j in (i - d, i + d) if 0 <= j < len(lines)]
            cargo = next((role_label(c) for c in cercanas if ROLE.search(c)), '')
            # La pista ("valor mes", "personal profesional"...) es del encabezado de la tabla: en
            # filas del medio queda lejos (Guadalupe: electricista y arquitecto a 4-5 lineas).
            # Se busca hasta 12 lineas arriba; el cargo si debe estar pegado al monto.
            window = '\n'.join(lines[max(0, i - 12) : i + radius + 1])
            if cargo and MONTHLY_HINT.search(window):
                valor = parse_money_co(m.group(1))
                if pago_plausible(valor):
                    hallazgos.append(
                        {
                            'cargo': cargo,
                            'pago_mensual_cop': valor,
                            'dedicacion': '',
                            'duracion': '',
                            'metodo': 'regex_texto',
                        }
                    )
    return hallazgos


MONEY_BARE = re.compile(r'(?<![\d.,])\d{1,3}(?:\.\d{3}){2,}(?:,\d{2})?(?![\d.,])')
MES_UNIT = re.compile(r'\bmes(?:es)?\s+\d{1,2}\b')


def extract_pay_filas(case_id, filename, page_number):
    """Tablas de analisis de precios por cotizaciones (Maripi, CO1.REQ.11051829, p. 20):

        profesional biologo con especializacion en ...
        ... experiencia general minima requerida de cinco (05) anos ...
                                   $        $        $        $
        ...                 mes  1   10.200.000  8.500.000  9.350.000  9.350.000

    Monto sin '$', unidad 'mes' en la fila (o la linea de arriba), perfil en una celda de
    varias lineas por encima. Con 3+ montos se toma el penultimo (valor unitario = promedio
    de cotizaciones); el ultimo es el total (unitario x cantidad). Los anos exigidos salen
    de la misma fila, asi que quedan ligados al cargo sin cruce adicional."""
    if page_method(case_id, filename, page_number) == 'ocr':
        return []  # columnas de OCR demasiado desalineadas para este formato
    lines = normal(cached_pages(case_id, filename).get(page_number, '')).splitlines()
    hallazgos, previo = [], -1
    for i, line in enumerate(lines):
        montos = MONEY_BARE.findall(line)
        if not montos:
            continue
        if not (MES_UNIT.search(line) or (i and MES_UNIT.search(lines[i - 1]))):
            previo = i
            continue
        inicio = next((j for j in range(i - 1, max(previo, i - 11), -1) if ROLE.search(lines[j])), None)
        previo = i
        if inicio is None:
            continue
        cargo = role_label(lines[inicio])
        valor = parse_money_co(montos[-2] if len(montos) >= 3 else montos[0])
        if not cargo or not pago_plausible(valor):
            continue
        fin = next(
            (j for j in range(i + 1, min(len(lines), i + 8)) if ROLE.search(lines[j]) or MONEY_BARE.search(lines[j])),
            i + 8,
        )
        anos = YEARS_ANY.search('\n'.join(lines[inicio:fin]))
        hallazgos.append(
            {
                'cargo': cargo,
                'pago_mensual_cop': valor,
                'dedicacion': '',
                'duracion': '',
                'metodo': 'regex_texto',
                'anos_fila': int(anos.group(1) or anos.group(2)) if anos else None,
            }
        )
    return hallazgos


def extract_pay_ollama(case_id, filename, page_number):
    """Ultimo recurso, local. Se marca 'metodo': 'ia_ollama' y siempre debe pasar
    por es_rol_valido()/pago_plausible() antes de contar como hallazgo."""
    text = page_window(case_id, filename, page_number)
    if not text.strip():
        return []
    body = json.dumps(
        {
            'model': OLLAMA_MODEL,
            'prompt': OLLAMA_PROMPT % text[:4000],
            'stream': False,
            'format': 'json',
            'options': {'temperature': 0},
        }
    ).encode('utf-8')
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = json.loads(resp.read())['response']
        parsed = json.loads(raw)
    except Exception:
        return []
    hallazgos = []
    for c in parsed.get('cargos', []):
        cargo, pago = c.get('cargo', ''), c.get('pago_mensual_cop')
        if es_rol_valido(cargo) and pago_plausible(pago):
            hallazgos.append(
                {
                    'cargo': cargo.strip(),
                    'pago_mensual_cop': float(pago),
                    'dedicacion': str(c.get('dedicacion', '')),
                    'duracion': '',
                    'metodo': 'ia_ollama',
                }
            )
    return hallazgos


def extract_pay(case_id, filename, page_number, use_ollama=True):
    """Orden: tabla (mas confiable) -> regex de texto -> Ollama local, solo si las
    anteriores no encontraron nada."""
    method = page_method(case_id, filename, page_number)
    hallazgos = (
        extract_pay_table(case_id, filename, page_number)
        or extract_pay_filas(case_id, filename, page_number)
        or extract_pay_regex(case_id, filename, page_number)
        or (extract_pay_ollama(case_id, filename, page_number) if use_ollama else [])
    )
    return [dict(h, metodo_pagina=method) for h in hallazgos]
