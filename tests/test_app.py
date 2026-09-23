import importlib
import json
from pathlib import Path

import fitz
import pytest

from evidence import analyze_pdf, classification, scores
from secop import SecopClient, doc_score


@pytest.fixture
def application(tmp_path, monkeypatch):
    monkeypatch.setenv('SECOP_DATA', str(tmp_path / 'data'))
    monkeypatch.setenv('SECOP_LOGS', str(tmp_path / 'logs'))
    import app
    return importlib.reload(app)


def make_pdf(path):
    with fitz.open() as doc:
        for text in [
            'ALCALDIA MUNICIPAL - MINIMA CUANTIA\nObjeto: consultoria tecnica para estudios de alumbrado publico.\nEntidad contratante: municipio de prueba. Este documento es una muestra sintetica de validacion.',
            'Equipo minimo profesional\nDirector de consultoria. Ingeniero con experiencia general de cinco (5) anos.\nExperiencia especifica en alumbrado publico. Matricula profesional vigente y titulo acreditado.',
            'Presupuesto de personal\nCargo: Director de consultoria. Salario mensual: $ 5.500.000\nDedicacion 80%. Valor mes: $ 4.400.000. Tres meses. Total: $ 13.200.000.\nAsesor financiero: $ 4.500.000 mensual.'
        ]:
            doc.new_page().insert_textbox(fitz.Rect(40,40,550,790), text, fontsize=12)
        doc.save(path)


def test_pay_evidence_is_per_person_not_per_item():
    por_item = ('ITEM DESCRIPCION UNIDAD CANTIDAD VALOR TOTAL COTIZACION 1 VALOR TOTAL PROMEDIO\n'
                'Diagnostico del sector con profesional de turismo, dedicacion mensual\n'
                'Producto 1 $ 10.400.000 $ 14.400.000 $ 12.400.000')
    assert scores(por_item)['presupuesto'] == 0
    telefono = 'Director del proyecto, dedicacion cuatro meses. Tel: (604) 8523101 Palacio Municipal'
    assert scores(telefono)['presupuesto'] == 0
    por_cargo = ('Cargo\nDirector de consultoria\nDedicacion 16,67%\nSalario base mensual\n$ 6.277.489\n'
                 'Disenador estructural\nSalario base mensual\n$ 4.511.149')
    assert scores(por_cargo)['presupuesto'] > 0


def test_classifier_requires_pay_and_role():
    assert scores('Experiencia en contratos. Presupuesto oficial $ 44.999.850')['presupuesto'] == 0
    assert scores('Director de consultoria. Salario mensual $ 5.500.000. Dedicacion 80%')['presupuesto'] > 0
    assert scores('Experiencia de la empresa en contratos de cinco anos')['experiencia'] == 0
    assert scores('Administracion del consultor. La experiencia nacional destaca los planos especificados.')['experiencia'] == 0
    assert classification({'objeto':1,'experiencia':1,'presupuesto':1},[],1)=='CANDIDATO'
    assert classification({'objeto':1,'experiencia':1,'presupuesto':1},['OCR incompleto'],1)=='REVISAR'
    assert classification({},[],0)=='SIN_DOCUMENTOS'


def test_pdf_extraction_and_cache(tmp_path, monkeypatch):
    path=tmp_path/'sample.pdf';make_pdf(path)
    result=analyze_pdf(path,tmp_path,{'max_pages':100,'max_ocr_pages':0})
    assert result['complete']
    assert result['pages'][0]['scores']['objeto']>0
    assert result['pages'][1]['scores']['experiencia']>0
    assert result['pages'][2]['scores']['presupuesto']>0
    monkeypatch.setattr(fitz,'open',lambda *args,**kwargs:pytest.fail('Cached PDF was reopened'))
    assert analyze_pdf(path,tmp_path,{'max_pages':100,'max_ocr_pages':0})==result


def test_analyze_case_produces_three_evidence(application):
    a = application
    cid = a.record_process({'id_del_proceso': 'TEST.1', 'referencia_del_proceso': 'MC-TEST',
                            'entidad': 'Entidad de prueba', 'fecha_de_publicacion_del': '2026-09-10'})
    make_pdf(a.case_folder(cid) / 'documents' / 'sample.pdf')
    a.analyze_case(cid, a.settings())
    case = a.get_case(cid)
    assert case['status'] == 'CANDIDATO'
    assert len(case['analysis']['evidence']) == 3
    assert case['analysis']['evidence']['presupuesto']['excerpt']


def test_local_rescore_does_not_download_inventory(application):
    a=application;cid=a.record_process({'id_del_proceso':'LOCAL'})
    make_pdf(a.case_folder(cid)/'documents'/'sample.pdf')
    with a.db() as conn:
        conn.execute('UPDATE cases SET analysis=? WHERE id=?',(json.dumps({'inventory':[{'nombre_archivo':'estudios previos.pdf'}]}),cid))
    a.analyze_case(cid,a.settings())
    assert a.get_case(cid)['analysis']['warnings']==[]


def test_settings_ignore_legacy_scheduler_keys(application):
    a = application
    a.set_value('settings', {'enabled': True, 'interval_hours': 1})
    assert 'enabled' not in a.settings()
    assert 'interval_hours' not in a.settings()


def test_next_candidate_skips_delivered_and_incomplete(application):
    a = application
    incomplete = a.record_process({'id_del_proceso': 'SIN.EVIDENCIA'})
    a.analyze_case(incomplete, a.settings())

    older = a.record_process({'id_del_proceso': 'MC.OLD', 'fecha_de_publicacion_del': '2026-01-01'})
    make_pdf(a.case_folder(older) / 'documents' / 'sample.pdf')
    a.analyze_case(older, a.settings())

    newer = a.record_process({'id_del_proceso': 'MC.NEW', 'fecha_de_publicacion_del': '2026-09-01'})
    make_pdf(a.case_folder(newer) / 'documents' / 'sample.pdf')
    a.analyze_case(newer, a.settings())

    assert a.next_candidate()['id'] == 'MC.NEW'

    a.mark_delivered('MC.NEW')
    assert a.next_candidate()['id'] == 'MC.OLD'
    assert a.load_delivered() == ['MC.NEW']

    a.mark_delivered('MC.OLD')
    assert a.next_candidate() is None
    assert a.next_candidate() != incomplete


def test_skip_reanalysis_discards_closed_cases_without_evidence(application):
    a = application
    open_case = {'id': 'X', 'status': 'SIN_DOCUMENTOS', 'checked_at': None,
                'metadata': {'fecha_de_recepcion_de': '2999-01-01T00:00:00.000'}}
    assert a.skip_reanalysis(open_case) is False

    closed_case = {'id': 'X', 'status': 'SIN_EVIDENCIA', 'checked_at': None,
                   'metadata': {'fecha_de_recepcion_de': '2000-01-01T00:00:00.000'}}
    assert a.skip_reanalysis(closed_case) is True

    closed_but_useful = {'id': 'X', 'status': 'CANDIDATO', 'checked_at': None,
                         'metadata': {'fecha_de_recepcion_de': '2000-01-01T00:00:00.000'}}
    assert a.skip_reanalysis(closed_but_useful) is False


def test_skip_reanalysis_retries_recent_cases_cut_by_old_limits(application):
    a = application
    reciente = a.now()
    base = {'id': 'X', 'status': 'REVISAR', 'checked_at': reciente, 'metadata': {}}
    assert a.skip_reanalysis(dict(base, analysis={'warnings': []})) is True
    cortado_ocr = dict(base, analysis={'warnings': ['A.pdf: Pagina 16: limite de OCR alcanzado']})
    assert a.skip_reanalysis(cortado_ocr) is False
    cortado_paginas = dict(base, analysis={'warnings': ['A.pdf: PDF limitado a 100 de 105 paginas']})
    assert a.skip_reanalysis(cortado_paginas) is False
    # Un PDF que ya supera el tope actual no se reintenta en cada corrida.
    tope_actual = dict(base, analysis={'warnings': ['A.pdf: PDF limitado a 500 de 900 paginas']})
    assert a.skip_reanalysis(tope_actual) is True


def test_cursor_key_is_stable_across_days(application):
    a = application
    today = a.settings() | {'date_from': '2026-01-01', 'date_to': '2026-09-15'}
    tomorrow = a.settings() | {'date_from': '2026-01-02', 'date_to': '2026-09-16'}
    assert a.cursor_key_for(today) == a.cursor_key_for(tomorrow)
    other_keywords = tomorrow | {'keywords': 'OTRO'}
    assert a.cursor_key_for(tomorrow) != a.cursor_key_for(other_keywords)


def test_query_escaping_and_filter(monkeypatch):
    client=SecopClient();captured={}
    def query(dataset,**params):
        captured.update(params);return []
    monkeypatch.setattr(client,'query',query)
    client.processes({'date_from':'2026-01-01','date_to':'2026-09-14','keywords':"O'HARE",'department':'Caldas','batch_size':10})
    assert "O''HARE" in captured['where']
    assert "departamento_entidad='Caldas'" in captured['where']
    assert "Mínima cuantía" in captured['where']


def test_download_blocks_external_hosts(tmp_path):
    with pytest.raises(ValueError,match='dominios'):
        SecopClient().download({'url_descarga_documento':{'url':'https://127.0.0.1/private'},'id_documento':'1'},tmp_path)
    assert doc_score({'nombre_archivo':'ESTUDIOS PREVIOS.pdf'})>0
    assert doc_score({'nombre_archivo':'oferta economica.pdf'})<0


def test_marcar_guarda_etiqueta_y_reporte_de_aprendizaje(application, tmp_path):
    import aprendizaje
    a = application
    cid = a.record_process({'id_del_proceso': 'APR.1', 'tipo_de_contrato': 'Consultoría',
                            'codigo_principal_de_categoria': 'V1.81101500', 'fecha_de_publicacion_del': '2026-09-18'})
    folder = a.case_folder(cid)
    texto = 'COSTOS DIRECTOS DE PERSONAL\nDirector de consultoria $ 5.500.000 dedicacion 50%\nFACTOR MULTIPLICADOR 2,1'
    (folder / 'text' / 'abc.json').write_text(json.dumps({'pages': [{'page': 1, 'text': 'x', 'method': 'texto'},
                                                                     {'page': 2, 'text': texto, 'method': 'ocr'}]}), encoding='utf-8')
    analysis = {'documents': [{'file': 'f.pdf', 'name': '6. Estudios del Sector.pdf', 'sha256': 'abc', 'pages': 2}]}
    with a.db() as conn:
        conn.execute('UPDATE cases SET analysis=?, checked_at=? WHERE id=?', (json.dumps(analysis), a.now(), cid))
        hid = conn.execute("INSERT INTO hallazgos(proceso_id,cargo,pago_mensual_cop,metodo,archivo_fuente,pagina_fuente,created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (cid, 'director', 5500000, 'tabla', 'f.pdf', 2, a.now())).lastrowid
    a.marcar_hallazgo(hid, 'confirmado')
    with a.db() as conn:
        e = dict(conn.execute('SELECT * FROM etiquetas WHERE hallazgo_id=?', (hid,)).fetchone())
    assert e['tipo_documento'] == 'estudio_sector' and e['segmento'] == '81' and e['metodo_pagina'] == 'ocr'
    assert {'costos_directos_personal', 'factor_multiplicador', 'dedicacion'} <= set(json.loads(e['senales']))
    assert 'costos directos de personal' in json.loads(e['titulos'])
    otro = a.record_process({'id_del_proceso': 'APR.2', 'tipo_de_contrato': 'Suministros',
                             'fecha_de_publicacion_del': '2026-09-19'})
    with a.db() as conn:
        conn.execute('UPDATE cases SET analysis=?, checked_at=? WHERE id=?', (json.dumps({'documents': []}), a.now(), otro))
        conn.executemany('INSERT INTO dias(fecha,publicados,analizados,con_documentos,con_presupuesto,con_hallazgo,'
                         'hallazgos,updated_at) VALUES (?,1,1,0,0,0,0,?)', [('2026-09-18', a.now()), ('2026-09-19', a.now())])
    salida = a.generar_aprendizaje(tmp_path)
    md = Path(salida['archivo']).read_text(encoding='utf-8')
    assert Path(salida['archivo']).name == 'acumulado.md'
    assert salida['etiquetas'] == 1 and 'Evidencia insuficiente' in md and '81 ingenieria' in md and 'Suministros' in md
    # Cada dia por separado: el 18 solo ve su consultoria, el 19 solo su suministro.
    assert set(salida['dias']) == {'2026-09-18', '2026-09-19'}
    d18 = Path(salida['dias']['2026-09-18']['archivo']).read_text(encoding='utf-8')
    d19 = Path(salida['dias']['2026-09-19']['archivo']).read_text(encoding='utf-8')
    assert 'Aprendizaje del dia 2026-09-18' in d18 and 'Consultoría' in d18 and 'Suministros' not in d18
    assert 'Suministros' in d19 and 'Consultoría' not in d19 and '## Sugerencias' not in d19
    assert salida['dias']['2026-09-18']['etiquetas'] == 1 and salida['dias']['2026-09-19']['etiquetas'] == 0
    a.marcar_hallazgo(hid, 'sin_revisar')
    with a.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM etiquetas').fetchone()[0] == 0


def test_skip_reanalysis_retries_cases_with_new_candidate_documents(application):
    a = application
    inventario = [{'nombre_archivo': 'ESTUDIO PREVIO.pdf'}, {'nombre_archivo': 'ANALISIS DEL SECTOR.pdf'},
                  {'nombre_archivo': 'CDP.pdf'}]
    base = {'id': 'X', 'status': 'REVISAR', 'checked_at': a.now(), 'metadata': {}}
    completo = dict(base, analysis={'inventory': inventario, 'warnings': [],
                                    'documents': [{'name': 'ESTUDIO PREVIO.pdf'}, {'name': 'ANALISIS DEL SECTOR.pdf'}]})
    assert a.skip_reanalysis(completo) is True
    falta_sector = dict(base, analysis={'inventory': inventario, 'warnings': [], 'documents': [{'name': 'ESTUDIO PREVIO.pdf'}]})
    assert a.skip_reanalysis(falta_sector) is False
    fallo_descarga = dict(base, analysis={'inventory': inventario, 'documents': [{'name': 'ESTUDIO PREVIO.pdf'}],
                                          'warnings': ['ANALISIS DEL SECTOR.pdf: Documento supera el limite de 40 MB']})
    assert a.skip_reanalysis(fallo_descarga) is True
