"""Caso de uso: analizar un proceso.

Descarga los PDF candidatos, extrae texto (OCR si hace falta), puntúa cada página y elige
las tres evidencias del caso: objeto, experiencia exigida y presupuesto de personal.
"""

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

from honorario_justo.almacenamiento.base_datos import case_folder, db, get_case, get_value, now
from honorario_justo.dominio.reglas import omitir_ocr
from honorario_justo.fuentes.documentos import analyze_pdf, classification, render_page
from honorario_justo.fuentes.secop import doc_score, link


def settings():
    defaults = {
        'date_from': (date.today() - timedelta(days=90)).isoformat(),
        'date_to': date.today().isoformat(),
        'follow_today': True,
        'keywords': 'CONSULTOR,INTERVENT,ESTUDIOS,DISEÑO,DISENO,ALUMBRADO',
        'department': '',
        'batch_size': 10,
        'max_documents': 8,
        # Lectura completa, OCR incluido: corre local y sin costo, y las tablas de pago de
        # los PDF escaneados suelen estar despues de la pagina 15 (Saravena, Puerto Salgar).
        # 500 es solo un tope de seguridad contra anexos gigantes.
        'max_pages': 500,
        'max_ocr_pages': 500,
    }
    saved = get_value('settings', {})
    defaults.update({k: v for k, v in saved.items() if k in defaults})
    if defaults['follow_today']:
        defaults['date_to'] = date.today().isoformat()
    return defaults


def analyze_case(case_id, config, client=None):
    case = get_case(case_id)
    folder = case_folder(case_id)
    if omitir_ocr(case['metadata']):
        config = dict(config, omitir_ocr=True)
    warnings = [] if client else list(case['analysis'].get('warnings', []))
    documents, available = [], case['analysis'].get('inventory', [])
    try:
        if client:
            available, warnings = client.documents(case['metadata'])
    except Exception as exc:
        warnings.append('Consulta de documentos: ' + str(exc))
    eligible = sorted([r for r in available if doc_score(r) > 0], key=doc_score, reverse=True)
    if len(eligible) > config['max_documents']:
        warnings.append(f'Se priorizaron {config["max_documents"]} de {len(eligible)} PDF candidatos')
    sources = {d['file']: d for d in case['analysis'].get('documents', [])}
    for row in eligible[: config['max_documents']] if client else []:
        logging.info('Descargando %s', row.get('nombre_archivo', 'PDF'))
        try:
            path = client.download(row, folder / 'documents')
            sources[path.name] = {
                'file': path.name,
                'name': row.get('nombre_archivo', path.name),
                'url': link(row.get('url_descarga_documento')),
                'dataset': row.get('dataset'),
            }
        except Exception as exc:
            warnings.append(row.get('nombre_archivo', 'PDF') + ': ' + str(exc))
    for path in (folder / 'documents').glob('*.pdf'):
        sources.setdefault(path.name, {'file': path.name, 'name': path.name, 'url': '', 'local': True})
    alternatives = {k: [] for k in ['objeto', 'experiencia', 'presupuesto']}
    for filename, source in sources.items():
        logging.info('Leyendo %s', source['name'])
        try:
            result = analyze_pdf(folder / 'documents' / filename, folder / 'text', config)
            warnings.extend(source['name'] + ': ' + w for w in result['warnings'])
            documents.append(
                dict(
                    source,
                    pages=len(result['pages']),
                    sha256=result['sha256'],
                    paginas_sin_ocr=result.get('paginas_sin_ocr', 0),
                )
            )
            for page in result['pages']:
                for kind, score in page['scores'].items():
                    if score:
                        alternatives[kind].append(
                            {
                                'file': filename,
                                'name': source['name'],
                                'page': page['page'],
                                'score': score,
                                'method': page['method'],
                                'excerpt': page['text'][:1800],
                            }
                        )
        except Exception as exc:
            warnings.append(source['name'] + ': ' + str(exc))
    evidence = {}
    previous = case['analysis'].get('evidence', {})
    for kind, choices in alternatives.items():
        choices.sort(key=lambda item: (-item['score'], item['page']))
        alternatives[kind] = choices[:20]
        selected = previous.get(kind)
        if selected and selected.get('manual') and (folder / 'documents' / selected['file']).exists():
            evidence[kind] = selected
        elif choices:
            evidence[kind] = choices[0]
        if evidence.get(kind):
            item = evidence[kind]
            render_page(folder / 'documents' / item['file'], item['page'], folder / 'captures' / f'{kind}-original.png')
            render_page(
                folder / 'documents' / item['file'], item['page'], folder / 'captures' / f'{kind}.png', item.get('crop')
            )
    if not available and client:
        warnings.append('Sin archivos en el indice publico para este portafolio; no implica ausencia en SECOP.')
    if available and not eligible and not documents:
        warnings.append('El indice no contiene PDF con nombres candidatos. Revise el inventario o adjunte el PDF.')
    warnings = list(dict.fromkeys(warnings))
    result = {
        'evidence': evidence,
        'alternatives': alternatives,
        'warnings': warnings,
        'documents': documents,
        'inventory': available,
        'analyzed_at': now(),
    }
    status = classification(evidence, warnings, len(documents))
    # Any source change invalidates approval; unchanged reruns preserve the human decision.
    fingerprints = lambda a: sorted((d['file'], d.get('sha256')) for d in a.get('documents', []))
    changed = fingerprints(case['analysis']) != fingerprints(result) or case['analysis'].get('evidence') != evidence
    review = '' if changed else case['reviewed']
    with db() as conn:
        conn.execute(
            'UPDATE cases SET analysis=?,status=?,reviewed=?,checked_at=?,updated_at=? WHERE id=?',
            (json.dumps(result, ensure_ascii=False), status, review, now(), now(), case_id),
        )
    (folder / 'metadata.json').write_text(json.dumps(case['metadata'], ensure_ascii=False, indent=2), encoding='utf-8')
    (folder / 'analysis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


def closing_date(metadata):
    for key in ('fecha_de_recepcion_de', 'fecha_de_apertura_de_respuesta'):
        value = metadata.get(key)
        if value:
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                continue
    return None


def skip_reanalysis(case):
    """Evita reanalizar lo revisado hace poco, y deja de intentarlo para siempre
    en casos que ya cerraron sin evidencia: no llegaran mas documentos previos."""
    if case['status'] in {'SIN_DOCUMENTOS', 'SIN_EVIDENCIA'}:
        closes = closing_date(case['metadata'])
        if closes and closes < datetime.now():
            return True
    if not case['checked_at']:
        return False
    if lectura_cortada(case['analysis'].get('warnings', []), settings()) or documentos_sin_bajar(case, settings()):
        return False
    return datetime.fromisoformat(case['checked_at']) > datetime.now(timezone.utc) - timedelta(hours=24)


def documentos_sin_bajar(case, config):
    """Candidatos del inventario (segun doc_score actual) que no se descargaron: pasa cuando
    cambian los criterios de seleccion. No cuenta los que ya fallaron al descargar, para no
    reintentarlos en cada corrida."""
    analysis = case['analysis']
    elegibles = sorted([r for r in analysis.get('inventory', []) if doc_score(r) > 0], key=doc_score, reverse=True)
    bajados = {d.get('name') for d in analysis.get('documents', [])}
    fallidos = {w.split(': ', 1)[0] for w in analysis.get('warnings', [])}
    return [
        r.get('nombre_archivo')
        for r in elegibles[: config['max_documents']]
        if r.get('nombre_archivo') not in bajados | fallidos
    ]


def lectura_cortada(warnings, config):
    """True si el analisis se quedo corto por un tope menor al actual (OCR o paginas):
    se reanaliza aunque sea reciente. El texto ya leido queda en cache, asi que solo
    se hace OCR de las paginas que faltaron."""
    for w in warnings:
        if 'limite de OCR alcanzado' in w:
            return True
        m = re.search(r'PDF limitado a (\d+) de', w)
        if m and int(m.group(1)) < config['max_pages']:
            return True
    return False
