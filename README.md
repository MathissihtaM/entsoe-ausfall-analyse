# ENTSO-E Ausfall-Analyse

Lokale Auswertung der Unverfügbarkeitsmeldungen auf der ENTSO-E Transparency Platform. Land, Zeitraum und Anzahl stehen in [`out/ausfaelle.html`](out/ausfaelle.html) und werden aus den geladenen Meldungen berechnet.

**Auswertung:** [`out/ausfaelle.html`](out/ausfaelle.html) (Daten sind eingebettet). Karte und Diagramm brauchen Internet (MapLibre, Plotly).

## Was man damit macht

Filter (ÜNB, Ursache, Kategorie, Spannung, Zeitraum) gelten überall: Karte, Diagramm, Tabelle.

**Karte** — graues 220/380-kV- und HGÜ-Hintergrundnetz; darüber nur Anlagen/Leitungen mit Treffer im Filter. Punkt/Strich-Stärke nach Anzahl, Dauer oder Ausfallleistung. Klick öffnet Kurzstatistik und setzt die Tabellenauswahl.

**Diagramm** — X/Y frei (Zeit, Ursache, Dauer, Ausfallleistung, Spannung, ÜNB, Kategorie, Anlagentyp). Y zusätzlich „Menge Ausfälle/Wartungen“ (Monatszählung). Bei Dauer Median oder Mittel. Klick oder Lasso wählt Zeilen in der Tabelle.

**Tabelle** — Start/Ende, Dauer, Kategorie, ÜNB, Ursache, Typ, Spannung, Anlagenname, Grenze, Restleistung, Ausfallleistung, Status. Zeitpunkt einer Meldung ist der **Beginn** der Unverfügbarkeit.

## Welche Daten drin sind

Unavailability-Dokumente nach VO 543/2013:

| Typ | Inhalt |
|-----|--------|
| A77 | Produktionseinheit (Kraftwerk/Standort) |
| A78 | Übertragungsnetz |
| A79 | Offshore-Netz |
| A80 | Erzeugungseinheit (einzelner Block) |

**Produktionseinheit (A77)** ist das Kraftwerk als Ganzes: eine oder mehrere Maschinen hinter einem Netzanschluss. **Erzeugungseinheit (A80)** ist der einzelne Block bzw. Generator. Dieselbe Unverfügbarkeit kann auf beiden Ebenen gemeldet sein, deshalb gibt es mehr A80- als A77-Zeilen.

Ursachen: Störung, geplante Wartung, Stilllegung. Status in der Tabelle: aktiv, storniert (bei Störungen: geplante Unverfügbarkeit fand nicht statt — Meldung bleibt), zurückgezogen.

**Restleistung** ist die während der Unverfügbarkeit noch verfügbare MW (`Available_Period`), nicht die ausgefallene Leistung. 0 MW = vollständig nicht verfügbar. **Ausfallleistung** ist Nennleistung minus Restleistung. Fürs Übertragungsnetz fehlt die Nennleistung — diese Meldungen gehen nicht in Karte/Diagramm zu Ausfallleistung ein.

Pflicht ab 100 MW (Erzeugungsleistung bzw. zonenübergreifende Kapazität). Darunter liegen teilweise trotzdem Meldungen; die werden mit angezeigt. Kein vollständiges Störungsgeschehen DE, kein Verteilnetz.

## Wie die Meldungen aufbereitet sind

Rohabruf über die vier ÜNB-Regelzonen plus Gebotszone. Übertragungsnetz zusätzlich jede Regelzone gegen jeden Nachbarn. Überlappende Zeitfenster, jeweils letzte Revision je Dokument/Anlage.

Entfernt: **stornierte geplante Wartungen** (finden nicht statt) und **zurückgezogene** Dokumente (Irrtum). Stornierte Störungen bleiben.

Kategorie, Ursache, Anlagentyp und Netzbetreiber kommen aus Codes im XML — kein Namensabgleich. Der Anzeigename in Tabelle und Diagramm steht so im Dokument (bei Erzeugung ohne Ort oft `unlocated`). Netzbetreiber: erster Treffer aus Absender-EIC der vier ÜNB, ÜNB-Name im Text, EIC-Präfix `11TD`, Regelzone, Nachbarzone; sonst **unbekannt** (Plattform als Absender, nur Gebotszone DE/LU — vor allem Erzeugung, nicht das Übertragungsnetz).

Koordinaten liefert Transparency praktisch nicht. Karte (eigener Schritt):

1. Namens-Token gegen OSM ≥110 kV (nicht für `unlocated`)
2. Anlagen-EIC gegen die OPSD-/BNetzA-Kraftwerksliste
3. EIC-Kürzel (`NEUR` → Neurath) auf genau ein OSM-Kraftwerk

`11W0-0000-…` ist bewusst anonym — nicht kartierbar. Tabelle und Diagramm enthalten auch die nicht verorteten Meldungen.

## Aufbau

Python-Paket `src`, Start mit `python -m src`. Danach: `fetch_outages` (API, ZIP-Cache), `parse_outages` (XML zur Zeile), `countries` (ÜNB), `osm_network` (OSM und Kraftwerksliste), `write_html` (JSON in `data/outages.json` und nach `out/ausfaelle.html`). Vorlage und Favicon liegen in `web/`, Tests in `tests/`, Referenzdaten in `data/ref/`, API- und OSM-Cache in `data/cache/`. Die Seite ist eine Datei, CSS und JavaScript, Karte über MapLibre, Diagramm über Plotly. Kein Server, kein Build.

Die API liefert ZIP mit `Unavailability_MarketDocument`. Dokumentart, Ursache, Status und Anlagentyp sind Codes (A77–A80, B18–B20, A05/A09/A13, B01–B24). `outages.json` hält die Zeilen und das Netz als GeoJSON.

Wiederverwendet werden die API-ZIPs (leere Antwort als `.empty`, zu große Fenster werden halbiert), die OSM-Kacheln und `osm_prepared.json` (nur die geschnittene Geometrie, nicht die Zuordnung). `--rebuild-network` matcht neu, ohne Overpass. Die Regeln dazu stehen in `tests/`.
