"""Schreibt ``out/ausfaelle.html`` aus der Vorlage und dem Auswertungs-JSON.

Die Vorlage enthält den Platzhalter ``__OUTAGES_JSON__`` in einem
``<script type="application/json">``. Er wird durch das JSON ersetzt.
``<`` wird zu ``\\u003c``, damit ein Wert wie ``</script>`` das HTML nicht
vorzeitig beendet. Die Favicons bleiben in ``web/``; die Seite verweist
dorthin. Die fertige Datei braucht keinen Server.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "web" / "ausfaelle.template.html"
OUTPUT = ROOT / "out" / "ausfaelle.html"
PLACEHOLDER = "__OUTAGES_JSON__"


def write_html(payload: Dict[str, Any], dest: Path = OUTPUT) -> Path:
    if not TEMPLATE.is_file():
        raise FileNotFoundError(f"Vorlage fehlt: {TEMPLATE}")
    tpl = TEMPLATE.read_text(encoding="utf-8")
    embedded = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(tpl.replace(PLACEHOLDER, embedded), encoding="utf-8")
    return dest
