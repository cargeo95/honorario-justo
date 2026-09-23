from pathlib import Path

import fitz

from honorario_justo.fuentes.documentos import ocr_page, ocr_status

folder = Path(__file__).resolve().parents[1] / 'qa-output'
folder.mkdir(exist_ok=True)
with fitz.open() as doc:
    page = doc.new_page()
    page.insert_text((45, 75), 'Director de consultoria. Experiencia: cinco anos.', fontsize=20)
    page.insert_text((45, 115), 'Salario mensual: $ 4.400.000. Dedicacion 80%.', fontsize=20)
    text = ocr_page(page, folder, 1)
    assert '4.400.000' in text, text
    assert 'Director' in text, text
    print(ocr_status())
    print(ascii(text))
