"""Re-evaluate local evidence rules without a new download or OCR pass."""
import sys
import json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
config=app.settings() | {'cache_only':True}
with app.db() as conn:
    ids=[r[0] for r in conn.execute('SELECT id FROM cases')]
for cid in ids:
    case=app.get_case(cid)
    analysis=case['analysis']
    # Remove an internal warning from the initial offline-rescore implementation.
    analysis['warnings']=[w for w in analysis.get('warnings',[]) if "'NoneType' object has no attribute 'download'" not in w]
    with app.db() as conn:
        conn.execute('UPDATE cases SET analysis=? WHERE id=?',(json.dumps(analysis,ensure_ascii=False),cid))
    app.analyze_case(cid,config)
    case=app.get_case(cid)
    print(cid,case['status'],{k:v['page'] for k,v in case['analysis']['evidence'].items()},flush=True)
