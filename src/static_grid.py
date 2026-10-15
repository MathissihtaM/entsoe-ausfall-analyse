"""JAO Core Static Grid Model: laden, parsen, Lookup-CSV schreiben.

Das SGM listet Netzelemente der Core-ÜNB mit EIC, Stationen, Spannung
und Nennstrom. Für deutsche A78/A79-Meldungen gibt das eine Nennleistung
(√3·U·Imax; bei Trafos Snom, sonst dieselbe Formel) und die Stationen
für die Geometrie.

Roh-ZIP und XLSX liegen unter ``data/cache/DE/sgm/`` (gitignored).
Die verdichtete Lookup-CSV steht in ``data/ref/grid_elements_de.csv``.
"""

from __future__ import annotations

import csv
import math
import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

import requests
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache" / "DE" / "sgm"
REF_CSV = ROOT / "data" / "ref" / "grid_elements_de.csv"
SGM_PAGE = "https://www.jao.eu/static-grid-model"
CSV_FIELDS = ["eic", "name", "tso", "type", "sub_from", "sub_to", "kv", "nominal_mw"]
DE_TSO_KEYS = ("50hertz", "amprion", "tennet", "transnetbw", "ttg")
TSO_LABEL = {
    "50hertz": "50Hertz",
    "amprion": "Amprion",
    "tennet": "TenneT",
    "ttg": "TenneT",
    "transnetbw": "TransnetBW",
}
SHEET_TYPE = {
    "line": "Line",
    "lines": "Line",
    "tieline": "Tieline",
    "tielines": "Tieline",
    "tie-line": "Tieline",
    "tie-lines": "Tieline",
    "transformer": "Transformer",
    "transformers": "Transformer",
    "pst": "PST",
    "psts": "PST",
    "dc": "DC-Link",
    "dc-link": "DC-Link",
    "dc-links": "DC-Link",
    "dclink": "DC-Link",
    "dclinks": "DC-Link",
}
_HEADER_ALIASES = {
    "eic": ("eic", "eiccode", "eic_code"),
    "name": ("nename", "ne_name", "name", "elementname", "element"),
    "tso": ("tso",),
    "sub_from": ("substation1", "substation_1", "substationfrom", "substation_from", "from"),
    "sub_to": ("substation2", "substation_2", "substationto", "substation_to", "to"),
    "location": ("location", "fullname", "full_name"),
    "kv": ("voltagelevelkv", "voltage_levelkv", "voltagelevel", "voltage", "u", "vnkv"),
    "imax": ("imax", "fixed", "maximumcurrentimaxa", "maximumcurrent"),
    "snom": ("snom", "snmva", "sn_mva", "s_n", "rated"),
    "type": ("elementtype", "element_type", "type"),
}
_KEEP_TYPES = {"Line", "Tieline", "Transformer", "PST", "DC-Link"}
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ZIP_NAME = re.compile(r"20\d{4}_.*static.?grid.?model.*\.zip", re.I)
REQUEST_TIMEOUT = 90
_USER_AGENT = "entsoe-ausfall-analyse/1.0"


def compact_eic(eic: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", eic or "").upper()


def nominal_mw(kv: Optional[float], imax_a: Optional[float], snom: Optional[float] = None) -> Optional[float]:
    """Nennleistung in MW: Snom wenn vorhanden, sonst √3·U[kV]·I[A]/1000."""
    if snom is not None and snom > 0:
        return round(float(snom), 3)
    if kv is None or imax_a is None or kv <= 0 or imax_a <= 0:
        return None
    return round(math.sqrt(3.0) * float(kv) * float(imax_a) / 1000.0, 3)


def _norm_header(text: Any) -> str:
    s = str(text or "").strip().lower()
    s = s.replace("µ", "u").replace("μ", "u")
    return _NON_ALNUM.sub("", s)


def _tso_label(raw: str) -> str:
    folded = _NON_ALNUM.sub("", (raw or "").lower())
    for key, label in TSO_LABEL.items():
        if key in folded:
            return label
    return (raw or "").strip()


def _is_de_tso(raw: str) -> bool:
    folded = _NON_ALNUM.sub("", (raw or "").lower())
    return any(key in folded for key in DE_TSO_KEYS)


def _sheet_type(title: str, element_type: str = "") -> str:
    raw = _NON_ALNUM.sub("", (element_type or title or "").lower())
    for key, label in SHEET_TYPE.items():
        if _NON_ALNUM.sub("", key) == raw or raw.startswith(_NON_ALNUM.sub("", key)):
            return label
    if "tieline" in raw or "tielin" in raw:
        return "Tieline"
    if "transform" in raw:
        return "Transformer"
    if "pst" in raw or "phaseshift" in raw:
        return "PST"
    if raw.startswith("dc") or "hvdc" in raw:
        return "DC-Link"
    if "line" in raw:
        return "Line"
    return ""


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    text = str(value).strip().replace(",", ".").replace(" ", "")
    if not text or text in {".", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _row_has_eic(headers: Sequence[str]) -> bool:
    return any(_norm_header(c) in {"eic", "eiccode"} for c in headers)


def _header_rows(ws: Worksheet) -> Tuple[List[str], int]:
    """Eine oder zwei Kopfzeilen zu flachen Namen, plus erste Datenzeile.

    Das echte SGM hat die Feldnamen in Zeile 2 (EIC_Code, Fixed, …) und
    Gruppenüberschriften in Zeile 1. Unsere Test-Fixtures haben die
    Feldnamen schon in Zeile 1.
    """
    row1 = [_cell_text(c) for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())]
    row2 = [_cell_text(c) for c in next(ws.iter_rows(min_row=2, max_row=2, values_only=True), ())]
    if _row_has_eic(row2) and not _row_has_eic(row1):
        width = max(len(row1), len(row2))
        headers = []
        for i in range(width):
            a = row1[i] if i < len(row1) else ""
            b = row2[i] if i < len(row2) else ""
            headers.append(" ".join(p for p in (a, b) if p))
        return headers, 3
    return row1, 2


def _imax_candidates(norms: Sequence[str]) -> List[int]:
    scored: List[Tuple[int, int]] = []
    for i, norm in enumerate(norms):
        if not norm or "period" in norm or "secondary" in norm or "dlrmin" in norm:
            continue
        if norm == "fixed" or ("fixed" in norm and "imax" in norm):
            scored.append((0, i))
        elif "imax" in norm and "max" in norm:
            scored.append((1, i))
        elif norm == "max":
            scored.append((2, i))
        elif "imax" in norm:
            scored.append((3, i))
        elif "dlrmax" in norm:
            scored.append((4, i))
    scored.sort()
    return [i for _rank, i in scored]


def _map_columns(headers: Sequence[str]) -> Dict[str, int]:
    mapped: Dict[str, int] = {}
    used = set()
    norms = [_norm_header(h) for h in headers]
    imax_cols = _imax_candidates(norms)
    if imax_cols:
        mapped["imax"] = imax_cols[0]
        used.add(imax_cols[0])
        if len(imax_cols) > 1:
            mapped["imax_alt"] = imax_cols[1]
    for field, aliases in _HEADER_ALIASES.items():
        if field == "imax":
            continue
        for i, norm in enumerate(norms):
            if i in used or not norm:
                continue
            if field == "kv" and "secondary" in norm:
                continue
            if any(alias == norm or alias in norm or norm in alias for alias in aliases):
                mapped[field] = i
                used.add(i)
                break
    return mapped


def _pick(row: Sequence[Any], cols: Dict[str, int], key: str) -> str:
    idx = cols.get(key)
    if idx is None or idx >= len(row):
        return ""
    return _cell_text(row[idx])


def _pick_float(row: Sequence[Any], cols: Dict[str, int], key: str) -> Optional[float]:
    idx = cols.get(key)
    if idx is None or idx >= len(row):
        return None
    return _to_float(row[idx])


def parse_sheet(ws: Worksheet) -> List[Dict[str, Any]]:
    headers, start = _header_rows(ws)
    cols = _map_columns(headers)
    if "eic" not in cols:
        return []
    default_type = _sheet_type(ws.title)
    out: List[Dict[str, Any]] = []
    for raw in ws.iter_rows(min_row=start, values_only=True):
        eic = _pick(raw, cols, "eic")
        if not compact_eic(eic):
            continue
        kind = _sheet_type(ws.title, _pick(raw, cols, "type")) or default_type
        if kind not in _KEEP_TYPES:
            continue
        tso_raw = _pick(raw, cols, "tso")
        if not _is_de_tso(tso_raw):
            continue
        sub_from = _pick(raw, cols, "sub_from") or _pick(raw, cols, "location")
        sub_to = _pick(raw, cols, "sub_to")
        if kind in {"Transformer", "PST"} and not sub_to:
            sub_to = sub_from
        kv = _pick_float(raw, cols, "kv")
        imax = _pick_float(raw, cols, "imax") or _pick_float(raw, cols, "imax_alt")
        snom = _pick_float(raw, cols, "snom")
        mw = nominal_mw(kv, imax, snom)
        out.append({
            "eic": eic.strip(),
            "key": compact_eic(eic),
            "name": _pick(raw, cols, "name") or f"{sub_from} - {sub_to}".strip(" -"),
            "tso": _tso_label(tso_raw),
            "type": kind,
            "sub_from": sub_from,
            "sub_to": sub_to,
            "kv": kv,
            "imax": imax,
            "snom": snom,
            "nominal_mw": mw,
        })
    return out


def parse_workbook(path: Path) -> List[Dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        rows: List[Dict[str, Any]] = []
        for title in wb.sheetnames:
            rows.extend(parse_sheet(wb[title]))
        return rows
    finally:
        wb.close()


def aggregate_by_eic(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Gleiche EIC (Doppelsysteme) auf eine Zeile: Imax-Summe, Nennleistung neu."""
    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = row.get("key") or compact_eic(row.get("eic") or "")
        if not key:
            continue
        rec = groups.get(key)
        if rec is None:
            groups[key] = {
                "eic": row.get("eic") or "",
                "key": key,
                "name": row.get("name") or "",
                "tso": row.get("tso") or "",
                "type": row.get("type") or "",
                "sub_from": row.get("sub_from") or "",
                "sub_to": row.get("sub_to") or "",
                "kv": row.get("kv"),
                "imax": row.get("imax") or 0.0,
                "snom": row.get("snom") or 0.0,
            }
            continue
        if row.get("imax"):
            rec["imax"] = (rec.get("imax") or 0.0) + float(row["imax"])
        if row.get("snom"):
            rec["snom"] = (rec.get("snom") or 0.0) + float(row["snom"])
        if row.get("kv") and (not rec.get("kv") or row["kv"] > rec["kv"]):
            rec["kv"] = row["kv"]
        if len(str(row.get("name") or "")) > len(str(rec.get("name") or "")):
            rec["name"] = row["name"]
    out = []
    for rec in groups.values():
        rec["nominal_mw"] = nominal_mw(rec.get("kv"), rec.get("imax") or None, rec.get("snom") or None)
        out.append(rec)
    out.sort(key=lambda r: (r.get("tso") or "", r.get("name") or "", r.get("eic") or ""))
    return out


def write_grid_csv(rows: Sequence[Dict[str, Any]], path: Path = REF_CSV) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            kv = row.get("kv")
            mw = row.get("nominal_mw")
            writer.writerow({
                "eic": row.get("eic") or "",
                "name": row.get("name") or "",
                "tso": row.get("tso") or "",
                "type": row.get("type") or "",
                "sub_from": row.get("sub_from") or "",
                "sub_to": row.get("sub_to") or "",
                "kv": "" if kv is None else f"{kv:g}",
                "nominal_mw": "" if mw is None else f"{mw:g}",
            })
    return path


def load_grid_elements(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    src = path or REF_CSV
    if not src.is_file():
        return []
    out: List[Dict[str, Any]] = []
    with src.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            eic = (row.get("eic") or "").strip()
            key = compact_eic(eic)
            if len(key) < 10:
                continue
            kv = _to_float(row.get("kv"))
            mw = _to_float(row.get("nominal_mw"))
            out.append({
                "eic": eic,
                "key": key,
                "name": (row.get("name") or "").strip(),
                "tso": (row.get("tso") or "").strip(),
                "type": (row.get("type") or "").strip(),
                "sub_from": (row.get("sub_from") or "").strip(),
                "sub_to": (row.get("sub_to") or "").strip(),
                "kv": kv,
                "nominal_mw": mw,
            })
    return out


def lookup_grid(eic: str, table: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    key = compact_eic(eic)
    if len(key) < 10:
        return None
    hit = table.get(key)
    if hit:
        return hit
    for n in range(min(len(key), 16), 11, -1):
        pref = key[:n]
        matches = [row for k, row in table.items() if k.startswith(pref)]
        if len(matches) == 1:
            return matches[0]
    return None


def grid_table(elements: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    rows = list(elements) if elements is not None else load_grid_elements()
    return {row["key"]: row for row in rows}


def discover_zip_url(html: str, base: str = SGM_PAGE) -> str:
    """Neueste nicht-outdated ZIP-URL aus der JAO-Seite."""
    hrefs = re.findall(r'href=["\']([^"\']+\.zip)["\']', html, flags=re.I)
    texts = re.findall(r'>([^<]*Static Grid Model[^<]*\.zip)<', html, flags=re.I)
    candidates: List[str] = []
    for raw in hrefs + texts:
        name = raw.split("/")[-1]
        if "outdated" in raw.lower() or "outdated" in name.lower():
            continue
        if not _ZIP_NAME.search(name.replace("%20", " ")):
            continue
        candidates.append(raw)
    if not candidates:
        raise RuntimeError("Keine aktuelle SGM-ZIP auf der JAO-Seite gefunden.")
    latest = sorted(candidates, key=lambda u: u.split("/")[-1])[-1]
    if latest.startswith("http"):
        return latest
    return urljoin(base, latest.replace(" ", "%20"))


def download_sgm(cache_dir: Path = CACHE_DIR, session: Optional[requests.Session] = None) -> Path:
    """ZIP von JAO holen, ins Cache-Verzeichnis legen, XLSX auspacken."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(cache_dir.glob("*.xlsx"))
    if existing:
        return existing[-1]
    http = session or requests.Session()
    page = http.get(SGM_PAGE, timeout=REQUEST_TIMEOUT, headers={"User-Agent": _USER_AGENT})
    page.raise_for_status()
    url = discover_zip_url(page.text)
    name = url.split("/")[-1].split("?")[0] or "sgm.zip"
    zip_path = cache_dir / name
    if not zip_path.is_file():
        resp = http.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        zip_path.write_bytes(resp.content)
    xlsx = _extract_xlsx(zip_path, cache_dir)
    if xlsx is None:
        raise RuntimeError(f"Keine XLSX in {zip_path.name}")
    return xlsx


def _extract_xlsx(zip_path: Path, dest: Path) -> Optional[Path]:
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".xlsx") and not n.startswith("__")]
        if not names:
            return None
        names.sort()
        member = names[-1]
        target = dest / Path(member).name
        if not target.is_file():
            target.write_bytes(zf.read(member))
        return target


def refresh_grid_ref(
    cache_dir: Path = CACHE_DIR,
    dest: Path = REF_CSV,
    session: Optional[requests.Session] = None,
) -> Path:
    xlsx = download_sgm(cache_dir, session=session)
    rows = aggregate_by_eic(parse_workbook(xlsx))
    path = write_grid_csv(rows, dest)
    print(f"  SGM: {len(rows)} Netzelemente -> {path}", flush=True)
    return path


def make_fixture_workbook(rows: Sequence[Dict[str, Any]]) -> bytes:
    """Kleine XLSX für Tests (zwei Kopfzeilen wie das echte SGM)."""
    wb = Workbook()
    by_sheet: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_sheet.setdefault(row.get("sheet") or "Lines", []).append(row)
    first = True
    for title, items in by_sheet.items():
        ws = wb.active if first else wb.create_sheet(title)
        if first:
            ws.title = title
            first = False
        ws.append(["NE_name", "EIC_Code", "TSO", "Substation_1", "Substation_2", "Voltage_level(kV)", "Maximum Current Imax (A)", "Snom"])
        ws.append(["", "", "", "Full_name", "Full_name", "", "Fixed", ""])
        for item in items:
            ws.append([
                item.get("name"),
                item.get("eic"),
                item.get("tso"),
                item.get("sub_from"),
                item.get("sub_to"),
                item.get("kv"),
                item.get("imax"),
                item.get("snom"),
            ])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
