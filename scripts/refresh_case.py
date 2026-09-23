"""Explicitly refresh one public process and finish local OCR for its PDFs."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from secop import SecopClient, literal

parser = argparse.ArgumentParser()
parser.add_argument('case_id')
args = parser.parse_args()
client = SecopClient()
rows = client.query('p6dx-8zbt', where='id_del_proceso=' + literal(args.case_id), limit=2)
if len(rows) != 1:
    raise ValueError('Se esperaba un unico proceso')
app.record_process(rows[0])
folder = app.case_folder(args.case_id)
snapshot = {'retrieved_at': app.now(), 'dataset': 'p6dx-8zbt', 'process': rows[0]}
(folder / 'consulta_fuente.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
print('Refreshing', args.case_id, flush=True)
app.analyze_case(args.case_id, app.settings() | {'max_ocr_pages': 100, 'max_pages': 300, 'max_documents': 30}, client)
case = app.get_case(args.case_id)
print(json.dumps({'metadata': case['metadata'], 'inventory': case['analysis']['inventory'],
                  'warnings': case['analysis']['warnings']}, ensure_ascii=True, indent=2), flush=True)
