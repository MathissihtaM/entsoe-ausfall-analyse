"""Abruf der Unverfügbarkeitsmeldungen und Ablage der Rohantworten.

Die Transparency-API antwortet mit einem ZIP voller XML-Dokumente, höchstens
200 je Aufruf (``offset`` blättert weiter, Obergrenze 4800). Ein Zeitraum von
mehreren Jahren passt nicht in einen Aufruf: er wird in Fenster von etwa
350 Tagen geschnitten. Übertragungsnetz (A78) wird je Richtung zwischen
Gebotszone und Nachbar, jeder Regelzone und Nachbar sowie intern zwischen
den Regelzonen abgefragt. Erzeugung und Offshore (A77, A79, A80) je
Gebotszone und Regelzone.

Jede Antwort liegt unter ``data/cache/<Land>/`` als
``<Dokumentart>_<Zonen>_<von>_<bis>_off<offset>.zip``. Eine leere fachliche
Antwort („no matching data“) wird als ``.empty`` gemerkt, damit sie nicht
erneut geholt wird. Ist die letzte Seite noch voll und der Offset am Limit,
war das Fenster zu groß und wird halbiert.

``fetch_country`` liest danach alle ZIP-Dateien, behält je Dokument und Anlage
die höchste Revision und schreibt ``data/outages.json``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from queue import Queue
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.countries import MAP_BOUNDS, MAP_MASKS, Country
from src.osm_network import build_network
from src.parse_outages import keep_event, latest_revision, parse_zip

API_URL = "https://web-api.tp.entsoe.eu/api"
CHUNK_DAYS = 350
PAGE_SIZE = 200
MAX_OFFSET = 4800
PAUSE_SEC = 1.2
FETCH_WORKERS = 24
REQUEST_TIMEOUT = 180
MAX_RETRIES = 5
WINDOW_SLACK = timedelta(days=2)
MIN_SPLIT = timedelta(days=3)
_PERIOD_IN_NAME = re.compile(r"_(\d{12})_(\d{12})_off(\d+)")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "cache"
OUTAGES_JSON = DATA_DIR / "outages.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M")


def year_windows(years: int, *, now: Optional[datetime] = None) -> List[tuple[datetime, datetime]]:
    """Zeitfenster von heute rückwärts, jedes höchstens ``CHUNK_DAYS`` lang."""
    end = (now or _utc_now()).astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end = end + timedelta(days=1)
    start = end - timedelta(days=365 * years)
    windows: List[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + timedelta(days=CHUNK_DAYS), end)
        windows.append((cursor, window_end))
        cursor = window_end
    return windows


def parse_all_cached(country: Country, workers: int = FETCH_WORKERS) -> List[Dict[str, Any]]:
    folder = RAW_DIR / country.code
    if not folder.is_dir():
        return []
    paths = [p for p in folder.glob("*.zip") if p.stat().st_size >= 50]
    n = max(1, workers)
    print(f"{len(paths)} ZIP-Dateien lesen, {n} parallel", flush=True)

    def one(path: Path) -> List[Dict[str, Any]]:
        doc_type = path.name[:3] if path.name[:3] in {"A77", "A78", "A79", "A80"} else "A78"
        return parse_zip(path.read_bytes(), country=country, doc_type=doc_type)

    events: List[Dict[str, Any]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=n) as pool:
        for rows in pool.map(one, paths):
            events.extend(rows)
            done += 1
            if done % 500 == 0 or done == len(paths):
                print(f"  {done}/{len(paths)} ZIP", flush=True)
    return events


def _safe_name(*parts: str) -> str:
    raw = "_".join(parts)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw)


def _cache_file(country: str, stem: str, offset: int) -> Path:
    return RAW_DIR / country / f"{stem}_off{offset:04d}.zip"


def _empty_marker(path: Path) -> Path:
    return path.with_suffix(".empty")


def load_token() -> str:
    token = os.environ.get("ENTSOE_API_TOKEN", "").strip()
    if token:
        return token
    for candidate in (
        ROOT / ".env",
        ROOT / ".env.local",
        Path(r"C:\Users\mathi\Documents\Code\Energy_Monitor\web\.env.local"),
        Path(r"C:\Users\mathi\Documents\Code\Energy_Monitor\.env"),
    ):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() in {"ENTSOE_API_TOKEN", "NEXT_PUBLIC_ENTSOE_API_TOKEN"}:
                token = val.strip().strip('"').strip("'")
                if token:
                    return token
    gcloud = shutil.which("gcloud.cmd") or shutil.which("gcloud")
    if gcloud:
        try:
            token = subprocess.check_output(
                [
                    gcloud,
                    "secrets",
                    "versions",
                    "access",
                    "latest",
                    "--secret=ENTSOE_API_TOKEN",
                    "--project=entsomonitor",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if token:
                os.environ["ENTSOE_API_TOKEN"] = token
                return token
        except (FileNotFoundError, subprocess.CalledProcessError, OSError):
            pass
    raise RuntimeError(
        "ENTSOE_API_TOKEN fehlt. Token setzen oder in .env / Energy_Monitor web/.env.local hinterlegen."
    )


def _is_no_data(content: bytes) -> bool:
    low = content.lower()
    return b"no matching data" in low or b"nomatchingdata" in low


def _request(params: Dict[str, Any], token: str) -> Optional[bytes]:
    query = {**params, "securityToken": token}
    last_err: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(API_URL, params=query, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last_err = exc
            time.sleep(PAUSE_SEC * (attempt + 1))
            continue
        if resp.status_code == 429:
            time.sleep(PAUSE_SEC * (attempt + 2))
            continue
        if resp.status_code == 400 and _is_no_data(resp.content):
            return None
        if resp.status_code >= 400:
            if _is_no_data(resp.content):
                return None
            last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            time.sleep(PAUSE_SEC)
            continue
        if _is_no_data(resp.content):
            return None
        return resp.content
    if last_err:
        raise last_err
    return None


def _load_cached(path: Path) -> Optional[bytes]:
    if _empty_marker(path).is_file():
        return None
    if path.is_file() and path.stat().st_size > 2:
        return path.read_bytes()
    return b""


def _store(path: Path, data: Optional[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is None:
        _empty_marker(path).write_text("no-data\n", encoding="utf-8")
        return
    path.write_bytes(data)


def _payload(country: Country, years: int, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    unique = [ev for ev in latest_revision(events) if keep_event(ev, country)]
    unique.sort(key=lambda e: e.get("start") or "", reverse=True)
    payload = {
        "generated_at": _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "years": years,
        "country": country.code,
        "country_label": country.label,
        "map": {
            "center": list(country.map_center),
            "zoom": country.map_zoom,
            "mask": MAP_MASKS.get(country.code),
            "bounds": MAP_BOUNDS.get(country.code),
        },
        "tsos": [z.label for z in country.control_areas],
        "count": len(unique),
        "events": unique,
    }
    try:
        payload["network"] = build_network(country, unique)
    except Exception as exc:
        print(f"  Netzdaten uebersprungen: {exc}", flush=True)
        payload["network"] = None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTAGES_JSON.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _parse_dt12(text: str) -> datetime:
    return datetime.strptime(text, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


def _parse_period(name: str, prefix: str) -> Optional[tuple[datetime, datetime]]:
    match = _PERIOD_IN_NAME.search(name)
    if not match:
        return None
    if name[: match.start()] != prefix:
        return None
    try:
        return _parse_dt12(match.group(1)), _parse_dt12(match.group(2))
    except ValueError:
        return None


def _xml_count(data: bytes) -> int:
    if not data or data[:2] != b"PK":
        return 0
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            return sum(1 for info in zf.infolist() if info.filename.lower().endswith(".xml"))
    except zipfile.BadZipFile:
        return 0


def _matched_stems(country_code: str, prefix: str, start: datetime, end: datetime) -> List[str]:
    folder = RAW_DIR / country_code
    if not folder.is_dir():
        return []
    found: List[str] = []
    seen: set[str] = set()
    for path in folder.glob(f"{prefix}_*"):
        parsed = _parse_period(path.name, prefix)
        if not parsed:
            continue
        cached_start, cached_end = parsed
        if abs(cached_start - start) > WINDOW_SLACK or abs(cached_end - end) > WINDOW_SLACK:
            continue
        match = _PERIOD_IN_NAME.search(path.name)
        if not match:
            continue
        stem = f"{prefix}_{match.group(1)}_{match.group(2)}"
        if stem not in seen:
            seen.add(stem)
            found.append(stem)
    return found


def _period_from_stem(stem: str) -> Optional[Dict[str, str]]:
    match = _PERIOD_IN_NAME.search(stem + "_off0000.zip")
    if not match:
        return None
    return {"periodStart": match.group(1), "periodEnd": match.group(2)}


def _stem_progress(country_code: str, stem: str) -> str:
    """Zustand dieses Cache-Stamms: missing, empty, ok, capped oder incomplete.

    ``capped``: jede Seite hat noch ``PAGE_SIZE`` Dokumente und der Offset
    ist am Limit, das Fenster muss geteilt werden. ``incomplete``: eine
    Seite fehlt mitten in der Reihe.
    """
    offset = 0
    page_documents = 0
    saw_zip = False
    while offset <= MAX_OFFSET:
        cached = _load_cached(_cache_file(country_code, stem, offset))
        if cached == b"":
            return "incomplete" if saw_zip else "missing"
        if cached is None:
            return "ok" if saw_zip else "empty"
        saw_zip = True
        page_documents = _xml_count(cached)
        if page_documents < PAGE_SIZE:
            return "ok"
        offset += PAGE_SIZE
    return "capped" if page_documents >= PAGE_SIZE else "ok"


def _stem_capped(country_code: str, stem: str) -> bool:
    return _stem_progress(country_code, stem) == "capped"


def window_state(country_code: str, prefix: str, start: datetime, end: datetime) -> str:
    """Wie vollständig dieses eine Zeitfenster im Cache liegt.

    Rückgabe: missing, empty, ok, capped oder incomplete. Ein späteres,
    längeres Fenster zählt nicht als Ersatz.
    """
    stems = _matched_stems(country_code, prefix, start, end)
    if not stems:
        return "missing"
    states = [_stem_progress(country_code, stem) for stem in stems]
    if any(state == "capped" for state in states):
        return "capped"
    if any(state == "incomplete" for state in states):
        return "incomplete"
    if any(state == "ok" for state in states):
        return "ok"
    if all(state == "empty" for state in states):
        return "empty"
    return "missing"


def fetch_pages(
    *,
    token: str,
    country: Country,
    stem: str,
    params: Dict[str, Any],
    cache_only: bool,
) -> Tuple[List[bytes], bool]:
    """Seiten eines Fensters aus dem Cache oder von der API.

    Rückgabe: die ZIP-Inhalte und ob das Offset-Limit bei voller letzter
    Seite erreicht wurde. Die Dokumentart steckt schon im Dateinamen ``stem``.
    """
    zips: List[bytes] = []
    offset = 0
    page_documents = 0
    while offset <= MAX_OFFSET:
        path = _cache_file(country.code, stem, offset)
        cached = _load_cached(path)
        if cached == b"":
            if cache_only:
                break
            data = _request({**params, "offset": offset}, token)
            _store(path, data)
        else:
            data = cached
        if data is None:
            break
        zips.append(data)
        page_documents = _xml_count(data)
        if page_documents < PAGE_SIZE:
            return zips, False
        offset += PAGE_SIZE
    capped = bool(zips) and page_documents >= PAGE_SIZE and offset > MAX_OFFSET
    return zips, capped


def _expand_jobs(
    country: Country,
    doc_type: str,
    name_parts: Tuple[str, ...],
    base_params: Dict[str, Any],
    start: datetime,
    end: datetime,
) -> List[Tuple[str, Tuple[str, ...], Dict[str, Any], datetime, datetime]]:
    prefix = _safe_name(*name_parts)
    state = window_state(country.code, prefix, start, end)
    span = end - start
    if state in {"ok", "empty"}:
        return []
    if state == "capped" and span > MIN_SPLIT:
        mid = start + span / 2
        return _expand_jobs(country, doc_type, name_parts, base_params, start, mid) + _expand_jobs(
            country, doc_type, name_parts, base_params, mid, end
        )
    return [(doc_type, name_parts, base_params, start, end)]


def _download_window(
    *,
    token: str,
    country: Country,
    doc_type: str,
    name_parts: Tuple[str, ...],
    base_params: Dict[str, Any],
    start: datetime,
    end: datetime,
) -> List[Tuple[str, Tuple[str, ...], Dict[str, Any], datetime, datetime]]:
    period = {"periodStart": _fmt(start), "periodEnd": _fmt(end)}
    prefix = _safe_name(*name_parts)
    stem = _safe_name(*name_parts, period["periodStart"], period["periodEnd"])
    state = window_state(country.code, prefix, start, end)
    span = end - start
    if state in {"ok", "empty"}:
        return []
    if state == "capped" and span > MIN_SPLIT:
        return _expand_jobs(country, doc_type, name_parts, base_params, start, end)

    targets: List[Tuple[str, Dict[str, str]]] = []
    if state == "incomplete":
        for old_stem in _matched_stems(country.code, prefix, start, end):
            if _stem_progress(country.code, old_stem) == "incomplete":
                targets.append((old_stem, _period_from_stem(old_stem) or period))
    else:
        targets.append((stem, period))

    extra: List[Tuple[str, Tuple[str, ...], Dict[str, Any], datetime, datetime]] = []
    for use_stem, use_period in targets:
        try:
            _blobs, capped = fetch_pages(
                token=token,
                country=country,
                stem=use_stem,
                params={**base_params, **use_period},
                cache_only=False,
            )
        except Exception as exc:
            print(f"  Fehler {doc_type} {prefix}: {exc}", flush=True)
            continue
        if capped and span > MIN_SPLIT:
            print(
                f"  Pagination-Limit, teile Fenster {use_period['periodStart']}-{use_period['periodEnd']}",
                flush=True,
            )
            extra.extend(_expand_jobs(country, doc_type, name_parts, base_params, start, end))
        elif capped:
            print(
                f"  Warnung: Pagination-Limit im kurzen Fenster {use_period['periodStart']}-{use_period['periodEnd']}",
                flush=True,
            )
    return extra


def fetch_country(
    country: Country,
    *,
    years: int = 5,
    cache_only: bool = False,
    token: Optional[str] = None,
    workers: int = FETCH_WORKERS,
) -> Dict[str, Any]:
    """Meldungen eines Landes: fehlende Fenster laden, Cache lesen, JSON schreiben."""
    if cache_only:
        return _payload(country, years, parse_all_cached(country, workers))

    token = token or load_token()
    windows = year_windows(years)
    corridors = country.a78_corridors()
    zones = country.bidding_zones()
    bidding_docs = ("A77", "A79", "A80")

    jobs: List[Tuple[str, Tuple[str, ...], Dict[str, Any], datetime, datetime]] = []
    for start, end in windows:
        for src, dst in corridors:
            jobs.extend(
                _expand_jobs(
                    country,
                    "A78",
                    ("A78", src.code, dst.code),
                    {"documentType": "A78", "out_Domain": src.eic, "in_Domain": dst.eic},
                    start,
                    end,
                )
            )
        for doc_type in bidding_docs:
            for zone in zones:
                jobs.extend(
                    _expand_jobs(
                        country,
                        doc_type,
                        (doc_type, zone.code),
                        {"documentType": doc_type, "biddingZone_Domain": zone.eic},
                        start,
                        end,
                    )
                )

    n_workers = max(1, workers)
    print(f"{len(jobs)} Fenster nachladen, {n_workers} parallel", flush=True)
    if jobs:
        pending: Queue = Queue()
        for job in jobs:
            pending.put(job)
        done = 0
        total_lock = threading.Lock()

        def worker() -> None:
            nonlocal done
            while True:
                job = pending.get()
                try:
                    if job is None:
                        return
                    doc_type, name_parts, base_params, start, end = job
                    prefix = _safe_name(*name_parts)
                    label = f"{doc_type} {prefix} {_fmt(start)}-{_fmt(end)}"
                    extra = _download_window(
                        token=token or "",
                        country=country,
                        doc_type=doc_type,
                        name_parts=name_parts,
                        base_params=base_params,
                        start=start,
                        end=end,
                    )
                    with total_lock:
                        done += 1
                        print(f"[{done}] {label}", flush=True)
                    for item in extra:
                        pending.put(item)
                except Exception as exc:
                    print(f"  Fehler Job: {exc}", flush=True)
                finally:
                    pending.task_done()

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = [pool.submit(worker) for _ in range(n_workers)]
            pending.join()
            for _ in range(n_workers):
                pending.put(None)
            for fut in futures:
                fut.result()

    print("Cache einlesen …", flush=True)
    return _payload(country, years, parse_all_cached(country, workers))
