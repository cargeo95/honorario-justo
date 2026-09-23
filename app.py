"""Motor local del Observatorio de Minima Cuantia: consulta SECOP, descarga,
extrae evidencia y elige el candidato mas reciente que no se haya entregado."""
import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import aprendizaje
from evidence import ROLE, analyze_pdf, classification, render_page
from extraccion import (confianza, en_smlv, extract_experience_years, extract_pay,
                        extract_requirements, match_requirement, referencia_tarifa, role_tokens)
from secop import SecopClient, doc_score, link, normal

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('SECOP_DATA', ROOT / 'data')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
# logs/observatorio.log: todo (rota a diario, 30 dias). logs/errores.log: solo WARNING+,
# para ver de un vistazo que fallo (descargas, CAPTCHA, OCR, PDF ilegibles, excepciones).
LOGS = Path(os.environ.get('SECOP_LOGS', ROOT / 'logs')).resolve()
LOGS.mkdir(parents=True, exist_ok=True)
_formato = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
_todo = TimedRotatingFileHandler(LOGS / 'observatorio.log', when='midnight', backupCount=30, encoding='utf-8')
_errores = logging.FileHandler(LOGS / 'errores.log', encoding='utf-8')
_errores.setLevel(logging.WARNING)
for _h in (_todo, _errores):
    _h.setFormatter(_formato)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', force=True,
                    handlers=[_todo, _errores, logging.StreamHandler()])


def now():
    return datetime.now(timezone.utc).isoformat()


def db():
    conn = sqlite3.connect(DATA / 'observatorio.sqlite', timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, metadata TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDIENTE', analysis TEXT NOT NULL DEFAULT '{}',
            reviewed TEXT NOT NULL DEFAULT '', draft TEXT NOT NULL DEFAULT '',
            checked_at TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started TEXT, finished TEXT,
            status TEXT, message TEXT, processed INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS hallazgos (id INTEGER PRIMARY KEY, proceso_id TEXT NOT NULL,
            entidad TEXT, departamento TEXT, fecha_publicacion TEXT, cargo TEXT,
            anos_experiencia INTEGER, pago_mensual_cop REAL, dedicacion TEXT, duracion TEXT,
            metodo TEXT NOT NULL, archivo_fuente TEXT, pagina_fuente INTEGER,
            estado TEXT NOT NULL DEFAULT 'sin_revisar', created_at TEXT NOT NULL,
            FOREIGN KEY(proceso_id) REFERENCES cases(id));
        CREATE INDEX IF NOT EXISTS idx_hallazgos_proceso ON hallazgos(proceso_id);
        CREATE INDEX IF NOT EXISTS idx_hallazgos_fecha ON hallazgos(fecha_publicacion);
        CREATE TABLE IF NOT EXISTS dias (fecha TEXT PRIMARY KEY, publicados INTEGER NOT NULL,
            analizados INTEGER NOT NULL DEFAULT 0, con_documentos INTEGER NOT NULL DEFAULT 0,
            con_presupuesto INTEGER NOT NULL DEFAULT 0, con_hallazgo INTEGER NOT NULL DEFAULT 0,
            hallazgos INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
        ''')
        # Columnas agregadas despues de la primera version de 'hallazgos'.
        existentes = {r[1] for r in conn.execute('PRAGMA table_info(hallazgos)')}
        for col, tipo in [('anos_nivel', 'TEXT'), ('pago_smlv', 'REAL'), ('confianza', 'TEXT'),
                          ('metodo_pagina', 'TEXT'), ('referencia_tarifa', 'TEXT')]:
            if col not in existentes:
                conn.execute(f'ALTER TABLE hallazgos ADD COLUMN {col} {tipo}')
        aprendizaje.init(conn)


def get_value(key, default=None):
    with db() as conn:
        row = conn.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_value(key, value):
    with db() as conn:
        conn.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value)))


def settings():
    defaults = {'date_from': (date.today() - timedelta(days=90)).isoformat(),
                'date_to': date.today().isoformat(), 'follow_today': True,
                'keywords': 'CONSULTOR,INTERVENT,ESTUDIOS,DISEÑO,DISENO,ALUMBRADO',
                'department': '', 'batch_size': 10, 'max_documents': 8,
                # Lectura completa, OCR incluido: corre local y sin costo, y las tablas de pago de
                # los PDF escaneados suelen estar despues de la pagina 15 (Saravena, Puerto Salgar).
                # 500 es solo un tope de seguridad contra anexos gigantes.
                'max_pages': 500, 'max_ocr_pages': 500}
    saved = get_value('settings', {})
    defaults.update({k: v for k, v in saved.items() if k in defaults})
    if defaults['follow_today']:
        defaults['date_to'] = date.today().isoformat()
    return defaults


def unpack(row):
    result = dict(row)
    result['metadata'] = json.loads(result['metadata'])
    result['analysis'] = json.loads(result['analysis'])
    return result


def get_case(case_id):
    with db() as conn:
        row = conn.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()
    if not row:
        raise KeyError(case_id)
    return unpack(row)


def case_folder(case_id):
    folder = DATA / 'cases' / hashlib.sha256(case_id.encode()).hexdigest()[:24]
    for name in ['documents', 'text', 'captures']:
        (folder / name).mkdir(parents=True, exist_ok=True)
    return folder


def record_process(meta):
    case_id = meta['id_del_proceso']
    with db() as conn:
        conn.execute('INSERT INTO cases(id,metadata,updated_at) VALUES (?,?,?) '
                     'ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,updated_at=excluded.updated_at',
                     (case_id, json.dumps(meta, ensure_ascii=False), now()))
    return case_id


def analyze_case(case_id, config, client=None):
    case = get_case(case_id)
    folder = case_folder(case_id)
    warnings = [] if client else list(case['analysis'].get('warnings', []))
    documents, available = [], case['analysis'].get('inventory', [])
    try:
        if client:
            available, warnings = client.documents(case['metadata'])
    except Exception as exc:
        warnings.append('Consulta de documentos: ' + str(exc))
    eligible = sorted([r for r in available if doc_score(r) > 0], key=doc_score, reverse=True)
    if len(eligible) > config['max_documents']:
        warnings.append(f'Se priorizaron {config["max_documents"]} de {len(eligible)} PDF candidatos')
    sources = {d['file']: d for d in case['analysis'].get('documents', [])}
    for row in (eligible[:config['max_documents']] if client else []):
        logging.info('Descargando %s', row.get('nombre_archivo', 'PDF'))
        try:
            path = client.download(row, folder / 'documents')
            sources[path.name] = {'file': path.name, 'name': row.get('nombre_archivo', path.name),
                                  'url': link(row.get('url_descarga_documento')), 'dataset': row.get('dataset')}
        except Exception as exc:
            warnings.append(row.get('nombre_archivo', 'PDF') + ': ' + str(exc))
    for path in (folder / 'documents').glob('*.pdf'):
        sources.setdefault(path.name, {'file': path.name, 'name': path.name, 'url': '', 'local': True})
    alternatives = {k: [] for k in ['objeto', 'experiencia', 'presupuesto']}
    for filename, source in sources.items():
        logging.info('Leyendo %s', source['name'])
        try:
            result = analyze_pdf(folder / 'documents' / filename, folder / 'text', config)
            warnings.extend(source['name'] + ': ' + w for w in result['warnings'])
            documents.append(dict(source, pages=len(result['pages']), sha256=result['sha256']))
            for page in result['pages']:
                for kind, score in page['scores'].items():
                    if score:
                        alternatives[kind].append({'file': filename, 'name': source['name'],
                                                   'page': page['page'], 'score': score,
                                                   'method': page['method'], 'excerpt': page['text'][:1800]})
        except Exception as exc:
            warnings.append(source['name'] + ': ' + str(exc))
    evidence = {}
    previous = case['analysis'].get('evidence', {})
    for kind, choices in alternatives.items():
        choices.sort(key=lambda item: (-item['score'], item['page']))
        alternatives[kind] = choices[:20]
        selected = previous.get(kind)
        if selected and selected.get('manual') and (folder / 'documents' / selected['file']).exists():
            evidence[kind] = selected
        elif choices:
            evidence[kind] = choices[0]
        if evidence.get(kind):
            item = evidence[kind]
            render_page(folder / 'documents' / item['file'], item['page'], folder / 'captures' / f'{kind}-original.png')
            render_page(folder / 'documents' / item['file'], item['page'], folder / 'captures' / f'{kind}.png', item.get('crop'))
    if not available and client:
        warnings.append('Sin archivos en el indice publico para este portafolio; no implica ausencia en SECOP.')
    if available and not eligible and not documents:
        warnings.append('El indice no contiene PDF con nombres candidatos. Revise el inventario o adjunte el PDF.')
    warnings = list(dict.fromkeys(warnings))
    result = {'evidence': evidence, 'alternatives': alternatives, 'warnings': warnings,
              'documents': documents, 'inventory': available, 'analyzed_at': now()}
    status = classification(evidence, warnings, len(documents))
    # Any source change invalidates approval; unchanged reruns preserve the human decision.
    fingerprints = lambda a: sorted((d['file'], d.get('sha256')) for d in a.get('documents', []))
    changed = fingerprints(case['analysis']) != fingerprints(result) or case['analysis'].get('evidence') != evidence
    review = '' if changed else case['reviewed']
    with db() as conn:
        conn.execute('UPDATE cases SET analysis=?,status=?,reviewed=?,checked_at=?,updated_at=? WHERE id=?',
                     (json.dumps(result, ensure_ascii=False), status, review, now(), now(), case_id))
    (folder / 'metadata.json').write_text(json.dumps(case['metadata'], ensure_ascii=False, indent=2), encoding='utf-8')
    (folder / 'analysis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


def closing_date(metadata):
    for key in ('fecha_de_recepcion_de', 'fecha_de_apertura_de_respuesta'):
        value = metadata.get(key)
        if value:
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                continue
    return None


def skip_reanalysis(case):
    """Evita reanalizar lo revisado hace poco, y deja de intentarlo para siempre
    en casos que ya cerraron sin evidencia: no llegaran mas documentos previos."""
    if case['status'] in {'SIN_DOCUMENTOS', 'SIN_EVIDENCIA'}:
        closes = closing_date(case['metadata'])
        if closes and closes < datetime.now():
            return True
    if not case['checked_at']:
        return False
    if lectura_cortada(case['analysis'].get('warnings', []), settings()) or documentos_sin_bajar(case, settings()):
        return False
    return datetime.fromisoformat(case['checked_at']) > datetime.now(timezone.utc) - timedelta(hours=24)


def documentos_sin_bajar(case, config):
    """Candidatos del inventario (segun doc_score actual) que no se descargaron: pasa cuando
    cambian los criterios de seleccion. No cuenta los que ya fallaron al descargar, para no
    reintentarlos en cada corrida."""
    analysis = case['analysis']
    elegibles = sorted([r for r in analysis.get('inventory', []) if doc_score(r) > 0], key=doc_score, reverse=True)
    bajados = {d.get('name') for d in analysis.get('documents', [])}
    fallidos = {w.split(': ', 1)[0] for w in analysis.get('warnings', [])}
    return [r.get('nombre_archivo') for r in elegibles[:config['max_documents']]
            if r.get('nombre_archivo') not in bajados | fallidos]


def lectura_cortada(warnings, config):
    """True si el analisis se quedo corto por un tope menor al actual (OCR o paginas):
    se reanaliza aunque sea reciente. El texto ya leido queda en cache, asi que solo
    se hace OCR de las paginas que faltaron."""
    import re
    for w in warnings:
        if 'limite de OCR alcanzado' in w:
            return True
        m = re.search(r'PDF limitado a (\d+) de', w)
        if m and int(m.group(1)) < config['max_pages']:
            return True
    return False


def cursor_key_for(config):
    # Solo depende de los filtros de contenido: date_from/date_to se recalculan
    # a diario (ventana de 90 dias), asi que incluirlos reiniciaba el cursor
    # cada dia y el barrido hacia atras nunca avanzaba.
    signature = hashlib.sha256(json.dumps({k: config[k] for k in ['keywords', 'department']}, sort_keys=True).encode()).hexdigest()
    return 'cursor:' + signature


def run_cycle():
    logging.info('Consultando minima cuantia en SECOP II')
    config = settings()
    count = 0
    with db() as conn:
        run_id = conn.execute('INSERT INTO runs(started,status,message) VALUES (?,?,?)',
                              (now(), 'EJECUTANDO', 'Consultando minima cuantia en SECOP II')).lastrowid
    try:
        client = SecopClient()
        cursor_key = cursor_key_for(config)
        offset = get_value(cursor_key, 0)
        latest = client.processes(config)
        backlog = client.processes(config, offset) if offset else []
        rows = {r['id_del_proceso']: r for r in latest + backlog}
        for meta in rows.values():
            item_id = record_process(meta)
            case = get_case(item_id)
            if skip_reanalysis(case):
                continue
            try:
                analyze_case(item_id, config, client)
            except Exception as exc:
                logging.exception('Process failed: %s', item_id)
                with db() as conn:
                    conn.execute('UPDATE cases SET status=?,analysis=? WHERE id=?',
                                 ('ERROR', json.dumps({'warnings': [str(exc)]}), item_id))
            count += 1
        set_value(cursor_key, offset + config['batch_size'] if len(backlog if offset else latest) == config['batch_size'] else 0)
        message = f'Ciclo terminado: {count} procesos analizados'
        with db() as conn:
            conn.execute('UPDATE runs SET finished=?,status=?,message=?,processed=? WHERE id=?',
                         (now(), 'COMPLETADO', message, count, run_id))
        logging.info(message)
    except Exception as exc:
        logging.exception('Cycle failed')
        with db() as conn:
            conn.execute('UPDATE runs SET finished=?,status=?,message=?,processed=? WHERE id=?',
                         (now(), 'ERROR', str(exc), count, run_id))
        raise
    return count


def delivered_file():
    return DATA / 'entregados.json'


def load_delivered():
    path = delivered_file()
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    return []


def mark_delivered(case_id):
    delivered = load_delivered()
    if case_id not in delivered:
        delivered.append(case_id)
        delivered_file().write_text(json.dumps(delivered, ensure_ascii=False, indent=2), encoding='utf-8')


def next_candidate():
    """Caso mas reciente con las tres evidencias que no se haya entregado antes."""
    delivered = set(load_delivered())
    with db() as conn:
        rows = [unpack(r) for r in conn.execute('SELECT * FROM cases')]
    candidates = []
    for case in rows:
        if case['id'] in delivered:
            continue
        evidence = case['analysis'].get('evidence', {})
        if not all(evidence.get(k) for k in ['objeto', 'experiencia', 'presupuesto']):
            continue
        candidates.append(case)
    candidates.sort(key=lambda c: c['metadata'].get('fecha_de_publicacion_del', ''), reverse=True)
    return candidates[0] if candidates else None


HALLAZGO_COLS = ['proceso_id', 'entidad', 'departamento', 'fecha_publicacion', 'cargo', 'anos_experiencia',
                 'anos_nivel', 'pago_mensual_cop', 'pago_smlv', 'dedicacion', 'duracion', 'metodo',
                 'metodo_pagina', 'confianza', 'referencia_tarifa', 'archivo_fuente', 'pagina_fuente', 'estado',
                 'created_at']


def save_hallazgo(conn, case, item):
    meta = case['metadata']
    values = {'proceso_id': case['id'], 'entidad': meta.get('entidad', ''),
              'departamento': meta.get('departamento_entidad', ''),
              'fecha_publicacion': meta.get('fecha_de_publicacion_del', ''), 'estado': 'sin_revisar',
              'created_at': now(), **{k: item[k] for k in HALLAZGO_COLS if k in item}}
    conn.execute(f'INSERT INTO hallazgos({",".join(HALLAZGO_COLS)}) VALUES ({",".join("?" * len(HALLAZGO_COLS))})',
                 [values.get(k) for k in HALLAZGO_COLS])


def _unicas(alternatives):
    vistas = set()
    for alt in alternatives:
        if (alt['file'], alt['page']) not in vistas:
            vistas.add((alt['file'], alt['page']))
            yield alt


def extraer_caso(case, use_ollama=True):
    """Hallazgos (cargo + pago mensual) de un caso ya analizado, con los anos exigidos
    para ESE cargo cuando se pueden cruzar con la tabla de requisitos (anos_nivel='cargo').
    Si no se puede cruzar, queda el maximo del caso con anos_nivel='caso': sirve de
    contexto pero el tablero no lo usa para la brecha por perfil."""
    alternatives = case['analysis'].get('alternatives', {})
    requisitos, anos_caso = [], []
    for alt in _unicas(alternatives.get('experiencia', [])):
        requisitos.extend(extract_requirements(case['id'], alt['file'], alt['page']))
        anos_caso.extend(extract_experience_years(case['id'], alt['file'], alt['page']))
    fecha = case['metadata'].get('fecha_de_publicacion_del', '')
    tarifa = referencia_tarifa(case['id'])
    por_clave = {}
    for alt in _unicas(alternatives.get('presupuesto', [])):
        for item in extract_pay(case['id'], alt['file'], alt['page'], use_ollama=use_ollama):
            # Paginas candidatas vecinas se solapan por la ventana +/-1: mismo
            # cargo+pago en el mismo caso es el mismo hallazgo, no uno nuevo.
            # Clave = rol principal + monto: el mismo renglon puede venir escrito distinto en
            # otro PDF del proceso ("director de lnterventorla" en una capa de texto mala).
            rol = ROLE.search(normal(item['cargo']))
            key = (rol.group(0) if rol else normal(item['cargo']), item['pago_mensual_cop'])
            # Anos en la misma fila del pago (tablas de cotizacion) > cruce con requisitos.
            anos = item.pop('anos_fila', None)
            if anos is None:
                anos = match_requirement(item['cargo'], requisitos)
            item = dict(item, archivo_fuente=alt['file'], pagina_fuente=alt['page'],
                        anos_experiencia=anos if anos is not None else (max(anos_caso) if anos_caso else None),
                        anos_nivel='cargo' if anos is not None else ('caso' if anos_caso else None),
                        pago_smlv=en_smlv(item['pago_mensual_cop'], fecha), referencia_tarifa=tarifa)
            item['confianza'] = confianza(item, item.get('metodo_pagina', 'texto'))
            # Entre lecturas repetidas del mismo renglon queda la de mayor confianza (PDF digital > OCR).
            rango = {'alta': 0, 'media': 1, 'baja': 2}
            if key not in por_clave or rango[item['confianza']] < rango[por_clave[key]['confianza']]:
                por_clave[key] = item
    encontrados = list(por_clave.values())
    # "profesional" suelto con el mismo monto que un cargo con nombre es el mismo
    # renglon leido dos veces: queda solo el que dice que perfil es.
    con_nombre = {i['pago_mensual_cop'] for i in encontrados if role_tokens(i['cargo'])}
    return [i for i in encontrados if role_tokens(i['cargo']) or i['pago_mensual_cop'] not in con_nombre]


def guardar_hallazgos_caso(case, encontrados):
    """Reemplaza los hallazgos sin revisar del caso. Los ya revisados a mano
    (confirmado/descartado) se conservan y no se vuelven a insertar duplicados:
    se reconocen por cargo+monto o por monto+archivo+pagina, porque en la revision
    el cargo puede corregirse a mano."""
    with db() as conn:
        conn.execute("DELETE FROM hallazgos WHERE proceso_id=? AND estado='sin_revisar'", (case['id'],))
        revisados = set()
        for cargo, pago, archivo, pagina in conn.execute(
                'SELECT cargo,pago_mensual_cop,archivo_fuente,pagina_fuente FROM hallazgos WHERE proceso_id=?',
                (case['id'],)):
            revisados |= {('cargo', normal(cargo), pago), ('fuente', pago, archivo, pagina)}
        nuevos = [i for i in encontrados
                  if ('cargo', normal(i['cargo']), i['pago_mensual_cop']) not in revisados
                  and ('fuente', i['pago_mensual_cop'], i.get('archivo_fuente'), i.get('pagina_fuente')) not in revisados]
        for item in nuevos:
            save_hallazgo(conn, case, item)
    return len(nuevos)


def extraer_hallazgos(use_ollama=True, reprocesar=False):
    """Puebla 'hallazgos' desde lo ya descargado y cacheado en data/cases/ (no
    consulta SECOP ni descarga nada nuevo). Con reprocesar, reemplaza los hallazgos
    sin revisar de cada caso (antes se duplicaban)."""
    with db() as conn:
        ids_existentes = {r[0] for r in conn.execute('SELECT DISTINCT proceso_id FROM hallazgos')}
        rows = [unpack(r) for r in conn.execute('SELECT * FROM cases')]
    total_casos = total_hallazgos = 0
    for case in rows:
        if not reprocesar and case['id'] in ids_existentes:
            continue
        guardados = guardar_hallazgos_caso(case, extraer_caso(case, use_ollama))
        total_casos += 1
        total_hallazgos += guardados
        logging.info('Hallazgos %s: %d', case['id'], guardados)
    for (fecha,) in db().execute('SELECT fecha FROM dias').fetchall():
        resumen_dia(fecha)
    return {'casos_procesados': total_casos, 'hallazgos_guardados': total_hallazgos}


def marcar_hallazgo(hallazgo_id, estado):
    """Revision humana: solo lo 'confirmado' se usa en publicaciones."""
    if estado not in {'confirmado', 'descartado', 'sin_revisar'}:
        raise ValueError('estado debe ser confirmado, descartado o sin_revisar')
    with db() as conn:
        changed = conn.execute('UPDATE hallazgos SET estado=? WHERE id=?', (estado, hallazgo_id)).rowcount
        if not changed:
            raise KeyError(hallazgo_id)
        # Cada revision queda como etiqueta: de donde salio la cifra y si era real (aprendizaje.py).
        hallazgo = dict(conn.execute('SELECT * FROM hallazgos WHERE id=?', (hallazgo_id,)).fetchone())
        case = unpack(conn.execute('SELECT * FROM cases WHERE id=?', (hallazgo['proceso_id'],)).fetchone())
        aprendizaje.etiquetar(conn, hallazgo, case, case_folder(case['id']))
    return {'id': hallazgo_id, 'estado': estado}


def generar_aprendizaje(root=None, fecha=None):
    """Indicadores/aprendizaje/: donde aparecen las cifras revisadas y cuanto OCR cuesta
    cada tipo de proceso. Un reporte por dia de corte (dia_AAAA-MM-DD.md, solo los procesos
    publicados ese dia) y acumulado.md, que es el unico que sugiere reglas porque un dia
    solo no junta evidencia suficiente. Con 'fecha' reescribe solo ese dia (y el acumulado).
    Solo informa; las reglas se deciden al leerlo."""
    with db() as conn:
        cases = [unpack(r) for r in conn.execute('SELECT * FROM cases WHERE checked_at IS NOT NULL')]
        hallazgos = [dict(r) for r in conn.execute('SELECT proceso_id, estado FROM hallazgos')]
        etiquetas = [dict(r) for r in conn.execute('SELECT * FROM etiquetas')]
        fechas = [fecha] if fecha else [r[0] for r in conn.execute('SELECT fecha FROM dias ORDER BY fecha')]
    folder = indicadores_folder(root) / 'aprendizaje'
    folder.mkdir(exist_ok=True)
    datos = aprendizaje.reporte(cases, hallazgos, etiquetas, case_folder)
    path = folder / 'acumulado.md'
    path.write_text(aprendizaje.markdown(datos), encoding='utf-8')
    dias = {}
    for f in fechas:
        del_dia = [c for c in cases if (c['metadata'].get('fecha_de_publicacion_del') or '').startswith(f)]
        ids = {c['id'] for c in del_dia}
        d = aprendizaje.reporte(del_dia, [h for h in hallazgos if h['proceso_id'] in ids],
                                [e for e in etiquetas if e['proceso_id'] in ids], case_folder)
        archivo = folder / f'dia_{f}.md'
        archivo.write_text(aprendizaje.markdown(d, fecha=f), encoding='utf-8')
        dias[f] = {'archivo': str(archivo), 'procesos': d['procesos'], 'con_cifra': d['con_cifra'],
                   'etiquetas': d['etiquetas']}
    return {'archivo': str(path), 'procesos': datos['procesos'], 'etiquetas': datos['etiquetas'],
            'sugerencias': datos['sugerencias'], 'dias': dias}


def resumen_dia(fecha):
    """Recalcula la fila de 'dias' desde cases/hallazgos (idempotente)."""
    with db() as conn:
        rows = [unpack(r) for r in conn.execute(
            "SELECT * FROM cases WHERE json_extract(metadata,'$.fecha_de_publicacion_del') LIKE ?", (fecha + '%',))]
        hallazgos = conn.execute("SELECT proceso_id FROM hallazgos WHERE fecha_publicacion LIKE ? "
                                 "AND estado!='descartado'", (fecha + '%',)).fetchall()
        publicados = conn.execute('SELECT publicados FROM dias WHERE fecha=?', (fecha,)).fetchone()
    analizados = [c for c in rows if c['checked_at']]
    fila = {'fecha': fecha, 'publicados': publicados[0] if publicados else len(rows),
            'analizados': len(analizados),
            'con_documentos': sum(1 for c in analizados if c['analysis'].get('documents')),
            'con_presupuesto': sum(1 for c in analizados if c['analysis'].get('evidence', {}).get('presupuesto')),
            'con_hallazgo': len({h[0] for h in hallazgos}), 'hallazgos': len(hallazgos)}
    with db() as conn:
        conn.execute('INSERT OR REPLACE INTO dias(fecha,publicados,analizados,con_documentos,con_presupuesto,'
                     'con_hallazgo,hallazgos,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                     (*fila.values(), now()))
    return fila


def run_day(fecha, limite=None, use_ollama=True):
    """Corte diario: TODOS los procesos de minima cuantia publicados ese dia (sin
    filtro de palabras clave), descarga + analisis + extraccion. Reanudable: lo
    analizado en las ultimas 24 h no se repite. Puede tardar horas (~150-250 procesos)."""
    config = dict(settings(), keywords='', date_from=fecha, date_to=fecha, department='')
    client = SecopClient()
    publicados = client.count_processes(config)
    with db() as conn:
        conn.execute('INSERT INTO dias(fecha,publicados,updated_at) VALUES (?,?,?) ON CONFLICT(fecha) '
                     'DO UPDATE SET publicados=excluded.publicados,updated_at=excluded.updated_at',
                     (fecha, publicados, now()))
    procesos = client.all_processes(config)
    procesos = procesos[:limite] if limite else procesos
    logging.info('Dia %s: %d publicados, %d a procesar', fecha, publicados, len(procesos))
    fallidos, con_alertas = [], 0
    for n, meta in enumerate(procesos, 1):
        case_id = record_process(meta)
        case = get_case(case_id)
        try:
            if not skip_reanalysis(case):
                logging.info('[%d/%d] %s', n, len(procesos), case_id)
                analyze_case(case_id, config, client)
                case = get_case(case_id)
                # Cada alerta queda en errores.log con el proceso: CAPTCHA, descarga
                # fallida, limite de OCR, PDF protegido... es lo que hay que revisar.
                for w in resumir_alertas(case['analysis'].get('warnings', [])):
                    logging.warning('[%s] %s: %s', fecha, case_id, w)
                con_alertas += bool(case['analysis'].get('warnings'))
            guardar_hallazgos_caso(case, extraer_caso(case, use_ollama))
        except Exception:
            logging.exception('[%s] Fallo el proceso %s', fecha, case_id)
            fallidos.append(case_id)
    fila = resumen_dia(fecha)
    nivel = logging.WARNING if fallidos else logging.INFO
    logging.log(nivel, 'RESUMEN %s: %d/%d analizados, %d con alertas, %d fallidos%s, %d cargos extraidos',
                fecha, fila['analizados'], fila['publicados'], con_alertas, len(fallidos),
                (' (' + ', '.join(fallidos) + ')') if fallidos else '', fila['hallazgos'])
    try:
        generar_aprendizaje(fecha=fecha)
    except Exception:
        logging.exception('[%s] No se pudo escribir el aprendizaje del dia', fecha)
    respaldar_db()
    return fila


def resumir_alertas(warnings):
    """Una linea por documento para las alertas repetidas por pagina ('Pagina 38: limite
    de OCR alcanzado' x 40 -> 'limite de OCR alcanzado en 40 paginas (38-77)')."""
    import re
    agrupadas, salida = {}, []
    for w in warnings:
        m = re.match(r'(.*): Pagina (\d+): (.*)$', w)
        if m:
            agrupadas.setdefault((m.group(1), m.group(3)), []).append(int(m.group(2)))
        else:
            salida.append(w)
    for (doc, motivo), paginas in agrupadas.items():
        rango = f'{min(paginas)}-{max(paginas)}' if len(paginas) > 1 else str(paginas[0])
        salida.append(f'{doc}: {motivo} en {len(paginas)} pagina(s) ({rango})')
    return salida


def respaldar_db(conservar=14):
    """Copia consistente de la base (API de backup de SQLite, segura aunque haya WAL)
    en data/respaldos/, una por dia, conservando las ultimas 'conservar'."""
    carpeta = DATA / 'respaldos'
    carpeta.mkdir(exist_ok=True)
    destino = carpeta / f'observatorio_{date.today().isoformat()}.sqlite'
    with db() as origen, sqlite3.connect(destino) as copia:
        origen.backup(copia)
    for viejo in sorted(carpeta.glob('observatorio_*.sqlite'))[:-conservar]:
        viejo.unlink()
    return destino


def dias_pendientes(ultimo, max_dias=7):
    """Dias por procesar hasta 'ultimo' (el mas reciente en Datos Abiertos): los que
    siguen al ultimo corte completo, mas cualquier corte que quedo a medias. Asi, si
    un dia no se corre, la siguiente corrida se pone al dia. Tope de max_dias hacia
    atras para que la primera vez no intente reconstruir todo el historial."""
    with db() as conn:
        filas = {r['fecha']: dict(r) for r in conn.execute('SELECT * FROM dias')}
    completo = lambda d: d['analizados'] >= d['publicados']
    hechos = sorted(f for f, d in filas.items() if completo(d) and f <= ultimo)
    a_medias = sorted(f for f, d in filas.items() if not completo(d) and f <= ultimo)
    fin = date.fromisoformat(ultimo)
    # Desde el dia siguiente al ultimo corte completo; si no hay ninguno, desde el primer
    # corte a medias (para no saltarse los dias entre ese y el ultimo); si tampoco, solo el ultimo.
    if hechos:
        inicio = date.fromisoformat(hechos[-1]) + timedelta(days=1)
    elif a_medias:
        inicio = date.fromisoformat(a_medias[0])
    else:
        inicio = fin
    inicio = max(inicio, fin - timedelta(days=max_dias - 1))
    pendientes = {(inicio + timedelta(days=i)).isoformat() for i in range((fin - inicio).days + 1)}
    pendientes |= {f for f, d in filas.items() if not completo(d) and f >= (fin - timedelta(days=max_dias - 1)).isoformat()}
    return sorted(pendientes)


MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre',
         'octubre', 'noviembre', 'diciembre']


def indicadores_folder(root=None):
    folder = Path(root or os.environ.get('SECOP_INDICADORES', ROOT / 'Indicadores')).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def pesos(valor):
    return f'{valor:,.0f}'.replace(',', '.')


def escribir_resumen_dia(fila, root=None):
    """Base del post diario, solo con cifras trazables a 'dias' y 'hallazgos'.
    La redaccion final sigue REDACCION.md."""
    with db() as conn:
        filas = conn.execute("SELECT id,cargo,anos_experiencia,anos_nivel,pago_mensual_cop,pago_smlv,entidad,"
                             "departamento,confianza,estado FROM hallazgos WHERE fecha_publicacion LIKE ? "
                             "AND estado!='descartado' ORDER BY pago_smlv", (fila['fecha'] + '%',)).fetchall()
    fecha = date.fromisoformat(fila['fecha'])
    lineas = [f"# {fecha.day} de {MESES[fecha.month - 1]} de {fecha.year}", '',
              f"- Procesos de mínima cuantía publicados en SECOP II: **{fila['publicados']}**",
              f"- Analizados (documentos descargados y leídos): {fila['analizados']}",
              f"- Con página de pago por cargo detectada: {fila['con_presupuesto']}",
              f"- Con al menos un cargo + pago mensual extraído: {fila['con_hallazgo']} "
              f"({fila['hallazgos']} cargos en total)", '', '## Cargos encontrados', '']
    for r in filas:
        if r['anos_experiencia'] is None:
            anos = 'años sin dato'
        else:
            anos = f"{r['anos_experiencia']} años" + ('' if r['anos_nivel'] == 'cargo' else ' (máx. del proceso)')
        smlv = f"{r['pago_smlv']:.2f} SMLV".replace('.', ',') if r['pago_smlv'] is not None else 'SMLV sin dato'
        lineas.append(f"- #{r['id']} {r['cargo']} - {anos} - ${pesos(r['pago_mensual_cop'])}/mes ({smlv}) - "
                      f"{r['entidad']}, {r['departamento']} [confianza {r['confianza']}, {r['estado'].replace('_', ' ')}]")
    if not filas:
        lineas.append('- (ninguno)')
    lineas += ['', 'Publicar solo filas confirmadas (`app.py --marcar ID confirmado`).']
    path = indicadores_folder(root) / f"dia_{fila['fecha']}.md"
    path.write_text('\n'.join(lineas) + '\n', encoding='utf-8')
    return str(path)


def exportar_hallazgos(root=None):
    """Vuelca 'hallazgos' a CSV en Indicadores/, paralelo a Resultados/: mismo espiritu
    de trazabilidad (queda registrado que alimento cada version del tablero)."""
    import csv
    path = indicadores_folder(root) / f'hallazgos_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    columns = ['id'] + HALLAZGO_COLS
    with db() as conn:
        rows = conn.execute(f'SELECT {",".join(columns)} FROM hallazgos ORDER BY fecha_publicacion DESC').fetchall()
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)
    return {'archivo': str(path), 'filas': len(rows)}


init_db()


def build_parser():
    parser = argparse.ArgumentParser(description='Motor local del Observatorio de Minima Cuantia')
    parser.add_argument('--buscar', action='store_true',
                        help='Consulta SECOP, analiza lo nuevo y muestra el candidato mas reciente sin entregar')
    parser.add_argument('--siguiente', action='store_true',
                        help='Muestra el candidato mas reciente sin entregar, sin volver a consultar SECOP')
    parser.add_argument('--entregado', metavar='ID', help='Marca un proceso como entregado, para no repetirlo')
    parser.add_argument('--once', action='store_true', help='Solo ejecuta un ciclo de consulta y analisis')
    parser.add_argument('--extraer', action='store_true',
                        help='Extrae cargo/experiencia/pago de lo ya cacheado (sin consultar SECOP) y llena hallazgos')
    parser.add_argument('--sin-ollama', action='store_true', help='Con --extraer: no usar el respaldo de Ollama local')
    parser.add_argument('--reprocesar', action='store_true', help='Con --extraer: reprocesa casos que ya tenian hallazgos')
    parser.add_argument('--exportar', action='store_true', help='Exporta hallazgos a CSV en Indicadores/')
    parser.add_argument('--dia', nargs='?', const='ultimo', metavar='AAAA-MM-DD',
                        help='Corte diario: TODOS los procesos de minima cuantia de ese dia (por defecto el ultimo '
                             'dia disponible en Datos Abiertos), sin filtro de palabras clave. Tarda horas; es reanudable')
    parser.add_argument('--limite', type=int, help='Con --dia: procesa solo los primeros N (para probar)')
    parser.add_argument('--resumen', nargs='?', const=(date.today() - timedelta(days=1)).isoformat(), metavar='AAAA-MM-DD',
                        help='Reescribe Indicadores/dia_AAAA-MM-DD.md con lo ya procesado, sin consultar SECOP')
    parser.add_argument('--marcar', nargs=2, metavar=('ID', 'ESTADO'),
                        help='Revision humana de un hallazgo: confirmado | descartado | sin_revisar')
    parser.add_argument('--aprendizaje', action='store_true',
                        help='Reporte de donde aparecen las cifras revisadas y el costo de OCR por tipo de proceso')
    parser.add_argument('--tablero', action='store_true', help='Genera Indicadores/tablero.html')
    parser.add_argument('--semana', nargs='?', const='ultima', metavar='AAAA-MM-DD',
                        help='Resumen de la semana (lunes a domingo) que contiene esa fecha; por defecto la del '
                             'ultimo corte. Escribe Indicadores/semanas/semana_AAAA-Www.md')
    parser.add_argument('--imagenes', choices=['dia', 'semana'],
                        help='PNG para LinkedIn (1080x1350 + PDF carrusel) y TikTok (1080x1920) del ultimo dia '
                             'o semana (o de --fecha)')
    parser.add_argument('--fecha', metavar='AAAA-MM-DD', help='Con --imagenes: dia o semana a graficar')
    parser.add_argument('--borrador', action='store_true',
                        help='Con --imagenes/--semana: incluye cifras sin revisar y marca BORRADOR')
    return parser


def ultimo_corte():
    with db() as conn:
        row = conn.execute('SELECT max(fecha) FROM dias').fetchone()
    if not row or not row[0]:
        raise SystemExit('Todavia no hay cortes diarios: correr primero app.py --dia')
    return row[0]


def main(args, parser):
    if args.semana:
        from indicadores import escribir_resumen_semana, resumen_semana
        fecha = ultimo_corte() if args.semana == 'ultima' else args.semana
        r = resumen_semana(fecha, solo_confirmados=not args.borrador)
        print(json.dumps({'archivo': escribir_resumen_semana(r), 'semana': r['semana_iso'], 'publicados': r['publicados'],
                          'cargos': r['cargos'], 'mediana_smlv': r['mediana_smlv']}, ensure_ascii=False, indent=2))
        return
    if args.imagenes:
        import imagenes
        fecha = args.fecha or ultimo_corte()
        generar = imagenes.imagenes_dia if args.imagenes == 'dia' else imagenes.imagenes_semana
        print(json.dumps(generar(fecha, borrador=args.borrador), ensure_ascii=False, indent=2))
        return
    if args.dia:
        if args.dia == 'ultimo':
            # Datos Abiertos llega con 1-2 dias de atraso: se toma lo ultimo disponible
            # y se completan los dias que hayan quedado sin procesar.
            ultimo = SecopClient().latest_day()
            fechas = dias_pendientes(ultimo)
            logging.info('Datos Abiertos llega hasta %s; dias por procesar: %s', ultimo, ', '.join(fechas) or 'ninguno')
        else:
            fechas = [args.dia]
        resultados = []
        for fecha in fechas:
            fila = run_day(fecha, limite=args.limite, use_ollama=not args.sin_ollama)
            resultados.append(dict(fila, resumen=escribir_resumen_dia(fila)))
        print(json.dumps(resultados, ensure_ascii=False, indent=2))
    elif args.resumen:
        fila = resumen_dia(args.resumen)
        print(json.dumps(dict(fila, resumen=escribir_resumen_dia(fila)), ensure_ascii=False, indent=2))
    elif args.marcar:
        print(json.dumps(marcar_hallazgo(int(args.marcar[0]), args.marcar[1]), ensure_ascii=False))
    elif args.aprendizaje:
        print(json.dumps(generar_aprendizaje(), ensure_ascii=False, indent=2))
    elif args.tablero:
        from tablero import generar_tablero
        print(json.dumps(generar_tablero(), ensure_ascii=False))
    elif args.extraer:
        print(json.dumps(extraer_hallazgos(use_ollama=not args.sin_ollama, reprocesar=args.reprocesar), ensure_ascii=False))
    elif args.exportar:
        print(json.dumps(exportar_hallazgos(), ensure_ascii=False))
    elif args.entregado:
        mark_delivered(args.entregado)
        print(json.dumps({'ok': True, 'entregado': args.entregado}, ensure_ascii=False))
    elif args.buscar or args.siguiente:
        if args.buscar:
            run_cycle()
        case = next_candidate()
        if not case:
            print(json.dumps({'candidato': None}, ensure_ascii=False))
        else:
            folder = case_folder(case['id'])
            evidencia = {kind: {'file': item['file'], 'page': item['page'], 'texto': item.get('excerpt', '')}
                        for kind, item in case['analysis']['evidence'].items()}
            print(json.dumps({'candidato': {
                'id': case['id'], 'metadata': case['metadata'], 'status': case['status'],
                'warnings': case['analysis'].get('warnings', []), 'evidencia': evidencia,
                'carpeta_local': str(folder)}}, ensure_ascii=False, indent=2))
    elif args.once:
        run_cycle()
    else:
        parser.print_help()


if __name__ == '__main__':
    parser = build_parser()
    args = parser.parse_args()
    try:
        main(args, parser)
    except Exception:
        logging.exception('Fallo no controlado: %s', ' '.join(sys.argv[1:]))
        raise
