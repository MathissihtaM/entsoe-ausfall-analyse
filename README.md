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

**Restleistung** ist die während der Unverfügbarkeit noch verfügbare MW (`Available_Period`), nicht die ausgefallene Leistung. 0 MW = vollständig nicht verfügbar. **Ausfallleistung** ist Nennleistung minus Restleistung. Fürs Netz nur, wo das JAO Static Grid Model eine Nennleistung kennt (380/220 kV, Trafos, PST, HGÜ); übrige A78/A79 gehen nicht in Karte/Diagramm zu Ausfallleistung ein.

Pflicht ab 100 MW (Erzeugungsleistung bzw. zonenübergreifende Kapazität). Darunter liegen teilweise trotzdem Meldungen; die werden mit angezeigt. Kein vollständiges Störungsgeschehen DE, kein Verteilnetz.

## Wie die Meldungen aufbereitet sind

Rohabruf über die vier ÜNB-Regelzonen plus Gebotszone. Übertragungsnetz zusätzlich jede Regelzone gegen jeden Nachbarn. Überlappende Zeitfenster, jeweils letzte Revision je Dokument/Anlage.

Entfernt: **stornierte geplante Wartungen** (finden nicht statt) und **zurückgezogene** Dokumente (Irrtum). Stornierte Störungen bleiben.

Kategorie, Ursache, Anlagentyp und Netzbetreiber kommen aus Codes im XML — kein Namensabgleich. Der Anzeigename in Tabelle und Diagramm steht so im Dokument (bei Erzeugung ohne Ort oft `unlocated`). Netzbetreiber: erster Treffer aus Absender-EIC der vier ÜNB, ÜNB-Name im Text, EIC-Präfix `11TD`, Regelzone, Nachbarzone; sonst **unbekannt** (Plattform als Absender, nur Gebotszone DE/LU — vor allem Erzeugung, nicht das Übertragungsnetz).

Koordinaten liefert Transparency praktisch nicht. Karte (eigener Schritt):

1. Anlagen-EIC gegen das JAO Core Static Grid Model (A78/A79: Stationen, Nennleistung √3·U·Imax). Der Verlauf ist der OSM-Korridor zwischen den Stationen (Teilstücke ab 220 kV zusammengesetzt, keine Luftlinie; eine Gerade nur bei nahen Stationen ohne Korridor). Gleichnamige Stationen werden über das andere Ende getrennt (St. Peter: Nordsee, Langenfeld, St. Peter am Hart). Steht der Meldungs-EIC nicht im SGM, greift das Namensmatching: Schaltfeld-Nummer plus Endpunkt-Token, Kürzel als Präfix (`L-380-BORK-WALD-TWIS-3` → `Y-Borken (-Waldeck - Twistetal) 3`), Systemfarben (`/SW` ~ `schwarz`) und Spannung als Filter
2. Namens-Token gegen OSM ≥110 kV (nicht für `unlocated`)
3. Anlagen-EIC gegen OPSD-/BNetzA-, MaStR- und kuratierte Kraftwerksliste. MaStR ab 10 MW: W-EIC von den öffentlichen Detailseiten (`IndexOeffentlich/{id}`), Koordinaten aus dem open-MaStR-Snapshot 2025-02 auf Zenodo (das Portal verschleiert sie inzwischen). Vorrang bei gleichem EIC: MaStR, zuletzt `manual_assets.csv` — dort liegt das TenneT-Aggregat „Nordsee AWZ" als Clusterpunkt in der deutschen AWZ
4. EIC-Kürzel (`NEUR` → Neurath) auf genau ein OSM-Kraftwerk

`11W0-0000-…` ist bewusst anonym — nicht kartierbar. Tabelle und Diagramm enthalten auch die nicht verorteten Meldungen. Stand DE/5 Jahre: etwa 85 % der Meldungen verortet; der Rest ist vor allem anonyme Erzeugung (`11W0-0000`, „unlocated") und ÜNB-interne Leitungscodes ohne SGM-/OSM-Anker.

## Aufbau

Python-Paket `src`, Start mit `python -m src`. Danach: `fetch_outages` (API, ZIP-Cache), `parse_outages` (XML zur Zeile), `countries` (ÜNB), `osm_network` (OSM, Static Grid Model und Kraftwerksliste), `write_html` (JSON in `data/outages.json` und nach `out/ausfaelle.html`). Vorlage und Favicon liegen in `web/`, Tests in `tests/`, Referenzdaten in `data/ref/` (OPSD, MaStR, SGM), API-, OSM- und SGM-Cache in `data/cache/`. Die Seite ist eine Datei, CSS und JavaScript, Karte über MapLibre, Diagramm über Plotly. Kein Server, kein Build.

Die API liefert ZIP mit `Unavailability_MarketDocument`. Dokumentart, Ursache, Status und Anlagentyp sind Codes (A77–A80, B18–B20, A05/A09/A13, B01–B24). `outages.json` hält die Zeilen und das Netz als GeoJSON.

Wiederverwendet werden die API-ZIPs (leere Antwort als `.empty`, zu große Fenster werden halbiert), die OSM-Kacheln und `osm_prepared.json` (nur die geschnittene Geometrie, nicht die Zuordnung). `--rebuild-network` matcht neu, ohne Overpass. Die Regeln dazu stehen in `tests/`.

## Cache und JSON nachladen

Im Repository liegen Code, Vorlage `web/ausfaelle.template.html`, Kraftwerksliste `data/ref/conventional_power_plants_DE.csv` und die fertige Seite `out/ausfaelle.html` (Git LFS). Zum Ansehen der Seite braucht es den Cache nicht. `data/cache/` und `data/outages.json` stehen in `.gitignore` und entstehen lokal, für DE etwa 1,6 GB Cache und 120 MB JSON.

Abhängigkeit und Token:

```
pip install -r requirements.txt
```

`ENTSOE_API_TOKEN` in der Umgebung oder in `.env` im Projektordner. Gelesen wird das von `load_token` in `src/fetch_outages.py`.

Alles neu erzeugen, fehlende Fenster nachladen:

```
python -m src --country DE --years 5
```

`src/__main__.py` ruft dabei der Reihe nach auf:

1. `fetch_country` in `src/fetch_outages.py` holt fehlende Fenster von `https://web-api.tp.entsoe.eu/api` nach `data/cache/DE/`. Dateiname: `<Dokumentart>_<Zonen>_<von>_<bis>_off<offset>.zip`, zum Beispiel `A77_DE_50HZ_202408260000_202508110000_off0000.zip`. Eine leere Antwort legt `fetch_pages` als dieselbe Datei mit Endung `.empty` ab.
2. `parse_all_cached` liest diese ZIPs. `parse_zip` in `src/parse_outages.py` macht aus jedem XML eine Zeile, `latest_revision` behält je Dokument und Anlage die höchste Revision.
3. `build_network` in `src/osm_network.py` ordnet die Zeilen dem Netz zu. Fehlende Kacheln holt `load_osm` bei Overpass und speichert `osm_lines_<n>.json`, `osm_points_<n>_0.json` (Umspannwerke) und `osm_points_<n>_1.json` (Kraftwerke). Daraus schreibt es `data/cache/DE/osm_prepared.json` (geschnittene Geometrie, ohne Meldungen). Die Kraftwerksliste liest `load_eic_plants` aus `data/ref/conventional_power_plants_DE.csv` und, wenn vorhanden, `data/ref/mastr_plants.csv`; zuletzt gewinnt die kuratierte `data/ref/manual_assets.csv` (Pseudo-Assets ohne echte Anlage, z. B. das TenneT-Aggregat „Nordsee AWZ" als Clusterpunkt in der deutschen AWZ). A78/A79 matcht zuerst gegen `data/ref/grid_elements_de.csv` (JAO Static Grid Model), danach per Schaltfeld-Nummer und Endpunkten im Namen. Der Leitungsverlauf ist der OSM-Korridor zwischen den beiden Stationen.
4. `_payload` schreibt `data/outages.json`. `write_html` in `src/write_html.py` setzt dasselbe JSON in die Vorlage ein und schreibt `out/ausfaelle.html`.

Weitere Aufrufe:

- `python -m src --cache-only` liest nur vorhandene ZIPs über `parse_all_cached`, ohne API. Fehlt der Token, fällt `python -m src` von selbst darauf zurück. OSM-Kacheln, die noch fehlen, holt `build_network` weiterhin.
- `python -m src --rebuild-network` setzt vorhandenes `data/outages.json` voraus und ruft nur `build_network` und `write_html` auf. Vorhandene OSM-Kacheln und `osm_prepared.json` bleiben liegen; Overpass nur für fehlende Kacheln. `--years` gilt dabei nicht.
- `python -m src --refresh-refs` lädt das JAO Core Static Grid Model und das Marktstammdatenregister (öffentliche JSON-Liste ab 10 MW, W-EIC von der Detailseite `IndexOeffentlich/{id}`; Koordinaten großer Einheiten sind im Portal inzwischen verschleiert und kommen aus dem open-MaStR-Snapshot 2025-02 auf Zenodo per MaStR-Nummer/W-EIC, sonst bleiben OPSD-Lagen; alternativ Einheiten-XMLs aus GCS oder aus `data/cache/DE/mastr/` bzw. einem lokalen `Energy_Monitor/mastr`-Ordner) und schreibt `data/ref/grid_elements_de.csv` sowie `data/ref/mastr_plants.csv`. Der Standardlauf liest nur diese CSVs. Kombinierbar mit `--rebuild-network`. MaStR-Daten: Bundesnetzagentur, Marktstammdatenregister, Datenlizenz Deutschland – Namensnennung – 2.0; Snapshot: OpenEnergyPlatform open-MaStR (Zenodo).
