"""Re-evaluate local evidence rules without a new download or OCR pass."""

import json

from honorario_justo.almacenamiento import base_datos
from honorario_justo.servicios import analisis

config = analisis.settings() | {'cache_only': True}
with base_datos.db() as conn:
    ids = [r[0] for r in conn.execute('SELECT id FROM cases')]
for cid in ids:
    case = base_datos.get_case(cid)
    analysis = case['analysis']
    # Remove an internal warning from the initial offline-rescore implementation.
    analysis['warnings'] = [
        w for w in analysis.get('warnings', []) if "'NoneType' object has no attribute 'download'" not in w
    ]
    with base_datos.db() as conn:
        conn.execute('UPDATE cases SET analysis=? WHERE id=?', (json.dumps(analysis, ensure_ascii=False), cid))
    analisis.analyze_case(cid, config)
    case = base_datos.get_case(cid)
    print(cid, case['status'], {k: v['page'] for k, v in case['analysis']['evidence'].items()}, flush=True)
