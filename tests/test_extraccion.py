import json

from honorario_justo import config
from honorario_justo.almacenamiento import base_datos
from honorario_justo.dominio.extraccion import (
    confianza,
    en_smlv,
    match_requirement,
    parse_money_co,
    parse_requisitos_texto,
    role_label,
    smlv_de,
)
from honorario_justo.reportes import indicadores
from honorario_justo.servicios import analisis, corte_diario, hallazgos
from test_app import make_pdf


def test_role_label_keeps_specialty_and_stops_before_requirements():
    assert role_label('Director de Interventoría con experiencia de 10 años') == 'director de interventoria'
    assert role_label('INGENIERO RESIDENTE DE INTERVENTORIA') == 'ingeniero residente de interventoria'
    assert role_label('sin ningun cargo aqui') == ''


def test_requisitos_por_cargo_toman_la_experiencia_general_de_cada_perfil():
    texto = (
        'Director de interventoria: ingeniero civil con experiencia profesional general minima de '
        'quince (15) anos y especifica de tres (3) anos.\n'
        'Residente de interventoria: ingeniero civil con experiencia general minima de diez (10) anos.\n'
        'Topografo con experiencia general de cinco (05) anos.'
    )
    req = {r['cargo']: r['anos'] for r in parse_requisitos_texto(texto)}
    assert req['director de interventoria'] == 15
    assert req['residente de interventoria'] == 10
    assert req['topografo'] == 5


def test_match_requirement_cruza_por_perfil_y_no_adivina_en_empates():
    req = [{'cargo': 'director de interventoria', 'anos': 15}, {'cargo': 'residente de interventoria', 'anos': 10}]
    assert match_requirement('DIRECTOR DE INTERVENTORIA', req) == 15
    assert match_requirement('Ingeniero Residente de Interventoría', req) == 10
    # "interventoria" sola empata entre dos perfiles con anos distintos: sin dato.
    assert match_requirement('apoyo de interventoria', req) is None
    # "profesional" no distingue ningun perfil.
    assert match_requirement('profesional', req) is None


def test_smlv_por_ano_de_publicacion():
    assert smlv_de('2026-09-18T00:00:00.000') == 1_750_905
    assert smlv_de('2025-03-01') == 1_423_500
    assert smlv_de('1999-01-01') is None
    assert en_smlv(3_501_810, '2026-01-10') == 2.0
    assert parse_money_co('6.829.554,00') == 6_829_554


def test_confianza_baja_en_ocr_ia_y_pagos_sospechosos():
    assert confianza({'metodo': 'tabla_pdfplumber', 'pago_smlv': 3}, 'texto') == 'alta'
    assert confianza({'metodo': 'regex_texto', 'pago_smlv': 3}, 'texto') == 'media'
    assert confianza({'metodo': 'regex_texto', 'pago_smlv': 3}, 'ocr') == 'baja'
    assert confianza({'metodo': 'ia_ollama', 'pago_smlv': 3}, 'texto') == 'baja'
    assert confianza({'metodo': 'tabla_pdfplumber', 'pago_smlv': 18.7}, 'texto') == 'baja'


def _caso_con_pdf(cid='MC.EXT', fecha='2026-09-10T00:00:00.000'):
    cid = base_datos.record_process(
        {
            'id_del_proceso': cid,
            'entidad': 'Municipio de prueba',
            'departamento_entidad': 'Boyacá',
            'fecha_de_publicacion_del': fecha,
        }
    )
    make_pdf(base_datos.case_folder(cid) / 'documents' / 'sample.pdf')
    analisis.analyze_case(cid, analisis.settings())
    return base_datos.get_case(cid)


def test_extraer_caso_cruza_anos_y_normaliza_en_smlv(application):
    items = hallazgos.extraer_caso(_caso_con_pdf(), use_ollama=False)
    director = [i for i in items if 'director' in i['cargo']]
    assert director, items
    d = director[0]
    assert d['anos_experiencia'] == 5 and d['anos_nivel'] == 'cargo'
    assert d['pago_smlv'] == en_smlv(d['pago_mensual_cop'], '2026')
    assert d['confianza'] in {'alta', 'media'}


def test_reprocesar_no_duplica_y_respeta_revision_humana(application):
    case = _caso_con_pdf()
    n1 = hallazgos.guardar_hallazgos_caso(case, hallazgos.extraer_caso(case, use_ollama=False))
    with base_datos.db() as conn:
        primero = conn.execute('SELECT id FROM hallazgos ORDER BY id').fetchone()[0]
    hallazgos.marcar_hallazgo(primero, 'confirmado')
    hallazgos.guardar_hallazgos_caso(case, hallazgos.extraer_caso(case, use_ollama=False))
    with base_datos.db() as conn:
        rows = conn.execute('SELECT id,estado FROM hallazgos').fetchall()
    assert len(rows) == n1
    assert (primero, 'confirmado') in [tuple(r) for r in rows]


def test_reprocesar_no_duplica_si_el_cargo_se_corrigio_a_mano(application):
    case = _caso_con_pdf()
    n1 = hallazgos.guardar_hallazgos_caso(case, hallazgos.extraer_caso(case, use_ollama=False))
    with base_datos.db() as conn:
        primero = conn.execute('SELECT id FROM hallazgos ORDER BY id').fetchone()[0]
        conn.execute("UPDATE hallazgos SET cargo='Coordinador corregido', anos_experiencia=1 WHERE id=?", (primero,))
    hallazgos.marcar_hallazgo(primero, 'confirmado')
    hallazgos.guardar_hallazgos_caso(case, hallazgos.extraer_caso(case, use_ollama=False))
    with base_datos.db() as conn:
        rows = conn.execute('SELECT id,cargo,estado FROM hallazgos').fetchall()
    assert len(rows) == n1
    assert (primero, 'Coordinador corregido', 'confirmado') in [tuple(r) for r in rows]


def test_run_day_procesa_todo_el_dia_sin_palabra_clave(application, monkeypatch):
    vistos = {}

    class FakeClient:
        def all_processes(self, settings):
            vistos['keywords'] = settings['keywords']
            # La fila de DIA.1 viene repetida, como pasa en Datos Abiertos.
            return [
                {
                    'id_del_proceso': 'DIA.1',
                    'fecha_de_publicacion_del': '2026-09-18T00:00:00.000',
                    'departamento_entidad': 'Santander',
                    'entidad': 'E',
                }
            ] + [
                {
                    'id_del_proceso': f'DIA.{i}',
                    'fecha_de_publicacion_del': '2026-09-18T00:00:00.000',
                    'departamento_entidad': 'Santander',
                    'entidad': 'E',
                }
                for i in (1, 2)
            ]

        def documents(self, meta):
            return [], []

    monkeypatch.setattr(corte_diario, 'SecopClient', FakeClient)
    for i in (1, 2):
        make_pdf(base_datos.case_folder(f'DIA.{i}') / 'documents' / 'sample.pdf')
    fila = corte_diario.run_day('2026-09-18')
    assert vistos['keywords'] == ''
    assert fila['publicados'] == 2 and fila['analizados'] == 2
    assert fila['con_hallazgo'] == 2
    resumen = indicadores.escribir_resumen_dia(fila, root=config.DATA / 'ind')
    texto = open(resumen, encoding='utf-8').read()
    assert '18 de septiembre de 2026' in texto and '**2**' in texto


def test_tablero_incrusta_datos_y_mapa(application, tmp_path):
    hallazgos.guardar_hallazgos_caso(case := _caso_con_pdf(), hallazgos.extraer_caso(case, use_ollama=False))
    from honorario_justo.reportes import tablero

    out = tablero.generar_tablero(root=tmp_path)
    html = open(out['archivo'], encoding='utf-8').read()
    assert '/*__DATOS__*/null' not in html
    datos = json.loads(html.split('const D = ')[1].split(';\nconst GEO')[0])
    assert datos['hallazgos'] and datos['hallazgos'][0]['dpto_k'] == 'boyaca'
    assert tablero.clave_dpto('Distrito Capital de Bogotá') == 'santafe de bogota d.c'


def test_dias_pendientes_se_pone_al_dia_y_retoma_cortes_a_medias(application):
    assert corte_diario.dias_pendientes('2026-09-20') == ['2026-09-20']  # primera vez: solo el ultimo
    with base_datos.db() as conn:
        conn.execute("INSERT INTO dias(fecha,publicados,analizados,updated_at) VALUES ('2026-09-16',226,226,'x')")
        conn.execute("INSERT INTO dias(fecha,publicados,analizados,updated_at) VALUES ('2026-09-17',144,144,'x')")
        conn.execute("INSERT INTO dias(fecha,publicados,analizados,updated_at) VALUES ('2026-09-15',254,40,'x')")
    # 18-20 no se corrieron y el 15 quedo a medias.
    assert corte_diario.dias_pendientes('2026-09-20') == ['2026-09-15', '2026-09-18', '2026-09-19', '2026-09-20']
    # Nunca mas de max_dias hacia atras.
    assert corte_diario.dias_pendientes('2026-10-30')[0] == '2026-10-24'


MARIPI_P20 = """el analisis de precios ... teniendo como base las cotizaciones requeridas.
      cotizacion  cotizacion    valor      concepto    unidad  cantidad   valor total      1    2    unitario
  honorarios profesional
  profesional biologo con especializacion en
  gestion ambiental con experiencia general
  minima requerida de cinco (05) anos, de los
                              $          $         $         $
  cuales certifique dos (02) anos de experiencia   mes      1     10.200.000  8.500.000   9.350.000   9.350.000
  especifica en  proyectos  ambientales  y/o
  profesional en  ingeniera  ambiental  con
  especializacion en sistemas integrados de
  gestion qhse y experiencia minima general
  requerida de dos (02) anos en actividades
  forestales, dos (02) anos de experiencia       $          $         $         $       mes      1
  especifica  en  sistemas de  informacion      7.800.000   6.200.000   7.000.000   7.000.000
"""


def test_tabla_de_cotizaciones_tipo_maripi(monkeypatch):
    """Regresion: Maripi (CO1.REQ.11051829) es el ejemplo de lo que se paga bien y el
    extractor no lo veia (unidad 'mes', montos sin $, perfil en celda de varias lineas)."""
    from honorario_justo.dominio import extraccion
    from honorario_justo.fuentes.documentos import scores

    monkeypatch.setattr(extraccion, 'cached_pages', lambda cid, f: {20: MARIPI_P20})
    monkeypatch.setattr(extraccion, 'page_method', lambda cid, f, p: 'texto')
    items = extraccion.extract_pay_filas('X', 'x.pdf', 20)
    assert [(i['cargo'], i['pago_mensual_cop'], i['anos_fila']) for i in items] == [
        ('profesional biologo', 9_350_000, 5),
        ('profesional en ingeniera ambiental', 7_000_000, 2),
    ]
    assert scores(MARIPI_P20)['presupuesto'] > 0


def test_dias_pendientes_no_se_salta_dias_tras_un_corte_a_medias(application):
    with base_datos.db() as conn:
        conn.execute("INSERT INTO dias(fecha,publicados,analizados,updated_at) VALUES ('2026-09-18',118,10,'x')")
    assert corte_diario.dias_pendientes('2026-09-20') == ['2026-09-18', '2026-09-19', '2026-09-20']


def test_resumir_alertas_agrupa_paginas_por_documento(application):
    w = [f'doc.pdf: Pagina {p}: limite de OCR alcanzado' for p in range(38, 78)] + ['otro.pdf: CAPTCHA']
    assert corte_diario.resumir_alertas(w) == [
        'otro.pdf: CAPTCHA',
        'doc.pdf: limite de OCR alcanzado en 40 pagina(s) (38-77)',
    ]


def test_clasificacion_relativa_a_la_misma_franja_de_experiencia(application):
    from honorario_justo.reportes import indicadores

    base = [{'anos_nivel': 'cargo', 'anos_experiencia': 5, 'pago_smlv': v} for v in (2, 2.5, 3, 3, 3.5, 3.5, 4, 5.34)]
    cortes = indicadores.cortes_por_franja(base)
    assert indicadores.clasificar(base[-1], cortes) == 'destacado'  # tipo Maripi
    assert indicadores.clasificar(base[0], cortes) == 'bajo'
    # Otra franja sin comparables suficientes: no se etiqueta.
    assert indicadores.clasificar({'anos_nivel': 'cargo', 'anos_experiencia': 15, 'pago_smlv': 2.58}, cortes) is None
    # Sin anos por cargo nunca se clasifica.
    assert indicadores.clasificar({'anos_nivel': 'caso', 'anos_experiencia': 5, 'pago_smlv': 9}, cortes) is None


def test_imagenes_tamano_exacto_para_linkedin_y_tiktok(application, tmp_path):
    from PIL import Image

    from honorario_justo.reportes import imagenes

    case = _caso_con_pdf(fecha='2026-09-16T00:00:00.000')
    hallazgos.guardar_hallazgos_caso(case, hallazgos.extraer_caso(case, use_ollama=False))
    with base_datos.db() as conn:
        conn.execute("INSERT INTO dias(fecha,publicados,analizados,updated_at) VALUES ('2026-09-16',5,5,'x')")
    base_datos.resumen_dia('2026-09-16')
    out = imagenes.imagenes_semana('2026-09-16', borrador=True, root=tmp_path)
    assert {Image.open(p).size for p in out['linkedin']} == {(1080, 1350)}
    assert {Image.open(p).size for p in out['tiktok']} == {(1080, 1920)}
    assert out['linkedin_pdf'].endswith('.pdf')
    # Sin cifras confirmadas y sin borrador no hay barras que mostrar: solo portada y metodo.
    solo = imagenes.imagenes_semana('2026-09-16', borrador=False, root=tmp_path / 'b')
    assert len(solo['linkedin']) == 2


def test_rol_no_corta_especialidad_que_empieza_por_articulo():
    assert role_label('ingeniero electricista    $1,750,000') == 'ingeniero electricista'
    assert role_label('director de la') == 'director'


def test_documentos_con_abreviaturas_se_descargan():
    from honorario_justo.fuentes.secop import doc_score

    assert doc_score({'nombre_archivo': '5. OK E.P. DISEÑO CUBIERTAS.pdf'}) > 0
    assert doc_score({'nombre_archivo': '6. IP CONS DISEÑO CUBIERTAS(1).pdf'}) > 0
    assert doc_score({'nombre_archivo': '4 CDP.pdf'}) <= 0
    assert doc_score({'nombre_archivo': '2. Analisis del Sector Estadio revisado ok.pdf'}) > 0
    assert doc_score({'nombre_archivo': 'ANALISIS PRECIOS DEL MERCADO CMI-2026073.pdf'}) > 0
    assert doc_score({'nombre_archivo': '2. COTIZACIONES.pdf'}) > 0
    assert doc_score({'nombre_archivo': 'SOLICITUD COTIZACION.xlsx'}) < 0
    assert doc_score({'nombre_archivo': '3. Estudio Previo.pdf'}) > doc_score({'nombre_archivo': '2. COTIZACIONES.pdf'})
    assert doc_score({'nombre_archivo': 'propuesta ip.pdf'}) < 0


GUADALUPE_P15 = """personal                factor     valor                    valor                  mes
director    de      la
$3,450,000        1.70        $5,865,000     2      100%     $11,730,000 consultoria
ingeniero estructural    $2,300,000        1.70        $3,910,000     2      100%      $7,820,000
ingeniero electricista    $1,750,000        1.70        $2,975,000     2      50%      $2,975,000
arquitecto             $2,200,000        1.70        $3,740,000     2      75%      $5,610,000
comision         de
topografia     (incluye  $1,750,000        1.70        $2,975,000     2      50%      $2,975,000
topografo y cadenero
subtotal personal profesional                          $31,110,000
"""


def test_tabla_de_personal_con_encabezado_lejano_tipo_guadalupe(monkeypatch):
    from honorario_justo.dominio import extraccion
    from honorario_justo.fuentes.documentos import scores

    monkeypatch.setattr(extraccion, 'cached_pages', lambda cid, f: {15: GUADALUPE_P15})
    monkeypatch.setattr(extraccion, 'page_method', lambda cid, f, p: 'texto')
    pagos = sorted(i['pago_mensual_cop'] for i in extraccion.extract_pay_regex('X', 'x.pdf', 15))
    assert pagos == [1_750_000, 1_750_000, 2_200_000, 2_300_000, 3_450_000]  # base, no el valor con FM ni el subtotal
    assert scores(GUADALUPE_P15)['presupuesto'] > 0


def test_experiencia_en_meses_y_formato():
    from honorario_justo.dominio.formato import experiencia

    # Putumayo, 18-sep-2026: requisito en meses.
    texto = 'Coordinador del proyecto. Experiencia profesional minima de doce (12) meses en coordinacion.'
    assert parse_requisitos_texto(texto) == [{'cargo': 'coordinador del proyecto', 'anos': 1}]
    assert parse_requisitos_texto('Psicologo con seis (6) meses de experiencia')[0]['anos'] == 0.5
    # Guapota: la experiencia general va primero; la especifica (3 anos) no la reemplaza.
    guapota = (
        'Director de Interventoria. No menor de diez (10) anos contados desde la matricula. '
        'Experiencia especifica no menor a tres (03) anos.'
    )
    assert parse_requisitos_texto(guapota)[0]['anos'] == 10
    assert experiencia(1) == '1 año' and experiencia(10) == '10 años'
    assert experiencia(0.5) == '6 meses' and experiencia(None) == 'sin dato'


def test_sin_cruce_de_requisitos_la_experiencia_queda_sin_dato(application, monkeypatch):
    # Antes se rellenaba con el maximo de anos de la pagina (Guapota: 3 en vez de 10 y 5).
    from honorario_justo.servicios import hallazgos as srv

    monkeypatch.setattr(srv, 'extract_requirements', lambda *a: [])
    items = srv.extraer_caso(_caso_con_pdf(), use_ollama=False)
    assert items and all(i['anos_experiencia'] is None and i['anos_nivel'] is None for i in items)
