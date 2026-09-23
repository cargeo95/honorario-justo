"""Caso de uso: reportes de aprendizaje, uno por día de corte y uno acumulado."""

from honorario_justo import config
from honorario_justo.almacenamiento.base_datos import case_folder, db, unpack
from honorario_justo.dominio import aprendizaje


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
    folder = config.indicadores_folder(root) / 'aprendizaje'
    folder.mkdir(exist_ok=True)
    datos = aprendizaje.reporte(cases, hallazgos, etiquetas, case_folder)
    path = folder / 'acumulado.md'
    path.write_text(aprendizaje.markdown(datos), encoding='utf-8')
    dias = {}
    for f in fechas:
        del_dia = [c for c in cases if (c['metadata'].get('fecha_de_publicacion_del') or '').startswith(f)]
        ids = {c['id'] for c in del_dia}
        d = aprendizaje.reporte(
            del_dia,
            [h for h in hallazgos if h['proceso_id'] in ids],
            [e for e in etiquetas if e['proceso_id'] in ids],
            case_folder,
        )
        archivo = folder / f'dia_{f}.md'
        archivo.write_text(aprendizaje.markdown(d, fecha=f), encoding='utf-8')
        dias[f] = {
            'archivo': str(archivo),
            'procesos': d['procesos'],
            'con_cifra': d['con_cifra'],
            'etiquetas': d['etiquetas'],
        }
    return {
        'archivo': str(path),
        'procesos': datos['procesos'],
        'etiquetas': datos['etiquetas'],
        'sugerencias': datos['sugerencias'],
        'dias': dias,
    }
