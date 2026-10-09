# ENTSO-E Störungsmeldungen

Lokale Auswertung der Unverfügbarkeitsmeldungen auf der ENTSO-E Transparency Platform für die Gebotszone DE/LU, Rückblick fünf Jahre. Datei: `ausfaelle.html` (Daten sind eingebettet, offline nutzbar).

## Was man damit macht

Filter (ÜNB, Ursache, Kategorie, Spannung, Zeitraum) gelten überall: Karte, Diagramm, Tabelle.

**Karte** — graues 220/380-kV- und HGÜ-Hintergrundnetz; darüber nur Anlagen/Leitungen mit Treffer im Filter. Punkt/Strich-Stärke nach Anzahl, Dauer oder Restleistung. Klick öffnet Kurzstatistik und setzt die Tabellenauswahl.

**Diagramm** — X/Y frei (Zeit, Ursache, Dauer, Restleistung, Spannung, ÜNB, Kategorie, Anlagentyp). Y zusätzlich „Menge Ausfälle/Wartungen“ (Monatszählung). Bei Dauer Median oder Mittel. Klick oder Lasso wählt Zeilen in der Tabelle.

**Tabelle** — Start/Ende, Dauer, Kategorie, ÜNB, Ursache, Typ, Spannung, Anlagenname, Grenze, Restleistung, Status. Zeitpunkt einer Meldung ist der **Beginn** der Unverfügbarkeit.

## Welche Daten drin sind

Unavailability-Dokumente nach VO 543/2013:

| Typ | Inhalt |
|-----|--------|
| A77 | Produktionseinheit (Kraftwerk/Standort) |
| A78 | Übertragungsnetz |
| A79 | Offshore-Netz |
| A80 | Erzeugungseinheit (einzelner Block) |

**Produktionseinheit (A77)** ist das Kraftwerk als Ganzes: eine oder mehrere Maschinen hinter einem Netzanschluss. **Erzeugungseinheit (A80)** ist der einzelne Block bzw. Generator. Dieselbe Unverfügbarkeit kann auf beiden Ebenen gemeldet sein, deshalb gibt es mehr A80- als A77-Zeilen.

**Unbekannter Netzbetreiber** heißt: Transparency nennt nur die Gebotszone DE/LU, Absender ist die Plattform selbst — ohne Zuordnung zu 50Hertz, Amprion, TenneT oder TransnetBW. Das betrifft vor allem Erzeugungs- und Produktionsmeldungen, nicht das Übertragungsnetz.

Ursachen: Störung, geplante Wartung, Stilllegung. Status in der Tabelle: aktiv, storniert (bei Störungen: geplante Unverfügbarkeit fand nicht statt — Meldung bleibt), zurückgezogen.

**Restleistung** ist die während der Unverfügbarkeit noch verfügbare MW (`Available_Period`), nicht die ausgefallene Leistung. 0 MW = vollständig nicht verfügbar.

Schwelle Transparency: in der Regel ab ca. 100 MW bzw. relevanter Grenzkuppel. Kein vollständiges Störungsgeschehen DE, kein Verteilnetz.

## Wie die Meldungen aufbereitet sind

Rohabruf über die vier ÜNB-Regelzonen plus Gebotszone, überlappende Zeitfenster, jeweils letzte Revision je Dokument/Anlage.

Entfernt: **stornierte geplante Wartungen** (finden nicht statt) und **zurückgezogene** Dokumente (Irrtum). Stornierte Störungen bleiben.

Koordinaten liefert Transparency praktisch nicht. Zuordnung:

- Erzeugung oft nur `intra_zonal` plus EIC → Abgleich mit der OPSD-/BNetzA-Kraftwerksliste (Name, lat/lon, EIC-Präfix). Deutsche Erzeugungs-EICs kodieren das KW oft in vier Buchstaben (`NEUR` → Neurath).
- Netz/Offshore: sprechende Namen (`Diele`, `DolWin`, …) gegen OSM ≥110 kV.
- `11W0-0000-…` ist bewusst anonym — nicht kartierbar.

Auf der Karte daher nur zuordenbare Höchst-/Hochspannungsanlagen und -leitungen. Tabelle und Diagramm enthalten auch die nicht verorteten Meldungen.
