"""Caso de uso: búsqueda por palabras clave y selección del próximo caso a investigar."""

import hashlib
import json
import logging

from honorario_justo import config
from honorario_justo.almacenamiento.base_datos import db, get_case, get_value, now, record_process, set_value, unpack
from honorario_justo.fuentes.secop import SecopClient
from honorario_justo.servicios.analisis import ErrorConsulta, analyze_case, settings, skip_reanalysis


def cursor_key_for(config):
    # Solo depende de los filtros de contenido: date_from/date_to se recalculan
    # a diario (ventana de 90 dias), asi que incluirlos reiniciaba el cursor
    # cada dia y el barrido hacia atras nunca avanzaba.
    signature = hashlib.sha256(
        json.dumps({k: config[k] for k in ['keywords', 'department']}, sort_keys=True).encode()
    ).hexdigest()
    return 'cursor:' + signature


def run_cycle():
    logging.info('Consultando minima cuantia en SECOP II')
    config = settings()
    count = 0
    with db() as conn:
        run_id = conn.execute(
            'INSERT INTO runs(started,status,message) VALUES (?,?,?)',
            (now(), 'EJECUTANDO', 'Consultando minima cuantia en SECOP II'),
        ).lastrowid
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
            except ErrorConsulta as exc:
                # Fallo de red: el caso queda como estaba y se reintenta en la siguiente corrida.
                logging.warning('%s: %s', item_id, exc)
                continue
            except Exception as exc:
                logging.exception('Process failed: %s', item_id)
                with db() as conn:
                    conn.execute(
                        'UPDATE cases SET status=?,analysis=? WHERE id=?',
                        ('ERROR', json.dumps({'warnings': [str(exc)]}), item_id),
                    )
            count += 1
        set_value(
            cursor_key,
            offset + config['batch_size'] if len(backlog if offset else latest) == config['batch_size'] else 0,
        )
        message = f'Ciclo terminado: {count} procesos analizados'
        with db() as conn:
            conn.execute(
                'UPDATE runs SET finished=?,status=?,message=?,processed=? WHERE id=?',
                (now(), 'COMPLETADO', message, count, run_id),
            )
        logging.info(message)
    except Exception as exc:
        logging.exception('Cycle failed')
        with db() as conn:
            conn.execute(
                'UPDATE runs SET finished=?,status=?,message=?,processed=? WHERE id=?',
                (now(), 'ERROR', str(exc), count, run_id),
            )
        raise
    return count


def delivered_file():
    return config.DATA / 'entregados.json'


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
