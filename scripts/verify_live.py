"""Read-only remote smoke test, then one public PDF download into local QA data."""

import json
from datetime import date, timedelta
from pathlib import Path

from honorario_justo.fuentes.secop import SecopClient, doc_score

client = SecopClient()
config = {
    'date_from': (date.today() - timedelta(days=90)).isoformat(),
    'date_to': date.today().isoformat(),
    'keywords': 'CONSULTOR,INTERVENT,ESTUDIOS,DISEÑO,ALUMBRADO',
    'department': '',
    'batch_size': 5,
}
rows = client.processes(config)
print(
    json.dumps(
        [
            {
                'id': r['id_del_proceso'],
                'reference': r['referencia_del_proceso'],
                'entity': r['entidad'],
                'date': r.get('fecha_de_publicacion_del'),
                'portfolio': r.get('id_del_portafolio'),
            }
            for r in rows
        ],
        ensure_ascii=True,
        indent=2,
    ),
    flush=True,
)
out = Path(__file__).resolve().parents[1] / 'qa-output/live'
out.mkdir(parents=True, exist_ok=True)
(out / 'processes.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
for process in rows:
    docs, warnings = client.documents(process)
    print(process['referencia_del_proceso'], 'documents:', len(docs), warnings, flush=True)
    candidates = sorted([r for r in docs if doc_score(r) > 0], key=doc_score, reverse=True)
    if candidates:
        print('Downloading', ascii(candidates[0]['nombre_archivo']), flush=True)
        path = client.download(candidates[0], out)
        print('PDF', path, 'bytes', path.stat().st_size, flush=True)
        (out / 'document.json').write_text(json.dumps(candidates[0], ensure_ascii=False), encoding='utf-8')
        break
