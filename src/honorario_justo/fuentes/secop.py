"""SECOP public data client. Download only published, allowlisted PDF URLs."""

import hashlib
import re
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from honorario_justo.dominio.texto import normal  # noqa: F401 (reexportado)

ARCHIVES = {'recent': 'dmgg-8hin', '2024': 'nbae-kzan', '2023': '3skv-9na7', '2022': 'kgcd-kt7i', 'older': 'f8va-cf4m'}


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def link(value):
    return value.get('url', '') if isinstance(value, dict) else (value or '')


def doc_score(row):
    name = normal(row.get('nombre_archivo', ''))
    if not (name.endswith('.pdf') or normal(row.get('extensi_n')) == 'pdf'):
        return -100
    score = sum(
        weight
        for word, weight in [
            ('estudio', 8),
            ('previo', 8),
            ('invitacion', 10),
            ('presupuesto', 10),
            ('tecnico', 5),
            ('anexo', 2),
            ('pliego', 7),
            ('adenda', 6),
            # El analisis del sector trae la estructura de costos de personal (Guapota); en el corte
            # del 18-sep no se bajaba en 18 de 30 procesos. Las cotizaciones, a veces con pago por
            # cargo (Maripi), van con menos peso para no desplazar estudio previo e invitacion.
            ('sector', 9),
            ('mercado', 6),
            ('precontractual', 6),
            ('cotizacion', 3),
        ]
        if word in name
    )
    # Abreviaturas usuales: "5. OK E.P. DISENO CUBIERTAS.pdf" (estudio previo), "EEPP",
    # "6. IP CONS ...pdf" (invitacion publica). Sin esto Guadalupe (CO1.REQ.11048302) solo bajaba la adenda.
    if re.search(r'(?<![a-z])(?:e\.?\s?p|ee\.?\s?pp)(?![a-z])', name):
        score += 16
    if re.search(r'(?<![a-z])i\.?\s?p(?![a-z])', name):
        score += 10
    score -= sum(
        20
        for word in [
            'oferta',
            'propuesta',
            'cedula',
            'seguridad social',
            'certificacion',
            'rut',
            'poliza',
            'hoja de vida',
        ]
        if word in name
    )
    return score


def valid_document(path, extension):
    if not path.is_file():
        return False
    if extension == 'pdf':
        with path.open('rb') as stream:
            return stream.read(5) == b'%PDF-'
    if extension in {'docx', 'xlsx'}:
        try:
            with zipfile.ZipFile(path) as archive:
                return ('word/document.xml' if extension == 'docx' else 'xl/workbook.xml') in archive.namelist()
        except zipfile.BadZipFile:
            return False
    return False


class SecopClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'SecopObservatorioLocal/1.0'
        retry = Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        self.session.mount('https://', HTTPAdapter(max_retries=retry))

    def query(self, dataset, **params):
        response = self.session.get(
            f'https://www.datos.gov.co/resource/{dataset}.json',
            params={'$' + k: v for k, v in params.items()},
            timeout=(15, 90),
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, list):
            raise ValueError('Respuesta inesperada de Datos Abiertos')
        return result

    @staticmethod
    def process_where(settings):
        clauses = [
            "modalidad_de_contratacion='Mínima cuantía'",
            'fecha_de_publicacion_del >= ' + literal(settings['date_from'] + 'T00:00:00'),
            'fecha_de_publicacion_del <= ' + literal(settings['date_to'] + 'T23:59:59'),
        ]
        terms = [t.strip().upper() for t in settings['keywords'].split(',') if t.strip()]
        if terms:
            expressions = [f'upper(descripci_n_del_procedimiento) like {literal("%" + t + "%")}' for t in terms]
            expressions.append("tipo_de_contrato='Consultoría'")
            clauses.append('(' + ' OR '.join(expressions) + ')')
        if settings.get('department'):
            clauses.append('departamento_entidad=' + literal(settings['department']))
        return ' AND '.join(clauses)

    def processes(self, settings, offset=0, limit=None):
        return self.query(
            'p6dx-8zbt',
            where=self.process_where(settings),
            order='fecha_de_publicacion_del DESC,id_del_proceso DESC',
            limit=limit or settings['batch_size'],
            offset=offset,
        )

    def count_processes(self, settings):
        rows = self.query('p6dx-8zbt', select='count(*) AS total', where=self.process_where(settings))
        return int(rows[0]['total']) if rows else 0

    def latest_day(self):
        """Ultimo dia con procesos de minima cuantia en Datos Abiertos. El dataset se
        refresca con 1-2 dias de atraso: a las 8 a. m. 'ayer' normalmente aun no esta."""
        rows = self.query(
            'p6dx-8zbt', select='max(fecha_de_publicacion_del) AS m', where="modalidad_de_contratacion='Mínima cuantía'"
        )
        return rows[0]['m'][:10] if rows and rows[0].get('m') else None

    def all_processes(self, settings, page=1000):
        """Todos los procesos que cumplen el filtro (para el corte diario sin palabra clave)."""
        rows, offset = [], 0
        while True:
            batch = self.processes(settings, offset, page)
            rows.extend(batch)
            if len(batch) < page:
                return rows
            offset += page

    def documents(self, process):
        year = str(process.get('fecha_de_publicacion_del', '2026'))[:4]
        dataset = ARCHIVES.get(year, ARCHIVES['recent'] if year >= '2025' else ARCHIVES['older'])
        portfolio = process.get('id_del_portafolio')
        if not portfolio:
            return [], ['El proceso no tiene identificador de portafolio.']
        rows = []
        for offset in range(0, 5000, 500):
            batch = self.query(
                dataset, where='proceso=' + literal(portfolio), limit=500, offset=offset, order='id_documento ASC'
            )
            rows.extend(dict(r, dataset=dataset) for r in batch)
            if len(batch) < 500:
                return rows, []
        return rows, ['Inventario limitado a 5.000 archivos; requiere revision.']

    def download(self, row, folder, max_mb=40):
        url = link(row.get('url_descarga_documento'))
        extension = normal(row.get('extensi_n') or Path(row.get('nombre_archivo', '')).suffix.lstrip('.') or 'pdf')
        if extension not in {'pdf', 'docx', 'xlsx'}:
            raise ValueError('Formato no soportado para descarga')
        filename = (
            re.sub(r'[^a-zA-Z0-9._-]', '_', str(row.get('id_documento', hashlib.sha256(url.encode()).hexdigest()[:20])))
            + '.'
            + extension
        )
        path = folder / filename
        if valid_document(path, extension):
            return path
        for _ in range(5):
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.hostname not in {
                'community.secop.gov.co',
                'www.secop.gov.co',
                'www.colombiacompra.gov.co',
            }:
                raise ValueError('Enlace de descarga fuera de los dominios publicos permitidos')
            response = self.session.get(url, timeout=(15, 90), stream=True, allow_redirects=False)
            if response.is_redirect:
                from urllib.parse import urljoin

                url = urljoin(url, response.headers['Location'])
                response.close()
                continue
            break
        else:
            raise ValueError('Demasiadas redirecciones')
        temp = path.with_suffix('.part')
        try:
            with response:
                response.raise_for_status()
                total = 0
                with temp.open('wb') as out:
                    for chunk in response.iter_content(65536):
                        total += len(chunk)
                        if total > max_mb * 1024 * 1024:
                            raise ValueError(f'Documento supera el limite de {max_mb} MB')
                        out.write(chunk)
            if not valid_document(temp, extension):
                raise ValueError('SECOP no devolvio el documento esperado (posible CAPTCHA o enlace no disponible)')
            temp.replace(path)
            return path
        finally:
            temp.unlink(missing_ok=True)
