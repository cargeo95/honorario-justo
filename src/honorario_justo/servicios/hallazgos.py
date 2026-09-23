"""Caso de uso: extraer y revisar hallazgos (cargo, experiencia exigida y pago mensual).

Todo hallazgo entra como ``sin_revisar``. La revisión humana (``marcar_hallazgo``) lo confirma
o lo descarta y deja una etiqueta de la que aprende el observatorio.
"""

import logging

from honorario_justo.almacenamiento.base_datos import case_folder, db, resumen_dia, save_hallazgo, unpack
from honorario_justo.dominio import aprendizaje
from honorario_justo.dominio.extraccion import (
    confianza,
    en_smlv,
    extract_pay,
    extract_requirements,
    match_requirement,
    referencia_tarifa,
    role_tokens,
)
from honorario_justo.dominio.texto import ROLE, normal


def _unicas(alternatives):
    vistas = set()
    for alt in alternatives:
        if (alt['file'], alt['page']) not in vistas:
            vistas.add((alt['file'], alt['page']))
            yield alt


def extraer_caso(case, use_ollama=True):
    """Hallazgos (cargo + pago mensual) de un caso ya analizado, con los anos exigidos
    para ESE cargo cuando se pueden cruzar con la tabla de requisitos (anos_nivel='cargo').
    Si no se pueden cruzar, quedan sin dato: antes se rellenaba con el maximo de anos de la
    pagina y eso mostraba cifras falsas (Putumayo 5 anos en vez de 12 meses; Guapota 3 en
    vez de 10 y 5, 18-sep-2026). Mejor vacio que inventado: lo completa la revision."""
    alternatives = case['analysis'].get('alternatives', {})
    requisitos = []
    for alt in _unicas(alternatives.get('experiencia', [])):
        requisitos.extend(extract_requirements(case['id'], alt['file'], alt['page']))
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
            formacion = item.pop('formacion_fila', None) or match_requirement(item['cargo'], requisitos, 'formacion')
            item = dict(
                item,
                archivo_fuente=alt['file'],
                pagina_fuente=alt['page'],
                anos_experiencia=anos,
                formacion=formacion,
                anos_nivel='cargo' if anos is not None else None,
                pago_smlv=en_smlv(item['pago_mensual_cop'], fecha),
                referencia_tarifa=tarifa,
            )
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
            (case['id'],),
        ):
            revisados |= {('cargo', normal(cargo), pago), ('fuente', pago, archivo, pagina)}
        nuevos = [
            i
            for i in encontrados
            if ('cargo', normal(i['cargo']), i['pago_mensual_cop']) not in revisados
            and ('fuente', i['pago_mensual_cop'], i.get('archivo_fuente'), i.get('pagina_fuente')) not in revisados
        ]
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
