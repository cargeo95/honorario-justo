"""Explicitly refresh one public process and finish local OCR for its PDFs."""

import argparse
import json

from honorario_justo.almacenamiento import base_datos
from honorario_justo.fuentes.secop import SecopClient, literal
from honorario_justo.servicios import analisis

parser = argparse.ArgumentParser()
parser.add_argument('case_id')
args = parser.parse_args()
client = SecopClient()
rows = client.query('p6dx-8zbt', where='id_del_proceso=' + literal(args.case_id), limit=2)
if len(rows) != 1:
    raise ValueError('Se esperaba un unico proceso')
base_datos.record_process(rows[0])
folder = base_datos.case_folder(args.case_id)
snapshot = {'retrieved_at': base_datos.now(), 'dataset': 'p6dx-8zbt', 'process': rows[0]}
(folder / 'consulta_fuente.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
print('Refreshing', args.case_id, flush=True)
analisis.analyze_case(
    args.case_id, analisis.settings() | {'max_ocr_pages': 100, 'max_pages': 300, 'max_documents': 30}, client
)
case = base_datos.get_case(args.case_id)
print(
    json.dumps(
        {
            'metadata': case['metadata'],
            'inventory': case['analysis']['inventory'],
            'warnings': case['analysis']['warnings'],
        },
        ensure_ascii=True,
        indent=2,
    ),
    flush=True,
)
