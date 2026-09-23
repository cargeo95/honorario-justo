"""Aprendizaje a partir de la revision humana: donde aparecen de verdad las cifras de pago.

Cada hallazgo marcado confirmado/descartado deja una etiqueta con el documento, la pagina,
los titulos y las senales de esa pagina. El reporte cruza esas etiquetas y el costo de lectura
(paginas de OCR) por tipo de contrato y segmento UNSPSC. No cambia nada solo: muestra la
evidencia para decidir reglas (que saltar, que priorizar) cuando haya suficientes revisiones.
"""

import json
import re
from collections import defaultdict
from datetime import datetime, timezone

from honorario_justo.dominio.texto import normal

# Con menos etiquetas que esto el reporte muestra conteos pero no sugiere reglas.
MIN_ETIQUETAS = 30
# Procesos analizados minimos en un grupo (tipo/segmento) para sugerir bajarle prioridad.
MIN_PROCESOS_GRUPO = 20

SENALES = {
    'costos_directos_personal': r'costos?\s+directos?\s+de\s+personal',
    'factor_multiplicador': r'factor\s+multiplicador',
    'honorarios': r'honorarios',
    'valor_mensual': r'valor\s+mensual|mensual(?:es)?\s*\$|salario\s+mensual',
    'dedicacion': r'dedicacion',
    'personal_profesional': r'personal\s+profesional|equipo\s+de\s+trabajo|perfil(?:es)?\s+requerid',
    'presupuesto_oficial': r'presupuesto\s+oficial',
    'cotizacion': r'cotizacion(?:es)?',
    'poliza_garantias': r'poliza|garantia|amparo',
    'riesgos': r'matriz\s+de\s+riesgo|riesgos?\s+previsibles',
    'conclusion': r'conclusion',
    'experiencia': r'experiencia\s+(?:general|especifica|minima|profesional)',
    'smmlv': r'smmlv|smlmv|salarios?\s+minimos',
}
_SENALES = {k: re.compile(v) for k, v in SENALES.items()}

TIPOS_DOCUMENTO = [
    ('estudio_sector', r'estudio.{0,4}(?:del\s+)?sector|analisis\s+del\s+sector'),
    ('estudio_previo', r'estudio.{0,4}previo'),
    ('invitacion', r'invitacion|pliego|aviso'),
    ('anexo', r'anexo|formato|ficha'),
    ('presupuesto', r'presupuesto|cotizacion|costos'),
]

SEGMENTOS = {
    '70': 'agro/pesca',
    '71': 'mineria/petroleo',
    '72': 'construccion y mantenimiento',
    '73': 'produccion industrial',
    '76': 'aseo y limpieza',
    '77': 'medio ambiente',
    '78': 'transporte y logistica',
    '80': 'gestion y administracion',
    '81': 'ingenieria e investigacion',
    '82': 'editorial y diseno',
    '83': 'servicios publicos',
    '84': 'financieros y seguros',
    '85': 'salud',
    '86': 'educacion y capacitacion',
    '90': 'viajes, alimentacion y eventos',
    '91': 'servicios personales',
    '92': 'defensa y seguridad',
    '93': 'politicos y civicos',
    '94': 'organizaciones',
    '95': 'terrenos y edificios',
}


def init(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS etiquetas (hallazgo_id INTEGER PRIMARY KEY,
        proceso_id TEXT NOT NULL, estado TEXT NOT NULL, documento TEXT, tipo_documento TEXT,
        pagina INTEGER, total_paginas INTEGER, metodo_pagina TEXT, titulos TEXT, senales TEXT,
        tipo_contrato TEXT, segmento TEXT, created_at TEXT NOT NULL)""")


def segmento(metadata):
    m = re.search(r'(\d{2})\d{6}', metadata.get('codigo_principal_de_categoria') or '')
    return m.group(1) if m else ''


def nombre_segmento(seg):
    if not seg:
        return 'sin codigo'
    if int(seg) < 70:
        return f'{seg} bienes'
    return f'{seg} {SEGMENTOS.get(seg, "servicios")}'


def tipo_documento(nombre):
    n = normal(nombre or '')
    return next((tipo for tipo, pat in TIPOS_DOCUMENTO if re.search(pat, n)), 'otro')


def titulos(texto, maximo=8):
    """Lineas cortas mayormente en mayuscula: encabezados de seccion de la pagina."""
    salida = []
    for linea in texto.splitlines():
        linea = linea.strip()
        letras = [c for c in linea if c.isalpha()]
        if 6 <= len(linea) <= 90 and len(letras) >= 5 and sum(c.isupper() for c in letras) / len(letras) > 0.7:
            t = re.sub(r'\s+', ' ', normal(linea)).strip(' .:-')
            if t and t not in salida:
                salida.append(t)
        if len(salida) >= maximo:
            break
    return salida


def senales(texto):
    n = normal(texto)
    return sorted(k for k, pat in _SENALES.items() if pat.search(n))


def pagina_cacheada(case_folder, case, archivo, pagina):
    """(texto, metodo, total_paginas, nombre_documento) de la cache de texto del analisis."""
    doc = next((d for d in case['analysis'].get('documents', []) if d['file'] == archivo), None)
    if not doc or not doc.get('sha256'):
        return '', '', None, archivo
    path = case_folder / 'text' / f'{doc["sha256"]}.json'
    if not path.exists():
        return '', '', doc.get('pages'), doc.get('name', archivo)
    pages = json.loads(path.read_text(encoding='utf-8'))['pages']
    page = next((p for p in pages if p['page'] == pagina), {})
    return page.get('text', ''), page.get('method', ''), len(pages), doc.get('name', archivo)


def etiquetar(conn, hallazgo, case, case_folder):
    """Guarda (o borra, si vuelve a sin_revisar) la etiqueta de un hallazgo revisado."""
    init(conn)
    if hallazgo['estado'] not in {'confirmado', 'descartado'}:
        conn.execute('DELETE FROM etiquetas WHERE hallazgo_id=?', (hallazgo['id'],))
        return None
    texto, metodo, total, nombre = pagina_cacheada(
        case_folder, case, hallazgo['archivo_fuente'], hallazgo['pagina_fuente']
    )
    fila = {
        'hallazgo_id': hallazgo['id'],
        'proceso_id': hallazgo['proceso_id'],
        'estado': hallazgo['estado'],
        'documento': nombre,
        'tipo_documento': tipo_documento(nombre),
        'pagina': hallazgo['pagina_fuente'],
        'total_paginas': total,
        'metodo_pagina': metodo,
        'titulos': json.dumps(titulos(texto), ensure_ascii=False),
        'senales': json.dumps(senales(texto)),
        'tipo_contrato': case['metadata'].get('tipo_de_contrato', ''),
        'segmento': segmento(case['metadata']),
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    conn.execute(
        f'INSERT OR REPLACE INTO etiquetas ({",".join(fila)}) VALUES ({",".join("?" * len(fila))})',
        tuple(fila.values()),
    )
    return fila


def costo_lectura(case_folder, case):
    """(paginas totales, paginas leidas con OCR) del caso, desde la cache de texto."""
    total = ocr = 0
    for doc in case['analysis'].get('documents', []):
        path = case_folder / 'text' / f'{doc.get("sha256")}.json'
        if doc.get('sha256') and path.exists():
            pages = json.loads(path.read_text(encoding='utf-8'))['pages']
            total += len(pages)
            ocr += sum(p.get('method') == 'ocr' for p in pages)
    return total, ocr


def _pct(a, b):
    return f'{100 * a / b:.0f}%' if b else '-'


def reporte(cases, hallazgos, etiquetas, folder_for):
    """Datos del reporte. cases: casos analizados (unpacked); hallazgos: filas de 'hallazgos'
    (no descartados cuentan como cifra); etiquetas: filas de 'etiquetas'; folder_for: id -> carpeta."""
    con_cifra = {h['proceso_id'] for h in hallazgos if h['estado'] != 'descartado'}
    confirmados = {h['proceso_id'] for h in hallazgos if h['estado'] == 'confirmado'}
    grupos = defaultdict(lambda: {'procesos': 0, 'con_cifra': 0, 'confirmados': 0, 'paginas': 0, 'ocr': 0})
    for case in cases:
        total, ocr = costo_lectura(folder_for(case['id']), case)
        for clave in [
            ('tipo', case['metadata'].get('tipo_de_contrato') or 'sin tipo'),
            ('segmento', nombre_segmento(segmento(case['metadata']))),
        ]:
            g = grupos[clave]
            g['procesos'] += 1
            g['con_cifra'] += case['id'] in con_cifra
            g['confirmados'] += case['id'] in confirmados
            g['paginas'] += total
            g['ocr'] += ocr
    por_estado = defaultdict(int)
    senal = defaultdict(lambda: {'confirmado': 0, 'descartado': 0})
    documento = defaultdict(lambda: {'confirmado': 0, 'descartado': 0})
    for e in etiquetas:
        por_estado[e['estado']] += 1
        documento[e['tipo_documento']][e['estado']] += 1
        for s in json.loads(e['senales'] or '[]'):
            senal[s][e['estado']] += 1
    sugerencias = []
    if len(etiquetas) >= MIN_ETIQUETAS:
        for (dim, nombre), g in grupos.items():
            if g['procesos'] >= MIN_PROCESOS_GRUPO and g['con_cifra'] == 0 and g['ocr']:
                sugerencias.append(
                    f'{dim} "{nombre}": {g["procesos"]} procesos, 0 con cifra, '
                    f'{g["ocr"]} paginas de OCR. Candidato a dejar el OCR para el final.'
                )
        for s, c in senal.items():
            n = c['confirmado'] + c['descartado']
            if n >= 5 and c['confirmado'] == 0:
                sugerencias.append(
                    f'Senal "{s}": {n} paginas revisadas, ninguna confirmada. '
                    'Candidata a descartar cifras que salgan solo de esas paginas.'
                )
    return {
        'etiquetas': len(etiquetas),
        'por_estado': dict(por_estado),
        'grupos': grupos,
        'senales': senal,
        'documentos': documento,
        'sugerencias': sugerencias,
        'procesos': len(cases),
        'con_cifra': len(con_cifra),
        'confirmados': len(confirmados),
    }


def markdown(datos, fecha=None):
    """Reporte en markdown. Con 'fecha' es el de un solo dia de corte: muestra lo que costo y
    rindio ese dia, pero no sugiere reglas (eso sale del acumulado)."""
    d = datos
    titulo = f'# Aprendizaje del dia {fecha}' if fecha else '# Aprendizaje del observatorio (acumulado)'
    lineas = [
        titulo,
        '',
        f'Generado {datetime.now().strftime("%Y-%m-%d %H:%M")}. Procesos analizados: {d["procesos"]}; '
        f'con cifra de pago: {d["con_cifra"]}; con cifra confirmada: {d["confirmados"]}.',
        '',
        f'Revisiones (etiquetas): {d["etiquetas"]} '
        f'({d["por_estado"].get("confirmado", 0)} confirmadas, {d["por_estado"].get("descartado", 0)} descartadas).',
    ]
    if fecha:
        lineas += [
            '',
            'Solo procesos publicados ese dia. Las reglas se sugieren en `acumulado.md`, '
            'porque un dia solo no junta evidencia suficiente.',
        ]
    elif d['etiquetas'] < MIN_ETIQUETAS:
        lineas += [
            '',
            f'**Evidencia insuficiente para sugerir reglas** (hacen falta {MIN_ETIQUETAS} revisiones). '
            'Revisar hallazgos con `honorario-justo --marcar ID confirmado|descartado`.',
        ]
    for dim, titulo in [('tipo', 'Por tipo de contrato'), ('segmento', 'Por segmento UNSPSC')]:
        filas = sorted(((k[1], g) for k, g in d['grupos'].items() if k[0] == dim), key=lambda x: -x[1]['procesos'])
        lineas += [
            '',
            f'## {titulo}',
            '',
            '| Grupo | Procesos | Con cifra | Confirmados | Paginas | Paginas OCR | OCR por cifra |',
            '|---|---|---|---|---|---|---|',
        ]
        for nombre, g in filas:
            por_cifra = (
                f'{g["ocr"] / g["con_cifra"]:.0f}' if g['con_cifra'] else ('todo sin cifra' if g['ocr'] else '-')
            )
            lineas.append(
                f'| {nombre} | {g["procesos"]} | {g["con_cifra"]} ({_pct(g["con_cifra"], g["procesos"])}) | '
                f'{g["confirmados"]} | {g["paginas"]} | {g["ocr"]} | {por_cifra} |'
            )
    lineas += [
        '',
        '## Donde estaban las cifras revisadas',
        '',
        '| Tipo de documento | Confirmadas | Descartadas |',
        '|---|---|---|',
    ]
    for tipo, c in sorted(d['documentos'].items(), key=lambda x: -sum(x[1].values())):
        lineas.append(f'| {tipo} | {c["confirmado"]} | {c["descartado"]} |')
    lineas += ['', '| Senal en la pagina | Confirmadas | Descartadas |', '|---|---|---|']
    for s, c in sorted(d['senales'].items(), key=lambda x: -sum(x[1].values())):
        lineas.append(f'| {s} | {c["confirmado"]} | {c["descartado"]} |')
    if not fecha:
        lineas += ['', '## Sugerencias', '']
        lineas += [f'- {s}' for s in d['sugerencias']] or [
            '- Ninguna todavia: no hay evidencia suficiente para proponer reglas.'
        ]
        lineas += ['', 'Las sugerencias no se aplican solas: se decide y se programa la regla despues de leerlas.']
    return '\n'.join(lineas) + '\n'
