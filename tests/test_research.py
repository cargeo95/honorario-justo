import json
from pathlib import Path

import fitz
import pytest

from honorario_justo.reportes.investigacion import allocate, export_research, x_length


def fixture_case(tmp_path):
    source = tmp_path / 'source'
    (source / 'documents').mkdir(parents=True)
    (source / 'text').mkdir()
    with fitz.open() as doc:
        doc.new_page().insert_text((40, 40), 'Original document for evidence export')
        doc.save(source / 'documents' / '1.pdf')
    evidence = {
        k: {'file': '1.pdf', 'name': 'Invitacion.pdf', 'page': 1} for k in ['objeto', 'experiencia', 'presupuesto']
    }
    case = {
        'id': 'TEST',
        'metadata': {
            'entidad': 'Entidad',
            'referencia_del_proceso': 'MC-1',
            'urlproceso': {'url': 'https://example.com/process'},
        },
        'analysis': {'evidence': evidence, 'documents': [{'file': '1.pdf', 'name': 'Invitacion.pdf'}]},
    }
    return case, source


def test_numbered_bundle_preserves_files(tmp_path):
    case, source = fixture_case(tmp_path)
    root = tmp_path / 'Resultados'
    first = export_research(case, source, 'Primera prueba', results_root=root)
    second = export_research(case, source, 'Otra prueba', results_root=root)
    assert [first['number'], second['number']] == [1, 2]
    folder = Path(first['folder'])
    assert len(list((folder / 'Evidencias').glob('*.png'))) == 3
    assert len(list((folder / 'Documentos').glob('*.pdf'))) == 1
    assert (folder / '01_LinkedIn.txt').exists()
    assert json.loads((folder / 'manifiesto.json').read_text(encoding='utf-8'))['case_id'] == 'TEST'
    assert (source / 'documents' / '1.pdf').exists()
    assert (folder / 'Datos/proceso_completo.json').exists()


def test_rejects_bad_posts_and_missing_evidence_before_allocating(tmp_path):
    case, source = fixture_case(tmp_path)
    root = tmp_path / 'Resultados'
    with pytest.raises(ValueError, match='280'):
        export_research(case, source, 'Prueba', {'x_post': 'x' * 281}, root)
    assert not root.exists()
    case['analysis']['evidence'].pop('experiencia')
    with pytest.raises(ValueError, match='tres'):
        export_research(case, source, 'Prueba', results_root=root)


def test_number_continues_existing_folders(tmp_path):
    root = tmp_path / 'Resultados'
    root.mkdir()
    (root / '7. anterior').mkdir()
    number, folder = allocate(root, '../Titulo: nuevo', 'TEST')
    assert number == 8
    assert folder.parent == root
    assert x_length('Hola https://example.com/' + 'a' * 200) == 28
