"""OSM-Netzdaten per Overpass laden, auf die Laendermaske schneiden und mit
ENTSO-E-Meldungen matchen. Ergebnis: schlankes GeoJSON fuer die HTML-Karte,
damit im Browser weder Vektorkacheln noch Laufzeit-Matching noetig sind."""

from __future__ import annotations

import csv
import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from analysis.countries import MAP_BOUNDS, MAP_MASKS, Country

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
EIC_PLANTS_PATH = ROOT / "data" / "conventional_power_plants_DE.csv"

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
BG_MIN_KV = 220.0  # Hintergrund nur Uebertragungsnetz; 110-kV-Stoerungen kommen ueber matched
COORD_DIGITS = 4
BG_COORD_DIGITS = 3
BG_SIMPLIFY_DEG = 0.004
MATCH_SIMPLIFY_DEG = 0.0015
# Nur echte Hoch/Hoechstspannung serverseitig vorfiltern (110-420 kV).
# Overpass mag keine Anker/Gruppen im Regex (504) -> schlichte Alternation,
# exakte Werte werden in Python nachgefiltert.
HV_TAG_RE = "110000|150000|220000|225000|380000|400000|420000"

NAME_STOP = {
    "ttg", "amp", "50h", "50hertz", "tennet", "amprion", "transnet", "transnetbw",
    "nord", "sued", "west", "ost", "blau", "weiss", "schwarz", "rot", "leitung",
    "line", "grid", "intra", "zonal", "cross", "gmbh", "senne", "awz", "nrw",
    "land", "trasse", "umspannwerk", "kraftwerk",
    # Franzoesische RTE-Namen: sonst matcht "liaison" (7 Zeichen) jede FR-Leitung
    "liaison", "ligne", "poste", "piquage", "circuit", "centrale", "reseau",
    "rte", "edf",
    # Stadtnamen: sonst landet jede Hamburg-Meldung auf jedem UW Hamburg/*
    "hamburg", "berlin", "bremen", "muenchen", "koln", "essen", "dortmund",
    "dresden", "leipzig", "frankfurt", "stuttgart", "hannover", "nurnberg",
    "duisburg", "dusseldorf", "bochum", "bielefeld", "bonn", "munster",
    "aachen", "kiel", "erfurt", "rostock", "mainz", "wiesbaden", "freiburg",
    "karlsruhe", "mannheim", "augsburg", "chemnitz", "krefeld", "potsdam",
}

_ZONAL_RE = re.compile(r"intra.?zonal|cross.?zonal", re.I)
_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_DIGITS_RE = re.compile(r"^\d+$")
_KV_RE = re.compile(r"\d+")
_EIC_LETTERS = re.compile(r"[A-Za-z]{3,}")
_EIC_CORE = re.compile(r"11WD\d*([A-Za-z]{3,})", re.I)
_ANON_EIC = re.compile(r"^11W0+$")
_PLANT_SOURCE = re.compile(r"coal|lignite|gas|nuclear|hydro|biomass|waste|oil|geothermal", re.I)
_HV_SUBSTATION = {"transmission", "transition", "converter", "compression"}
# Kuerzel aus deutschen Erzeugungs-EICs -> OSM-/Anlagenname
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
    "tief": ["tiefstack"],
    "kwkn": ["knapsack"],
    "lich": ["lichterfelde"],
    "berg": ["bergkamen"],
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
    "west": ["westfalen"],
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
    return [
        t
        for t in _SPLIT_RE.split(_fold(text))
        if len(t) >= 4 and t not in NAME_STOP and not _DIGITS_RE.match(t)
    ]


def _eic_compact(eic: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", eic or "").upper()


def _eic_tokens(eic: str, extra: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """Zieht sprechende Kuerzel aus 11WD7NEUR-B-KW-B -> neurath."""
    aliases = _EIC_ALIAS if extra is None else {**_EIC_ALIAS, **extra}
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
        extras = [raw, folded, *aliases.get(raw, []), *aliases.get(folded, [])]
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


def load_eic_plants(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """OPSD-Kraftwerksliste: EIC -> Name/Koordinaten, auch AT/LU mit DE-EIC."""
    src = path or EIC_PLANTS_PATH
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
                continue
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
                gkey = (round(lat, 3), round(lon, 3))
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
        plant = _lookup_plant(ev.get("asset_eic") or "", lookup)
        if not plant or not _in_map_bounds(plant["lon"], plant["lat"], country):
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
        for tok in _eic_tokens(ev.get("asset_eic") or "", extra_aliases):
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


def _dedupe_points(matched: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fasst OSM-Duplikate am selben Ort und identische Event-Sets zusammen."""
    lines: List[Dict[str, Any]] = []
    points: List[Dict[str, Any]] = []
    for feat in matched:
        if feat.get("geometry", {}).get("type") != "Point":
            lines.append(feat)
        else:
            points.append(feat)
    used = [False] * len(points)
    collapsed: List[Dict[str, Any]] = []
    for i, feat in enumerate(points):
        if used[i]:
            continue
        lon, lat = feat["geometry"]["coordinates"]
        name = _fold(feat.get("properties", {}).get("name") or "")
        evs = set(feat.get("properties", {}).get("ev") or [])
        group = [i]
        for j in range(i + 1, len(points)):
            if used[j]:
                continue
            other = points[j]
            olon, olat = other["geometry"]["coordinates"]
            oname = _fold(other.get("properties", {}).get("name") or "")
            oevs = set(other.get("properties", {}).get("ev") or [])
            near = abs(olon - lon) < 0.08 and abs(olat - lat) < 0.08
            same_name = bool(name) and name == oname
            same_evs = bool(evs) and evs == oevs
            if near and (same_name or same_evs):
                evs.update(oevs)
                group.append(j)
                used[j] = True
        best = min(group, key=lambda k: (_point_rank(points[k]["properties"].get("name") or ""), -len(points[k]["properties"].get("name") or "")))
        keep = points[best]
        keep["properties"]["ev"] = sorted(evs)
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
    """Schneidet eine Linie auf die Maske; Grenzuebertritte werden interpoliert."""
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
            # Sofort naechster Spiegel; nur nach einer vollen Spiegelrunde warten.
            pause = 0.2 if (attempt + 1) % len(OVERPASS_URLS) else PAUSE_SEC * (attempt + 1)
            time.sleep(pause)
    raise RuntimeError(f"Overpass fehlgeschlagen: {last_err}")


def _bbox(country: Country) -> Tuple[float, float, float, float]:
    bounds = MAP_BOUNDS.get(country.code)
    if not bounds:
        c = country.map_center
        return (c[1] - 4, c[0] - 6, c[1] + 4, c[0] + 6)
    (w, s), (e, n) = bounds
    return (s, w, n, e)


def _bbox_chunks(country: Country, n_split: int = 4) -> List[Tuple[float, float, float, float]]:
    s, w, n, e = _bbox(country)
    lat_step = (n - s) / n_split
    lon_step = (e - w) / n_split
    chunks = []
    for i in range(n_split):
        for j in range(n_split):
            chunks.append((s + i * lat_step, w + j * lon_step, s + (i + 1) * lat_step, w + (j + 1) * lon_step))
    return chunks


def _line_query(s: float, w: float, n: float, e: float) -> str:
    return f"""[out:json][timeout:90];
(
  way["power"="line"]["voltage"~"{HV_TAG_RE}"]({s},{w},{n},{e});
  way["power"="cable"]["voltage"~"{HV_TAG_RE}"]({s},{w},{n},{e});
);
out tags geom;"""


def _sub_query(s: float, w: float, n: float, e: float) -> str:
    return f"""[out:json][timeout:90];
(
  nwr["power"="substation"]["name"]["voltage"~"{HV_TAG_RE}"]({s},{w},{n},{e});
  nwr["power"="substation"]["name"]["substation"~"transmission|converter|transition"]({s},{w},{n},{e});
);
out tags center;"""


def _plant_query(s: float, w: float, n: float, e: float) -> str:
    return f"""[out:json][timeout:90];
(
  nwr["power"="plant"]["name"]["plant:source"]({s},{w},{n},{e});
  nwr["power"="plant"]["name"]["plant:output:electricity"]({s},{w},{n},{e});
);
out tags center;"""


def _round_coords(coords: List[List[float]], digits: int = COORD_DIGITS) -> List[List[float]]:
    return [[round(lon, digits), round(lat, digits)] for lon, lat in coords]


def _decimate(coords: List[List[float]], min_deg: float) -> List[List[float]]:
    """Laesst Punkte weg, die naeher als min_deg am Vorgaenger liegen."""
    if len(coords) <= 2:
        return coords
    out = [coords[0]]
    for pt in coords[1:-1]:
        if abs(pt[0] - out[-1][0]) >= min_deg or abs(pt[1] - out[-1][1]) >= min_deg:
            out.append(pt)
    out.append(coords[-1])
    return out


def _fetch_all(jobs: List[Tuple[Path, str]], allow_fetch: bool, workers: int = 6) -> List[Dict[str, Any]]:
    """Laedt alle (Cache-Pfad, Query)-Jobs parallel; gecachte werden uebersprungen."""
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
                    # Einzelner Chunk darf die Karte nicht blockieren
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


def load_osm(country: Country, allow_fetch: bool = True) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Gibt (lines, points) zurueck, bereits auf die Laendermaske geschnitten."""
    folder = RAW_DIR / country.code
    ring = (MAP_MASKS.get(country.code) or {}).get("coordinates", [[]])[0]

    chunks = _bbox_chunks(country)
    jobs: List[Tuple[Path, str]] = []
    for ci, (s, w, n, e) in enumerate(chunks):
        jobs.append((folder / f"osm_lines_{ci}.json", _line_query(s, w, n, e)))
    point_start = len(jobs)
    for ci, (s, w, n, e) in enumerate(chunks):
        jobs.append((folder / f"osm_points_{ci}_0.json", _sub_query(s, w, n, e)))
    bbox = _bbox(country)
    jobs.append((folder / "osm_plants.json", _plant_query(*bbox)))
    datasets = _fetch_all(jobs, allow_fetch)

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
            coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
            if ring:
                inside_n = sum(1 for lon, lat in coords if _point_in_ring(lon, lat, ring))
                # Nur ein Punkt in der Maske: typisch FR/CH-Leitung, die den Rhein streift
                if inside_n < 2:
                    continue
                runs = _clip_line_to_ring(coords, ring)
            else:
                runs = [coords] if len(coords) >= 2 else []
            if not runs:
                continue
            name = tags.get("name") or tags.get("name:de") or tags.get("ref") or ""
            for run in runs:
                lines.append({
                    "name": name,
                    "kv": int(round(kv)),
                    "hvdc": _is_hvdc(tags),
                    "coords": _round_coords(run),
                })

    points: List[Dict[str, Any]] = []
    seen_pts = set()
    elements: List[Dict[str, Any]] = []
    for data in datasets[point_start:]:
        elements.extend(data.get("elements", []))
    for el in elements:
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
            "lon": round(lon, COORD_DIGITS),
            "lat": round(lat, COORD_DIGITS),
        })
    return lines, points


def _event_index(
    events: List[Dict[str, Any]],
    extra_aliases: Optional[Dict[str, List[str]]] = None,
) -> Tuple[List[Tuple[int, List[str]]], Dict[str, List[int]]]:
    items: List[Tuple[int, List[str]]] = []
    token_index: Dict[str, List[int]] = {}
    for i, ev in enumerate(events):
        reason = ev.get("reason") or ""
        if reason not in ("Störung", "Geplante Wartung"):
            continue
        raw = f"{ev.get('asset_name') or ''} {ev.get('location') or ''}"
        eic_toks = _eic_tokens(ev.get("asset_eic") or "", extra_aliases)
        name_toks = _tokens(raw)
        if _ZONAL_RE.search(raw):
            # intra_zonal hat keine Ortsnamen; Koordinaten kommen aus der EIC-Kraftwerksliste.
            continue
        toks = _uniq_tokens(name_toks, eic_toks)
        if not toks:
            continue
        items.append((i, toks))
        for t in toks:
            if len(t) >= 5:
                token_index.setdefault(t, []).append(len(items) - 1)
    return items, token_index


def _match(name: str, items: List[Tuple[int, List[str]]], token_index: Dict[str, List[int]]) -> List[int]:
    ot = _tokens(name)
    if not ot:
        return []
    cand = set()
    for t in ot:
        cand.update(token_index.get(t, []))
    joined = " " + " ".join(ot) + " "
    hits: List[int] = []
    for it in cand:
        _, toks = items[it]
        n = 0
        strong = False
        for t in toks:
            if f" {t} " in joined:
                n += 1
                if len(t) >= 6:
                    strong = True
        if n >= 2 or (n == 1 and (strong or (len(ot) == 1 and len(ot[0]) >= 5))):
            hits.append(items[it][0])
    return hits


def build_network(country: Country, events: List[Dict[str, Any]], allow_fetch: bool = True) -> Dict[str, Any]:
    """Erzeugt {'background': FC, 'matched': FC}. matched-Features tragen die
    Indizes ihrer Meldungen in ``events`` unter properties.ev."""
    lines, points = load_osm(country, allow_fetch=allow_fetch)
    plants = load_eic_plants()
    extra_aliases = _aliases_from_plants(plants)
    items, token_index = _event_index(events, extra_aliases)

    background = []
    matched = []
    matched_events = set()
    for line in lines:
        if line["kv"] >= BG_MIN_KV or line["hvdc"]:
            background.append({
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": _round_coords(_decimate(line["coords"], BG_SIMPLIFY_DEG), BG_COORD_DIGITS),
                },
                "properties": {"v": line["kv"], "d": 1 if line["hvdc"] else 0, "n": line["name"]},
            })
        if not line["name"]:
            continue
        hits = _match(line["name"], items, token_index)
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
            "properties": {"name": line["name"], "ev": ev_idx},
        })
    for pt in points:
        hits = _match(pt["name"], items, token_index)
        if not hits:
            continue
        ev_idx = sorted(set(hits))
        matched_events.update(ev_idx)
        matched.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [pt["lon"], pt["lat"]]},
            "properties": {"name": pt["name"], "ev": ev_idx},
        })

    eic_pts = _attach_eic_plants(events, matched, matched_events, plants, country)
    osm_pts = _attach_unique_osm_plants(events, points, matched, matched_events, extra_aliases)
    matched = _dedupe_points(matched)
    n_all = sum(1 for e in events if e.get("reason") in ("Störung", "Geplante Wartung"))
    n_pts = sum(1 for f in matched if f.get("geometry", {}).get("type") == "Point")
    print(
        f"  Netz: {len(lines)} Leitungen, {len(points)} OSM-Stationen, {len(plants)} EIC-Kraftwerke | "
        f"gematcht: {len(matched)} Objekte ({n_pts} Punkte, {eic_pts} via EIC, {osm_pts} via OSM-EIC), "
        f"{len(matched_events)}/{n_all} Meldungen",
        flush=True,
    )
    return {
        "background": {"type": "FeatureCollection", "features": background},
        "matched": {"type": "FeatureCollection", "features": matched},
    }
