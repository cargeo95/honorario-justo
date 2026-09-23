"""One-time public Spanish OCR model download; OCR itself runs locally."""
from pathlib import Path
import gzip
import requests

folder = Path(__file__).resolve().parents[1] / 'data/ocr'
folder.mkdir(parents=True, exist_ok=True)
target = folder / 'spa.traineddata.gz'
if not target.exists():
    response = requests.get('https://tessdata.projectnaptha.com/4.0.0/spa.traineddata.gz', timeout=120)
    response.raise_for_status()
    content = response.content
    if len(content) > 40 * 1024 * 1024 or len(gzip.decompress(content)) < 100000:
        raise ValueError('Unexpected OCR model')
    target.write_bytes(content)
print('Spanish OCR model:', target)
