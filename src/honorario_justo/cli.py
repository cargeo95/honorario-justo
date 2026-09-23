"""Interfaz de línea de comandos: ``honorario-justo`` (o ``python -m honorario_justo``)."""

import argparse
import json
import logging
import sys
from datetime import date, timedelta

from honorario_justo import config
from honorario_justo.almacenamiento.base_datos import case_folder, resumen_dia
from honorario_justo.fuentes.secop import SecopClient
from honorario_justo.reportes.indicadores import escribir_resumen_dia, exportar_hallazgos
from honorario_justo.servicios.aprendizaje import generar_aprendizaje
from honorario_justo.servicios.busqueda import mark_delivered, next_candidate, run_cycle
from honorario_justo.servicios.corte_diario import dias_pendientes, run_day, ultimo_corte
from honorario_justo.servicios.hallazgos import extraer_hallazgos, marcar_hallazgo


def build_parser():
    parser = argparse.ArgumentParser(
        prog='honorario-justo', description='Honorario Justo: observatorio de mínima cuantía en SECOP II'
    )
    parser.add_argument(
        '--buscar',
        action='store_true',
        help='Consulta SECOP, analiza lo nuevo y muestra el candidato mas reciente sin entregar',
    )
    parser.add_argument(
        '--siguiente',
        action='store_true',
        help='Muestra el candidato mas reciente sin entregar, sin volver a consultar SECOP',
    )
    parser.add_argument('--entregado', metavar='ID', help='Marca un proceso como entregado, para no repetirlo')
    parser.add_argument('--once', action='store_true', help='Solo ejecuta un ciclo de consulta y analisis')
    parser.add_argument(
        '--extraer',
        action='store_true',
        help='Extrae cargo/experiencia/pago de lo ya cacheado (sin consultar SECOP) y llena hallazgos',
    )
    parser.add_argument('--sin-ollama', action='store_true', help='Con --extraer: no usar el respaldo de Ollama local')
    parser.add_argument(
        '--reprocesar', action='store_true', help='Con --extraer: reprocesa casos que ya tenian hallazgos'
    )
    parser.add_argument('--exportar', action='store_true', help='Exporta hallazgos a CSV en Indicadores/')
    parser.add_argument(
        '--dia',
        nargs='?',
        const='ultimo',
        metavar='AAAA-MM-DD',
        help='Corte diario: TODOS los procesos de minima cuantia de ese dia (por defecto el ultimo '
        'dia disponible en Datos Abiertos), sin filtro de palabras clave. Tarda horas; es reanudable',
    )
    parser.add_argument('--limite', type=int, help='Con --dia: procesa solo los primeros N (para probar)')
    parser.add_argument(
        '--resumen',
        nargs='?',
        const=(date.today() - timedelta(days=1)).isoformat(),
        metavar='AAAA-MM-DD',
        help='Reescribe Indicadores/dia_AAAA-MM-DD.md con lo ya procesado, sin consultar SECOP',
    )
    parser.add_argument(
        '--marcar',
        nargs=2,
        metavar=('ID', 'ESTADO'),
        help='Revision humana de un hallazgo: confirmado | descartado | sin_revisar',
    )
    parser.add_argument(
        '--aprendizaje',
        action='store_true',
        help='Reporte de donde aparecen las cifras revisadas y el costo de OCR por tipo de proceso',
    )
    parser.add_argument('--tablero', action='store_true', help='Genera Indicadores/tablero.html')
    parser.add_argument(
        '--semana',
        nargs='?',
        const='ultima',
        metavar='AAAA-MM-DD',
        help='Resumen de la semana (lunes a domingo) que contiene esa fecha; por defecto la del '
        'ultimo corte. Escribe Indicadores/semanas/semana_AAAA-Www.md',
    )
    parser.add_argument(
        '--imagenes',
        choices=['dia', 'semana'],
        help='PNG para LinkedIn (1080x1350 + PDF carrusel) y TikTok (1080x1920) del ultimo dia o semana (o de --fecha)',
    )
    parser.add_argument('--fecha', metavar='AAAA-MM-DD', help='Con --imagenes: dia o semana a graficar')
    parser.add_argument(
        '--borrador', action='store_true', help='Con --imagenes/--semana: incluye cifras sin revisar y marca BORRADOR'
    )
    return parser


def main(args, parser):
    if args.semana:
        from honorario_justo.reportes.indicadores import escribir_resumen_semana, resumen_semana

        fecha = ultimo_corte() if args.semana == 'ultima' else args.semana
        r = resumen_semana(fecha, solo_confirmados=not args.borrador)
        print(
            json.dumps(
                {
                    'archivo': escribir_resumen_semana(r),
                    'semana': r['semana_iso'],
                    'publicados': r['publicados'],
                    'cargos': r['cargos'],
                    'mediana_smlv': r['mediana_smlv'],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.imagenes:
        from honorario_justo.reportes import imagenes

        fecha = args.fecha or ultimo_corte()
        generar = imagenes.imagenes_dia if args.imagenes == 'dia' else imagenes.imagenes_semana
        print(json.dumps(generar(fecha, borrador=args.borrador), ensure_ascii=False, indent=2))
        return
    if args.dia:
        if args.dia == 'ultimo':
            # Datos Abiertos llega con 1-2 dias de atraso: se toma lo ultimo disponible
            # y se completan los dias que hayan quedado sin procesar.
            ultimo = SecopClient().latest_day()
            fechas = dias_pendientes(ultimo)
            logging.info('Datos Abiertos llega hasta %s; dias por procesar: %s', ultimo, ', '.join(fechas) or 'ninguno')
        else:
            fechas = [args.dia]
        resultados = []
        for fecha in fechas:
            fila = run_day(fecha, limite=args.limite, use_ollama=not args.sin_ollama)
            resultados.append(dict(fila, resumen=escribir_resumen_dia(fila)))
        print(json.dumps(resultados, ensure_ascii=False, indent=2))
    elif args.resumen:
        fila = resumen_dia(args.resumen)
        print(json.dumps(dict(fila, resumen=escribir_resumen_dia(fila)), ensure_ascii=False, indent=2))
    elif args.marcar:
        print(json.dumps(marcar_hallazgo(int(args.marcar[0]), args.marcar[1]), ensure_ascii=False))
    elif args.aprendizaje:
        print(json.dumps(generar_aprendizaje(), ensure_ascii=False, indent=2))
    elif args.tablero:
        from honorario_justo.reportes.tablero import generar_tablero

        print(json.dumps(generar_tablero(), ensure_ascii=False))
    elif args.extraer:
        print(
            json.dumps(
                extraer_hallazgos(use_ollama=not args.sin_ollama, reprocesar=args.reprocesar), ensure_ascii=False
            )
        )
    elif args.exportar:
        print(json.dumps(exportar_hallazgos(), ensure_ascii=False))
    elif args.entregado:
        mark_delivered(args.entregado)
        print(json.dumps({'ok': True, 'entregado': args.entregado}, ensure_ascii=False))
    elif args.buscar or args.siguiente:
        if args.buscar:
            run_cycle()
        case = next_candidate()
        if not case:
            print(json.dumps({'candidato': None}, ensure_ascii=False))
        else:
            folder = case_folder(case['id'])
            evidencia = {
                kind: {'file': item['file'], 'page': item['page'], 'texto': item.get('excerpt', '')}
                for kind, item in case['analysis']['evidence'].items()
            }
            print(
                json.dumps(
                    {
                        'candidato': {
                            'id': case['id'],
                            'metadata': case['metadata'],
                            'status': case['status'],
                            'warnings': case['analysis'].get('warnings', []),
                            'evidencia': evidencia,
                            'carpeta_local': str(folder),
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
    elif args.once:
        run_cycle()
    else:
        parser.print_help()


def run(argv=None):
    """Punto de entrada del comando ``honorario-justo``."""
    config.configurar_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        main(args, parser)
    except Exception:
        logging.exception('Fallo no controlado: %s', ' '.join(sys.argv[1:] if argv is None else argv))
        raise
