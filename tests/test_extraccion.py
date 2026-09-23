import json

from test_app import application, make_pdf  # noqa: F401  (fixture reutilizado)

from extraccion import (confianza, en_smlv, match_requirement, parse_money_co, parse_requisitos_texto,
                        role_label, smlv_de)


def test_role_label_keeps_specialty_and_stops_before_requirements():
    assert role_label('Director de Interventoría con experiencia de 10 años') == 'director de interventoria'
    assert role_label('INGENIERO RESIDENTE DE INTERVENTORIA') == 'ingeniero residente de interventoria'
    assert role_label('sin ningun cargo aqui') == ''


def test_requisitos_por_cargo_toman_la_experiencia_general_de_cada_perfil():
    texto = ('Director de interventoria: ingeniero civil con experiencia profesional general minima de '
             'quince (15) anos y especifica de tres (3) anos.\n'
             'Residente de interventoria: ingeniero civil con experiencia general minima de diez (10) anos.\n'
             'Topografo con experiencia general de cinco (05) anos.')
    req = {r['cargo']: r['anos'] for r in parse_requisitos_texto(texto)}
    assert req['director de interventoria'] == 15
    assert req['residente de interventoria'] == 10
    assert req['topografo'] == 5


def test_match_requirement_cruza_por_perfil_y_no_adivina_en_empates():
    req = [{'cargo': 'director de interventoria', 'anos': 15},
           {'cargo': 'residente de interventoria', 'anos': 10}]
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


def _caso_con_pdf(a, cid='MC.EXT', fecha='2026-09-10T00:00:00.000'):
    cid = a.record_process({'id_del_proceso': cid, 'entidad': 'Municipio de prueba',
                            'departamento_entidad': 'Boyacá', 'fecha_de_publicacion_del': fecha})
    make_pdf(a.case_folder(cid) / 'documents' / 'sample.pdf')
    a.analyze_case(cid, a.settings())
    return a.get_case(cid)


def test_extraer_caso_cruza_anos_y_normaliza_en_smlv(application):
    a = application
    items = a.extraer_caso(_caso_con_pdf(a), use_ollama=False)
    director = [i for i in items if 'director' in i['cargo']]
    assert director, items
    d = director[0]
    assert d['anos_experiencia'] == 5 and d['anos_nivel'] == 'cargo'
    assert d['pago_smlv'] == en_smlv(d['pago_mensual_cop'], '2026')
    assert d['confianza'] in {'alta', 'media'}


def test_reprocesar_no_duplica_y_respeta_revision_humana(application):
    a = application
    case = _caso_con_pdf(a)
    n1 = a.guardar_hallazgos_caso(case, a.extraer_caso(case, use_ollama=False))
    with a.db() as conn:
        primero = conn.execute('SELECT id FROM hallazgos ORDER BY id').fetchone()[0]
    a.marcar_hallazgo(primero, 'confirmado')
    a.guardar_hallazgos_caso(case, a.extraer_caso(case, use_ollama=False))
    with a.db() as conn:
        rows = conn.execute('SELECT id,estado FROM hallazgos').fetchall()
    assert len(rows) == n1
    assert (primero, 'confirmado') in [tuple(r) for r in rows]


def test_reprocesar_no_duplica_si_el_cargo_se_corrigio_a_mano(application):
    a = application
    case = _caso_con_pdf(a)
    n1 = a.guardar_hallazgos_caso(case, a.extraer_caso(case, use_ollama=False))
    with a.db() as conn:
        primero = conn.execute('SELECT id FROM hallazgos ORDER BY id').fetchone()[0]
        conn.execute("UPDATE hallazgos SET cargo='Coordinador corregido', anos_experiencia=1 WHERE id=?", (primero,))
    a.marcar_hallazgo(primero, 'confirmado')
    a.guardar_hallazgos_caso(case, a.extraer_caso(case, use_ollama=False))
    with a.db() as conn:
        rows = conn.execute('SELECT id,cargo,estado FROM hallazgos').fetchall()
    assert len(rows) == n1
    assert (primero, 'Coordinador corregido', 'confirmado') in [tuple(r) for r in rows]


def test_run_day_procesa_todo_el_dia_sin_palabra_clave(application, monkeypatch):
    a = application
    vistos = {}

    class FakeClient:
        def count_processes(self, settings):
            vistos['keywords'] = settings['keywords']
            return 2

        def all_processes(self, settings):
            return [{'id_del_proceso': f'DIA.{i}', 'fecha_de_publicacion_del': '2026-09-18T00:00:00.000',
                     'departamento_entidad': 'Santander', 'entidad': 'E'} for i in (1, 2)]

        def documents(self, meta):
            return [], []

    monkeypatch.setattr(a, 'SecopClient', FakeClient)
    for i in (1, 2):
        make_pdf(a.case_folder(f'DIA.{i}') / 'documents' / 'sample.pdf')
    fila = a.run_day('2026-09-18')
    assert vistos['keywords'] == ''
    assert fila['publicados'] == 2 and fila['analizados'] == 2
    assert fila['con_hallazgo'] == 2
    resumen = a.escribir_resumen_dia(fila, root=a.DATA / 'ind')
    texto = open(resumen, encoding='utf-8').read()
    assert '18 de septiembre de 2026' in texto and '**2**' in texto


def test_tablero_incrusta_datos_y_mapa(application, tmp_path):
    a = application
    a.guardar_hallazgos_caso(case := _caso_con_pdf(a), a.extraer_caso(case, use_ollama=False))
    import tablero
    out = tablero.generar_tablero(root=tmp_path)
    html = open(out['archivo'], encoding='utf-8').read()
    assert '/*__DATOS__*/null' not in html
    datos = json.loads(html.split('const D = ')[1].split(';\nconst GEO')[0])
    assert datos['hallazgos'] and datos['hallazgos'][0]['dpto_k'] == 'boyaca'
    assert tablero.clave_dpto('Distrito Capital de Bogotá') == 'santafe de bogota d.c'


def test_dias_pendientes_se_pone_al_dia_y_retoma_cortes_a_medias(application):
    a = application
    assert a.dias_pendientes('2026-09-20') == ['2026-09-20']  # primera vez: solo el ultimo
    with a.db() as conn:
        conn.execute("INSERT INTO dias(fecha,publicados,analizados,updated_at) VALUES ('2026-09-16',226,226,'x')")
        conn.execute("INSERT INTO dias(fecha,publicados,analizados,updated_at) VALUES ('2026-09-17',144,144,'x')")
        conn.execute("INSERT INTO dias(fecha,publicados,analizados,updated_at) VALUES ('2026-09-15',254,40,'x')")
    # 18-20 no se corrieron y el 15 quedo a medias.
    assert a.dias_pendientes('2026-09-20') == ['2026-09-15', '2026-09-18', '2026-09-19', '2026-09-20']
    # Nunca mas de max_dias hacia atras.
    assert a.dias_pendientes('2026-10-30')[0] == '2026-10-24'


MARIPI_P20 = '''el analisis de precios ... teniendo como base las cotizaciones requeridas.
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
'''


def test_tabla_de_cotizaciones_tipo_maripi(monkeypatch):
    """Regresion: Maripi (CO1.REQ.11051829) es el ejemplo de lo que se paga bien y el
    extractor no lo veia (unidad 'mes', montos sin $, perfil en celda de varias lineas)."""
    import extraccion
    from evidence import scores
    monkeypatch.setattr(extraccion, 'cached_pages', lambda cid, f: {20: MARIPI_P20})
    monkeypatch.setattr(extraccion, 'page_method', lambda cid, f, p: 'texto')
    items = extraccion.extract_pay_filas('X', 'x.pdf', 20)
    assert [(i['cargo'], i['pago_mensual_cop'], i['anos_fila']) for i in items] == [
        ('profesional biologo', 9_350_000, 5), ('profesional en ingeniera ambiental', 7_000_000, 2)]
    assert scores(MARIPI_P20)['presupuesto'] > 0


def test_dias_pendientes_no_se_salta_dias_tras_un_corte_a_medias(application):
    a = application
    with a.db() as conn:
        conn.execute("INSERT INTO dias(fecha,publicados,analizados,updated_at) VALUES ('2026-09-18',118,10,'x')")
    assert a.dias_pendientes('2026-09-20') == ['2026-09-18', '2026-09-19', '2026-09-20']


def test_resumir_alertas_agrupa_paginas_por_documento(application):
    w = [f'doc.pdf: Pagina {p}: limite de OCR alcanzado' for p in range(38, 78)] + ['otro.pdf: CAPTCHA']
    assert application.resumir_alertas(w) == [
        'otro.pdf: CAPTCHA', 'doc.pdf: limite de OCR alcanzado en 40 pagina(s) (38-77)']


def test_clasificacion_relativa_a_la_misma_franja_de_experiencia(application):
    import indicadores
    base = [{'anos_nivel': 'cargo', 'anos_experiencia': 5, 'pago_smlv': v} for v in (2, 2.5, 3, 3, 3.5, 3.5, 4, 5.34)]
    cortes = indicadores.cortes_por_franja(base)
    assert indicadores.clasificar(base[-1], cortes) == 'destacado'   # tipo Maripi
    assert indicadores.clasificar(base[0], cortes) == 'bajo'
    # Otra franja sin comparables suficientes: no se etiqueta.
    assert indicadores.clasificar({'anos_nivel': 'cargo', 'anos_experiencia': 15, 'pago_smlv': 2.58}, cortes) is None
    # Sin anos por cargo nunca se clasifica.
    assert indicadores.clasificar({'anos_nivel': 'caso', 'anos_experiencia': 5, 'pago_smlv': 9}, cortes) is None


def test_imagenes_tamano_exacto_para_linkedin_y_tiktok(application, tmp_path):
    from PIL import Image
    import imagenes
    a = application
    case = _caso_con_pdf(a, fecha='2026-09-16T00:00:00.000')
    a.guardar_hallazgos_caso(case, a.extraer_caso(case, use_ollama=False))
    with a.db() as conn:
        conn.execute("INSERT INTO dias(fecha,publicados,analizados,updated_at) VALUES ('2026-09-16',5,5,'x')")
    a.resumen_dia('2026-09-16')
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
    from secop import doc_score
    assert doc_score({'nombre_archivo': '5. OK E.P. DISEÑO CUBIERTAS.pdf'}) > 0
    assert doc_score({'nombre_archivo': '6. IP CONS DISEÑO CUBIERTAS(1).pdf'}) > 0
    assert doc_score({'nombre_archivo': '4 CDP.pdf'}) <= 0
    assert doc_score({'nombre_archivo': '2. Analisis del Sector Estadio revisado ok.pdf'}) > 0
    assert doc_score({'nombre_archivo': 'ANALISIS PRECIOS DEL MERCADO CMI-2026073.pdf'}) > 0
    assert doc_score({'nombre_archivo': '2. COTIZACIONES.pdf'}) > 0
    assert doc_score({'nombre_archivo': 'SOLICITUD COTIZACION.xlsx'}) < 0
    assert doc_score({'nombre_archivo': '3. Estudio Previo.pdf'}) > doc_score({'nombre_archivo': '2. COTIZACIONES.pdf'})
    assert doc_score({'nombre_archivo': 'propuesta ip.pdf'}) < 0


GUADALUPE_P15 = '''personal                factor     valor                    valor                  mes
director    de      la
$3,450,000        1.70        $5,865,000     2      100%     $11,730,000 consultoria
ingeniero estructural    $2,300,000        1.70        $3,910,000     2      100%      $7,820,000
ingeniero electricista    $1,750,000        1.70        $2,975,000     2      50%      $2,975,000
arquitecto             $2,200,000        1.70        $3,740,000     2      75%      $5,610,000
comision         de
topografia     (incluye  $1,750,000        1.70        $2,975,000     2      50%      $2,975,000
topografo y cadenero
subtotal personal profesional                          $31,110,000
'''


def test_tabla_de_personal_con_encabezado_lejano_tipo_guadalupe(monkeypatch):
    import extraccion
    from evidence import scores
    monkeypatch.setattr(extraccion, 'cached_pages', lambda cid, f: {15: GUADALUPE_P15})
    monkeypatch.setattr(extraccion, 'page_method', lambda cid, f, p: 'texto')
    pagos = sorted(i['pago_mensual_cop'] for i in extraccion.extract_pay_regex('X', 'x.pdf', 15))
    assert pagos == [1_750_000, 1_750_000, 2_200_000, 2_300_000, 3_450_000]  # base, no el valor con FM ni el subtotal
    assert scores(GUADALUPE_P15)['presupuesto'] > 0
