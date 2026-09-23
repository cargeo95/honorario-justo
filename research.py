"""Create numbered, self-contained research folders. No AI calls or publication."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from evidence import render_page
from secop import link, normal

ROOT = Path(__file__).resolve().parent
KINDS = {'objeto': '01_Entidad_y_objeto', 'experiencia': '02_Perfiles_y_experiencia',
         'presupuesto': '03_Presupuesto'}


def safe_name(value, maximum=100):
    name = re.sub(r'[^a-zA-Z0-9 ._-]', '', normal(value)).strip(' .')
    name = re.sub(r'\s+', ' ', name)[:maximum].rstrip(' .')
    return name or 'Investigacion'


def x_length(text):
    # Spanish/plain-text posts; URLs count as 23 characters on X.
    return len(re.sub(r'https?://\S+', 'x' * 23, text))


def allocate(root, title, case_id):
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(root / '.indice.sqlite', timeout=30) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS investigaciones (numero INTEGER PRIMARY KEY, carpeta TEXT, proceso TEXT, fecha TEXT, estado TEXT)')
        conn.execute('BEGIN IMMEDIATE')
        on_disk = [int(m.group(1)) for p in root.iterdir() if p.is_dir() and (m := re.match(r'^(\d+)\. ', p.name))]
        previous = conn.execute('SELECT COALESCE(MAX(numero),0) FROM investigaciones').fetchone()[0]
        number = max([previous] + on_disk) + 1
        folder = root / f'{number}. {safe_name(title)}'
        folder.mkdir()
        conn.execute('INSERT INTO investigaciones VALUES (?,?,?,?,?)',
                     (number, folder.name, case_id, datetime.now().astimezone().isoformat(), 'PREPARANDO'))
    return number, folder


def mark(root, number, state):
    with sqlite3.connect(root / '.indice.sqlite') as conn:
        conn.execute('UPDATE investigaciones SET estado=? WHERE numero=?', (state, number))


def default_content(case):
    meta = case['metadata']
    url = link(meta.get('urlproceso'))
    reference = meta.get('referencia_del_proceso', case['id'])
    entity = meta.get('entidad', '')
    obj = meta.get('descripci_n_del_procedimiento', '')
    linkedin = (f'{entity}\nMínima cuantía {reference}\n\n{obj}\n\n'
                'Comparto las páginas que describen el objeto, los perfiles y el presupuesto del proceso. '
                '¿Cómo se relacionan la experiencia exigida, la dedicación prevista y el costo del equipo?\n\n'
                'El costo presupuestado de un cargo no equivale necesariamente a su salario neto. '
                'La comparación requiere distinguir dedicación, factor multiplicador y plazo.\n\n'
                f'Fuente: {url}\n\n#ContrataciónPública #Ingeniería #SECOPII')
    short = f'Mínima cuantía {reference[:70]}: perfiles profesionales y presupuesto bajo revisión. Adjunto objeto, experiencia y costos.\n{url}\n#ContrataciónPública'
    return {'linkedin': linkedin, 'x_post': short, 'x_thread': [short],
            'report': '# Investigación documental\n\n'
                      f'Proceso: {reference}\n\nEntidad: {entity}\n\nObjeto: {obj}\n\n'
                      'Las páginas fueron sugeridas por reglas locales. Este expediente no acredita por sí solo '
                      'subremuneración ni incumplimiento. Las conclusiones específicas requieren revisión de las fuentes.\n',
            'review_status': 'BORRADOR_AUTOMATICO', 'facts': [], 'calculations': {}}


def export_research(case, source, title, content=None, results_root=None):
    source = Path(source).resolve()
    root = Path(results_root or os.environ.get('SECOP_RESULTS', ROOT / 'Resultados')).resolve()
    evidence = case['analysis'].get('evidence', {})
    if not all(evidence.get(k) for k in KINDS):
        raise ValueError('La investigacion requiere las tres evidencias seleccionadas')
    content = default_content(case) | (content or {})
    if not isinstance(content['x_thread'], list) or not content['x_thread']:
        raise ValueError('El hilo de X debe contener al menos una publicacion')
    for text in [content['x_post']] + content['x_thread']:
        if not isinstance(text, str) or x_length(text) > 280:
            raise ValueError('Cada publicacion de X debe tener como maximo 280 caracteres ponderados')
    if len(content['linkedin']) > 3000:
        raise ValueError('El texto de LinkedIn supera 3.000 caracteres')
    for item in evidence.values():
        path = (source / 'documents' / item['file']).resolve()
        if path.parent != source / 'documents' or not path.is_file():
            raise ValueError('No se encuentra el PDF original de la evidencia')
    number, folder = allocate(root, title, case['id'])
    try:
        for name in ['Documentos', 'Evidencias', 'Evidencias/Paginas_completas', 'Texto_extraido', 'Datos']:
            (folder / name).mkdir(parents=True, exist_ok=True)
        docs = {d['file']: d for d in case['analysis'].get('documents', [])}
        for item in case['analysis'].get('inventory', []):
            key = str(item.get('id_documento', '')) + '.' + item.get('extensi_n', '').lower()
            if (source / 'documents' / key).is_file():
                docs.setdefault(key, {'name': item['nombre_archivo'], 'url': link(item.get('url_descarga_documento'))})
        document_names = {}
        for file in (source / 'documents').iterdir():
            if not file.is_file():
                continue
            original = docs.get(file.name, {}).get('name', file.name)
            name = f'{file.stem[:24]} - {safe_name(Path(original).stem, 75)}{file.suffix.lower()}'
            shutil.copy2(file, folder / 'Documentos' / name)
            document_names[file.name] = name
        for file in (source / 'text').glob('*.json'):
            shutil.copy2(file, folder / 'Texto_extraido' / file.name)
            record = json.loads(file.read_text(encoding='utf-8'))
            text = '\n\n'.join(f'--- PAGINA {p["page"]} ({p.get("method", "texto")}) ---\n{p["text"]}' for p in record.get('pages', []))
            (folder / 'Texto_extraido' / (file.stem + '.txt')).write_text(text, encoding='utf-8')
        citations = []
        for kind, name in KINDS.items():
            item = evidence[kind]
            pdf = source / 'documents' / item['file']
            render_page(pdf, item['page'], folder / 'Evidencias' / (name + '.png'), item.get('crop'))
            render_page(pdf, item['page'], folder / 'Evidencias/Paginas_completas' / (name + '.png'))
            citations.append(f'- {name}: [{item["name"]}](<Documentos/{document_names[item["file"]]}>), página {item["page"]}. Recorte normalizado: {item.get("crop") or "página completa"}.')
        for file in source.glob('*.json'):
            shutil.copy2(file, folder / 'Datos' / file.name)
        (folder / 'Datos/proceso_completo.json').write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding='utf-8')
        (folder / 'Datos/contenido_editorial.json').write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding='utf-8')
        files = {'01_LinkedIn.txt': content['linkedin'], '02_X.txt': content['x_post'],
                 '03_X_hilo.txt': '\n\n--------------------\n\n'.join(content['x_thread']),
                 'Investigacion.md': content['report']}
        for name, text in files.items():
            (folder / name).write_text(text.rstrip() + '\n', encoding='utf-8')
        sources = '# Fuentes y trazabilidad\n\n' + '\n'.join(citations)
        sources += '\n\nProceso público: ' + link(case['metadata'].get('urlproceso'))
        sources += '\n\n## Descargas\n\n' + '\n'.join(f'- {d["name"]}: {d.get("url") or "Adjunto local"}' for d in docs.values())
        sources += '\n\n## Observaciones del análisis\n\n' + ('\n'.join('- '+w for w in case['analysis'].get('warnings', [])) or 'Sin alertas del procesamiento local.')
        (folder / 'Fuentes.md').write_text(sources + '\n', encoding='utf-8')
        readme = (f'# Investigación {number}: {title}\n\n'
                  f'Estado editorial: {content["review_status"]}.\n\n'
                  '- LinkedIn: copie `01_LinkedIn.txt`.\n'
                  '- X: copie `02_X.txt`, o publique en orden los bloques de `03_X_hilo.txt`.\n'
                  '- Adjunte los tres PNG de `Evidencias/`. Los originales sin recortar están en `Evidencias/Paginas_completas/`.\n'
                  '- `Investigacion.md` contiene el análisis y sus límites; `Fuentes.md`, las referencias.\n'
                  '- `Documentos/` conserva todos los archivos descargados para este caso. `Datos/` y `Texto_extraido/` conservan la trazabilidad.\n\n'
                  'Nada se ha publicado automáticamente. La fecha de investigación no implica que el proceso se publicara ese día.\n')
        (folder / 'LEEME.md').write_text(readme, encoding='utf-8')
        manifest = {'number': number, 'title': title, 'case_id': case['id'],
                    'created_at': datetime.now().astimezone().isoformat(), 'review_status': content['review_status'],
                    'characters': {'linkedin': len(content['linkedin']), 'x': x_length(content['x_post']),
                                   'x_thread': [x_length(t) for t in content['x_thread']]},
                    'files': {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in folder.rglob('*') if p.is_file()}}
        (folder / 'manifiesto.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        mark(root, number, 'COMPLETO')
        return {'number': number, 'folder': str(folder), 'name': folder.name}
    except Exception:
        mark(root, number, 'INCOMPLETO')
        raise


if __name__ == '__main__':
    import argparse
    import app
    parser = argparse.ArgumentParser()
    parser.add_argument('case_id')
    parser.add_argument('--title', required=True)
    parser.add_argument('--content', type=Path)
    args = parser.parse_args()
    content = json.loads(args.content.read_text(encoding='utf-8')) if args.content else None
    result = export_research(app.get_case(args.case_id), app.case_folder(args.case_id), args.title, content)
    print(json.dumps(result, ensure_ascii=False))
