"""Mide el extractor contra los casos investigados a mano en Resultados/ (set de oro).

Re-puntua cada caso desde el cache local (sin descargar ni OCR), extrae sin Ollama y
compara pago mensual y anos exigidos contra lo verificado en cada Investigacion.md.
No guarda hallazgos: solo reporta. Uso: python scripts/evaluar_referencias.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402

# (pago mensual base del documento, anos exigidos) por caso, tomados de Resultados/*/Investigacion.md.
REFERENCIAS = {
    'CO1.REQ.11030890': ('1. Santander - puente La Colorada', [(6_829_554, None)]),
    # Giron (CO1.REQ.11034499) no entra: sus valores salen de una Tabla de Honorarios externa
    # (Circular 3-2025-000251) que no esta publicada en el expediente; no son extraibles.
    'CO1.REQ.11051829': ('3. Maripi - restauracion hidrica (PAGA BIEN)', [(9_350_000, 5), (7_000_000, 2)]),
    'CO1.REQ.11046730': ('4. Villa de Leyva - placa huella (bajo)', [(6_277_489.71, 10), (4_511_149.71, 15)]),
    'CO1.REQ.11046183': ('5. Jerico - plan de turismo (sin tabla de personal)', []),
    'CO1.REQ.11055414': ('6. Combita - interventoria acueducto', [(3_700_000, 15), (2_500_000, 10)]),
    'CO1.REQ.11034019': ('7. Sutamarchan - interventoria reforestacion', [(2_177_109, None)]),
    # Verificado contra 859998002.pdf p25 (tabla "costos directos de personal"). Ollama habia
    # dado $1.680.000 y $6.300.000 (parcial y total): error de la IA, no de la regla.
    'CO1.REQ.11069057': ('Guapota - interventoria (prueba al azar 22-sep)', [(2_800_000, None), (2_100_000, None)]),
    # Verificado contra "5. OK E.P. DISENO CUBIERTAS.pdf" p15 (no se descargaba: abreviatura E.P.).
    'CO1.REQ.11048302': ('Guadalupe (Huila) - diseno cubiertas (prueba al azar 22-sep)', [
        (3_450_000, None), (2_300_000, None), (1_750_000, None), (2_200_000, None), (1_750_000, None)]),
}


def evaluar(rescore=True):
    config = app.settings() | {'cache_only': True}
    total = encontrados = anos_ok = anos_eval = falsos = 0
    for cid, (nombre, esperados) in REFERENCIAS.items():
        if rescore:
            app.analyze_case(cid, config)
        items = app.extraer_caso(app.get_case(cid), use_ollama=False)
        print(f'\n{nombre} ({cid})')
        restantes = list(items)
        for pago, anos in esperados:
            total += 1
            match = next((i for i in restantes if abs(i['pago_mensual_cop'] - pago) < 1), None)
            if match:
                restantes.remove(match)
                encontrados += 1
                nota = ''
                if anos is not None:
                    anos_eval += 1
                    ok = match['anos_nivel'] == 'cargo' and match['anos_experiencia'] == anos
                    anos_ok += ok
                    nota = f" anos {match['anos_experiencia']} ({match['anos_nivel']}) {'OK' if ok else f'esperado {anos}'}"
                print(f"  OK     ${pago:,.0f} -> {match['cargo']} [{match['confianza']}]{nota}")
            else:
                print(f'  FALTA  ${pago:,.0f} (anos {anos})')
        for i in restantes:
            falsos += 1
            print(f"  EXTRA  ${i['pago_mensual_cop']:,.0f} -> {i['cargo']} [{i['metodo']}, {i['confianza']}]")
    print(f'\nPagos encontrados: {encontrados}/{total} ({100 * encontrados / total:.0f}%)')
    print(f'Anos correctos por cargo: {anos_ok}/{anos_eval}')
    print(f'Extras (no estan en la referencia): {falsos}')
    return encontrados, total, anos_ok, anos_eval, falsos


if __name__ == '__main__':
    evaluar(rescore='--sin-rescore' not in sys.argv)
