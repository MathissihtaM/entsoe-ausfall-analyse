"""Auswertung der ENTSO-E-Unverfügbarkeitsmeldungen.

Ablauf, gestartet mit ``python -m src``:

1. ``fetch_outages`` holt die Meldungen der Transparency-API und legt die ZIP-Antworten ab.
2. ``parse_outages`` macht aus jedem XML eine Zeile (Zeit, Anlage, Ursache, Restleistung).
3. ``countries`` ordnet die Zeile einem Übertragungsnetzbetreiber zu.
4. ``osm_network`` sucht Leitung oder Kraftwerk in OpenStreetMap und der Kraftwerksliste.
5. ``write_html`` schreibt dasselbe JSON in ``data/outages.json`` und in die HTML-Seite.

Die Seite selbst ist eine Datei: ``web/ausfaelle.template.html`` plus eingebettete Daten in ``out/ausfaelle.html``.
"""
