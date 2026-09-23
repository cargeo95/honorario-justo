"""Caso de uso: corte diario.

Analiza TODOS los procesos de mínima cuantía publicados un día en SECOP II, sin filtro de
palabras clave, y se pone al día con los días que falten. Es reanudable.
"""

import logging
import re
from datetime import date, timedelta

from honorario_justo.almacenamiento.base_datos import db, get_case, now, record_process, respaldar_db, resumen_dia
from honorario_justo.fuentes.secop import SecopClient
from honorario_justo.servicios.analisis import analyze_case, settings, skip_reanalysis
from honorario_justo.servicios.aprendizaje import generar_aprendizaje
from honorario_justo.servicios.hallazgos import extraer_caso, guardar_hallazgos_caso


def run_day(fecha, limite=None, use_ollama=True):
    """Corte diario: TODOS los procesos de minima cuantia publicados ese dia (sin
    filtro de palabras clave), descarga + analisis + extraccion. Reanudable: lo
    analizado en las ultimas 24 h no se repite. Puede tardar horas (~150-250 procesos)."""
    config = dict(settings(), keywords='', date_from=fecha, date_to=fecha, department='')
    client = SecopClient()
    publicados = client.count_processes(config)
    with db() as conn:
        conn.execute(
            'INSERT INTO dias(fecha,publicados,updated_at) VALUES (?,?,?) ON CONFLICT(fecha) '
            'DO UPDATE SET publicados=excluded.publicados,updated_at=excluded.updated_at',
            (fecha, publicados, now()),
        )
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
    logging.log(
        nivel,
        'RESUMEN %s: %d/%d analizados, %d con alertas, %d fallidos%s, %d cargos extraidos',
        fecha,
        fila['analizados'],
        fila['publicados'],
        con_alertas,
        len(fallidos),
        (' (' + ', '.join(fallidos) + ')') if fallidos else '',
        fila['hallazgos'],
    )
    try:
        generar_aprendizaje(fecha=fecha)
    except Exception:
        logging.exception('[%s] No se pudo escribir el aprendizaje del dia', fecha)
    respaldar_db()
    return fila


def resumir_alertas(warnings):
    """Una linea por documento para las alertas repetidas por pagina ('Pagina 38: limite
    de OCR alcanzado' x 40 -> 'limite de OCR alcanzado en 40 paginas (38-77)')."""
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
    pendientes |= {
        f for f, d in filas.items() if not completo(d) and f >= (fin - timedelta(days=max_dias - 1)).isoformat()
    }
    return sorted(pendientes)


def ultimo_corte():
    with db() as conn:
        row = conn.execute('SELECT max(fecha) FROM dias').fetchone()
    if not row or not row[0]:
        raise SystemExit('Todavia no hay cortes diarios: correr primero honorario-justo --dia')
    return row[0]
