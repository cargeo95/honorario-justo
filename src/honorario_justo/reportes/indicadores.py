"""Agregados sobre 'dias' y 'hallazgos': clasificacion de cada cargo frente a sus
comparables (lo bajo y lo destacado) y resumen semanal.

No hay un piso legal de honorarios para contratacion estatal, asi que "paga bien" o
"paga poco" se define de forma relativa y explicita: cada cargo se compara contra los
demas cargos con la misma franja de experiencia exigida. Destacado = cuartil superior,
bajo = cuartil inferior, y solo si la franja tiene suficientes comparables.

Ejemplo de referencia de "lo bueno": Maripi (Boyaca, CO1.REQ.11051829) paga 5,34 SMLV a un
profesional con 5 anos exigidos y 4,0 SMLV a uno con 2, ambos al 100%. Villa de Leyva cita
una tabla oficial de la Gobernacion y aun asi paga bajo: la fuente del precio no es criterio.
"""

import csv
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median, quantiles

from honorario_justo import config
from honorario_justo.almacenamiento.base_datos import HALLAZGO_COLS, db
from honorario_justo.config import indicadores_folder
from honorario_justo.dominio.formato import MESES, pesos

FRANJAS = [(0, 4, '0-4 años'), (5, 9, '5-9 años'), (10, 14, '10-14 años'), (15, 99, '15 o más años')]
MIN_COMPARABLES = 8


def franja(anos):
    if anos is None:
        return None
    return next(label for lo, hi, label in FRANJAS if lo <= anos <= hi)


def cargar_hallazgos(desde=None, hasta=None, solo_confirmados=False):
    """Hallazgos no descartados en [desde, hasta] (fechas AAAA-MM-DD, inclusive)."""
    sql, params = "SELECT * FROM hallazgos WHERE estado!='descartado'", []
    if solo_confirmados:
        sql += " AND estado='confirmado'"
    if desde:
        sql += ' AND substr(fecha_publicacion,1,10) >= ?'
        params.append(desde)
    if hasta:
        sql += ' AND substr(fecha_publicacion,1,10) <= ?'
        params.append(hasta)
    with db() as conn:
        return [dict(r) for r in conn.execute(sql + ' ORDER BY fecha_publicacion', params)]


def cortes_por_franja(hallazgos):
    """{franja: (p25, p75, n)} con todos los cargos con anos cruzados por cargo. Usa todo
    el historico disponible, no solo la semana: la referencia gana estabilidad con los dias."""
    grupos = defaultdict(list)
    for h in hallazgos:
        if h['anos_nivel'] == 'cargo' and h['pago_smlv'] is not None:
            grupos[franja(h['anos_experiencia'])].append(h['pago_smlv'])
    cortes = {}
    for f, valores in grupos.items():
        if len(valores) >= MIN_COMPARABLES:
            q = quantiles(valores, n=4, method='inclusive')
            cortes[f] = (q[0], q[2], len(valores))
    return cortes


def clasificar(h, cortes):
    """'destacado' | 'bajo' | 'en_rango' | None (sin comparables suficientes o sin anos por cargo)."""
    if h['anos_nivel'] != 'cargo' or h['pago_smlv'] is None:
        return None
    corte = cortes.get(franja(h['anos_experiencia']))
    if not corte:
        return None
    p25, p75, _ = corte
    if h['pago_smlv'] >= p75:
        return 'destacado'
    if h['pago_smlv'] <= p25:
        return 'bajo'
    return 'en_rango'


def semana_de(fecha):
    """Lunes y domingo de la semana ISO que contiene 'fecha'."""
    d = date.fromisoformat(fecha)
    lunes = d - timedelta(days=d.weekday())
    return lunes, lunes + timedelta(days=6)


def resumen_semana(fecha, solo_confirmados=False):
    lunes, domingo = semana_de(fecha)
    desde, hasta = lunes.isoformat(), domingo.isoformat()
    with db() as conn:
        dias = [
            dict(r)
            for r in conn.execute('SELECT * FROM dias WHERE fecha BETWEEN ? AND ? ORDER BY fecha', (desde, hasta))
        ]
    todos = cargar_hallazgos(solo_confirmados=solo_confirmados)
    cortes = cortes_por_franja(todos)
    semana = [dict(h, clase=clasificar(h, cortes)) for h in todos if desde <= h['fecha_publicacion'][:10] <= hasta]
    smlv = [h['pago_smlv'] for h in semana if h['pago_smlv'] is not None]

    por_entidad = defaultdict(list)
    for h in semana:
        if h['pago_smlv'] is not None:
            por_entidad[(h['entidad'], h['departamento'])].append(h)
    entidades = [
        {
            'entidad': e,
            'departamento': d,
            'cargos': len(hs),
            'mediana_smlv': round(median(x['pago_smlv'] for x in hs), 2),
            'destacados': sum(x['clase'] == 'destacado' for x in hs),
            'bajos': sum(x['clase'] == 'bajo' for x in hs),
            'referencia_tarifa': next((x['referencia_tarifa'] for x in hs if x.get('referencia_tarifa')), ''),
        }
        for (e, d), hs in por_entidad.items()
    ]
    por_dpto = defaultdict(list)
    for h in semana:
        if h['pago_smlv'] is not None:
            por_dpto[h['departamento']].append(h['pago_smlv'])
    return {
        'desde': desde,
        'hasta': hasta,
        'semana_iso': f'{lunes.isocalendar()[0]}-W{lunes.isocalendar()[1]:02d}',
        'dias': dias,
        'publicados': sum(d['publicados'] for d in dias),
        'analizados': sum(d['analizados'] for d in dias),
        'procesos_con_dato': len({h['proceso_id'] for h in semana}),
        'cargos': len(semana),
        'mediana_smlv': round(median(smlv), 2) if smlv else None,
        'bajo_2_smlv': sum(v < 2 for v in smlv),
        'hallazgos': semana,
        'cortes': cortes,
        'destacados': sorted([h for h in semana if h['clase'] == 'destacado'], key=lambda h: -h['pago_smlv']),
        'bajos': sorted([h for h in semana if h['clase'] == 'bajo'], key=lambda h: h['pago_smlv']),
        # Sin comparables suficientes todavia, igual se listan los extremos de la semana,
        # marcados como tales (no como "bajo"/"destacado").
        'mas_altos': sorted([h for h in semana if h['pago_smlv'] is not None], key=lambda h: -h['pago_smlv'])[:5],
        'mas_bajos': sorted([h for h in semana if h['pago_smlv'] is not None], key=lambda h: h['pago_smlv'])[:5],
        'con_tabla_oficial': sorted([e for e in entidades if e['referencia_tarifa']], key=lambda e: -e['mediana_smlv']),
        'entidades': sorted(entidades, key=lambda e: -e['mediana_smlv']),
        'por_departamento': {d: {'cargos': len(v), 'mediana_smlv': round(median(v), 2)} for d, v in por_dpto.items()},
        'solo_confirmados': solo_confirmados,
    }


def _linea(h):
    anos = f'{h["anos_experiencia"]} años' if h['anos_nivel'] == 'cargo' else 'años sin cruzar'
    return (
        f'- {h["cargo"]} ({anos}): ${pesos(h["pago_mensual_cop"])}/mes = '
        f'{str(h["pago_smlv"]).replace(".", ",")} SMLV. {h["entidad"]}, {h["departamento"]} '
        f'[#{h["id"]}, {h["estado"].replace("_", " ")}]'
    )


def escribir_resumen_semana(r, root=None):
    lunes, domingo = date.fromisoformat(r['desde']), date.fromisoformat(r['hasta'])
    titulo = (
        f'Semana del {lunes.day} de {MESES[lunes.month - 1]} al {domingo.day} de '
        f'{MESES[domingo.month - 1]} de {domingo.year}'
    )
    L = [
        f'# {titulo}',
        '',
        f'- Días con corte: {len(r["dias"])} de 7',
        f'- Procesos de mínima cuantía publicados: **{r["publicados"]}**',
        f'- Analizados: {r["analizados"]}',
        f'- Procesos con cargo y pago identificables: {r["procesos_con_dato"]} ({r["cargos"]} cargos)',
        f'- Mediana del pago mensual: **{str(r["mediana_smlv"]).replace(".", ",")} SMLV**'
        if r['mediana_smlv'] is not None
        else '- Mediana del pago mensual: sin dato',
        f'- Cargos por debajo de 2 SMLV: {r["bajo_2_smlv"]}',
        '',
    ]
    L += ['## Lo que se paga bien', '']
    if r['destacados']:
        L += ['Cuartil superior frente a cargos con la misma experiencia exigida:', '']
        L += [_linea(h) for h in r['destacados'][:10]]
    else:
        L += [
            'Aún no hay suficientes cargos comparables por franja de experiencia '
            f'(mínimo {MIN_COMPARABLES}) para marcar destacados. Los pagos más altos de la semana:',
            '',
        ]
        L += [_linea(h) for h in r['mas_altos']]
    L += ['', '## Lo que se paga poco', '']
    if r['bajos']:
        L += ['Cuartil inferior frente a cargos con la misma experiencia exigida:', '']
        L += [_linea(h) for h in r['bajos'][:10]]
    else:
        L += ['Aún sin comparables suficientes. Los pagos más bajos de la semana:', '']
        L += [_linea(h) for h in r['mas_bajos']]
    if r['con_tabla_oficial']:
        L += [
            '',
            '## De dónde sale el precio',
            '',
            'Procesos que citan una tabla oficial de tarifas. Es contexto, no un juicio: '
            'la tabla puede estar alta o baja.',
            '',
        ]
        L += [
            f'- {e["entidad"]} ({e["departamento"]}): "{e["referencia_tarifa"]}" '
            f'(mediana {str(e["mediana_smlv"]).replace(".", ",")} SMLV)'
            for e in r['con_tabla_oficial']
        ]
    L += ['', '## Por departamento', '']
    L += [
        f'- {d}: {v["cargos"]} cargos, mediana {str(v["mediana_smlv"]).replace(".", ",")} SMLV'
        for d, v in sorted(r['por_departamento'].items(), key=lambda x: -x[1]['cargos'])
    ] or ['- (sin datos)']
    L += ['', '## Referencia por franja de experiencia (todo el histórico)', '']
    L += [
        f'- {f}: P25 {p25:.2f} · P75 {p75:.2f} SMLV (n={n})'.replace('.', ',')
        for f, (p25, p75, n) in sorted(r['cortes'].items())
    ] or [f'- Aún ninguna franja con {MIN_COMPARABLES} o más cargos comparables.']
    L += [
        '',
        'Publicar solo cifras con estado confirmado (`honorario-justo --marcar ID confirmado`).'
        if not r['solo_confirmados']
        else 'Solo cifras confirmadas.',
    ]
    carpeta = indicadores_folder(root) / 'semanas'
    carpeta.mkdir(exist_ok=True)
    path = carpeta / f'semana_{r["semana_iso"]}.md'
    path.write_text('\n'.join(L) + '\n', encoding='utf-8')
    return str(path)


def escribir_resumen_dia(fila, root=None):
    """Base del post diario, solo con cifras trazables a 'dias' y 'hallazgos'.
    La redaccion final sigue REDACCION.md."""
    with db() as conn:
        filas = conn.execute(
            'SELECT id,cargo,anos_experiencia,anos_nivel,pago_mensual_cop,pago_smlv,entidad,'
            'departamento,confianza,estado FROM hallazgos WHERE fecha_publicacion LIKE ? '
            "AND estado!='descartado' ORDER BY pago_smlv",
            (fila['fecha'] + '%',),
        ).fetchall()
    fecha = date.fromisoformat(fila['fecha'])
    lineas = [
        f'# {fecha.day} de {MESES[fecha.month - 1]} de {fecha.year}',
        '',
        f'- Procesos de mínima cuantía publicados en SECOP II: **{fila["publicados"]}**',
        f'- Analizados (documentos descargados y leídos): {fila["analizados"]}',
        f'- Con página de pago por cargo detectada: {fila["con_presupuesto"]}',
        f'- Con al menos un cargo + pago mensual extraído: {fila["con_hallazgo"]} '
        f'({fila["hallazgos"]} cargos en total)',
        '',
        '## Cargos encontrados',
        '',
    ]
    for r in filas:
        if r['anos_experiencia'] is None:
            anos = 'años sin dato'
        else:
            anos = f'{r["anos_experiencia"]} años' + ('' if r['anos_nivel'] == 'cargo' else ' (máx. del proceso)')
        smlv = f'{r["pago_smlv"]:.2f} SMLV'.replace('.', ',') if r['pago_smlv'] is not None else 'SMLV sin dato'
        lineas.append(
            f'- #{r["id"]} {r["cargo"]} - {anos} - ${pesos(r["pago_mensual_cop"])}/mes ({smlv}) - '
            f'{r["entidad"]}, {r["departamento"]} [confianza {r["confianza"]}, {r["estado"].replace("_", " ")}]'
        )
    if not filas:
        lineas.append('- (ninguno)')
    lineas += ['', 'Publicar solo filas confirmadas (`honorario-justo --marcar ID confirmado`).']
    path = config.indicadores_folder(root) / f'dia_{fila["fecha"]}.md'
    path.write_text('\n'.join(lineas) + '\n', encoding='utf-8')
    return str(path)


def exportar_hallazgos(root=None):
    """Vuelca 'hallazgos' a CSV en Indicadores/, paralelo a Resultados/: mismo espiritu
    de trazabilidad (queda registrado que alimento cada version del tablero)."""
    path = config.indicadores_folder(root) / f'hallazgos_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    columns = ['id'] + HALLAZGO_COLS
    with db() as conn:
        rows = conn.execute(f'SELECT {",".join(columns)} FROM hallazgos ORDER BY fecha_publicacion DESC').fetchall()
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)
    return {'archivo': str(path), 'filas': len(rows)}
