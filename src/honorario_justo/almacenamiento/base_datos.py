"""Persistencia en SQLite (data/observatorio.sqlite) y en la carpeta de cada caso.

Tablas: ``cases`` (procesos y su análisis), ``hallazgos`` (cargo + pago extraído), ``dias``
(resumen de cada corte diario), ``etiquetas`` (revisiones humanas), ``kv`` y ``runs``.
"""

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone

from honorario_justo import config
from honorario_justo.dominio import aprendizaje

_iniciadas = set()


def now():
    return datetime.now(timezone.utc).isoformat()


def db():
    """Conexión a la base del observatorio. El esquema se crea la primera vez que se usa
    cada base, sin efectos al importar el módulo."""
    path = config.DATA / 'observatorio.sqlite'
    if path not in _iniciadas:
        config.DATA.mkdir(parents=True, exist_ok=True)
        init_db(path)
        _iniciadas.add(path)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path):
    with sqlite3.connect(path, timeout=30) as conn:
        conn.executescript("""
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
        """)
        # Columnas agregadas despues de la primera version de 'hallazgos'.
        existentes = {r[1] for r in conn.execute('PRAGMA table_info(hallazgos)')}
        for col, tipo in [
            ('anos_nivel', 'TEXT'),
            ('pago_smlv', 'REAL'),
            ('confianza', 'TEXT'),
            ('metodo_pagina', 'TEXT'),
            ('referencia_tarifa', 'TEXT'),
        ]:
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
    folder = config.DATA / 'cases' / hashlib.sha256(case_id.encode()).hexdigest()[:24]
    for name in ['documents', 'text', 'captures']:
        (folder / name).mkdir(parents=True, exist_ok=True)
    return folder


def record_process(meta):
    case_id = meta['id_del_proceso']
    with db() as conn:
        conn.execute(
            'INSERT INTO cases(id,metadata,updated_at) VALUES (?,?,?) '
            'ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,updated_at=excluded.updated_at',
            (case_id, json.dumps(meta, ensure_ascii=False), now()),
        )
    return case_id


HALLAZGO_COLS = [
    'proceso_id',
    'entidad',
    'departamento',
    'fecha_publicacion',
    'cargo',
    'anos_experiencia',
    'anos_nivel',
    'pago_mensual_cop',
    'pago_smlv',
    'dedicacion',
    'duracion',
    'metodo',
    'metodo_pagina',
    'confianza',
    'referencia_tarifa',
    'archivo_fuente',
    'pagina_fuente',
    'estado',
    'created_at',
]


def save_hallazgo(conn, case, item):
    meta = case['metadata']
    values = {
        'proceso_id': case['id'],
        'entidad': meta.get('entidad', ''),
        'departamento': meta.get('departamento_entidad', ''),
        'fecha_publicacion': meta.get('fecha_de_publicacion_del', ''),
        'estado': 'sin_revisar',
        'created_at': now(),
        **{k: item[k] for k in HALLAZGO_COLS if k in item},
    }
    conn.execute(
        f'INSERT INTO hallazgos({",".join(HALLAZGO_COLS)}) VALUES ({",".join("?" * len(HALLAZGO_COLS))})',
        [values.get(k) for k in HALLAZGO_COLS],
    )


def resumen_dia(fecha):
    """Recalcula la fila de 'dias' desde cases/hallazgos (idempotente)."""
    with db() as conn:
        rows = [
            unpack(r)
            for r in conn.execute(
                "SELECT * FROM cases WHERE json_extract(metadata,'$.fecha_de_publicacion_del') LIKE ?", (fecha + '%',)
            )
        ]
        hallazgos = conn.execute(
            "SELECT proceso_id FROM hallazgos WHERE fecha_publicacion LIKE ? AND estado!='descartado'", (fecha + '%',)
        ).fetchall()
        publicados = conn.execute('SELECT publicados FROM dias WHERE fecha=?', (fecha,)).fetchone()
    analizados = [c for c in rows if c['checked_at']]
    fila = {
        'fecha': fecha,
        'publicados': publicados[0] if publicados else len(rows),
        'analizados': len(analizados),
        'con_documentos': sum(1 for c in analizados if c['analysis'].get('documents')),
        'con_presupuesto': sum(1 for c in analizados if c['analysis'].get('evidence', {}).get('presupuesto')),
        'con_hallazgo': len({h[0] for h in hallazgos}),
        'hallazgos': len(hallazgos),
    }
    with db() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO dias(fecha,publicados,analizados,con_documentos,con_presupuesto,'
            'con_hallazgo,hallazgos,updated_at) VALUES (?,?,?,?,?,?,?,?)',
            (*fila.values(), now()),
        )
    return fila


def respaldar_db(conservar=14):
    """Copia consistente de la base (API de backup de SQLite, segura aunque haya WAL)
    en data/respaldos/, una por dia, conservando las ultimas 'conservar'."""
    carpeta = config.DATA / 'respaldos'
    carpeta.mkdir(exist_ok=True)
    destino = carpeta / f'observatorio_{date.today().isoformat()}.sqlite'
    with db() as origen, sqlite3.connect(destino) as copia:
        origen.backup(copia)
    for viejo in sorted(carpeta.glob('observatorio_*.sqlite'))[:-conservar]:
        viejo.unlink()
    return destino
