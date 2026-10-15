"""OpenStreetMap-Netz laden, zuschneiden und mit den Meldungen verbinden.

Die Transparency-Dokumente haben praktisch keine Koordinaten. Die Karte
bekommt deshalb zwei GeoJSON-Sammlungen:

- ``background``: graues 220/380-kV- und HGÜ-Netz, ohne Meldungsbezug.
  Kurze Feldnamen, weil dieses Netz in jede HTML-Datei kopiert wird:
  ``v`` Spannung in kV, ``d`` 1 bei HGÜ.
- ``matched``: nur Anlagen mit Treffer. ``properties.ev`` sind Indizes in
  die Meldungsliste, ``name`` der OSM-Name, ``op_tso`` der Betreiber aus OSM.

Zwei Caches, beide unter ``data/cache/<Land>/``:

- Rohantworten von Overpass, je Kachel eine Datei: ``osm_lines_<n>.json``,
  ``osm_points_<n>_0.json`` (Umspannwerke), ``osm_points_<n>_1.json``
  (Kraftwerke). Eine einzelne Abfrage fürs ganze Land läuft regelmäßig
  in den Timeout.
- ``osm_prepared.json``: schon auf die Landesmaske geschnittene und
  ausgedünnte Geometrie. Gültig, solange Dateiname, Größe und
  Änderungszeit der Rohdateien und die Maske gleich sind. Die Meldungen
  stehen hier nicht drin. ``--rebuild-network`` ordnet deshalb neu zu,
  ohne Overpass noch einmal zu fragen.

Zuordnung einer Meldung, in dieser Reihenfolge: Anlagen-EIC gegen das
JAO Static Grid Model (A78/A79, inkl. Nennleistung), Namensteile gegen
OSM (nicht bei ``unlocated``), Anlagen-EIC gegen OPSD- und MaStR-CSV,
Kürzel in der EIC (``NEUR`` → Neurath) auf genau ein OSM-Kraftwerk.
"""

from __future__ import annotations

import csv
import heapq
import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from src.countries import MAP_BOUNDS, MAP_MASKS, Country, foreign_grid_eic, tso_from_operator_name
from src.parse_outages import classify_voltage
from src.static_grid import grid_table, load_grid_elements, lookup_grid

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "cache"
EIC_PLANTS_PATH = ROOT / "data" / "ref" / "conventional_power_plants_DE.csv"
MASTR_PLANTS_PATH = ROOT / "data" / "ref" / "mastr_plants.csv"
# Kuratierte Pseudo-Assets ohne echte Anlage (z. B. TenneT-Cluster "Nordsee AWZ")
MANUAL_PLANTS_PATH = ROOT / "data" / "ref" / "manual_assets.csv"

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
REQUEST_TIMEOUT = 90
MAX_RETRIES = 4
PAUSE_SEC = 1.5
RETRYABLE_HTTP = {429, 500, 502, 503, 504}
MIN_KV = 110.0
BG_MIN_KV = 220.0  # Hintergrund nur Übertragungsnetz; 110-kV-Störungen kommen über matched
# A79: Küste (Nord- oder Ostsee) oder bekannte Anlande- und Konverterpunkte, auch NRW
# (A-Nord endet in Osterath). Eine reine Breitengrenze würde NRW abschneiden.
A79_MIN_LAT = 52.55
# Ein Namensteil gilt als ortsfest, wenn alle OSM-Vorkommen in diesem Fenster liegen.
TOKEN_SPAN_DEG = 2.0
_PLATFORM_RE = re.compile(r"(dolwin|borwin|helwin|sylwin|nordlink|kontek)")
_TSO_LABELS = {"50Hertz", "TenneT", "Amprion", "TransnetBW"}
A79_PLACES = {
    "diele", "doerpen", "emden", "buettel", "buttel", "wilster", "lubmin",
    "hagermarsch", "garrel", "conneforde", "hanekenfaehr", "meppen",
    "borkum", "riffgrund", "nordlink", "arkona", "wikinger",
    "dolwin", "borwin", "helwin", "sylwin",
    "osterath", "meerbusch", "rommerskirchen", "oberzier", "anord",
}
COORD_DIGITS = 4
BG_COORD_DIGITS = 3
BG_SIMPLIFY_DEG = 0.004
MATCH_SIMPLIFY_DEG = 0.0015
# Nur echte Hoch- und Höchstspannung schon in der Abfrage filtern (110–420 kV).
# Overpass verträgt keine Anker und Gruppen im regulären Ausdruck (HTTP 504),
# deshalb eine einfache Aufzählung. Die exakten Werte filtert Python danach.
HV_TAG_RE = "110000|150000|220000|225000|380000|400000|420000"

NAME_STOP = {
    "ttg", "amp", "50h", "50hertz", "tennet", "amprion", "transnet", "transnetbw",
    "nord", "sued", "west", "ost", "blau", "weiss", "schwarz", "rot", "leitung",
    "line", "grid", "intra", "zonal", "cross", "gmbh", "senne", "awz", "nrw",
    "land", "trasse", "umspannwerk", "umspannstation", "kraftwerk",
    "schaltanlage", "unterwerk", "konverter", "konverterstation",
    "windpark", "offshore",
    "sudost", "nordost", "sudwest", "nordwest", "suedost", "suedwest",
    # Französische RTE-Namen: sonst trifft „liaison“ (7 Zeichen) jede französische Leitung
    "liaison", "ligne", "poste", "piquage", "circuit", "centrale", "reseau",
    "rte", "edf",
    # Stadtnamen: sonst landet jede Hamburg-Meldung auf jedem UW Hamburg/*
    "hamburg", "berlin", "bremen", "muenchen", "koln", "essen", "dortmund",
    "dresden", "leipzig", "frankfurt", "stuttgart", "hannover", "nurnberg",
    "duisburg", "dusseldorf", "bochum", "bielefeld", "bonn", "munster",
    "aachen", "kiel", "erfurt", "rostock", "mainz", "wiesbaden", "freiburg",
    "karlsruhe", "mannheim", "augsburg", "chemnitz", "krefeld", "potsdam",
}

_ZONAL_RE = re.compile(r"intra.?zonal|cross.?zonal|unlocated", re.I)
_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_DIGITS_RE = re.compile(r"^\d+$")
_KV_RE = re.compile(r"\d+")
_EIC_LETTERS = re.compile(r"[A-Za-z]{3,}")
_EIC_CORE = re.compile(r"11WD\d*([A-Za-z]{3,})", re.I)
_ANON_EIC = re.compile(r"^11W0+$")
_PLANT_SOURCE = re.compile(r"coal|lignite|gas|nuclear|hydro|biomass|waste|oil|geothermal", re.I)
_HV_SUBSTATION = {"transmission", "transition", "converter", "compression"}
# Kürzel aus deutschen Erzeugungs-EICs, abgebildet auf den OSM- oder Anlagennamen
_EIC_ALIAS = {
    "neur": ["neurath"],
    "nied": ["niederaussem"],
    "weis": ["weisweiler"],
    "jaen": ["janschwalde"],
    "boxb": ["boxberg"],
    "emsl": ["emsland"],
    "huck": ["huckingen"],
    "klin": ["klingenberg"],
    "lipd": ["lippendorf"],
    "reuw": ["reuter"],
    "kuet": ["kuehtai"],
    "silt": ["silz"],
    "swpu": ["schwarze", "pumpe"],
    "gold": ["goldisthal"],
    "wals": ["walsum"],
    "scho": ["scholven"],
    "hern": ["herne"],
    # nicht "berg"/"west": zu viele EICs, sonst landet alles auf Bergkamen/Westfalen
    "tief": ["tiefstack"],
    "kwkn": ["knapsack"],
    "lich": ["lichterfelde"],
    "via": ["vianden"],
    "vian": ["vianden"],
    "swsw": ["schluchsee"],
    "stau": ["staudinger"],
    "irsc": ["irsching"],
    "zoll": ["zolling"],
    "volk": ["voelklingen"],
    "gers": ["gersteinwerk"],
    "daxla": ["daxlanden"],
    "weih": ["weiher"],
    "ibbe": ["ibbenbueren"],
    "ensd": ["ensdorf"],
    "mehr": ["mehrum"],
    "westf": ["westfalen"],
    "erzh": ["erzhausen"],
    "wilk": ["wilhelmshaven"],
    "hoh2": ["hohenwarte"],
    "geha": ["geesthacht"],
}


def _fold(text: str) -> str:
    s = (text or "").lower()
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.replace("ue", "u").replace("ae", "a").replace("oe", "o")


def _tokens(text: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in _SPLIT_RE.split(_fold(text)):
        pieces = [raw]
        if len(raw) >= 8:
            for part in _PLATFORM_RE.findall(raw):
                if part != raw:
                    pieces.append(part)
        for t in pieces:
            if len(t) < 4 or t in NAME_STOP or t in seen or _DIGITS_RE.match(t):
                continue
            seen.add(t)
            out.append(t)
    return out


def _eic_compact(eic: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", eic or "").upper()


def _alias_table(extra: Optional[Dict[str, List[str]]] = None) -> Dict[str, List[str]]:
    """Einmal zusammenführen. Pro Meldung eine neue Tabelle zu bauen kostet Sekunden."""
    if not extra:
        return _EIC_ALIAS
    merged = dict(_EIC_ALIAS)
    merged.update(extra)
    return merged


def _eic_tokens(
    eic: str,
    extra: Optional[Dict[str, List[str]]] = None,
    aliases: Optional[Dict[str, List[str]]] = None,
) -> List[str]:
    """Zieht sprechende Kürzel aus 11WD7NEUR-B-KW-B, zum Beispiel neurath."""
    table = aliases if aliases is not None else _alias_table(extra)
    text = eic or ""
    parts = _EIC_LETTERS.findall(re.sub(r"\d+", " ", text))
    core = _EIC_CORE.search(text)
    if core:
        parts = [core.group(1), *parts]
    out: List[str] = []
    seen = set()
    for part in parts:
        raw = part.lower()
        folded = _fold(part)
        extras = [raw, folded, *table.get(raw, []), *table.get(folded, [])]
        for token in extras:
            if token in NAME_STOP or token in seen:
                continue
            if len(token) < 4:
                continue
            seen.add(token)
            out.append(token)
    return out


def _plant_label(row: Dict[str, str]) -> str:
    uba = (row.get("name_uba") or "").split(",")[0].strip()
    bnetza = (row.get("name_bnetza") or "").strip()
    city = (row.get("city") or "").strip()
    label = bnetza or uba or city
    if city and city.lower() not in label.lower() and len(label) <= 14:
        label = f"{label} ({city})"
    return label or "Kraftwerk"


def _plants_from_csv(src: Path) -> List[Dict[str, Any]]:
    """Eine Kraftwerks-CSV im OPSD-Schema: EIC → Name/Koordinaten."""
    if not src.is_file():
        return []
    groups: Dict[Tuple[float, float], Dict[str, Any]] = {}
    with src.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                lat = float(row.get("lat") or "")
                lon = float(row.get("lon") or "")
            except ValueError:
                lat = lon = None
            label = _plant_label(row)
            city = (row.get("city") or "").strip()
            bnetza = (row.get("name_bnetza") or "").strip()
            uba = (row.get("name_uba") or "").split(",")[0].strip()
            city_toks = set(_tokens(city))
            toks = [
                t
                for t in _tokens(bnetza or uba)
                if t not in city_toks
            ]
            for eic in (row.get("eic_code_plant") or "", row.get("eic_code_block") or ""):
                key = _eic_compact(eic)
                if len(key) < 10 or key.startswith("11W00000") or _ANON_EIC.match(key):
                    continue
                gkey = (round(lat, 3), round(lon, 3)) if lat is not None and lon is not None else ("eic", key)
                rec = groups.get(gkey)
                if rec is None:
                    rec = {
                        "name": label,
                        "lat": lat,
                        "lon": lon,
                        "keys": set(),
                        "tokens": toks,
                    }
                    groups[gkey] = rec
                rec["keys"].add(key)
                if len(label) > len(str(rec["name"])):
                    rec["name"] = label
    plants: List[Dict[str, Any]] = []
    for rec in groups.values():
        for key in rec["keys"]:
            plants.append({
                "name": rec["name"],
                "lat": rec["lat"],
                "lon": rec["lon"],
                "key": key,
                "tokens": rec["tokens"],
            })
    return plants


def _merge_plants(base: Sequence[Dict[str, Any]], overlay: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Gleiche EIC: Overlay (MaStR) gewinnt; fehlende Koordinaten bleiben aus der Basis."""
    by_key = {plant["key"]: plant for plant in base}
    for plant in overlay:
        prev = by_key.get(plant["key"])
        if prev is None:
            by_key[plant["key"]] = plant
            continue
        merged = dict(plant)
        if merged.get("lat") is None or merged.get("lon") is None:
            merged["lat"] = prev.get("lat")
            merged["lon"] = prev.get("lon")
        by_key[plant["key"]] = merged
    return list(by_key.values())


def load_eic_plants(
    path: Optional[Path] = None,
    extra: Optional[Path] = None,
    manual: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """OPSD- plus MaStR-Kraftwerksliste: EIC -> Name/Koordinaten.

    Bei gleichem EIC gewinnt MaStR, danach die kuratierte Pflegeliste
    (``manual_assets.csv``). ``11W0-0000`` bleibt ausgeschlossen.
    """
    plants = _plants_from_csv(path or EIC_PLANTS_PATH)
    extra_path = extra if extra is not None else MASTR_PLANTS_PATH
    if extra_path.is_file():
        plants = _merge_plants(plants, _plants_from_csv(extra_path))
    manual_path = manual if manual is not None else MANUAL_PLANTS_PATH
    if manual_path.is_file():
        plants = _merge_plants(plants, _plants_from_csv(manual_path))
    return plants


def _aliases_from_plants(plants: Sequence[Dict[str, Any]]) -> Dict[str, List[str]]:
    extra: Dict[str, List[str]] = {}
    for plant in plants:
        toks = [t for t in plant.get("tokens") or [] if len(t) >= 6][:4]
        if not toks:
            continue
        core = _EIC_CORE.search(plant.get("key") or "")
        letters = _EIC_LETTERS.findall(re.sub(r"\d+", " ", plant.get("key") or ""))
        if core:
            letters = [core.group(1), *letters]
        for part in letters:
            raw = part.lower()
            extra.setdefault(raw, toks)
    return extra


def _build_eic_lookup(plants: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    lookup: Dict[str, Dict[str, Any]] = {}
    for plant in plants:
        key = plant["key"]
        for n in range(10, min(len(key), 16) + 1):
            pref = key[:n]
            prev = lookup.get(pref)
            if prev is None or len(key) >= len(prev["key"]):
                lookup[pref] = plant
    return lookup


def _lookup_plant(eic: str, lookup: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    key = _eic_compact(eic)
    if len(key) < 10 or key.startswith("11W00000"):
        return None
    for n in range(min(len(key), 16), 9, -1):
        hit = lookup.get(key[:n])
        if hit:
            return hit
    return None


def _in_map_bounds(lon: float, lat: float, country: Country) -> bool:
    bounds = MAP_BOUNDS.get(country.code)
    if not bounds:
        return True
    (west, south), (east, north) = bounds
    pad = 0.35
    return (west - pad) <= lon <= (east + pad) and (south - pad) <= lat <= (north + pad)


def _in_country_mask(lon: float, lat: float, country: Country) -> bool:
    """True nur innerhalb der Landesmaske, nicht im Rechteck um die Nachbarn."""
    ring = (MAP_MASKS.get(country.code) or {}).get("coordinates", [[]])[0]
    if not ring:
        return _in_map_bounds(lon, lat, country)
    return _point_in_ring(lon, lat, ring)


_STATION_STOP = {
    "uw", "usw", "ss", "hs", "ns", "ms",
    "umspannwerk", "umspannstation", "schaltanlage", "unterwerk",
    "konverter", "konverterstation", "station", "substation",
}


def _station_key(name: str) -> str:
    folded = _fold(name or "")
    parts = []
    for p in _SPLIT_RE.split(folded):
        if not p or p in _STATION_STOP or _DIGITS_RE.match(p):
            continue
        if p == "sankt":
            p = "st"
        parts.append(p)
    return "".join(parts)


# Grenzstationen, die im DE-OSM-Auszug fehlen. St. Peter am Hart (APG) liegt
# in Österreich; ohne den Punkt landet jede TenneT-Leitung „nach St. Peter“
# auf dem gleichnamigen Umspannwerk in Sankt Peter-Ording.
_BORDER_STATIONS = (
    {"name": "St. Peter", "lon": 13.089, "lat": 48.263, "kind": "substation"},
)
_STATION_CLUSTER_DEG = 0.03
# Nur bei mehreren Kandidaten: das andere Leitungsende muss in der Nähe liegen.
_STATION_ANCHOR_DEG = 1.6


def _station_pool(points: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cands = [pt for pt in points if pt.get("kind") != "plant"]
    if not cands:
        cands = list(points)
    return [*cands, *_BORDER_STATIONS]


def _cluster_sites(hits: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sites: List[Dict[str, Any]] = []
    for hit in hits:
        lon, lat = float(hit["lon"]), float(hit["lat"])
        if any(
            abs(lon - float(s["lon"])) < _STATION_CLUSTER_DEG and abs(lat - float(s["lat"])) < _STATION_CLUSTER_DEG
            for s in sites
        ):
            continue
        sites.append(hit)
    return sites


def _station_keys_match(query: str, candidate: str) -> bool:
    """Präfix oder längerer Teilstring. Kurze Schlüssel (``bach``) zählen nicht."""
    if not query or not candidate or query == candidate:
        return query == candidate and bool(query)
    shorter, longer = (query, candidate) if len(query) <= len(candidate) else (candidate, query)
    if len(shorter) < 6:
        return False
    return longer.startswith(shorter) or shorter in longer


def station_sites(name: str, points: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Gleichnamige Umspannwerke, nah beieinander zu einem Ort zusammengezogen."""
    key = _station_key(name)
    if len(key) < 4:
        return []
    pool = _station_pool(points)
    exact = [pt for pt in pool if _station_key(pt.get("name") or "") == key]
    hits = exact or [pt for pt in pool if _station_keys_match(key, _station_key(pt.get("name") or ""))]
    return _cluster_sites(hits)


def _anchor_dist(pt: Dict[str, Any], anchor: Dict[str, Any]) -> float:
    return abs(float(pt["lon"]) - float(anchor["lon"])) + abs(float(pt["lat"]) - float(anchor["lat"]))


def match_station(
    name: str,
    points: Sequence[Dict[str, Any]],
    anchor: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Eine SGM-Station auf ein OSM-Umspannwerk.

    Mehrere Orte desselben Namens (St. Peter an der Nordsee, bei Langenfeld,
    an der österreichischen Grenze) werden über das andere Leitungsende
    getrennt. Ein eindeutiger Name gilt auch ohne Anker.
    """
    sites = station_sites(name, points)
    if not sites:
        return None
    if len(sites) == 1:
        return sites[0]
    if anchor is None:
        return None
    ranked = sorted(sites, key=lambda s: _anchor_dist(s, anchor))
    best = _anchor_dist(ranked[0], anchor)
    if best > _STATION_ANCHOR_DEG:
        return None
    if len(ranked) > 1 and _anchor_dist(ranked[1], anchor) < best + 0.2:
        return None
    return ranked[0]


def match_line_ends(
    name_a: str,
    name_b: str,
    points: Sequence[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Beide Enden einer SGM-Leitung. Ein eindeutiges Ende löst das andere auf."""
    sites_a = station_sites(name_a, points)
    sites_b = station_sites(name_b, points)
    a = sites_a[0] if len(sites_a) == 1 else None
    b = sites_b[0] if len(sites_b) == 1 else None
    if a is None and b is not None:
        a = match_station(name_a, points, anchor=b)
    elif b is None and a is not None:
        b = match_station(name_b, points, anchor=a)
    elif a is None and b is None and sites_a and sites_b:
        best: Optional[Tuple[Dict[str, Any], Dict[str, Any]]] = None
        best_d = _STATION_ANCHOR_DEG
        for left in sites_a:
            for right in sites_b:
                dist = _anchor_dist(left, right)
                if dist < best_d:
                    best = (left, right)
                    best_d = dist
        if best:
            a, b = best
    return a, b


def _pt_dist(a: Sequence[float], b: Sequence[float]) -> float:
    return abs(float(a[0]) - float(b[0])) + abs(float(a[1]) - float(b[1]))


def line_between_stations(
    lines: Sequence[Dict[str, Any]],
    a: Dict[str, Any],
    b: Dict[str, Any],
    tol: float = 0.18,
) -> Optional[Dict[str, Any]]:
    """OSM-Leitung, deren Enden nahe den beiden Stationen liegen."""
    pa = (a["lon"], a["lat"])
    pb = (b["lon"], b["lat"])
    best: Optional[Dict[str, Any]] = None
    best_score = tol * 2
    for line in lines:
        coords = line.get("coords") or []
        if len(coords) < 2:
            continue
        start, end = coords[0], coords[-1]
        score = min(_pt_dist(start, pa) + _pt_dist(end, pb), _pt_dist(start, pb) + _pt_dist(end, pa))
        if score < best_score:
            best = line
            best_score = score
    return best


_GRAPH_MIN_KV = 200
_GRAPH_SNAP = 3
_ROUTE_ATTACH_DEG = 0.06
_ROUTE_DETOUR = 1.9
# Ohne OSM-Korridor nur kurze Verbindungen als Gerade. Längere Sehnen
# schneiden das Land und sehen aus wie Leitungen, die es so nicht gibt.
_STRAIGHT_MAX_DEG = 0.22


def _graph_node(lon: float, lat: float) -> Tuple[float, float]:
    return (round(float(lon), _GRAPH_SNAP), round(float(lat), _GRAPH_SNAP))


def _path_length(coords: Sequence[Sequence[float]]) -> float:
    return sum(_pt_dist(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def build_line_graph(lines: Sequence[Dict[str, Any]]) -> Dict[Tuple[float, float], List[Tuple]]:
    """OSM-Teilstücke ab 220 kV als Kanten. Knoten sind die gerundeten Enden."""
    adj: Dict[Tuple[float, float], List[Tuple]] = {}
    for line in lines:
        kv = line.get("kv") or 0
        if kv < _GRAPH_MIN_KV and not line.get("hvdc"):
            continue
        coords = line.get("coords") or []
        if len(coords) < 2:
            continue
        start = _graph_node(coords[0][0], coords[0][1])
        end = _graph_node(coords[-1][0], coords[-1][1])
        if start == end:
            continue
        path = [list(start), *[[float(p[0]), float(p[1])] for p in coords[1:-1]], list(end)]
        length = _path_length(path)
        adj.setdefault(start, []).append((end, path, length))
        adj.setdefault(end, []).append((start, list(reversed(path)), length))
    return adj


def _nodes_near(
    adj: Dict[Tuple[float, float], List[Tuple]],
    lon: float,
    lat: float,
    tol: float = _ROUTE_ATTACH_DEG,
) -> List[Tuple[float, float]]:
    found = [node for node in adj if abs(node[0] - lon) <= tol and abs(node[1] - lat) <= tol]
    found.sort(key=lambda node: abs(node[0] - lon) + abs(node[1] - lat))
    return found[:8]


def route_between(
    adj: Dict[Tuple[float, float], List[Tuple]],
    a: Dict[str, Any],
    b: Dict[str, Any],
) -> Optional[List[List[float]]]:
    """Kürzester OSM-Korridor zwischen zwei Stationen, sonst None.

    Gesucht wird nur in der Umgebung der Luftlinie, und ein Umweg,
    der deutlich länger ist als die Stationen auseinanderliegen, entfällt.
    """
    if not adj:
        return None
    starts = _nodes_near(adj, float(a["lon"]), float(a["lat"]))[:3]
    goal_nodes = _nodes_near(adj, float(b["lon"]), float(b["lat"]))[:1]
    goals = set(goal_nodes)
    if not starts or not goals:
        return None
    straight = _pt_dist((a["lon"], a["lat"]), (b["lon"], b["lat"]))
    margin = max(0.25, straight * 0.45)
    west = min(float(a["lon"]), float(b["lon"])) - margin
    east = max(float(a["lon"]), float(b["lon"])) + margin
    south = min(float(a["lat"]), float(b["lat"])) - margin
    north = max(float(a["lat"]), float(b["lat"])) + margin
    limit = max(straight * _ROUTE_DETOUR, straight + 0.12)

    best_path: Optional[List[List[float]]] = None
    best_len = limit
    for start in starts:
        heap: List[Tuple[float, Tuple[float, float]]] = [(0.0, start)]
        dist: Dict[Tuple[float, float], float] = {start: 0.0}
        prev: Dict[Tuple[float, float], Tuple[Tuple[float, float], List[List[float]]]] = {}
        while heap:
            cost, node = heapq.heappop(heap)
            if cost > dist.get(node, limit):
                continue
            if node in goals and node != start and cost < best_len:
                piece: List[List[float]] = []
                cursor: Optional[Tuple[float, float]] = node
                while cursor is not None and cursor in prev:
                    parent, coords = prev[cursor]
                    if piece and coords and _pt_dist(coords[-1], piece[0]) < 1e-4:
                        piece = coords[:-1] + piece
                    else:
                        piece = coords + piece
                    cursor = parent
                best_path = piece
                best_len = cost
                break
            if cost >= best_len:
                continue
            for other, coords, length in adj.get(node, ()):
                if not (west <= other[0] <= east and south <= other[1] <= north):
                    continue
                nxt = cost + length
                if nxt >= best_len or nxt >= dist.get(other, limit):
                    continue
                dist[other] = nxt
                prev[other] = (node, coords)
                heapq.heappush(heap, (nxt, other))
        if best_path:
            break
    return best_path


def _empty_nominal(value: Any) -> bool:
    if value is None or value == "":
        return True
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return True


# --- SGM-Namensmatching: Schaltfeld-Nummer + Endpunkt-Token ---
# ENTSO-E-Meldungen tragen oft den ÜNB-internen EIC, der nicht im Core-SGM
# steht. Name und Schaltfeld-Nummer ("... Brunsbüttel 951") wiederholen sich
# aber im SGM-Namen, dazu Farben bei Doppelsystemen (".../SW" vs. "schwarz").
_TSO_NAME_RE = re.compile(r"50\s*hertz|50h|tennet|ttg|amprion|ampron|transnet\s*bw|transnet|tbw", re.I)
_GRID_STOP_EXTRA = {"gelb", "gruen", "grau", "trafo", "trafos", "pst", "hgu", "hgue", "tr", "hvdc"}
_COLOR_ABBR = {
    "sw": "schwarz", "rt": "rot", "bl": "blau", "ge": "gelb",
    "ws": "weiss", "gr": "gruen", "gn": "gruen",
}
_GRID_COLORS = {"schwarz", "rot", "blau", "gelb", "weiss", "gruen", "grau"}
_VOLTAGE_NUMS = {"30", "60", "110", "132", "150", "220", "225", "380", "400", "420", "500", "750"}
_GRID_KV_RE = re.compile(r"(?<!\d)(110|132|150|220|225|380|400|420|500|750)(?!\d)")


def _grid_sig(text: str) -> Dict[str, Any]:
    """Signatur eines Elementnamens: Endpunkt-Token, Schaltfeld-Nummern, Farbe, kV."""
    cleaned = _TSO_NAME_RE.sub(" ", text or "")
    volts = {int(m) for m in _GRID_KV_RE.findall(cleaned)}
    circuits = {int(n) for n in _KV_RE.findall(cleaned) if n not in _VOLTAGE_NUMS}
    color = ""
    tokens: List[str] = []
    seen = set()
    for raw in _SPLIT_RE.split(_fold(cleaned)):
        if _DIGITS_RE.match(raw):
            continue
        if not color:
            color = _COLOR_ABBR.get(raw) or (raw if raw in _GRID_COLORS else "")
        if len(raw) < 4 or raw in NAME_STOP or raw in _GRID_STOP_EXTRA or raw in seen:
            continue
        seen.add(raw)
        tokens.append(raw)
    return {"tokens": tokens, "circuits": circuits, "color": color, "volts": volts}


def _shared_tokens(a: Sequence[str], b: Sequence[str]) -> int:
    """Gemeinsame Token, Präfix zählt (bork ~ borken, twis ~ twistetal)."""
    n = 0
    used: set = set()
    for x in a:
        for y in b:
            if y in used:
                continue
            if x == y or y.startswith(x) or x.startswith(y):
                used.add(y)
                n += 1
                break
    return n


def grid_name_index(elements: Optional[Sequence[Dict[str, Any]]] = None) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Index SGM-Zeilen mit Nennleistung für das Namensmatching."""
    rows = list(elements) if elements is not None else load_grid_elements()
    index: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for row in rows:
        if not row.get("nominal_mw"):
            continue
        text = " ".join(p for p in (row.get("name"), row.get("sub_from"), row.get("sub_to")) if p)
        sig = _grid_sig(text)
        if not sig["tokens"]:
            continue
        index.append((sig, row))
    return index


def _grid_name_match(name: str, voltage: str, index: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    sig = _grid_sig(name)
    if not sig["tokens"]:
        return None
    volts = set(sig["volts"])
    m = re.search(r"\d{3}", voltage or "")
    if m and (voltage or "").strip() != "unbekannt":
        volts.add(int(m.group(0)))
    best_score: Optional[Tuple[int, int, int, int, int]] = None
    best: List[Dict[str, Any]] = []
    for rsig, row in index:
        kv = row.get("kv")
        if volts and kv and int(round(float(kv))) not in volts:
            continue
        shared_t = _shared_tokens(sig["tokens"], rsig["tokens"])
        if shared_t == 0:
            continue
        shared_c = sig["circuits"] & rsig["circuits"]
        if shared_c:
            if all(c < 100 for c in shared_c) and shared_t < 2:
                continue
        else:
            if shared_t < 2:
                continue
            if sig["color"] and rsig["color"] and sig["color"] != rsig["color"]:
                continue
        color_eq = int(bool(sig["color"]) and sig["color"] == rsig["color"])
        first_eq = int(bool(sig["tokens"] and rsig["tokens"] and sig["tokens"][0] == rsig["tokens"][0]))
        score = (int(bool(shared_c)), len(shared_c), shared_t, color_eq, first_eq)
        if best_score is None or score > best_score:
            best_score = score
            best = [row]
        elif score == best_score:
            best.append(row)
    if not best:
        return None
    if len(best) == 1:
        return best[0]
    mws = {row.get("nominal_mw") for row in best}
    if len(mws) == 1:
        return best[0]
    # Uneindeutig (z. B. Y-Leitung in zwei Richtungen): konservativ das Minimum.
    mw = min(m for m in mws if m)
    return next(row for row in best if row.get("nominal_mw") == mw)


def _grid_name_lookup(
    name: str,
    voltage: str,
    index: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]],
    cache: Dict[Tuple[str, str], Optional[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    key = (name or "", voltage or "")
    if key not in cache:
        cache[key] = _grid_name_match(name, voltage, index)
    return cache[key]


def backfill_from_grid(
    events: List[Dict[str, Any]],
    table: Dict[str, Dict[str, Any]],
    name_index: Optional[Sequence[Tuple[Dict[str, Any], Dict[str, Any]]]] = None,
) -> Tuple[int, int, int]:
    """Nennleistung und Spannung aus dem SGM, nur wenn die Meldung sie nicht hat.

    Zuerst per EIC, dann per Name (Schaltfeld-Nummer + Endpunkte). Dritter
    Rückgabewert: Treffer über den Namen.
    """
    cache: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}
    n_nom = n_v = n_name = 0
    for ev in events:
        if ev.get("doc_type") not in ("A78", "A79"):
            continue
        hit = lookup_grid(ev.get("asset_eic") or "", table)
        via_name = False
        if hit is None and name_index:
            hit = _grid_name_lookup(ev.get("asset_name") or "", ev.get("voltage") or "", name_index, cache)
            via_name = hit is not None
        if not hit:
            continue
        filled = False
        if _empty_nominal(ev.get("nominal_mw")) and hit.get("nominal_mw"):
            ev["nominal_mw"] = hit["nominal_mw"]
            n_nom += 1
            filled = True
        volt = ev.get("voltage") or ""
        if (not volt or volt == "unbekannt") and hit.get("kv"):
            ev["voltage"] = classify_voltage(
                xml_kv=float(hit["kv"]),
                type_code="B22" if hit.get("type") == "DC-Link" else "",
                name="",
                location="",
            )
            n_v += 1
            filled = True
        if via_name and filled:
            n_name += 1
    return n_nom, n_v, n_name


def _merge_or_add_line(
    matched: List[Dict[str, Any]],
    coords: List[List[float]],
    name: str,
    evs: Sequence[int],
    op_tso: str = "",
) -> None:
    for feat in matched:
        if feat.get("geometry", {}).get("type") != "LineString":
            continue
        if (feat.get("properties") or {}).get("name") == name:
            feat["properties"]["ev"] = sorted(set(feat["properties"]["ev"]) | set(evs))
            return
    matched.append({
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": _round_coords(_decimate(coords, MATCH_SIMPLIFY_DEG)),
        },
        "properties": {"name": name, "ev": sorted(set(evs)), "op_tso": op_tso},
    })


def _merge_or_add_point(
    matched: List[Dict[str, Any]],
    lon: float,
    lat: float,
    name: str,
    evs: Sequence[int],
    op_tso: str = "",
) -> int:
    existing = [
        (feat, feat["geometry"]["coordinates"])
        for feat in matched
        if feat.get("geometry", {}).get("type") == "Point"
    ]
    for feat, coords in existing:
        if abs(coords[0] - lon) >= 0.03 or abs(coords[1] - lat) >= 0.03:
            continue
        feat["properties"]["ev"] = sorted(set(feat["properties"]["ev"]) | set(evs))
        if not (feat["properties"].get("name") or "").strip():
            feat["properties"]["name"] = name
        return 0
    matched.append({
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [round(lon, COORD_DIGITS), round(lat, COORD_DIGITS)],
        },
        "properties": {"name": name, "ev": sorted(set(evs)), "op_tso": op_tso},
    })
    return 1


def _attach_grid_elements(
    events: List[Dict[str, Any]],
    lines: Sequence[Dict[str, Any]],
    points: Sequence[Dict[str, Any]],
    matched: List[Dict[str, Any]],
    matched_events: set,
    table: Dict[str, Dict[str, Any]],
    name_index: Optional[Sequence[Tuple[Dict[str, Any], Dict[str, Any]]]] = None,
) -> int:
    """A78/A79 per EIC (oder Name) an SGM-Elemente hängen, Geometrie über Stationen.

    Der Verlauf kommt aus OSM: Teilstücke ab 220 kV werden zwischen den
    beiden Stationen zusammengesetzt. Eine Gerade nur, wenn die Stationen
    nah beieinanderliegen und kein Korridor gefunden wurde.
    """
    cache: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}
    graph = build_line_graph(lines)
    buckets: Dict[str, Dict[str, Any]] = {}
    for i, ev in enumerate(events):
        if i in matched_events:
            continue
        if ev.get("reason") not in ("Störung", "Geplante Wartung"):
            continue
        if ev.get("doc_type") not in ("A78", "A79"):
            continue
        hit = lookup_grid(ev.get("asset_eic") or "", table)
        if hit is None and name_index:
            hit = _grid_name_lookup(ev.get("asset_name") or "", ev.get("voltage") or "", name_index, cache)
        if not hit:
            continue
        rec = buckets.get(hit["key"])
        if rec is None:
            rec = {"el": hit, "ev": []}
            buckets[hit["key"]] = rec
        rec["ev"].append(i)

    added = 0
    for rec in buckets.values():
        el = rec["el"]
        evs = rec["ev"]
        a, b = match_line_ends(el.get("sub_from") or "", el.get("sub_to") or "", points)
        name = el.get("name") or f"{el.get('sub_from') or ''} – {el.get('sub_to') or ''}".strip(" –")
        op_tso = el.get("tso") or ""
        if a and b and (round(a["lon"], 3), round(a["lat"], 3)) != (round(b["lon"], 3), round(b["lat"], 3)):
            route = route_between(graph, a, b)
            if route and len(route) >= 2:
                _merge_or_add_line(matched, route, name, evs, op_tso)
            elif _pt_dist((a["lon"], a["lat"]), (b["lon"], b["lat"])) <= _STRAIGHT_MAX_DEG:
                coords = [
                    [round(a["lon"], COORD_DIGITS), round(a["lat"], COORD_DIGITS)],
                    [round(b["lon"], COORD_DIGITS), round(b["lat"], COORD_DIGITS)],
                ]
                _merge_or_add_line(matched, coords, name, evs, op_tso)
            else:
                continue
            matched_events.update(evs)
            added += 1
            continue
        pt = a or b
        if pt:
            added += _merge_or_add_point(matched, pt["lon"], pt["lat"], name, evs, op_tso)
            matched_events.update(evs)
    return added


def _attach_eic_plants(
    events: List[Dict[str, Any]],
    matched: List[Dict[str, Any]],
    matched_events: set,
    plants: Sequence[Dict[str, Any]],
    country: Country,
) -> int:
    lookup = _build_eic_lookup(plants)
    buckets: Dict[Tuple[float, float, str], Dict[str, Any]] = {}
    for i, ev in enumerate(events):
        if ev.get("reason") not in ("Störung", "Geplante Wartung"):
            continue
        if ev.get("doc_type") not in ("A77", "A80"):
            continue
        plant = _lookup_plant(ev.get("asset_eic") or "", lookup)
        if not plant or plant.get("lat") is None or plant.get("lon") is None:
            continue
        if not _in_country_mask(plant["lon"], plant["lat"], country):
            continue
        key = (round(plant["lat"], 3), round(plant["lon"], 3), plant["name"])
        rec = buckets.get(key)
        if rec is None:
            rec = {"plant": plant, "ev": []}
            buckets[key] = rec
        rec["ev"].append(i)

    existing = [
        (feat, feat["geometry"]["coordinates"])
        for feat in matched
        if feat.get("geometry", {}).get("type") == "Point"
    ]
    added = 0
    for rec in buckets.values():
        plant = rec["plant"]
        lon, lat = float(plant["lon"]), float(plant["lat"])
        evs = rec["ev"]
        merged = False
        plant_strong = {t for t in plant.get("tokens") or [] if len(t) >= 6}
        for feat, coords in existing:
            if abs(coords[0] - lon) >= 0.03 or abs(coords[1] - lat) >= 0.03:
                continue
            feat_name = feat.get("properties", {}).get("name") or ""
            if re.search(r"umspannwerk|unterwerk|schaltanlage|substation", feat_name, re.I):
                continue
            feat_toks = set(_tokens(feat_name))
            if plant_strong and not (plant_strong & feat_toks):
                continue
            feat["properties"]["ev"] = sorted(set(feat["properties"]["ev"]) | set(evs))
            if not (feat["properties"].get("name") or "").strip():
                feat["properties"]["name"] = plant["name"]
            matched_events.update(evs)
            merged = True
            break
        if merged:
            continue
        matched.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [round(lon, COORD_DIGITS), round(lat, COORD_DIGITS)],
            },
            "properties": {"name": plant["name"], "ev": sorted(set(evs))},
        })
        matched_events.update(evs)
        added += 1
    return added


def _attach_unique_osm_plants(
    events: List[Dict[str, Any]],
    points: List[Dict[str, Any]],
    matched: List[Dict[str, Any]],
    matched_events: set,
    extra_aliases: Optional[Dict[str, List[str]]] = None,
) -> int:
    """Restliche Erzeugungs-EICs auf genau ein OSM-Kraftwerk gleichen Namens legen."""
    aliases = _alias_table(extra_aliases)
    by_token: Dict[str, List[Dict[str, Any]]] = {}
    for pt in points:
        if pt.get("kind") != "plant":
            continue
        for tok in _tokens(pt.get("name") or ""):
            if len(tok) >= 6:
                by_token.setdefault(tok, []).append(pt)
    buckets: Dict[Tuple[float, float, str], Dict[str, Any]] = {}
    for i, ev in enumerate(events):
        if i in matched_events:
            continue
        if ev.get("reason") not in ("Störung", "Geplante Wartung"):
            continue
        if ev.get("doc_type") not in ("A77", "A80"):
            continue
        for tok in _eic_tokens(ev.get("asset_eic") or "", aliases=aliases):
            if len(tok) < 6:
                continue
            hits = by_token.get(tok) or []
            sites = {(h["lon"], h["lat"]): h for h in hits}
            if len(sites) != 1:
                continue
            pt = next(iter(sites.values()))
            key = (pt["lat"], pt["lon"], pt["name"])
            rec = buckets.get(key)
            if rec is None:
                rec = {"pt": pt, "ev": []}
                buckets[key] = rec
            rec["ev"].append(i)
            break
    added = 0
    existing = {
        (round(f["geometry"]["coordinates"][0], 3), round(f["geometry"]["coordinates"][1], 3))
        for f in matched
        if f.get("geometry", {}).get("type") == "Point"
    }
    for rec in buckets.values():
        pt = rec["pt"]
        lon, lat = pt["lon"], pt["lat"]
        evs = rec["ev"]
        matched_events.update(evs)
        loc = (round(lon, 3), round(lat, 3))
        if loc in existing:
            for feat in matched:
                if feat.get("geometry", {}).get("type") != "Point":
                    continue
                coords = feat["geometry"]["coordinates"]
                if round(coords[0], 3) == loc[0] and round(coords[1], 3) == loc[1]:
                    feat["properties"]["ev"] = sorted(set(feat["properties"]["ev"]) | set(evs))
                    break
            continue
        matched.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"name": pt["name"], "ev": sorted(set(evs))},
        })
        existing.add(loc)
        added += 1
    return added


def _point_rank(name: str) -> int:
    n = _fold(name)
    if any(k in n for k in ("kernkraft", "pumpspeicher", "heizkraft", "kraftwerk", "psw")):
        return 0
    if any(k in n for k in ("umspannwerk", "unterwerk", "schaltanlage")):
        return 2
    return 1


_DEDUPE_DEG = 0.08


def _dedupe_points(matched: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fasst OSM-Duplikate am selben Ort und identische Meldungsmengen zusammen."""
    lines: List[Dict[str, Any]] = []
    points: List[Dict[str, Any]] = []
    for feat in matched:
        if feat.get("geometry", {}).get("type") != "Point":
            lines.append(feat)
        else:
            points.append(feat)
    folded: List[str] = []
    coords: List[Tuple[float, float]] = []
    ev_sets: List[set] = []
    buckets: Dict[Tuple[int, int], List[int]] = {}
    for i, feat in enumerate(points):
        lon, lat = feat["geometry"]["coordinates"]
        coords.append((lon, lat))
        folded.append(_fold((feat.get("properties") or {}).get("name") or ""))
        ev_sets.append(set((feat.get("properties") or {}).get("ev") or []))
        key = (int(lon // _DEDUPE_DEG), int(lat // _DEDUPE_DEG))
        buckets.setdefault(key, []).append(i)
    used = [False] * len(points)
    collapsed: List[Dict[str, Any]] = []
    for i in range(len(points)):
        if used[i]:
            continue
        lon, lat = coords[i]
        name = folded[i]
        evs = set(ev_sets[i])
        group = [i]
        cx, cy = int(lon // _DEDUPE_DEG), int(lat // _DEDUPE_DEG)
        # Index-Reihenfolge beibehalten: ein späterer Treffer darf die Meldungsmenge
        # vergrößern, bevor der nächste Punkt verglichen wird.
        cands: List[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cands.extend(buckets.get((cx + dx, cy + dy), ()))
        cands.sort()
        for j in cands:
            if j <= i or used[j]:
                continue
            olon, olat = coords[j]
            if abs(olon - lon) >= _DEDUPE_DEG or abs(olat - lat) >= _DEDUPE_DEG:
                continue
            oname = folded[j]
            oevs = ev_sets[j]
            if (name and name == oname) or (evs and evs == oevs):
                evs.update(oevs)
                group.append(j)
                used[j] = True
        best = min(
            group,
            key=lambda k: (
                _point_rank(points[k]["properties"].get("name") or ""),
                -len(points[k]["properties"].get("name") or ""),
            ),
        )
        keep = points[best]
        keep["properties"]["ev"] = sorted(evs)
        labels = {(points[k]["properties"].get("op_tso") or "") for k in group}
        labels.discard("")
        keep["properties"]["op_tso"] = labels.pop() if len(labels) == 1 else ""
        collapsed.append(keep)
        used[i] = True
    return lines + collapsed


def _uniq_tokens(*groups: Sequence[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for group in groups:
        for t in group:
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _keep_plant(tags: Dict[str, Any]) -> bool:
    src = str(tags.get("plant:source") or tags.get("generator:source") or "")
    if re.search(r"solar|wind", src, re.I):
        return False
    if _PLANT_SOURCE.search(src):
        return True
    return bool(tags.get("plant:output:electricity") or tags.get("output"))


def _keep_substation(tags: Dict[str, Any]) -> bool:
    kind = str(tags.get("substation") or "").lower()
    if kind in _HV_SUBSTATION:
        return True
    kv = _max_kv(str(tags.get("voltage") or ""))
    return kv >= MIN_KV


def _cut_to_border(
    inside_pt: Sequence[float],
    outside_pt: Sequence[float],
    ring: Sequence[Sequence[float]],
    iters: int = 24,
) -> List[float]:
    """Punkt auf der Strecke knapp innerhalb der Maske."""
    lo = [float(inside_pt[0]), float(inside_pt[1])]
    hi = [float(outside_pt[0]), float(outside_pt[1])]
    for _ in range(iters):
        mid = [(lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0]
        if _point_in_ring(mid[0], mid[1], ring):
            lo = mid
        else:
            hi = mid
    return lo


def _clip_line_to_ring(
    coords: List[List[float]],
    ring: Sequence[Sequence[float]],
) -> List[List[List[float]]]:
    """Schneidet eine Linie auf die Maske; Grenzübertritte werden interpoliert."""
    if len(coords) < 2 or not ring:
        return [coords] if len(coords) >= 2 else []
    runs: List[List[List[float]]] = []
    cur: List[List[float]] = []
    prev = coords[0]
    prev_in = _point_in_ring(prev[0], prev[1], ring)
    if prev_in:
        cur.append(prev)
    for pt in coords[1:]:
        inn = _point_in_ring(pt[0], pt[1], ring)
        if prev_in and inn:
            cur.append(pt)
        elif prev_in and not inn:
            cur.append(_cut_to_border(prev, pt, ring))
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
        elif (not prev_in) and inn:
            cur = [_cut_to_border(pt, prev, ring), pt]
        prev, prev_in = pt, inn
    if len(cur) >= 2:
        runs.append(cur)
    return runs


def _point_in_ring(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _max_kv(tag: str) -> float:
    vals = [float(v) for v in _KV_RE.findall(tag or "")]
    return max(vals) / 1000.0 if vals else 0.0


def _is_hvdc(tags: Dict[str, Any]) -> bool:
    freq = str(tags.get("frequency") or "").split(";")[0].strip()
    return freq == "0"


def _overpass(query: str, cache_path: Path, allow_fetch: bool) -> Dict[str, Any]:
    if cache_path.is_file() and cache_path.stat().st_size > 100:
        return json.loads(cache_path.read_text(encoding="utf-8-sig"))
    if not allow_fetch:
        raise RuntimeError(f"kein Overpass-Cache: {cache_path.name}")
    last_err: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        url = OVERPASS_URLS[attempt % len(OVERPASS_URLS)]
        try:
            resp = requests.post(
                url,
                data={"data": query},
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": "entsoe-ausfall-analyse/1.0"},
            )
            if resp.status_code == 200:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(resp.content)
                return resp.json()
            last_err = RuntimeError(f"Overpass HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code not in RETRYABLE_HTTP:
                break
        except requests.RequestException as exc:
            last_err = exc
        if attempt + 1 < MAX_RETRIES:
            # Sofort der nächste Spiegel; nur nach einer vollen Spiegelrunde warten.
            pause = 0.2 if (attempt + 1) % len(OVERPASS_URLS) else PAUSE_SEC * (attempt + 1)
            time.sleep(pause)
    raise RuntimeError(f"Overpass fehlgeschlagen: {last_err}")


def _bbox(country: Country) -> Tuple[float, float, float, float]:
    """Rechteck Süd, West, Nord, Ost, so wie Overpass es erwartet."""
    bounds = MAP_BOUNDS.get(country.code)
    if not bounds:
        center_lon, center_lat = country.map_center
        return (center_lat - 4, center_lon - 6, center_lat + 4, center_lon + 6)
    (west, south), (east, north) = bounds
    return (south, west, north, east)


def _bbox_chunks(country: Country, n_split: int = 4) -> List[Tuple[float, float, float, float]]:
    """Gitter aus ``n_split`` mal ``n_split`` Kacheln, jede wieder Süd/West/Nord/Ost."""
    south, west, north, east = _bbox(country)
    lat_step = (north - south) / n_split
    lon_step = (east - west) / n_split
    chunks = []
    for row in range(n_split):
        for col in range(n_split):
            chunks.append((
                south + row * lat_step,
                west + col * lon_step,
                south + (row + 1) * lat_step,
                west + (col + 1) * lon_step,
            ))
    return chunks


def _line_query(south: float, west: float, north: float, east: float) -> str:
    return f"""[out:json][timeout:90];
(
  way["power"="line"]["voltage"~"{HV_TAG_RE}"]({south},{west},{north},{east});
  way["power"="cable"]["voltage"~"{HV_TAG_RE}"]({south},{west},{north},{east});
);
out tags geom;"""


def _sub_query(south: float, west: float, north: float, east: float) -> str:
    return f"""[out:json][timeout:90];
(
  nwr["power"="substation"]["name"]["voltage"~"{HV_TAG_RE}"]({south},{west},{north},{east});
  nwr["power"="substation"]["name"]["substation"~"transmission|converter|transition"]({south},{west},{north},{east});
);
out tags center;"""


def _plant_query(south: float, west: float, north: float, east: float) -> str:
    return f"""[out:json][timeout:90];
(
  nwr["power"="plant"]["name"]["plant:source"]({south},{west},{north},{east});
  nwr["power"="plant"]["name"]["plant:output:electricity"]({south},{west},{north},{east});
);
out tags center;"""


def _round_coords(coords: List[List[float]], digits: int = COORD_DIGITS) -> List[List[float]]:
    return [[round(lon, digits), round(lat, digits)] for lon, lat in coords]


def _decimate(coords: List[List[float]], min_deg: float) -> List[List[float]]:
    """Lässt Punkte weg, die näher als min_deg am Vorgänger liegen."""
    if len(coords) <= 2:
        return coords
    out = [coords[0]]
    for pt in coords[1:-1]:
        if abs(pt[0] - out[-1][0]) >= min_deg or abs(pt[1] - out[-1][1]) >= min_deg:
            out.append(pt)
    out.append(coords[-1])
    return out


def _fetch_all(jobs: List[Tuple[Path, str]], allow_fetch: bool, workers: int = 6) -> List[Dict[str, Any]]:
    """Lädt alle Aufträge (Cache-Pfad und Abfrage) parallel; gespeicherte werden übersprungen."""
    missing = [(p, q) for p, q in jobs if not (p.is_file() and p.stat().st_size > 100)]
    results: Dict[Path, Dict[str, Any]] = {}
    if missing:
        print(f"  OSM: {len(missing)} von {len(jobs)} Abrufen offen ({workers} parallel)", flush=True)
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_overpass, q, p, allow_fetch): p for p, q in missing}
            for fut in as_completed(futures):
                path = futures[fut]
                try:
                    results[path] = fut.result()
                except Exception as exc:
                    # Eine einzelne Kachel darf die Karte nicht blockieren
                    print(f"  OSM {path.stem} uebersprungen: {exc}", flush=True)
                    results[path] = {"elements": []}
                done += 1
                print(f"  OSM {path.stem} fertig ({done}/{len(missing)})", flush=True)
    out = []
    for path, _ in jobs:
        data = results.get(path)
        if data is None:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        out.append(data)
    return out


# Hochzählen, wenn sich Zuschnitt oder die behaltenen Leitungen ändern.
PREPARED_VERSION = 3


def _osm_jobs(country: Country) -> Tuple[List[Tuple[Path, str]], int]:
    """Leitungen, Umspannwerke, Kraftwerke. Kraftwerke bleiben kachelweise
    in osm_points_*_1.json. Eine einzelne Abfrage fürs ganze Land
    (osm_plants.json) wurde bei jedem Neuaufbau erneut bei Overpass geholt,
    weil die Datei nie im Cache ankam."""
    folder = RAW_DIR / country.code
    chunks = _bbox_chunks(country)
    jobs: List[Tuple[Path, str]] = []
    for tile_index, (south, west, north, east) in enumerate(chunks):
        jobs.append((folder / f"osm_lines_{tile_index}.json", _line_query(south, west, north, east)))
    point_start = len(jobs)
    for tile_index, (south, west, north, east) in enumerate(chunks):
        jobs.append((folder / f"osm_points_{tile_index}_0.json", _sub_query(south, west, north, east)))
    for tile_index, (south, west, north, east) in enumerate(chunks):
        jobs.append((folder / f"osm_points_{tile_index}_1.json", _plant_query(south, west, north, east)))
    return jobs, point_start


def _source_stamp(paths: Sequence[Path]) -> List[List[Any]]:
    stamp: List[List[Any]] = []
    for path in paths:
        st = path.stat()
        stamp.append([path.name, st.st_size, st.st_mtime_ns])
    return stamp


def _mask_key(ring: Sequence[Sequence[float]]) -> List[List[float]]:
    return [[round(float(pt[0]), 5), round(float(pt[1]), 5)] for pt in ring]


def _read_prepared(
    path: Path,
    stamp: List[List[Any]],
    mask: List[List[float]],
) -> Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    if not path.is_file() or path.stat().st_size < 100:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("v") != PREPARED_VERSION or data.get("stamp") != stamp or data.get("mask") != mask:
        return None
    lines, points = data.get("lines"), data.get("points")
    if not isinstance(lines, list) or not isinstance(points, list):
        return None
    return lines, points


def _write_prepared(
    path: Path,
    stamp: List[List[Any]],
    mask: List[List[float]],
    lines: List[Dict[str, Any]],
    points: List[Dict[str, Any]],
) -> None:
    payload = {"v": PREPARED_VERSION, "stamp": stamp, "mask": mask, "lines": lines, "points": points}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _parse_osm(
    datasets: Sequence[Dict[str, Any]],
    point_start: int,
    ring: Sequence[Sequence[float]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    lines: List[Dict[str, Any]] = []
    seen_ways = set()
    for data in datasets[:point_start]:
        for el in data.get("elements", []):
            if el.get("type") != "way" or not el.get("geometry"):
                continue
            if el.get("id") in seen_ways:
                continue
            seen_ways.add(el.get("id"))
            tags = el.get("tags") or {}
            kv = _max_kv(str(tags.get("voltage") or ""))
            if kv < MIN_KV:
                continue
            name = tags.get("name") or tags.get("name:de") or tags.get("ref") or ""
            hvdc = _is_hvdc(tags)
            # Unbenannte 110-kV-Leitungen landen weder im Hintergrund noch in der Zuordnung.
            if not name and kv < BG_MIN_KV and not hvdc:
                continue
            coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
            if ring:
                inside_n = sum(1 for lon, lat in coords if _point_in_ring(lon, lat, ring))
                # Nur ein Punkt in der Maske: typisch FR/CH-Leitung, die den Rhein streift
                if inside_n < 2:
                    continue
                if inside_n == len(coords):
                    runs = [coords]
                else:
                    runs = _clip_line_to_ring(coords, ring)
            else:
                runs = [coords] if len(coords) >= 2 else []
            if not runs:
                continue
            op_tso = tso_from_operator_name(str(tags.get("operator") or ""))
            for run in runs:
                lines.append({
                    "name": name,
                    "kv": int(round(kv)),
                    "hvdc": hvdc,
                    "op_tso": op_tso,
                    "coords": _round_coords(run),
                })

    points: List[Dict[str, Any]] = []
    seen_pts = set()
    for data in datasets[point_start:]:
        for el in data.get("elements", []):
            key = (el.get("type"), el.get("id"))
            if key in seen_pts:
                continue
            seen_pts.add(key)
            tags = el.get("tags") or {}
            name = tags.get("name") or tags.get("name:de") or ""
            if not name:
                continue
            power = tags.get("power")
            if power == "substation" and not _keep_substation(tags):
                continue
            if power == "plant" and not _keep_plant(tags):
                continue
            if el.get("type") == "node":
                lat, lon = el.get("lat"), el.get("lon")
            else:
                center = el.get("center") or {}
                lat, lon = center.get("lat"), center.get("lon")
            if lat is None or lon is None:
                continue
            if ring and not _point_in_ring(lon, lat, ring):
                continue
            points.append({
                "name": name,
                "kind": "sub" if power == "substation" else "plant",
                "op_tso": tso_from_operator_name(str(tags.get("operator") or "")),
                "lon": round(lon, COORD_DIGITS),
                "lat": round(lat, COORD_DIGITS),
            })
    return lines, points


def _cached_sources(paths: Sequence[Path]) -> bool:
    return all(path.is_file() and path.stat().st_size > 100 for path in paths)


def load_osm(country: Country, allow_fetch: bool = True) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Leitungen und Punkte, schon auf die Landesmaske geschnitten.

    Liegt ``osm_prepared.json`` zur aktuellen Rohdatei und Maske vor, wird
    es gelesen. Sonst kommen die Kachel-Caches oder, wenn die fehlen, Overpass.
    """
    folder = RAW_DIR / country.code
    ring = (MAP_MASKS.get(country.code) or {}).get("coordinates", [[]])[0]
    jobs, point_start = _osm_jobs(country)
    paths = [path for path, _query in jobs]
    prepared = folder / "osm_prepared.json"
    mask = _mask_key(ring) if ring else []
    if _cached_sources(paths):
        hit = _read_prepared(prepared, _source_stamp(paths), mask)
        if hit is not None:
            print(
                f"  OSM: Geometrie aus Cache ({len(hit[0])} Leitungen, {len(hit[1])} Punkte)",
                flush=True,
            )
            return hit
    datasets = _fetch_all(jobs, allow_fetch)
    lines, points = _parse_osm(datasets, point_start, ring)
    if _cached_sources(paths):
        try:
            _write_prepared(prepared, _source_stamp(paths), mask, lines, points)
            print(
                f"  OSM: Geometrie gecacht ({len(lines)} Leitungen, {len(points)} Punkte)",
                flush=True,
            )
        except OSError as exc:
            print(f"  OSM: Geometrie-Cache nicht geschrieben: {exc}", flush=True)
    return lines, points


def _event_index(
    events: List[Dict[str, Any]],
    extra_aliases: Optional[Dict[str, List[str]]] = None,
) -> Tuple[List[Tuple[int, List[str]]], Dict[str, List[int]]]:
    items: List[Tuple[int, List[str]]] = []
    token_index: Dict[str, List[int]] = {}
    aliases = _alias_table(extra_aliases)
    for event_index, event in enumerate(events):
        reason = event.get("reason") or ""
        if reason not in ("Störung", "Geplante Wartung"):
            continue
        raw_name = f"{event.get('asset_name') or ''} {event.get('location') or ''}"
        eic_tokens = _eic_tokens(event.get("asset_eic") or "", aliases=aliases)
        name_tokens = _tokens(raw_name)
        if _ZONAL_RE.search(raw_name):
            # unlocated hat keine Ortsnamen; Koordinaten kommen aus der EIC-Kraftwerksliste.
            continue
        tokens = _uniq_tokens(name_tokens, eic_tokens)
        if not tokens:
            continue
        items.append((event_index, tokens))
        for token in tokens:
            if len(token) >= 5:
                token_index.setdefault(token, []).append(len(items) - 1)
    return items, token_index


def _region_tso(lon: float, lat: float) -> str:
    """Grobe Regelzonen-Lage. Unsicher bleibt leer, dann greift kein Filter."""
    if not (5.5 <= lon <= 15.6 and 47.2 <= lat <= 55.6):
        return ""
    if 47.5 <= lat <= 49.75 and 7.5 <= lon <= 10.55:
        return "TransnetBW"
    if lon >= 10.9 and lat >= 50.55:
        return "50Hertz"
    if lat < 50.55 and lon > 9.4:
        return "TenneT"
    if lat >= 51.75 and lon <= 10.85:
        return "TenneT"
    if lon <= 9.4 and lat <= 52.5:
        return "Amprion"
    return ""


def _unique_local_tokens(
    lines: Sequence[Dict[str, Any]],
    points: Sequence[Dict[str, Any]],
) -> set:
    """Namensteile, deren OSM-Objekte in einem lokalen Fenster liegen."""
    boxes: Dict[str, List[float]] = {}
    for line in lines:
        coords = line.get("coords") or []
        name = line.get("name") or ""
        if not name or len(coords) < 2:
            continue
        lon = sum(c[0] for c in coords) / len(coords)
        lat = sum(c[1] for c in coords) / len(coords)
        for t in _tokens(name):
            if len(t) < 5:
                continue
            b = boxes.get(t)
            if b is None:
                boxes[t] = [lon, lat, lon, lat]
            else:
                b[0] = min(b[0], lon)
                b[1] = min(b[1], lat)
                b[2] = max(b[2], lon)
                b[3] = max(b[3], lat)
    for pt in points:
        name = pt.get("name") or ""
        lon, lat = pt.get("lon"), pt.get("lat")
        if not name or lon is None or lat is None:
            continue
        for t in _tokens(name):
            if len(t) < 5:
                continue
            b = boxes.get(t)
            if b is None:
                boxes[t] = [lon, lat, lon, lat]
            else:
                b[0] = min(b[0], lon)
                b[1] = min(b[1], lat)
                b[2] = max(b[2], lon)
                b[3] = max(b[3], lat)
    return {
        t
        for t, b in boxes.items()
        if (b[2] - b[0]) <= TOKEN_SPAN_DEG and (b[3] - b[1]) <= TOKEN_SPAN_DEG
    }


def _a79_place_tokens(*texts: str) -> set:
    blob = " ".join(texts)
    blob = blob.replace("A-Nord", " anord ").replace("a-nord", " anord ")
    return set(_tokens(blob))


def _a79_ok(lat: float, osm_name: str, ev: Optional[Dict[str, Any]] = None) -> bool:
    if lat >= A79_MIN_LAT:
        return True
    osm = _a79_place_tokens(osm_name or "")
    if osm & A79_PLACES:
        return True
    if not ev:
        return False
    named = _a79_place_tokens(ev.get("asset_name") or "", ev.get("location") or "")
    return bool(named & A79_PLACES & osm)


def _filter_hits(
    hits: List[int],
    events: List[Dict[str, Any]],
    *,
    lat: float,
    name: str = "",
    plant: bool = False,
) -> List[int]:
    out = []
    for i in hits:
        doc = events[i].get("doc_type")
        if plant and doc not in ("A77", "A80"):
            continue
        if doc == "A79" and not _a79_ok(lat, name, events[i]):
            continue
        out.append(i)
    return out


def _match(
    name: str,
    items: List[Tuple[int, List[str]]],
    token_index: Dict[str, List[int]],
    unique_tokens: Optional[set] = None,
    region_tso: str = "",
    events: Optional[List[Dict[str, Any]]] = None,
) -> List[int]:
    """Indizes der Meldungen, deren Name zur OSM-Anlage passt.

    Stoppwörter sind schon aus den Namensteilen. Ein Treffer braucht zwei
    gemeinsame Namensteile oder ein längeres, das in OSM nur an einem Ort
    vorkommt. Liegt die Anlage in einer anderen Regelzone als die Meldung,
    fällt dieser Treffer mit nur einem Namensteil weg.
    """
    osm_tokens = _tokens(name)
    if not osm_tokens:
        return []
    candidates = set()
    for token in osm_tokens:
        candidates.update(token_index.get(token, []))
    joined = " " + " ".join(osm_tokens) + " "
    hits: List[int] = []
    for item_index in candidates:
        event_index, event_tokens = items[item_index]
        overlap = 0
        matched_tokens: List[str] = []
        for token in event_tokens:
            if f" {token} " in joined:
                overlap += 1
                matched_tokens.append(token)
        long_token = any(len(token) >= 6 for token in matched_tokens)
        accepted = overlap >= 2
        if not accepted and overlap == 1 and (long_token or (len(osm_tokens) == 1 and len(osm_tokens[0]) >= 5)):
            token = matched_tokens[0]
            if unique_tokens is None:
                accepted = True
            else:
                accepted = token in unique_tokens
            if accepted and region_tso and events:
                event_tso = events[event_index].get("tso") or ""
                if event_tso in _TSO_LABELS and event_tso != region_tso:
                    accepted = False
        if accepted:
            hits.append(event_index)
    return hits


def _backfill_tso_from_operator(events: List[Dict[str, Any]], matched: List[Dict[str, Any]]) -> int:
    """Setzt unbekannten ÜNB, wenn alle Treffer derselben Leitung denselben Betreiber nennen."""
    votes: Dict[int, set] = {}
    for feat in matched:
        label = (feat.get("properties") or {}).get("op_tso") or ""
        if not label:
            continue
        for i in (feat.get("properties") or {}).get("ev") or []:
            if not isinstance(i, int) or i < 0 or i >= len(events):
                continue
            event = events[i]
            if (event.get("tso") or "") != "unbekannt":
                continue
            if foreign_grid_eic(event.get("asset_eic") or ""):
                continue
            votes.setdefault(i, set()).add(label)
    changed = 0
    for i, labels in votes.items():
        if len(labels) == 1:
            events[i]["tso"] = next(iter(labels))
            changed += 1
    return changed


def build_network(country: Country, events: List[Dict[str, Any]], allow_fetch: bool = True) -> Dict[str, Any]:
    """Hintergrundnetz und die Anlagen mit mindestens einer Meldung.

    ``matched``-Features tragen unter ``properties.ev`` die Indizes in
    ``events``. Stilllegungen und ``unlocated`` ohne EIC-Treffer fehlen
    auf der Karte, bleiben aber in der Meldungsliste.
    """
    lines, points = load_osm(country, allow_fetch=allow_fetch)
    plants = load_eic_plants()
    extra_aliases = _aliases_from_plants(plants)
    items, token_index = _event_index(events, extra_aliases)
    unique_tokens = _unique_local_tokens(lines, points)
    grid_elements = load_grid_elements()
    grid = grid_table(grid_elements)
    grid_names = grid_name_index(grid_elements)
    n_nom, n_volt, n_sgm_name = backfill_from_grid(events, grid, grid_names)

    background = []
    matched = []
    matched_events = set()
    sgm_pts = _attach_grid_elements(events, lines, points, matched, matched_events, grid, grid_names)
    for line in lines:
        if line["kv"] >= BG_MIN_KV or line["hvdc"]:
            background.append({
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": _round_coords(_decimate(line["coords"], BG_SIMPLIFY_DEG), BG_COORD_DIGITS),
                },
                "properties": {"v": line["kv"], "d": 1 if line["hvdc"] else 0},
            })
        if not line["name"]:
            continue
        coords = line["coords"]
        clon = sum(c[0] for c in coords) / len(coords)
        clat = sum(c[1] for c in coords) / len(coords)
        hits = _match(
            line["name"], items, token_index,
            unique_tokens=unique_tokens,
            region_tso=_region_tso(clon, clat),
            events=events,
        )
        max_lat = max(pt[1] for pt in coords) if coords else 0.0
        hits = _filter_hits(hits, events, lat=max_lat, name=line["name"])
        hits = [i for i in hits if i not in matched_events]
        if not hits:
            continue
        ev_idx = sorted(set(hits))
        matched_events.update(ev_idx)
        matched.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": _round_coords(_decimate(line["coords"], MATCH_SIMPLIFY_DEG)),
            },
            "properties": {"name": line["name"], "ev": ev_idx, "op_tso": line.get("op_tso") or ""},
        })
    for pt in points:
        hits = _match(
            pt["name"], items, token_index,
            unique_tokens=unique_tokens,
            region_tso=_region_tso(pt["lon"], pt["lat"]),
            events=events,
        )
        hits = _filter_hits(hits, events, lat=pt["lat"], name=pt["name"], plant=pt.get("kind") == "plant")
        hits = [i for i in hits if i not in matched_events]
        if not hits:
            continue
        ev_idx = sorted(set(hits))
        matched_events.update(ev_idx)
        matched.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [pt["lon"], pt["lat"]]},
            "properties": {"name": pt["name"], "ev": ev_idx, "op_tso": pt.get("op_tso") or ""},
        })

    eic_pts = _attach_eic_plants(events, matched, matched_events, plants, country)
    osm_pts = _attach_unique_osm_plants(events, points, matched, matched_events, extra_aliases)
    matched = _dedupe_points(matched)
    n_op = _backfill_tso_from_operator(events, matched)
    for feat in matched:
        (feat.get("properties") or {}).pop("op_tso", None)
    n_all = sum(1 for e in events if e.get("reason") in ("Störung", "Geplante Wartung"))
    n_pts = sum(1 for f in matched if f.get("geometry", {}).get("type") == "Point")
    print(
        f"  Netz: {len(lines)} Leitungen, {len(points)} OSM-Stationen, {len(plants)} EIC-Kraftwerke, "
        f"{len(grid)} SGM-Elemente | "
        f"gematcht: {len(matched)} Objekte ({n_pts} Punkte, {sgm_pts} via SGM, {eic_pts} via EIC, "
        f"{osm_pts} via OSM-EIC), "
        f"{len(matched_events)}/{n_all} Meldungen, Nennleistung Netz: {n_nom} (davon {n_sgm_name} per Name), Spannung: {n_volt}, "
        f"ÜNB aus OSM-Betreiber: {n_op}",
        flush=True,
    )
    return {
        "background": {"type": "FeatureCollection", "features": background},
        "matched": {"type": "FeatureCollection", "features": matched},
    }
