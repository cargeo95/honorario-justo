"""Manually archive every supported public attachment for a selected case."""

import argparse
import json

from honorario_justo.almacenamiento import base_datos
from honorario_justo.fuentes.secop import SecopClient

parser = argparse.ArgumentParser()
parser.add_argument('case_id')
args = parser.parse_args()
client = SecopClient()
case = base_datos.get_case(args.case_id)
folder = base_datos.case_folder(args.case_id)
results = []
for row in case['analysis'].get('inventory', []):
    if row.get('extensi_n', '').lower() not in {'pdf', 'docx', 'xlsx'}:
        continue
    try:
        path = client.download(row, folder / 'documents')
        results.append(
            {'id': row['id_documento'], 'name': row['nombre_archivo'], 'file': path.name, 'status': 'DESCARGADO'}
        )
        print('OK', path.name, flush=True)
    except Exception as exc:
        results.append({'id': row['id_documento'], 'name': row['nombre_archivo'], 'status': 'ERROR', 'error': str(exc)})
        print('ERROR', row['id_documento'], str(exc), flush=True)
(folder / 'descargas_investigacion.json').write_text(
    json.dumps({'retrieved_at': base_datos.now(), 'files': results}, ensure_ascii=False, indent=2), encoding='utf-8'
)
if any(r['status'] == 'ERROR' for r in results):
    raise SystemExit(1)
