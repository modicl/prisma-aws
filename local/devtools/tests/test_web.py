"""La página se sirve, tiene sus tres paneles y nunca inserta datos de la API como HTML."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app import main

HTML = Path(__file__).resolve().parent.parent / "web" / "index.html"


def test_la_raiz_sirve_la_pagina_con_los_tres_paneles():
    texto = TestClient(main.app).get("/").text
    for panel in ("panel-ejecuciones", "panel-colas", "panel-kafka"):
        assert f'id="{panel}"' in texto


def test_los_datos_de_la_api_nunca_entran_como_html():
    """Foco de revisión 5: `cause` y `error` pueden traer cualquier texto; se muestran con textContent."""
    js = HTML.read_text(encoding="utf-8")
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "outerHTML" not in js and "document.write" not in js


def test_la_pagina_no_carga_nada_externo():
    texto = HTML.read_text(encoding="utf-8")
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', texto)


def test_la_api_tiene_prioridad_sobre_los_estaticos():
    assert TestClient(main.app).get("/health").json() == {"status": "ok"}


def test_la_pagina_no_lanza_un_refresco_nuevo_mientras_el_anterior_sigue_en_curso():
    """I3: sin esta guarda las peticiones se acumulan y congelan todos los paneles."""
    js = HTML.read_text(encoding="utf-8")
    assert "refrescando" in js and "if (pausado || refrescando) return" in js
