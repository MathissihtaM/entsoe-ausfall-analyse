"""XML der Transparency-API in eine Zeile je Meldung.

Die API liefert ZIP-Archive. Darin liegt je Dokument ein
``Unavailability_MarketDocument``. Ein Dokument hat eine Kennung (``mRID``),
eine Revisionsnummer und einen Status, und darunter eine oder mehrere
``TimeSeries`` — je gemeldeter Anlage eine. Klartext steht dort kaum:
Dokumentart, Ursache, Status und Anlagentyp sind Codes.

Dokumentart, wie sie in der Abfrage angefordert wird:

- A77 Produktionseinheit (Kraftwerk als Ganzes)
- A78 Übertragungsnetz
- A79 Offshore-Netz
- A80 Erzeugungseinheit (einzelner Block)

Ursache im Element ``Reason``: B18 Störung, B19 geplante Wartung, B20 Stilllegung.
Fehlt der Code, steht ersatzweise ``businessType`` A54 (Störung) oder A53 (Wartung).

Status im ``docStatus``: A05 aktiv, A09 storniert, A13 zurückgezogen.

Anlagentyp im ``psrType``: B01–B24 (Braunkohle, AC-Leitung, HGÜ, …).
Die Bezeichnung in der Tabelle ist die Übersetzung, nicht der Code.

Leistung: ``Available_Period`` / ``quantity`` ist die noch verfügbare MW
(Restleistung), nicht die ausgefallene. ``nominalP`` ist die Nennleistung
und fehlt bei Leitungen oft. Koordinaten sind im Dokument fast immer leer.

Ausgabe je TimeSeries, die Felder, die Karte und Tabelle lesen:

- ``mrid``, ``revision``, ``created``, ``status``, ``sender_eic``
- ``doc_type``, ``category``, ``tso``, ``reason``, ``reason_text``
- ``start``, ``end``, ``duration_h`` (UTC, Dauer in Stunden)
- ``asset_name``, ``asset_eic``, ``asset_type``, ``location``, ``voltage``
  Der Ortsplatzhalter ``intra_zonal`` aus dem Dokument heißt ``unlocated``.
- ``border``, ``in_domain``, ``out_domain``, ``bidding_domain`` (EIC der Zonen)
- ``avail_mw``, ``nominal_mw``, ``lat``, ``lon``
"""

from __future__ import annotations

import re
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, Iterable, List, Optional
from xml.etree import ElementTree as ET

from src.countries import Country, fully_abroad, tso_label

_UNLOCATED_RE = re.compile(r"^intra[-_ ]?zonal$", re.IGNORECASE)


def label_unlocated(value: str) -> str:
    """Ortsplatzhalter aus dem Dokument heißen unlocated."""
    text = value or ""
    if _UNLOCATED_RE.match(text.strip()):
        return "unlocated"
    return text

REASON_LABELS = {
    "B18": "Störung",
    "B19": "Geplante Wartung",
    "B20": "Stilllegung",
}

DOC_STATUS_LABELS = {
    "A05": "aktiv",
    "A09": "storniert",
    "A13": "zurückgezogen",
}

DOC_TYPE_LABELS = {
    "A77": "Produktionseinheit",
    "A78": "Übertragungsnetz",
    "A79": "Offshore-Netz",
    "A80": "Erzeugungseinheit",
}

PSR_LABELS = {
    "B01": "Biomasse",
    "B02": "Braunkohle",
    "B03": "Steinkohlengas",
    "B04": "Erdgas",
    "B05": "Steinkohle",
    "B06": "Mineralöl",
    "B07": "Ölschiefer",
    "B08": "Torf",
    "B09": "Geothermie",
    "B10": "Pumpspeicher",
    "B11": "Laufwasser",
    "B12": "Speicherwasser",
    "B13": "Marine",
    "B14": "Kernenergie",
    "B15": "Sonstige EE",
    "B16": "Solar",
    "B17": "Abfall",
    "B18": "Wind Offshore",
    "B19": "Wind Onshore",
    "B20": "Sonstige Erzeugung",
    "B21": "AC-Leitung",
    "B22": "HGÜ",
    "B23": "Transformator",
    "B24": "Umrichter",
}

VOLTAGE_KV = re.compile(r"(?i)(?:L\s*)?(110|150|220|225|380|400|420)\s*k\s*v")
VOLTAGE_L_PREFIX = re.compile(r"(?i)(?:^|[-_/])L-(110|220|380|400)(?:[-_/]|$)")
VOLTAGE_UNDERSCORE = re.compile(r"(?i)(?:^|_|/)(110|220|380|400)(?:-|_|$)")
HVDC_IN_NAME = re.compile(r"(?i)\b(hg[uü]|hvdc|dc[- ]?link|gleichstrom)\b")
BUSINESS_REASON = {
    "A53": "Geplante Wartung",
    "A54": "Störung",
}

def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _walk(el: ET.Element) -> Iterable[ET.Element]:
    yield el
    for child in list(el):
        yield from _walk(child)


def _child(parent: ET.Element, name: str) -> Optional[ET.Element]:
    want = name.lower()
    for child in list(parent):
        if _local(child.tag).lower() == want:
            return child
    return None


def _child_text(parent: Optional[ET.Element], name: str) -> str:
    if parent is None:
        return ""
    node = _child(parent, name)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _first_text(parent: Optional[ET.Element], *names: str) -> str:
    if parent is None:
        return ""
    for name in names:
        text = _child_text(parent, name)
        if text:
            return text
    want = {n.lower() for n in names}
    for node in _walk(parent):
        if _local(node.tag).lower() in want and node.text and node.text.strip():
            return node.text.strip()
    return ""


def _attr(el: Optional[ET.Element], name: str) -> str:
    if el is None:
        return ""
    for key, val in el.attrib.items():
        if _local(key).lower() == name.lower():
            return str(val).strip()
    return ""


def _parse_dt(text: str) -> Optional[str]:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _combine_date_time(date_s: str, time_s: str) -> Optional[str]:
    if not date_s:
        return None
    t = time_s or "00:00:00Z"
    if not t.endswith("Z") and "+" not in t:
        t = t + "Z"
    return _parse_dt(f"{date_s}T{t}")


def _hours(start: Optional[str], end: Optional[str]) -> Optional[float]:
    if not start or not end:
        return None
    a = datetime.fromisoformat(start.replace("Z", "+00:00"))
    b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    sec = (b - a).total_seconds()
    if sec < 0:
        return None
    return round(sec / 3600.0, 3)


def _to_float(text: str) -> Optional[float]:
    s = (text or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _kv_class(kv: float) -> str:
    if kv >= 1000:
        kv = kv / 1000.0
    if kv >= 350:
        return "380 kV"
    if kv >= 200:
        return "220 kV"
    if kv >= 100:
        return "110 kV"
    return f"{int(round(kv))} kV"


def classify_voltage(*, xml_kv: Optional[float], type_code: str, name: str, location: str) -> str:
    """Spannungsklasse für die Anzeige.

    ``type_code`` ist der Anlagentyp aus dem Dokument (B22 = HGÜ). Sonst zählt
    die kV-Angabe im XML, und wenn die fehlt, eine Zahl im Anlagenamen
    (``L-380``, ``400-S2``). Unter 100 kV bleibt die Zahl stehen, darüber
    wird auf 110, 220 oder 380 kV gerundet.
    """
    blob = f"{name} {location}"
    if type_code == "B22" or HVDC_IN_NAME.search(blob):
        return "HGÜ"
    if xml_kv is not None:
        return _kv_class(xml_kv)
    for rx in (VOLTAGE_KV, VOLTAGE_L_PREFIX, VOLTAGE_UNDERSCORE):
        m = rx.search(blob)
        if m:
            return _kv_class(float(m.group(1)))
    return "unbekannt"


def _reason(root: ET.Element, series: ET.Element) -> tuple[str, str]:
    """Deutsche Ursache und der freie Text daneben.

    Zuerst ein Reason-Code B18/B19/B20, sonst businessType A53/A54.
    Der zweite Rückgabewert ist ein zusätzlicher Freitext, der kein Code ist.
    """
    codes: List[str] = []
    texts: List[str] = []
    for node in list(root) + list(series):
        if _local(node.tag).lower() != "reason":
            continue
        code = _child_text(node, "code")
        text = _child_text(node, "text")
        if code:
            codes.append(code)
        if text:
            texts.append(text)
    preferred = next((c for c in codes if c in REASON_LABELS), "")
    extra = next((t for t in texts if t and t not in REASON_LABELS and t != preferred), "")
    if preferred:
        return REASON_LABELS[preferred], extra
    business = _child_text(series, "businessType")
    if business in BUSINESS_REASON:
        return BUSINESS_REASON[business], extra or (texts[0] if texts else "")
    if codes:
        return codes[0], extra
    return "unbekannt", extra


def _period_bounds(series: ET.Element, root: ET.Element) -> tuple[Optional[str], Optional[str]]:
    """Beginn und Ende der Unverfügbarkeit, UTC, oder None wenn das Feld fehlt.

    Die Dokumente legen das Intervall an unterschiedlichen Stellen ab:
    eigene Start/Ende-Felder, ``Available_Period``, oder das Intervall
    im Dokumentkopf.
    """
    start = _combine_date_time(
        _first_text(series, "start_DateAndOrTime.date"),
        _first_text(series, "start_DateAndOrTime.time"),
    )
    end = _combine_date_time(
        _first_text(series, "end_DateAndOrTime.date"),
        _first_text(series, "end_DateAndOrTime.time"),
    )
    if start and end:
        return start, end
    for period_name in ("Available_Period", "WindPowerFeedin_Period", "unavailability_Time_Period.timeInterval"):
        period = _child(series, period_name) or _child(root, period_name)
        if period is None:
            continue
        interval = _child(period, "timeInterval") or period
        p_start = _parse_dt(_first_text(interval, "start"))
        p_end = _parse_dt(_first_text(interval, "end"))
        if p_start or p_end:
            return p_start or start, p_end or end
    header = _child(root, "unavailability_Time_Period.timeInterval")
    if header is not None:
        return (
            _parse_dt(_first_text(header, "start")),
            _parse_dt(_first_text(header, "end")),
        )
    return start, end


def _avail_qty(series: ET.Element) -> Optional[float]:
    """Kleinste Restleistung in MW innerhalb der Meldung, nicht die ausgefallene."""
    values: List[float] = []
    for period_name in ("Available_Period", "WindPowerFeedin_Period"):
        period = _child(series, period_name)
        if period is None:
            continue
        for node in _walk(period):
            if _local(node.tag).lower() == "quantity" and node.text:
                val = _to_float(node.text)
                if val is not None:
                    values.append(val)
    if not values:
        return None
    return min(values)


def _asset(series: ET.Element) -> Dict[str, str]:
    """Anlage aus der TimeSeries.

    Netz-Dokumente nennen sie ``Asset_RegisteredResource``, Erzeugung
    ``production_RegisteredResource``. ``type_code`` ist der rohe Anlagentyp
    (B01–B24), ``nominal`` die Nennleistung als Text.
    """
    asset = _child(series, "Asset_RegisteredResource")
    prod = _child(series, "production_RegisteredResource")
    node = asset if asset is not None else prod
    volt_el = None
    if asset is not None:
        volt_el = _child(asset, "voltage_PowerSystemResources.highVoltageLimit")
    return {
        "eic": (
            _child_text(node, "mRID")
            or _first_text(series, "production_RegisteredResource.mRID")
            or _first_text(series, "production_RegisteredResource.pSRType.powerSystemResources.mRID")
        ),
        "name": (
            _first_text(series, "production_RegisteredResource.location.name")
            or _first_text(series, "production_RegisteredResource.name")
            or _first_text(series, "production_RegisteredResource.pSRType.powerSystemResources.name")
            or _child_text(node, "name")
        ),
        "location": _first_text(node, "location.name")
        or _first_text(series, "production_RegisteredResource.location.name"),
        "type_code": (
            _first_text(asset, "asset_PSRType.psrType")
            or _first_text(series, "production_RegisteredResource.pSRType.psrType")
            or _first_text(prod, "pSRType.psrType")
        ),
        "nominal": (
            _first_text(series, "production_RegisteredResource.pSRType.powerSystemResources.nominalP")
            or _first_text(prod, "nominalP")
        ),
        "voltage_raw": _first_text(
            asset,
            "voltage_PowerSystemResources.highVoltageLimit",
            "highVoltageLimit",
        ),
        "voltage_unit": _attr(volt_el, "unit"),
    }


def _coords(asset_el: Optional[ET.Element]) -> tuple[Optional[float], Optional[float]]:
    if asset_el is None:
        return None, None
    lat = _to_float(_first_text(asset_el, "latitude", "position.latitude"))
    lon = _to_float(_first_text(asset_el, "longitude", "position.longitude"))
    return lat, lon


def parse_unavailability_xml(xml_bytes: bytes, *, country: Country, doc_type: str) -> List[Dict[str, Any]]:
    """Eine XML-Datei, eine Zeile je TimeSeries. ``doc_type`` ist die Abfrage (A77–A80)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    if _local(root.tag).lower() != "unavailability_marketdocument":
        return []

    mrid = _child_text(root, "mRID")
    if not mrid:
        return []
    revision = int(_to_float(_child_text(root, "revisionNumber")) or 0)
    created = _parse_dt(_child_text(root, "createdDateTime"))
    status_el = _child(root, "docStatus")
    status_code = _first_text(status_el, "value") if status_el is not None else ""
    sender = _child_text(root, "sender_MarketParticipant.mRID")

    series_nodes = [c for c in list(root) if _local(c.tag).lower() == "timeseries"]
    if not series_nodes:
        series_nodes = [root]

    events: List[Dict[str, Any]] = []
    for series in series_nodes:
        asset = _asset(series)
        start, end = _period_bounds(series, root)
        in_eic = _first_text(series, "in_Domain.mRID")
        out_eic = _first_text(series, "out_Domain.mRID")
        bidding_eic = _first_text(series, "biddingZone_Domain.mRID")
        zone_by_eic = country.zone_by_eic()
        in_label = zone_by_eic[in_eic].label if in_eic in zone_by_eic else in_eic
        out_label = zone_by_eic[out_eic].label if out_eic in zone_by_eic else out_eic
        internal = bool(in_eic and in_eic == out_eic)
        if bidding_eic and not (in_eic and out_eic):
            zone = zone_by_eic.get(bidding_eic)
            border = zone.label if zone else bidding_eic
        else:
            border = "intern" if internal or not (in_eic and out_eic) else f"{out_label} → {in_label}"

        xml_kv = _to_float(asset["voltage_raw"])
        # Einheit VT im Dokument heißt Volt, die Anzeige rechnet in kV.
        if xml_kv is not None and asset["voltage_unit"].upper() == "VT":
            xml_kv = xml_kv / 1000.0

        reason, reason_text = _reason(root, series)
        asset_name = label_unlocated(asset["name"] or asset["location"] or asset["eic"] or mrid)
        location = label_unlocated(asset["location"])
        tso = tso_label(
            country,
            sender,
            (in_eic, out_eic, bidding_eic),
            asset_eic=asset["eic"],
            asset_name=asset_name,
            location=location,
        )

        asset_el = _child(series, "Asset_RegisteredResource")
        if asset_el is None:
            asset_el = _child(series, "production_RegisteredResource")
        lat, lon = _coords(asset_el)
        voltage = classify_voltage(
            xml_kv=xml_kv,
            type_code=asset["type_code"],
            name=asset_name,
            location=location,
        )

        events.append(
            {
                "mrid": mrid,
                "revision": revision,
                "country": country.code,
                "country_label": country.label,
                "doc_type": doc_type,
                "category": DOC_TYPE_LABELS.get(doc_type, doc_type),
                "tso": tso,
                "reason": reason,
                "reason_text": reason_text,
                "start": start,
                "end": end,
                "duration_h": _hours(start, end),
                "voltage": voltage,
                "asset_name": asset_name,
                "asset_eic": asset["eic"],
                "asset_type": PSR_LABELS.get(asset["type_code"], asset["type_code"] or "unbekannt"),
                "location": location,
                "border": border,
                "in_domain": in_eic,
                "out_domain": out_eic,
                "avail_mw": _avail_qty(series),
                "nominal_mw": _to_float(asset.get("nominal") or ""),
                "bidding_domain": bidding_eic,
                "status": DOC_STATUS_LABELS.get(status_code, status_code or "aktiv"),
                "sender_eic": sender,
                "created": created,
                "lat": lat,
                "lon": lon,
            }
        )
    return events


def parse_zip(data: bytes, *, country: Country, doc_type: str) -> List[Dict[str, Any]]:
    if not data or data[:2] != b"PK":
        return []
    events: List[Dict[str, Any]] = []
    with zipfile.ZipFile(BytesIO(data)) as zf:
        for info in zf.infolist():
            if not info.filename.lower().endswith(".xml"):
                continue
            events.extend(parse_unavailability_xml(zf.read(info), country=country, doc_type=doc_type))
    return events


def latest_revision(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Je Dokumentkennung und Anlage nur die höchste Revisionsnummer."""
    best: Dict[str, Dict[str, Any]] = {}
    for event in events:
        key = f"{event.get('mrid')}|{event.get('asset_eic') or event.get('asset_name')}"
        prev = best.get(key)
        if prev is None or int(event.get("revision") or 0) >= int(prev.get("revision") or 0):
            best[key] = event
    return list(best.values())


def keep_published(event: Dict[str, Any]) -> bool:
    """Stornierte Wartung und zurückgezogene Dokumente entfallen.

    Eine stornierte Störung bleibt: die Meldung sagt, dass eine geplante
    Nichtverfügbarkeit nicht stattfand. Zurückgezogen heißt, das Dokument
    war ein Irrtum.
    """
    status = event.get("status") or ""
    if status == "zurückgezogen":
        return False
    if status == "storniert" and event.get("reason") == "Geplante Wartung":
        return False
    return True


def keep_event(event: Dict[str, Any], country: Country) -> bool:
    """Veröffentlichte Meldung, die zum ausgewerteten Land gehört.

    Für DE entfallen Anlagen komplett im Nachbarland: ausländische EIC
    (RTE, Swissgrid, Energinet, …), Kuppel ohne DE (BE–FR) und Namen
    ohne deutsches Ende (Westtirol–Bürs, Tauern–Lienz). DE-Kuppel und
    deutsche ``11…``-Codes bleiben.
    """
    if not keep_published(event):
        return False
    if country.code == "DE" and fully_abroad(
        event.get("asset_eic") or "",
        event.get("asset_name") or "",
        event.get("location") or "",
    ):
        return False
    return True
