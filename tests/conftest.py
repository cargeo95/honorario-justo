import pytest

from honorario_justo import config
from honorario_justo.almacenamiento import base_datos


@pytest.fixture
def application(tmp_path, monkeypatch):
    """Base de datos, logs e indicadores en una carpeta temporal por prueba."""
    for nombre, carpeta in [
        ('DATA', 'data'),
        ('LOGS', 'logs'),
        ('INDICADORES', 'Indicadores'),
        ('RESULTADOS', 'Resultados'),
    ]:
        monkeypatch.setattr(config, nombre, tmp_path / carpeta)
    monkeypatch.setattr(base_datos, '_iniciadas', set())
    return tmp_path
