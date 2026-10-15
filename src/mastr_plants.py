"""Marktstammdatenregister: Kraftwerke ab 10 MW mit W-EIC.

Die öffentliche Listen-API liefert Stammdaten, aber kein Weic. Der W-EIC
steht in der öffentlichen Detailseite und im Gesamtdatenexport-XML
(``<Weic>``). Fehlt er in der Liste, werden die Detailseiten gelesen;
sonst die Einheiten-XMLs.

Koordinaten großer Einheiten sind im Portal inzwischen gesperrt
(„Verschleierung", § 15 Abs. 1 Nr. 1c MaStRV). Bis Anfang 2025 waren sie
öffentlich; der open-MaStR-Snapshot auf Zenodo (Stand 2025-02-09,
Datenlizenz Deutschland – Namensnennung – 2.0, Quelle: Bundesnetzagentur /
OpenEnergyPlatform) liefert sie per MaStR-Nummer oder W-EIC nach.

Die verdichtete Lookup-CSV steht im Schema der OPSD-Liste unter
``data/ref/mastr_plants.csv``. Rohantworten liegen in
``data/cache/DE/mastr/``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree.ElementTree import Element

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache" / "DE" / "mastr"
REF_CSV = ROOT / "data" / "ref" / "mastr_plants.csv"
API_URL = (
    "https://www.marktstammdatenregister.de/MaStR/Einheit/"
    "EinheitJson/GetErweiterteOeffentlicheEinheitStromerzeugung"
)
DETAIL_URL = (
    "https://www.marktstammdatenregister.de/MaStR/Einheit/Detail/IndexOeffentlich/{id}"
)
MIN_KW = 10_000.0
PAGE_SIZE = 5000
DETAIL_WORKERS = 4
DETAIL_PAUSE_SEC = 0.15
_WEIC_RE = re.compile(
    r'title="W-EIC-Code der Stromerzeugungseinheit">\s*(11[A-Za-z0-9-]{10,})\s*<',
    re.I,
)
_WEIC_CACHE = CACHE_DIR / "weic_by_id.json"
# open-MaStR-Snapshot (unboxed), Stand 2025-02-09 — letzte öffentliche
# Version mit Koordinaten für Großanlagen. Solar entfällt (722 MB,
# Kleinstanlagen; große Solarparks sind nicht verschleiert).
ZENODO_RECORD = "14843222"
ZENODO_FILE_URL = f"https://zenodo.org/api/records/{ZENODO_RECORD}/files/{{key}}/content"
ZENODO_TABLES = (
    "bnetza_mastr_combustion_raw.csv.zip",
    "bnetza_mastr_wind_raw.csv.zip",
    "bnetza_mastr_biomass_raw.csv.zip",
    "bnetza_mastr_hydro_raw.csv.zip",
    "bnetza_mastr_nuclear_raw.csv.zip",
    "bnetza_mastr_gsgk_raw.csv.zip",
    "bnetza_mastr_storage_raw.csv.zip",
)
GCS_BUCKETS = (
    "entsomonitor.firebasestorage.app",
    "entsomonitor-energy-map-euw3",
    "entsomonitor",
)
GCS_PREFIXES = ("data/mastr/xml/", "mastr/xml/", "mastr/")
LOCAL_MASTR_DIRS = (
    ROOT / "data" / "mastr" / "xml",
    Path(r"C:\Users\mathi\Documents\Code\Energy_Monitor\mastr"),
    Path(r"C:\Users\mathi\Documents\Code\Energy_Monitor\data\mastr"),
    Path(r"C:\Users\mathi\Documents\Code\Energy_Monitor\energymonitor\mastr"),
)
XML_KINDS = ("Verbrennung", "Wasser", "Kernkraft", "Wind", "StromSpeicher")
_XML_NAME = re.compile(
    r"Einheiten(Verbrennung|Wasser|Kernkraft|Wind|StromSpeicher)(?:_\d+)?\.xml$",
    re.I,
)
CSV_FIELDS = [
    "name_bnetza", "name_uba", "city", "lat", "lon",
    "eic_code_plant", "eic_code_block",
    "state", "country", "capacity_net_bnetza", "energy_source", "network_operator",
]
REQUEST_TIMEOUT = 90
_USER_AGENT = "entsoe-ausfall-analyse/1.0"
_ANON_EIC = re.compile(r"^11W0+$")


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _text(el: Optional[Element], *names: str) -> str:
    if el is None:
        return ""
    want = {n.lower() for n in names}
    for child in list(el):
        if _local(child.tag).lower() in want and child.text:
            return child.text.strip()
    return ""


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _compact_eic(eic: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", eic or "").upper()


def _valid_eic(eic: str) -> bool:
    key = _compact_eic(eic)
    return len(key) >= 10 and not key.startswith("11W00000") and not _ANON_EIC.match(key)


def _kw_to_mw(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(value / 1000.0, 6) if value >= 100 else round(value, 6)


def row_has_weic(row: Dict[str, Any]) -> bool:
    for key, val in row.items():
        if "weic" in str(key).lower() and val not in (None, "", []):
            return True
    return False


def _weic_from_row(row: Dict[str, Any]) -> str:
    for key, val in row.items():
        if "weic" in str(key).lower() and val not in (None, ""):
            return str(val).strip()
    return ""


def api_filter(min_kw: float = MIN_KW) -> str:
    """Öffentliche API filtert über den Anzeigenamen; Wert in kW."""
    return f"Bruttoleistung der Einheit~gt~'{int(min_kw)}'"


def fetch_api_page(
    page: int,
    page_size: int = PAGE_SIZE,
    filt: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    http = session or requests.Session()
    resp = http.get(
        API_URL,
        params={
            "page": page,
            "pageSize": page_size,
            "filter": filt or api_filter(),
            "sort": "",
            "group": "",
        },
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()


def fetch_api(
    min_kw: float = MIN_KW,
    page_size: int = PAGE_SIZE,
    session: Optional[requests.Session] = None,
    max_pages: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Seiten der öffentlichen API. Zweites Ergebnis: ob Weic vorkommt."""
    rows: List[Dict[str, Any]] = []
    has_weic = False
    page = 1
    while True:
        payload = fetch_api_page(page, page_size, api_filter(min_kw), session=session)
        batch = payload.get("Data") or []
        if not isinstance(batch, list):
            break
        for item in batch:
            if isinstance(item, dict):
                rows.append(item)
                if row_has_weic(item):
                    has_weic = True
        if page == 1 and rows and not has_weic:
            print("  MaStR-API: kein Weic in der Liste, Detailseiten folgen", flush=True)
        total = payload.get("Total")
        if not batch:
            break
        if isinstance(total, (int, float)) and len(rows) >= int(total):
            break
        if max_pages is not None and page >= max_pages:
            break
        if len(batch) < page_size:
            break
        page += 1
    return rows, has_weic


def parse_weic_html(html: str) -> str:
    """W-EIC aus der öffentlichen Detailseite, sonst leer."""
    match = _WEIC_RE.search(html or "")
    if not match:
        return ""
    return match.group(1).strip()


def _load_weic_cache(path: Path = _WEIC_CACHE) -> Dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save_weic_cache(cache: Dict[str, str], path: Path = _WEIC_CACHE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")


def fetch_detail_weic(unit_id: Any, session: Optional[requests.Session] = None) -> str:
    http = session or requests.Session()
    url = DETAIL_URL.format(id=unit_id)
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = http.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
            )
            if resp.status_code in (429, 503):
                time.sleep(2.0 * (attempt + 1))
                continue
            resp.raise_for_status()
            return parse_weic_html(resp.text)
        except (requests.RequestException, RuntimeError) as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    if last_exc:
        raise last_exc
    return ""


def attach_weic_from_details(
    rows: Sequence[Dict[str, Any]],
    session: Optional[requests.Session] = None,
    cache_path: Path = _WEIC_CACHE,
    workers: int = DETAIL_WORKERS,
) -> int:
    """Ergänzt fehlendes Weic aus der öffentlichen Detailseite. Rückgabe: neue Treffer."""
    cache = _load_weic_cache(cache_path)
    missing: List[Any] = []
    for row in rows:
        uid = row.get("Id")
        if uid is None:
            continue
        cached = cache.get(str(uid))
        if cached:
            row["Weic"] = cached
            continue
        if str(uid) in cache:
            continue
        missing.append(uid)
    if not missing:
        return 0
    print(f"  MaStR: {len(missing)} Detailseiten für W-EIC", flush=True)
    found = 0

    def one(uid: Any) -> Tuple[str, str]:
        time.sleep(DETAIL_PAUSE_SEC)
        try:
            return str(uid), fetch_detail_weic(uid, session=None)
        except (requests.RequestException, RuntimeError):
            return str(uid), ""

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(one, uid) for uid in missing]
        done = 0
        for fut in as_completed(futures):
            uid, weic = fut.result()
            cache[uid] = weic
            done += 1
            if weic:
                found += 1
            if done % 100 == 0 or done == len(missing):
                _save_weic_cache(cache, cache_path)
                print(f"  MaStR-Detail {done}/{len(missing)}, W-EIC {found}", flush=True)
    _save_weic_cache(cache, cache_path)
    for row in rows:
        uid = row.get("Id")
        if uid is None:
            continue
        weic = cache.get(str(uid))
        if weic:
            row["Weic"] = weic
    return found


def download_zenodo_table(
    file_key: str,
    cache_dir: Path = CACHE_DIR,
    session: Optional[requests.Session] = None,
) -> Optional[Path]:
    """Eine open-MaStR-Tabelle von Zenodo laden, als CSV im Cache ablegen."""
    dest_dir = cache_dir / "zenodo"
    dest = dest_dir / file_key[:-4] if file_key.endswith(".zip") else dest_dir / file_key
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    dest_dir.mkdir(parents=True, exist_ok=True)
    http = session or requests.Session()
    resp = http.get(ZENODO_FILE_URL.format(key=file_key), timeout=900)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            return None
        with zf.open(names[0]) as src, dest.open("wb") as out:
            shutil.copyfileobj(src, out)
    return dest


def zenodo_coord_index(
    cache_dir: Path = CACHE_DIR,
    session: Optional[requests.Session] = None,
    tables: Sequence[str] = ZENODO_TABLES,
) -> Dict[str, Dict[str, float]]:
    """MaStR-Nummer und W-EIC auf Koordinaten, Snapshot 2025-02 (nur ab 10 MW)."""
    index: Dict[str, Dict[str, float]] = {}
    for key in tables:
        try:
            path = download_zenodo_table(key, cache_dir, session=session)
        except (requests.RequestException, zipfile.BadZipFile, OSError) as exc:
            print(f"  Zenodo {key}: {exc}", flush=True)
            continue
        if not path:
            continue
        n = 0
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                brutto = _to_float(row.get("Bruttoleistung"))
                if brutto is None or brutto < MIN_KW:
                    continue
                lat = _to_float(row.get("Breitengrad"))
                lon = _to_float(row.get("Laengengrad"))
                if lat is None or lon is None:
                    continue
                rec = {"lat": lat, "lon": lon}
                snr = (row.get("EinheitMastrNummer") or "").strip()
                if snr:
                    index.setdefault(snr, rec)
                weic = (row.get("Weic") or "").strip()
                if _valid_eic(weic):
                    index.setdefault(weic, rec)
                n += 1
        print(f"  Zenodo {path.name}: {n} Einheiten ab 10 MW mit Koordinaten", flush=True)
    return index


def attach_coords_from_snapshot(
    rows: Sequence[Dict[str, Any]],
    index: Dict[str, Dict[str, float]],
) -> int:
    """Fehlende Koordinaten aus dem Zenodo-Snapshot ergänzen. Rückgabe: Treffer."""
    found = 0
    for row in rows:
        if _to_float(row.get("Breitengrad")) is not None and _to_float(row.get("Laengengrad")) is not None:
            continue
        rec = index.get((row.get("MaStRNummer") or "").strip())
        if rec is None:
            weic = _weic_from_row(row)
            if weic:
                rec = index.get(weic)
        if rec is None:
            continue
        row["Breitengrad"] = rec["lat"]
        row["Laengengrad"] = rec["lon"]
        found += 1
    return found


def plants_from_api(rows: Sequence[Dict[str, Any]], min_kw: float = MIN_KW) -> List[Dict[str, Any]]:
    plants: List[Dict[str, Any]] = []
    for row in rows:
        eic = _weic_from_row(row)
        if not _valid_eic(eic):
            continue
        brutto = _to_float(row.get("Bruttoleistung") or row.get("Nettonennleistung"))
        if brutto is not None and brutto < min_kw and brutto < min_kw / 1000.0:
            continue
        if brutto is not None and brutto < 100:
            mw = brutto
        else:
            mw = _kw_to_mw(brutto)
        lat = _to_float(row.get("Breitengrad"))
        lon = _to_float(row.get("Laengengrad"))
        name = (
            (row.get("KraftwerkName") or "").strip()
            or (row.get("EinheitName") or "").strip()
            or (row.get("WindparkName") or "").strip()
            or "Kraftwerk"
        )
        plants.append({
            "name_bnetza": name,
            "name_uba": (row.get("KraftwerkBlockName") or "").strip(),
            "city": (row.get("Ort") or row.get("Gemeinde") or "").strip(),
            "lat": lat if lat is not None else "",
            "lon": lon if lon is not None else "",
            "eic_code_plant": eic,
            "eic_code_block": "",
            "state": (row.get("Bundesland") or "").strip(),
            "country": "DE",
            "capacity_net_bnetza": mw if mw is not None else "",
            "energy_source": (row.get("EnergietraegerName") or "").strip(),
            "network_operator": (row.get("NetzbetreiberNamen") or "").strip(),
        })
    return plants


def parse_mastr_xml(path: Path, min_kw: float = MIN_KW) -> List[Dict[str, Any]]:
    """Eine Einheiten-XML des Gesamtdatenexports (mit Weic)."""
    tree = ET.parse(path)
    root = tree.getroot()
    plants: List[Dict[str, Any]] = []
    for el in root.iter():
        weic = _text(el, "Weic", "WEIC", "weic")
        if not _valid_eic(weic):
            continue
        brutto = _to_float(_text(el, "Bruttoleistung", "Nettonennleistung"))
        if brutto is None:
            continue
        if brutto < min_kw and not (1 <= brutto < 100 and brutto * 1000 >= min_kw):
            if brutto < 100:
                continue
            if brutto < min_kw:
                continue
        if brutto >= 100:
            mw = _kw_to_mw(brutto)
        else:
            mw = brutto
        lat = _to_float(_text(el, "Breitengrad"))
        lon = _to_float(_text(el, "Laengengrad"))
        if lat is None or lon is None:
            continue
        name = (
            _text(el, "NameStromerzeugungseinheit", "EinheitName", "Name")
            or "Kraftwerk"
        )
        plants.append({
            "name_bnetza": name,
            "name_uba": _text(el, "NameKraftwerk", "KraftwerkName"),
            "city": _text(el, "Ort", "Gemeinde"),
            "lat": lat,
            "lon": lon,
            "eic_code_plant": weic,
            "eic_code_block": "",
            "state": _text(el, "Bundesland"),
            "country": "DE",
            "capacity_net_bnetza": mw if mw is not None else "",
            "energy_source": _text(el, "Energietraeger", "EnergietraegerName"),
            "network_operator": _text(el, "NameAktenzeichenNetzbetreiber", "NetzbetreiberNamen"),
        })
    return plants


def _gcloud() -> Optional[str]:
    from shutil import which
    return which("gcloud.cmd") or which("gcloud") or which("gsutil")


def _gcs_ls(prefix: str, bucket: str) -> List[str]:
    tool = _gcloud()
    if not tool:
        return []
    uri = f"gs://{bucket}/{prefix}"
    try:
        if tool.endswith("gsutil") or tool.endswith("gsutil.cmd"):
            cmd = [tool, "ls", uri]
        else:
            cmd = [tool, "storage", "ls", uri]
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _gcs_cp(uri: str, dest: Path) -> bool:
    tool = _gcloud()
    if not tool:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if tool.endswith("gsutil") or tool.endswith("gsutil.cmd"):
            cmd = [tool, "cp", uri, str(dest)]
        else:
            cmd = [tool, "storage", "cp", uri, str(dest)]
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return dest.is_file() and dest.stat().st_size > 0
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        return False


def _gcs_via_client(prefix: str, dest_dir: Path, bucket_name: str) -> List[Path]:
    try:
        from google.cloud import storage  # type: ignore
    except ImportError:
        return []
    try:
        client = storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or "entsomonitor")
        bucket = client.bucket(bucket_name)
        found: List[Path] = []
        for blob in client.list_blobs(bucket, prefix=prefix):
            name = blob.name.replace("\\", "/")
            if not _XML_NAME.search(name.split("/")[-1]):
                continue
            dest = dest_dir / Path(name).name
            if not dest.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                blob.download_to_filename(str(dest))
            if dest.is_file():
                found.append(dest)
        return found
    except Exception as exc:
        print(f"  MaStR GCS {bucket_name}/{prefix}: {exc}", flush=True)
        return []


def discover_xml_uris() -> List[str]:
    uris: List[str] = []
    for bucket in GCS_BUCKETS:
        for prefix in GCS_PREFIXES:
            listing = _gcs_ls(prefix, bucket)
            if not listing:
                continue
            folders = [u for u in listing if u.endswith("/")]
            files = [u for u in listing if _XML_NAME.search(u.split("/")[-1])]
            if files:
                uris.extend(files)
                return sorted(set(uris))
            if folders:
                latest = sorted(folders)[-1]
                nested_prefix = latest.replace(f"gs://{bucket}/", "")
                nested = _gcs_ls(nested_prefix, bucket)
                uris.extend(u for u in nested if _XML_NAME.search(u.split("/")[-1]))
            if uris:
                return sorted(set(uris))
    return sorted(set(uris))


def _local_xmls() -> List[Path]:
    found: List[Path] = []
    for folder in LOCAL_MASTR_DIRS:
        if not folder.exists():
            continue
        for path in folder.rglob("*.xml"):
            if _XML_NAME.search(path.name):
                found.append(path)
    return sorted(found)


def download_mastr_xmls(cache_dir: Path = CACHE_DIR) -> List[Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    local = [p for p in cache_dir.glob("*.xml") if _XML_NAME.search(p.name)]
    if local:
        return sorted(local)
    nearby = _local_xmls()
    if nearby:
        return nearby
    found: List[Path] = []
    for uri in discover_xml_uris():
        dest = cache_dir / uri.rstrip("/").split("/")[-1]
        if dest.is_file() or _gcs_cp(uri, dest):
            found.append(dest)
    if found:
        return sorted(found)
    return []


def plants_from_xmls(paths: Sequence[Path], min_kw: float = MIN_KW) -> List[Dict[str, Any]]:
    plants: List[Dict[str, Any]] = []
    for path in paths:
        plants.extend(parse_mastr_xml(path, min_kw=min_kw))
    return _dedupe_plants(plants)


def _dedupe_plants(plants: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_eic: Dict[str, Dict[str, Any]] = {}
    for plant in plants:
        key = _compact_eic(plant.get("eic_code_plant") or plant.get("eic_code_block") or "")
        if not key:
            continue
        prev = by_eic.get(key)
        if prev is None or len(str(plant.get("name_bnetza") or "")) > len(str(prev.get("name_bnetza") or "")):
            by_eic[key] = plant
    return list(by_eic.values())


def write_mastr_csv(rows: Sequence[Dict[str, Any]], path: Path = REF_CSV) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})
    return path


def load_mastr_plants(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    src = path or REF_CSV
    if not src.is_file():
        return []
    with src.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def refresh_mastr_ref(
    dest: Path = REF_CSV,
    cache_dir: Path = CACHE_DIR,
    session: Optional[requests.Session] = None,
    min_kw: float = MIN_KW,
) -> Path:
    plants: List[Dict[str, Any]] = []
    source = "keine"
    try:
        rows, has_weic = fetch_api(min_kw=min_kw, session=session)
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        print(f"  MaStR-API nicht nutzbar: {exc}", flush=True)
        rows, has_weic = [], False
    if rows and not has_weic:
        attach_weic_from_details(rows, session=session, cache_path=cache_dir / "weic_by_id.json")
        has_weic = any(row_has_weic(row) for row in rows)
    if rows:
        index = zenodo_coord_index(cache_dir, session=session)
        n_geo = attach_coords_from_snapshot(rows, index)
        print(f"  MaStR: {n_geo} Koordinaten aus dem Snapshot 2025-02 ergänzt", flush=True)
    if has_weic:
        plants = plants_from_api(rows, min_kw=min_kw)
        source = "API+Detail"
    if not plants:
        try:
            xmls = download_mastr_xmls(cache_dir)
        except Exception as exc:
            print(f"  MaStR-XML nicht geladen: {exc}", flush=True)
            xmls = []
        plants = plants_from_xmls(xmls, min_kw=min_kw)
        source = "XML" if xmls else source
    path = write_mastr_csv(plants, dest)
    print(f"  MaStR ({source}): {len(plants)} Anlagen mit W-EIC -> {path}", flush=True)
    return path
