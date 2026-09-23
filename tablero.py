"""Genera Indicadores/tablero.html: un HTML autocontenido (datos + mapa incrustados)
con lo que hay en 'dias', 'cases' y 'hallazgos'. No consulta SECOP.

Cada cifra del tablero sale de una fila auditable: la tabla de detalle enlaza al
proceso en SECOP y al archivo/pagina de donde se extrajo el pago.
"""
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

from app import ROOT, db, indicadores_folder, unpack
from extraccion import SMLV, SMLV_FUENTE
from secop import link, normal

GEO = ROOT / 'data' / 'geo' / 'colombia.geo.json'
# Departamentos de Colombia (DANE, 33 poligonos, propiedad NOMBRE_DPT). data/ no se versiona:
# si falta, se descarga una vez.
GEO_URL = ('https://gist.githubusercontent.com/john-guerra/43c7656821069d00dcbc/raw/'
           'be6a6e239cd5b5b803c6e7c2ec405b793a9064dd/Colombia.geo.json')
# Nombres de SECOP que no coinciden con NOMBRE_DPT del GeoJSON (DANE antiguo).
DPTO_ALIAS = {'distrito capital de bogota': 'santafe de bogota d.c', 'bogota d.c.': 'santafe de bogota d.c',
              'san andres, providencia y santa catalina': 'archipielago de san andres providencia y santa catalina',
              'archipielago de san andres, providencia y santa catalina':
                  'archipielago de san andres providencia y santa catalina'}


def clave_dpto(nombre):
    n = normal(nombre).strip()
    return DPTO_ALIAS.get(n, n)


def _simplificar(coords, paso=0.02):
    """Redondea a ~2 km y quita puntos repetidos: el GeoJSON baja de 1.5 MB a ~100 KB."""
    if coords and isinstance(coords[0], (int, float)):
        return [round(round(coords[0] / paso) * paso, 2), round(round(coords[1] / paso) * paso, 2)]
    out = [_simplificar(c, paso) for c in coords]
    if out and isinstance(out[0], list) and out[0] and isinstance(out[0][0], float):
        dedup = [p for i, p in enumerate(out) if i == 0 or p != out[i - 1]]
        return dedup if len(dedup) >= 4 else out
    return out


def geo_simplificado():
    if not GEO.exists():
        try:
            import requests
            response = requests.get(GEO_URL, timeout=60)
            response.raise_for_status()
            GEO.parent.mkdir(parents=True, exist_ok=True)
            GEO.write_bytes(response.content)
        except Exception:
            return None  # el tablero sale igual, sin mapa
    data = json.loads(GEO.read_text(encoding='utf-8'))
    feats = []
    for f in data['features']:
        g = f['geometry']
        coords = _simplificar(g['coordinates'])
        feats.append({'type': 'Feature', 'properties': {'k': clave_dpto(f['properties']['NOMBRE_DPT']),
                                                        'n': f['properties']['NOMBRE_DPT'].title()},
                      'geometry': {'type': g['type'], 'coordinates': coords}})
    return {'type': 'FeatureCollection', 'features': feats}


def datos():
    with db() as conn:
        cases = [unpack(r) for r in conn.execute('SELECT * FROM cases')]
        hallazgos = [dict(r) for r in conn.execute('SELECT * FROM hallazgos ORDER BY fecha_publicacion DESC')]
        dias = {r['fecha']: dict(r) for r in conn.execute('SELECT * FROM dias')}
    meta = {c['id']: c['metadata'] for c in cases}
    por_dia = defaultdict(lambda: {'en_base': 0, 'analizados': 0})
    for c in cases:
        f = str(c['metadata'].get('fecha_de_publicacion_del', ''))[:10]
        if f:
            por_dia[f]['en_base'] += 1
            por_dia[f]['analizados'] += bool(c['checked_at'])
    con_hallazgo = defaultdict(set)
    for h in hallazgos:
        if h['estado'] != 'descartado':
            con_hallazgo[str(h['fecha_publicacion'])[:10]].add(h['proceso_id'])
    serie = []
    fechas = sorted(set(por_dia) | set(dias))
    # Todos los dias del rango, tambien los que no tienen procesos (fines de semana):
    # una serie diaria que se salta dias exagera la continuidad.
    if fechas:
        inicio, fin = date.fromisoformat(fechas[0]), date.fromisoformat(fechas[-1])
        fechas = [(inicio + timedelta(days=i)).isoformat() for i in range((fin - inicio).days + 1)]
    for f in fechas:
        d = dias.get(f)
        serie.append({'fecha': f, 'publicados': d['publicados'] if d else None,
                      'analizados': d['analizados'] if d else por_dia[f]['analizados'],
                      'en_base': por_dia[f]['en_base'], 'con_hallazgo': len(con_hallazgo[f]),
                      # Completo = se analizo todo lo publicado (una corrida con --limite no cuenta).
                      'corte_completo': bool(d) and d['analizados'] >= d['publicados'] > 0})
    from indicadores import clasificar, cortes_por_franja
    cortes = cortes_por_franja([h for h in hallazgos if h['estado'] != 'descartado'])
    filas = []
    for h in hallazgos:
        m = meta.get(h['proceso_id'], {})
        filas.append({'id': h['id'], 'proceso': h['proceso_id'], 'url': link(m.get('urlproceso')),
                      'entidad': h['entidad'], 'dpto': h['departamento'], 'dpto_k': clave_dpto(h['departamento']),
                      'fecha': str(h['fecha_publicacion'])[:10], 'cargo': h['cargo'],
                      'anos': h['anos_experiencia'], 'anos_nivel': h['anos_nivel'],
                      'pago': h['pago_mensual_cop'], 'smlv': h['pago_smlv'], 'metodo': h['metodo'],
                      'confianza': h['confianza'] or 'baja', 'estado': h['estado'],
                      'clase': clasificar(h, cortes), 'tarifa': h.get('referencia_tarifa') or '',
                      'fuente': f"{h['archivo_fuente']} p. {h['pagina_fuente']}"})
    procesos_dpto = Counter(clave_dpto(c['metadata'].get('departamento_entidad', '')) for c in cases)
    return {'serie': serie, 'hallazgos': filas, 'procesos_dpto': procesos_dpto,
            'total_casos': len(cases), 'analizados': sum(1 for c in cases if c['checked_at']),
            'smlv': {str(k): {'valor': v, 'fuente': SMLV_FUENTE[k]} for k, v in SMLV.items()},
            'generado': datetime.now().strftime('%Y-%m-%d %H:%M')}


def generar_tablero(root=None):
    payload = datos()
    geo = geo_simplificado()
    html = PLANTILLA.replace('/*__DATOS__*/null', json.dumps(payload, ensure_ascii=False)) \
                    .replace('/*__GEO__*/null', json.dumps(geo, separators=(',', ':')) if geo else 'null')
    path = indicadores_folder(root) / 'tablero.html'
    path.write_text(html, encoding='utf-8')
    return {'archivo': str(path), 'hallazgos': len(payload['hallazgos']), 'dias': len(payload['serie']),
            'mediana_smlv': median([h['smlv'] for h in payload['hallazgos'] if h['smlv']] or [0])}


PLANTILLA = r'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Observatorio Mínima Cuantía</title>
<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>
<style>
:root {
  color-scheme: light;
  --surface-0: #f5f4f1; --surface-1: #fcfcfb; --grid: #e6e5e0; --border: #dcdbd5;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #7a7974;
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a;
  --muted-bar: #cfcdc6; --ref: #e34948;
  --seq-0: #f0efec; --seq-1: #cde2fb; --seq-2: #9ec5f4; --seq-3: #5598e7; --seq-4: #256abf; --seq-5: #104281;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface-0: #121211; --surface-1: #1a1a19; --grid: #2c2c2a; --border: #383835;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #8f8e86;
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
    --muted-bar: #4a4945; --ref: #e66767;
    --seq-0: #383835; --seq-1: #104281; --seq-2: #1c5cab; --seq-3: #2a78d6; --seq-4: #5598e7; --seq-5: #9ec5f4;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-0: #121211; --surface-1: #1a1a19; --grid: #2c2c2a; --border: #383835;
  --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #8f8e86;
  --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
  --muted-bar: #4a4945; --ref: #e66767;
  --seq-0: #383835; --seq-1: #104281; --seq-2: #1c5cab; --seq-3: #2a78d6; --seq-4: #5598e7; --seq-5: #9ec5f4;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface-0); color: var(--text-primary);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 24px 16px 48px; }
header h1 { font-size: 22px; margin: 0 0 4px; font-weight: 600; }
header p { margin: 0; color: var(--text-secondary); }
.filtros { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin: 20px 0; }
.filtros label { color: var(--text-secondary); display: flex; gap: 6px; align-items: center; }
select, button { font: inherit; color: var(--text-primary); background: var(--surface-1);
  border: 1px solid var(--border); border-radius: 6px; padding: 4px 8px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
.tile, .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
.tile .label { color: var(--text-secondary); font-size: 13px; }
.tile .value { font-size: 28px; font-weight: 600; margin-top: 4px; font-variant-numeric: proportional-nums; }
.tile.hero .value { font-size: 48px; line-height: 1.1; }
.tile .note { color: var(--text-muted); font-size: 12px; margin-top: 4px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }
@media (max-width: 860px) { .grid2 { grid-template-columns: 1fr; } }
.card h2 { font-size: 15px; font-weight: 600; margin: 0 0 2px; }
.card .sub { color: var(--text-secondary); font-size: 13px; margin: 0 0 10px; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 5px; vertical-align: -1px; }
.legend i.dot { border-radius: 50%; }
svg { display: block; width: 100%; height: auto; overflow: visible; }
svg text { fill: var(--text-muted); font-size: 11px; }
.axis path, .axis line { stroke: var(--grid); }
.gridline { stroke: var(--grid); stroke-width: 1; }
#tip { position: fixed; pointer-events: none; background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,.15); opacity: 0; transition: opacity .1s; max-width: 280px; z-index: 10; }
#tip b { font-weight: 600; }
.tabla { overflow-x: auto; margin-top: 12px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th { color: var(--text-secondary); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.wrap { white-space: normal; min-width: 180px; }
a { color: var(--series-1); }
.chip { display: inline-flex; gap: 5px; align-items: center; color: var(--text-secondary); }
.chip i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.vacio { color: var(--text-muted); padding: 24px 0; text-align: center; }
footer { color: var(--text-muted); font-size: 12px; margin-top: 16px; }
</style>
</head>
<body>
<main>
  <header>
    <h1>Observatorio de Mínima Cuantía</h1>
    <p>Experiencia exigida frente a pago mensual en procesos de mínima cuantía de SECOP II, expresado en salarios mínimos (SMLV).</p>
  </header>

  <div class="filtros">
    <label>Confianza
      <select id="f-conf">
        <option value="todas">Todas</option>
        <option value="alta-media" selected>Alta y media</option>
        <option value="alta">Solo alta</option>
      </select>
    </label>
    <label>Revisión
      <select id="f-estado">
        <option value="no-descartado" selected>Sin descartar</option>
        <option value="confirmado">Solo confirmados</option>
      </select>
    </label>
    <button id="f-tema" type="button">Tema: automático</button>
  </div>

  <section class="tiles" id="tiles"></section>

  <div class="grid2">
    <div class="card">
      <h2>Experiencia exigida frente a pago</h2>
      <p class="sub">Un punto por cargo. Solo cargos con años cruzados a su propio perfil.</p>
      <div class="legend" id="leg-scatter"></div>
      <div id="scatter"></div>
    </div>
    <div class="card">
      <h2>Cargos encontrados por departamento</h2>
      <p class="sub">Color: número de cargos con pago extraído. Pase el cursor para ver la mediana en SMLV.</p>
      <div id="mapa"></div>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <h2>Procesos por día de publicación</h2>
    <p class="sub" id="serie-sub"></p>
    <div class="legend" id="leg-serie"></div>
    <div id="serie"></div>
  </div>

  <div class="card" style="margin-top:12px">
    <h2>Detalle de cada cargo</h2>
    <p class="sub">Cada fila enlaza al proceso en SECOP y al archivo y página de donde salió el pago.</p>
    <div class="tabla"><table id="tabla"></table></div>
  </div>

  <footer id="pie"></footer>
</main>
<div id="tip" role="tooltip"></div>

<script>
const D = /*__DATOS__*/null;
const GEO = /*__GEO__*/null;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmt = new Intl.NumberFormat('es-CO');
const fmt2 = new Intl.NumberFormat('es-CO', {maximumFractionDigits: 2});
const peso = v => '$' + fmt.format(Math.round(v));
const tip = document.getElementById('tip');
function showTip(ev, html) {
  tip.innerHTML = html; tip.style.opacity = 1;
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
const hideTip = () => tip.style.opacity = 0;
const CONF = {alta: '--series-1', media: '--series-2', baja: '--series-3'};

function filtrados() {
  const c = document.getElementById('f-conf').value, e = document.getElementById('f-estado').value;
  return D.hallazgos.filter(h =>
    (c === 'todas' || (c === 'alta' ? h.confianza === 'alta' : h.confianza !== 'baja')) &&
    (e === 'confirmado' ? h.estado === 'confirmado' : h.estado !== 'descartado'));
}
const med = a => { if (!a.length) return null; const s = [...a].sort((x, y) => x - y), m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };

function tiles(H) {
  const procesos = new Set(H.map(h => h.proceso)).size;
  const smlv = H.filter(h => h.smlv != null).map(h => h.smlv);
  const bajo2 = smlv.filter(v => v < 2).length;
  const publicados = D.serie.filter(d => d.corte_completo).reduce((s, d) => s + d.publicados, 0);
  const t = [
    {cls: 'hero', label: 'Mediana del pago mensual', value: smlv.length ? fmt2.format(med(smlv)) + ' SMLV' : '—',
     note: smlv.length ? `sobre ${smlv.length} cargos` : 'sin cargos con este filtro'},
    {label: 'Cargos pagados por debajo de 2 SMLV', value: smlv.length ? fmt.format(bajo2) : '—',
     note: smlv.length ? `${Math.round(100 * bajo2 / smlv.length)} % de los cargos` : ''},
    {label: 'Procesos con dato usable', value: fmt.format(procesos),
     note: `de ${fmt.format(D.analizados)} analizados`},
    {label: publicados ? 'Publicados en días con corte completo' : 'Procesos en la base',
     value: fmt.format(publicados || D.total_casos),
     note: publicados ? 'todos los de mínima cuantía de esos días' : 'aún filtrados por palabra clave'}];
  document.getElementById('tiles').innerHTML = t.map(x =>
    `<div class="tile ${x.cls || ''}"><div class="label">${x.label}</div><div class="value">${x.value}</div><div class="note">${x.note}</div></div>`).join('');
}

function scatter(H) {
  const el = document.getElementById('scatter');
  const P = H.filter(h => h.anos != null && h.anos_nivel === 'cargo' && h.smlv != null);
  const confs = ['alta', 'media', 'baja'].filter(c => P.some(p => p.confianza === c));
  document.getElementById('leg-scatter').innerHTML = confs.map(c =>
    `<span><i class="dot" style="background:${css(CONF[c])}"></i>Confianza ${c}</span>`).join('') +
    `<span><i style="background:${css('--ref')};height:2px;width:14px;border-radius:0;vertical-align:3px"></i>1 SMLV</span>`;
  if (!P.length) { el.innerHTML = '<div class="vacio">Sin cargos con años cruzados para este filtro.</div>'; return; }
  const W = 520, Ht = 300, m = {t: 10, r: 16, b: 36, l: 44};
  const x = d3.scaleLinear().domain([0, Math.max(20, d3.max(P, p => p.anos)) + 1]).range([m.l, W - m.r]);
  const y = d3.scaleLinear().domain([0, Math.max(6, d3.max(P, p => p.smlv)) * 1.1]).nice().range([Ht - m.b, m.t]);
  const svg = d3.create('svg').attr('viewBox', `0 0 ${W} ${Ht}`).attr('role', 'img')
    .attr('aria-label', 'Dispersión de años de experiencia exigidos contra pago mensual en SMLV');
  svg.append('g').selectAll('line').data(y.ticks(5)).join('line').attr('class', 'gridline')
    .attr('x1', m.l).attr('x2', W - m.r).attr('y1', d => y(d)).attr('y2', d => y(d));
  svg.append('g').attr('class', 'axis').attr('transform', `translate(0,${Ht - m.b})`).call(d3.axisBottom(x).ticks(6).tickSizeOuter(0));
  svg.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(5).tickSize(0)).select('.domain').remove();
  svg.append('text').attr('x', W - m.r).attr('y', Ht - 4).attr('text-anchor', 'end').text('Años de experiencia exigidos');
  svg.append('text').attr('x', m.l).attr('y', m.t - 0).attr('dy', -2).text('SMLV / mes');
  svg.append('line').attr('x1', m.l).attr('x2', W - m.r).attr('y1', y(1)).attr('y2', y(1))
    .attr('stroke', css('--ref')).attr('stroke-width', 2);
  svg.append('g').selectAll('circle').data(P).join('circle')
    .attr('cx', p => x(p.anos)).attr('cy', p => y(p.smlv)).attr('r', 5)
    .attr('fill', p => css(CONF[p.confianza])).attr('stroke', css('--surface-1')).attr('stroke-width', 2)
    .style('cursor', 'pointer')
    .on('mousemove', (ev, p) => showTip(ev, `<b>${p.cargo}</b><br>${p.entidad}<br>${p.anos} años exigidos · ${peso(p.pago)}/mes<br><b>${fmt2.format(p.smlv)} SMLV</b> · confianza ${p.confianza}`))
    .on('mouseleave', hideTip);
  el.replaceChildren(svg.node());
}

function mapa(H) {
  const el = document.getElementById('mapa');
  if (!GEO) { el.innerHTML = '<div class="vacio">Falta data/geo/colombia.geo.json</div>'; return; }
  const por = d3.group(H, h => h.dpto_k);
  const max = d3.max([...por.values()], v => v.length) || 1;
  const steps = ['--seq-1', '--seq-2', '--seq-3', '--seq-4', '--seq-5'].map(css);
  const color = n => n ? steps[Math.min(steps.length - 1, Math.floor((n - 1) / max * steps.length))] : css('--seq-0');
  const W = 420, Ht = 470;
  const proj = d3.geoMercator().fitSize([W, Ht], GEO);
  const svg = d3.create('svg').attr('viewBox', `0 0 ${W} ${Ht}`).attr('role', 'img')
    .attr('aria-label', 'Mapa de Colombia por departamento con el número de cargos encontrados');
  svg.append('g').selectAll('path').data(GEO.features).join('path')
    .attr('d', d3.geoPath(proj)).attr('fill', f => color((por.get(f.properties.k) || []).length))
    .attr('stroke', css('--surface-1')).attr('stroke-width', 1).style('cursor', 'pointer')
    .on('mousemove', (ev, f) => {
      const hs = por.get(f.properties.k) || [], s = hs.filter(h => h.smlv != null).map(h => h.smlv);
      showTip(ev, `<b>${f.properties.n}</b><br>${D.procesos_dpto[f.properties.k] || 0} procesos en la base<br>${hs.length} cargos con pago` +
        (s.length ? `<br>Mediana: <b>${fmt2.format(med(s))} SMLV</b>` : ''));
    })
    .on('mouseleave', hideTip);
  const lg = svg.append('g').attr('transform', `translate(8,${Ht - 30})`);
  lg.append('text').attr('y', -6).text('Cargos con pago');
  [0, ...steps.map((_, i) => i)].forEach((s, i) => {
    lg.append('rect').attr('x', i * 26).attr('width', 24).attr('height', 10).attr('rx', 2)
      .attr('fill', i === 0 ? css('--seq-0') : steps[i - 1]);
  });
  lg.append('text').attr('x', 0).attr('y', 24).text('0');
  lg.append('text').attr('x', steps.length * 26 + 24).attr('y', 24).attr('text-anchor', 'end').text(fmt.format(max));
  el.replaceChildren(svg.node());
}

function serie() {
  const el = document.getElementById('serie');
  const S = D.serie.slice(-60);
  const completo = S.some(d => d.publicados != null);
  document.getElementById('serie-sub').textContent = completo
    ? 'Barra gris: publicados ese día en SECOP (o procesos en la base, en días sin corte diario). Barra azul: procesos con al menos un cargo y pago extraído.'
    : 'Todavía no hay cortes diarios completos (app.py --dia). Barra gris: procesos en la base, aún filtrados por palabra clave.';
  document.getElementById('leg-serie').innerHTML =
    `<span><i style="background:${css('--muted-bar')}"></i>${completo ? 'Publicados' : 'En la base'}</span>` +
    `<span><i style="background:${css('--series-1')}"></i>Con dato usable</span>`;
  if (!S.length) { el.innerHTML = '<div class="vacio">Sin datos.</div>'; return; }
  const W = 1100, Ht = 220, m = {t: 10, r: 8, b: 28, l: 40};
  const total = d => d.publicados ?? d.en_base;
  const x = d3.scaleBand().domain(S.map(d => d.fecha)).range([m.l, W - m.r]).paddingInner(0.25);
  const y = d3.scaleLinear().domain([0, d3.max(S, total) || 1]).nice().range([Ht - m.b, m.t]);
  const bw = Math.min(24, x.bandwidth());
  const svg = d3.create('svg').attr('viewBox', `0 0 ${W} ${Ht}`).attr('role', 'img').attr('aria-label', 'Procesos por día');
  svg.append('g').selectAll('line').data(y.ticks(4)).join('line').attr('class', 'gridline')
    .attr('x1', m.l).attr('x2', W - m.r).attr('y1', d => y(d)).attr('y2', d => y(d));
  svg.append('g').attr('class', 'axis').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4).tickSize(0)).select('.domain').remove();
  const every = Math.ceil(S.length / 12);
  svg.append('g').attr('class', 'axis').attr('transform', `translate(0,${Ht - m.b})`)
    .call(d3.axisBottom(x).tickValues(S.filter((_, i) => i % every === 0).map(d => d.fecha))
      .tickFormat(f => f.slice(8, 10) + '/' + f.slice(5, 7)).tickSizeOuter(0));
  const bar = (v, x0, w) => { const h = Math.max(0, y(0) - y(v)), r = Math.min(4, h, w / 2);
    return `M${x0},${y(0)}V${y(0) - h + r}Q${x0},${y(0) - h} ${x0 + r},${y(0) - h}H${x0 + w - r}Q${x0 + w},${y(0) - h} ${x0 + w},${y(0) - h + r}V${y(0)}Z`; };
  const g = svg.append('g').selectAll('g').data(S).join('g');
  const off = d => x(d.fecha) + (x.bandwidth() - bw) / 2;
  g.append('path').attr('d', d => bar(total(d), off(d), bw)).attr('fill', css('--muted-bar'));
  g.append('path').attr('d', d => bar(d.con_hallazgo, off(d), bw)).attr('fill', css('--series-1'));
  g.append('rect').attr('x', d => x(d.fecha)).attr('width', x.step()).attr('y', m.t).attr('height', Ht - m.t - m.b)
    .attr('fill', 'transparent')
    .on('mousemove', (ev, d) => showTip(ev, `<b>${d.fecha}</b><br>${completo ? 'Publicados' : 'En la base'}: ${fmt.format(total(d))}` +
      `<br>Analizados: ${fmt.format(d.analizados)}<br>Con dato usable: ${fmt.format(d.con_hallazgo)}`))
    .on('mouseleave', hideTip);
  el.replaceChildren(svg.node());
}

function tabla(H) {
  const t = document.getElementById('tabla');
  const head = '<tr><th>Fecha</th><th>Cargo</th><th class="num">Años</th><th class="num">Pago mensual</th><th class="num">SMLV</th><th>Entidad</th><th>Departamento</th><th>Frente a su franja</th><th>Confianza</th><th>Estado</th><th>Fuente</th></tr>';
  if (!H.length) { t.innerHTML = head + '<tr><td colspan="11" class="vacio">Sin cargos para este filtro.</td></tr>'; return; }
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  t.innerHTML = head + H.map(h => `<tr>
    <td>${h.fecha}</td><td class="wrap">${esc(h.cargo)}</td>
    <td class="num">${h.anos ?? '—'}${h.anos != null && h.anos_nivel !== 'cargo' ? '*' : ''}</td>
    <td class="num">${peso(h.pago)}</td><td class="num">${h.smlv != null ? fmt2.format(h.smlv) : '—'}</td>
    <td class="wrap">${esc(h.entidad)}${h.tarifa ? `<br><span style="color:var(--text-muted)" title="${esc(h.tarifa)}">cita tabla oficial de tarifas</span>` : ''}</td><td>${esc(h.dpto)}</td>
    <td>${{destacado: 'Paga bien (cuartil superior)', bajo: 'Paga poco (cuartil inferior)', en_rango: 'En rango'}[h.clase] || '<span style="color:var(--text-muted)">Sin comparables aún</span>'}</td>
    <td><span class="chip"><i style="background:${css(CONF[h.confianza])}"></i>${h.confianza}</span></td>
    <td>${h.estado.replace('_', ' ')}</td>
    <td>${h.url ? `<a href="${esc(h.url)}" target="_blank" rel="noopener">${esc(h.proceso)}</a>` : esc(h.proceso)}<br><span style="color:var(--text-muted)">${esc(h.fuente)}</span></td>
  </tr>`).join('');
}

function render() {
  const H = filtrados();
  tiles(H); scatter(H); mapa(H); serie(); tabla(H);
}

const temas = ['auto', 'light', 'dark'];
let tema = 'auto';
try { tema = localStorage.getItem('tema') || 'auto'; } catch (e) {}
function aplicarTema() {
  if (tema === 'auto') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', tema);
  document.getElementById('f-tema').textContent = 'Tema: ' + {auto: 'automático', light: 'claro', dark: 'oscuro'}[tema];
}
document.getElementById('f-tema').onclick = () => {
  tema = temas[(temas.indexOf(tema) + 1) % 3];
  try { localStorage.setItem('tema', tema); } catch (e) {}
  aplicarTema(); render();
};
document.getElementById('f-conf').onchange = render;
document.getElementById('f-estado').onchange = render;
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', render);
aplicarTema();
render();

const s = Object.entries(D.smlv).map(([a, v]) => `${a}: ${peso(v.valor)} (${v.fuente})`).join(' · ');
document.getElementById('pie').innerHTML =
  `Generado ${D.generado}. SMLV del año de publicación de cada proceso: ${s}. ` +
  `Años con * = máximo exigido en el proceso, no cruzado con el cargo; no entran en la dispersión. ` +
  `Confianza alta: tabla del PDF; media: texto digital; baja: OCR, IA local o pago mayor a 15 SMLV. ` +
  `"Paga bien/poco" = cuartil superior/inferior frente a cargos con la misma franja de experiencia exigida (mínimo 8 comparables). ` +
  `Ninguna cifra se publica sin revisión humana (estado confirmado).`;
</script>
</body>
</html>
'''
