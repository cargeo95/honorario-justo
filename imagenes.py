"""Imagenes para LinkedIn y TikTok (PNG a tamano exacto) desde 'dias' y 'hallazgos'.

Formatos:
- linkedin: 1080x1350 (4:5, vertical; el que mas ocupa en el feed). Las diapositivas de
  una misma pieza se juntan ademas en un PDF para publicarlas como carrusel/documento.
- tiktok:   1080x1920 (9:16, modo foto/carrusel). Zonas seguras: la interfaz de TikTok
  tapa ~180 px arriba, ~440 px abajo (texto y audio) y ~150 px a la derecha (botones).

Por defecto solo usa cifras confirmadas (--marcar). Con borrador=True incluye las
'sin_revisar' y estampa "BORRADOR" en cada imagen, para revisar antes de publicar.
"""
import textwrap
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Polygon  # noqa: E402

from app import MESES, db, indicadores_folder, pesos  # noqa: E402
from extraccion import SMLV, SMLV_FUENTE  # noqa: E402

FORMATOS = {
    'linkedin': {'w': 1080, 'h': 1350, 'top': 80, 'bottom': 80, 'left': 80, 'right': 80},
    'tiktok': {'w': 1080, 'h': 1920, 'top': 200, 'bottom': 460, 'left': 80, 'right': 170},
}
# Paleta clara del tablero (validada con el script de dataviz: slots 1-3 pasan CVD).
C = {'fondo': '#fcfcfb', 'tinta': '#0b0b0b', 'sec': '#52514e', 'tenue': '#7a7974', 'grid': '#e6e5e0',
     'serie': '#2a78d6', 'borrador': '#fff4d6',
     'seq': ['#f0efec', '#cde2fb', '#9ec5f4', '#5598e7', '#256abf', '#104281']}
DIAS_SEMANA = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']

for _f in ('segoeui.ttf', 'seguisb.ttf', 'segoeuib.ttf'):
    _p = Path('C:/Windows/Fonts') / _f
    if _p.exists():
        font_manager.fontManager.addfont(str(_p))
plt.rcParams['font.family'] = ['Segoe UI', 'Arial', 'DejaVu Sans']


def _smlv(v):
    return f'{v:.2f}'.replace('.', ',')


def nombre_propio(s):
    """'ALCALDIA MUNICIPAL DE GUAPOTA' -> 'Alcaldia Municipal de Guapota'."""
    menores = {'de', 'del', 'la', 'las', 'los', 'el', 'y', 'e', 'en'}
    palabras = (s or '').lower().split()
    return ' '.join(w if i and w in menores else w.capitalize() for i, w in enumerate(palabras))


def _fecha_larga(d):
    return f'{DIAS_SEMANA[d.weekday()].capitalize()} {d.day} de {MESES[d.month - 1]} de {d.year}'


class Lienzo:
    """Figura a tamano exacto en px, con helpers en coordenadas de pixel (origen arriba-izquierda)."""

    def __init__(self, formato, borrador):
        self.f = FORMATOS[formato]
        self.W, self.H = self.f['w'], self.f['h']
        self.fig = plt.figure(figsize=(self.W / 100, self.H / 100), dpi=100, facecolor=C['fondo'])
        self.x0, self.x1 = self.f['left'], self.W - self.f['right']
        self.y = self.f['top']  # cursor vertical
        if borrador:
            self.fig.patches.append(FancyBboxPatch((0, 1 - 56 / self.H), 1, 56 / self.H, boxstyle='square,pad=0',
                                                   transform=self.fig.transFigure, facecolor=C['borrador'],
                                                   edgecolor='none'))
            self.texto(self.W / 2, 28, 'BORRADOR · cifras sin revisar', 20, peso='semibold', ha='center', va='center')

    def fx(self, px):
        return px / self.W

    def fy(self, py):
        return 1 - py / self.H

    def texto(self, x, y, s, size, color=None, peso='normal', ha='left', va='top', **kw):
        return self.fig.text(self.fx(x), self.fy(y), s, fontsize=size * 0.72, color=color or C['tinta'],
                             fontweight=peso, ha=ha, va=va, **kw)

    def parrafo(self, s, size, color=None, peso='normal', interlineado=1.3, espacio_despues=0):
        # ~0.5 em de ancho medio por caracter en Segoe UI (0.55 en negrita).
        ancho_chars = int((self.x1 - self.x0) / (size * (0.55 if peso != 'normal' else 0.5)))
        lineas = textwrap.wrap(s, ancho_chars) or ['']
        for linea in lineas:
            self.texto(self.x0, self.y, linea, size, color=color, peso=peso)
            self.y += size * interlineado
        self.y += espacio_despues

    def eje(self, alto):
        ax = self.fig.add_axes([self.fx(self.x0), self.fy(self.y + alto), self.fx(self.x1 - self.x0), alto / self.H])
        self.y += alto
        return ax

    def pie(self, lineas):
        y = self.H - self.f['bottom'] - 22 * len(lineas)
        self.fig.lines.append(plt.Line2D([self.fx(self.x0), self.fx(self.x1)], [self.fy(y - 18)] * 2,
                                         transform=self.fig.transFigure, color=C['grid'], lw=1))
        for linea in lineas:
            self.texto(self.x0, y, linea, 18, color=C['tenue'])
            y += 22

    def guardar(self, path):
        self.fig.savefig(path, dpi=100, facecolor=C['fondo'])
        plt.close(self.fig)
        return path


def _encabezado(lz, etiqueta, titulo, sub=None):
    lz.texto(lz.x0, lz.y, etiqueta.upper(), 20, color=C['serie'], peso='semibold')
    lz.y += 46
    lz.parrafo(titulo, 50, peso='semibold', interlineado=1.18, espacio_despues=12)
    if sub:
        lz.parrafo(sub, 26, color=C['sec'], espacio_despues=18)


def _cifra(lz, valor, etiqueta, grande=False):
    size = 120 if grande else 64
    lz.texto(lz.x0, lz.y, valor, size, peso='semibold')
    lz.y += size * 1.12
    lz.parrafo(etiqueta, 26, color=C['sec'], espacio_despues=34)


def _fuente(fecha_ref):
    anio = int(fecha_ref[:4])
    smlv = SMLV.get(anio)
    return ['Fuente: SECOP II, Datos Abiertos Colombia (p6dx-8zbt) y documentos del proceso.',
            f'SMLV {anio}: ${pesos(smlv)} ({SMLV_FUENTE.get(anio, "")}). Valores antes de impuestos y factor prestacional.'
            if smlv else 'SMLV del año de publicación.',
            'Observatorio de Mínima Cuantía']


def _barras_smlv(lz, hallazgos, alto):
    """Barras horizontales en SMLV, una por cargo, con linea de referencia en 1 SMLV."""
    hs = hallazgos[::-1]
    ax = lz.eje(alto)
    ax.set_facecolor(C['fondo'])
    ys = range(len(hs))
    ax.barh(list(ys), [h['pago_smlv'] for h in hs], height=0.3, color=C['serie'], zorder=2)
    xmax = max(6, max(h['pago_smlv'] for h in hs) * 1.25)
    ax.set_xlim(0, xmax)
    ax.set_ylim(-0.6, len(hs) - 0.35)
    ax.axvline(1, color=C['tinta'], lw=1.5, zorder=3)
    ax.text(1, len(hs) - 0.45, ' 1 SMLV', fontsize=15, color=C['tinta'], va='bottom', zorder=5)
    # Las etiquetas tapan la linea de referencia y la grilla (fondo del color de la superficie).
    tapa = {'facecolor': C['fondo'], 'edgecolor': 'none', 'pad': 1.5}
    for x in range(0, int(xmax) + 1):
        ax.axvline(x, color=C['grid'], lw=1, zorder=1)
    for y, h in zip(ys, hs):
        ax.text(h['pago_smlv'] + xmax * 0.015, y, f"{_smlv(h['pago_smlv'])} SMLV", va='center', fontsize=17,
                color=C['tinta'], fontweight='semibold', zorder=5, bbox=tapa)
        anos = f"{h['anos_experiencia']} años exigidos" if h['anos_nivel'] == 'cargo' else 'años sin cruzar'
        cargo = textwrap.shorten(h['cargo'].capitalize(), 48, placeholder='…')
        ax.text(0, y + 0.2, cargo, va='bottom', fontsize=17, color=C['tinta'], zorder=5, bbox=tapa)
        ax.text(0, y - 0.2, f"{anos} · {textwrap.shorten(nombre_propio(h['entidad']), 40, placeholder='…')}",
                va='top', fontsize=13.5, color=C['sec'], zorder=5, bbox=tapa)
    ax.set_yticks([])
    ax.tick_params(axis='x', colors=C['tenue'], labelsize=13, length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xlabel('Pago mensual en salarios mínimos (SMLV)', fontsize=14, color=C['sec'])


def _mapa(lz, por_dpto, alto):
    import tablero
    geo = tablero.geo_simplificado()
    ax = lz.eje(alto)
    ax.set_axis_off()
    if not geo:
        ax.text(0.5, 0.5, 'Mapa no disponible', ha='center', transform=ax.transAxes)
        return
    maximo = max((v['cargos'] for v in por_dpto.values()), default=1) or 1
    claves = {tablero.clave_dpto(d): dict(v, nombre=d) for d, v in por_dpto.items()}
    etiquetas = []
    for f in geo['features']:
        dato = claves.get(f['properties']['k'], {})
        n = dato.get('cargos', 0)
        color = C['seq'][0] if not n else C['seq'][min(5, 1 + int((n - 1) / maximo * 5))]
        g = f['geometry']
        polys = [g['coordinates']] if g['type'] == 'Polygon' else g['coordinates']
        for poly in polys:
            ax.add_patch(Polygon(poly[0], closed=True, facecolor=color, edgecolor=C['fondo'], lw=0.8))
        if n:
            anillo = max((p[0] for p in polys), key=len)
            cx, cy = sum(p[0] for p in anillo) / len(anillo), sum(p[1] for p in anillo) / len(anillo)
            etiquetas.append((cx, cy, dato['nombre'], n, dato['mediana_smlv']))  # nombre de SECOP, con tildes
    # Sin hover en una imagen: cada departamento con datos lleva su etiqueta, con linea guia
    # hacia el margen derecho para que no se tapen entre si.
    for i, (cx, cy, nombre, n, med) in enumerate(sorted(etiquetas, key=lambda e: -e[1])):
        ty = 11.8 - i * 1.7
        ax.plot([cx, -66.4], [cy, ty], color=C['sec'], lw=1, zorder=4)
        ax.plot([cx], [cy], 'o', ms=6, color=C['tinta'], mec=C['fondo'], mew=2, zorder=5)
        ax.text(-66.2, ty + 0.25, nombre, fontsize=17, fontweight='semibold', color=C['tinta'],
                va='bottom', zorder=5)
        ax.text(-66.2, ty + 0.2, f"{n} cargo{'s' if n != 1 else ''} · mediana {_smlv(med)} SMLV", fontsize=14,
                color=C['sec'], va='top', zorder=5)
    ax.set_xlim(-79.2, -58.5)  # margen a la derecha para las etiquetas
    ax.set_ylim(-4.3, 12.6)
    ax.set_aspect('equal')
    # Leyenda: rampa de 0 a maximo.
    for i, col in enumerate(C['seq']):
        ax.add_patch(FancyBboxPatch((0.02 + i * 0.07, 0.03), 0.06, 0.022, boxstyle='round,pad=0,rounding_size=0.004',
                                    transform=ax.transAxes, facecolor=col, edgecolor='none'))
    ax.text(0.02, 0.075, 'Cargos con pago identificado', transform=ax.transAxes, fontsize=14, color=C['sec'])
    ax.text(0.02, 0.0, '0', transform=ax.transAxes, fontsize=13, color=C['sec'], va='top')
    ax.text(0.02 + 6 * 0.07 - 0.01, 0.0, str(maximo), transform=ax.transAxes, fontsize=13, color=C['sec'],
            va='top', ha='right')


def _hallazgos(desde, hasta, borrador):
    sql = ("SELECT * FROM hallazgos WHERE estado!='descartado' AND substr(fecha_publicacion,1,10) BETWEEN ? AND ? "
           "AND pago_smlv IS NOT NULL")
    if not borrador:
        sql += " AND estado='confirmado'"
    with db() as conn:
        return [dict(r) for r in conn.execute(sql + ' ORDER BY pago_smlv', (desde, hasta))]


def _pdf(pngs, destino):
    from PIL import Image
    imgs = [Image.open(p).convert('RGB') for p in pngs]
    imgs[0].save(destino, save_all=True, append_images=imgs[1:], resolution=100)
    return destino


def imagenes_dia(fecha, borrador=False, root=None):
    with db() as conn:
        dia = conn.execute('SELECT * FROM dias WHERE fecha=?', (fecha,)).fetchone()
    if not dia:
        raise ValueError(f'No hay corte para {fecha}: correr app.py --dia {fecha}')
    hs = _hallazgos(fecha, fecha, borrador)
    carpeta = indicadores_folder(root) / 'imagenes' / f'dia_{fecha}'
    carpeta.mkdir(parents=True, exist_ok=True)
    d = date.fromisoformat(fecha)
    salida = {}
    for formato in FORMATOS:
        pngs = []
        lz = Lienzo(formato, borrador)
        _encabezado(lz, 'Observatorio de Mínima Cuantía', _fecha_larga(d))
        _cifra(lz, str(dia['publicados']), 'procesos de mínima cuantía publicados en SECOP II', grande=True)
        _cifra(lz, str(dia['con_hallazgo']), f"con cargo y pago mensual identificables ({len(hs)} cargos)")
        if hs:
            med = sorted(h['pago_smlv'] for h in hs)[len(hs) // 2]
            _cifra(lz, f'{_smlv(med)} SMLV', 'pago mensual mediano de esos cargos')
        lz.pie(_fuente(fecha))
        pngs.append(lz.guardar(carpeta / f'{formato}_1_portada.png'))
        if hs:
            muestra = hs[:4] + hs[-4:] if len(hs) > 8 else hs
            lz = Lienzo(formato, borrador)
            _encabezado(lz, _fecha_larga(d), 'Cuánto se paga a cada cargo',
                        'Pago mensual frente a la experiencia exigida' + (' (los 4 más bajos y los 4 más altos)'
                                                                           if len(hs) > 8 else ''))
            alto_disp = lz.H - lz.f['bottom'] - 110 - lz.y
            _barras_smlv(lz, muestra, min(alto_disp, 140 * len(muestra) + 60))
            lz.pie(_fuente(fecha))
            pngs.append(lz.guardar(carpeta / f'{formato}_2_cargos.png'))
        if formato == 'linkedin':
            salida['linkedin_pdf'] = str(_pdf(pngs, carpeta / 'linkedin_carrusel.pdf'))
        salida[formato] = [str(p) for p in pngs]
    return salida


def imagenes_semana(fecha, borrador=False, root=None):
    from indicadores import resumen_semana
    r = resumen_semana(fecha, solo_confirmados=not borrador)
    lunes, domingo = date.fromisoformat(r['desde']), date.fromisoformat(r['hasta'])
    rango = f'Del {lunes.day} de {MESES[lunes.month - 1]} al {domingo.day} de {MESES[domingo.month - 1]} de {domingo.year}'
    carpeta = indicadores_folder(root) / 'imagenes' / f"semana_{r['semana_iso']}"
    carpeta.mkdir(parents=True, exist_ok=True)
    bajos = r['bajos'] or r['mas_bajos']
    altos = r['destacados'] or r['mas_altos']
    salida = {}
    for formato in FORMATOS:
        pngs = []
        lz = Lienzo(formato, borrador)
        _encabezado(lz, 'Resumen semanal', rango)
        _cifra(lz, str(r['publicados']), 'procesos de mínima cuantía publicados en SECOP II', grande=True)
        _cifra(lz, str(r['cargos']), f"cargos con pago mensual identificable en {r['procesos_con_dato']} procesos")
        if r['mediana_smlv'] is not None:
            _cifra(lz, f"{_smlv(r['mediana_smlv'])} SMLV", 'pago mensual mediano')
        lz.pie(_fuente(r['hasta']))
        pngs.append(lz.guardar(carpeta / f'{formato}_1_portada.png'))

        if r['por_departamento']:
            lz = Lienzo(formato, borrador)
            _encabezado(lz, rango, 'Dónde se encontraron')
            _mapa(lz, r['por_departamento'], lz.H - lz.f['bottom'] - 110 - lz.y)
            lz.pie(_fuente(r['hasta']))
            pngs.append(lz.guardar(carpeta / f'{formato}_2_mapa.png'))

        for n, (titulo, sub, lista) in enumerate([
            ('Lo que se paga poco',
             'Cuartil inferior frente a cargos con la misma experiencia exigida' if r['bajos']
             else 'Los pagos más bajos de la semana', bajos),
            ('Lo que se paga bien',
             'Cuartil superior frente a cargos con la misma experiencia exigida' if r['destacados']
             else 'Los pagos más altos de la semana', altos)], start=3):
            if not lista:
                continue
            lz = Lienzo(formato, borrador)
            _encabezado(lz, rango, titulo, sub)
            muestra = sorted(lista[:5], key=lambda h: h['pago_smlv'])
            alto_disp = lz.H - lz.f['bottom'] - 110 - lz.y
            _barras_smlv(lz, muestra, min(alto_disp, 150 * len(muestra) + 60))
            lz.pie(_fuente(r['hasta']))
            pngs.append(lz.guardar(carpeta / f'{formato}_{n}_{"bajos" if n == 3 else "altos"}.png'))

        lz = Lienzo(formato, borrador)
        _encabezado(lz, 'Cómo se calcula', 'Método y fuentes')
        for linea in [
            'Todos los procesos de mínima cuantía publicados cada día en SECOP II, sin filtrar por tema.',
            'De sus estudios previos se extrae cada cargo con pago mensual y la experiencia que se le exige, '
            'con reglas fijas y trazables a archivo y página.',
            'El pago se expresa en salarios mínimos del año de publicación. Es el valor del documento, '
            'antes de impuestos y factor prestacional.',
            '"Paga bien" o "paga poco" se mide contra cargos con la misma franja de experiencia exigida, '
            'no contra un valor fijo.',
            'Cada cifra fue revisada antes de publicarse.' if not borrador else 'BORRADOR: cifras sin revisar.']:
            lz.parrafo('• ' + linea, 26, color=C['sec'], espacio_despues=20)
        lz.pie(_fuente(r['hasta']))
        pngs.append(lz.guardar(carpeta / f'{formato}_9_metodo.png'))
        if formato == 'linkedin':
            salida['linkedin_pdf'] = str(_pdf(pngs, carpeta / 'linkedin_carrusel.pdf'))
        salida[formato] = [str(p) for p in pngs]
    return salida
