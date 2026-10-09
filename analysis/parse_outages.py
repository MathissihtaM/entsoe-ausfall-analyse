"""XML-Parser für ENTSO-E Unavailability-Dokumente (A78, A79)."""

from __future__ import annotations

import re
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, Iterable, List, Optional
from xml.etree import ElementTree as ET

from analysis.countries import Country, tso_label

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


def classify_voltage(*, xml_kv: Optional[float], psr: str, name: str, location: str) -> str:
    blob = f"{name} {location}"
    if psr == "B22" or HVDC_IN_NAME.search(blob):
        return "HGÜ"
    if xml_kv is not None:
        return _kv_class(xml_kv)
    for rx in (VOLTAGE_KV, VOLTAGE_L_PREFIX, VOLTAGE_UNDERSCORE):
        m = rx.search(blob)
        if m:
            return _kv_class(float(m.group(1)))
    return "unbekannt"


def _reason(root: ET.Element, series: ET.Element) -> tuple[str, str]:
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
        "psr": (
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
        if xml_kv is not None and asset["voltage_unit"].upper() == "VT":
            xml_kv = xml_kv / 1000.0

        reason, reason_text = _reason(root, series)
        tso = tso_label(
            country,
            sender,
            (in_eic, out_eic, bidding_eic),
            asset_eic=asset["eic"],
            asset_name=asset["name"] or asset["location"],
            location=asset["location"],
        )

        asset_el = _child(series, "Asset_RegisteredResource")
        if asset_el is None:
            asset_el = _child(series, "production_RegisteredResource")
        lat, lon = _coords(asset_el)
        asset_name = asset["name"] or asset["location"] or asset["eic"] or mrid
        voltage = classify_voltage(
            xml_kv=xml_kv,
            psr=asset["psr"],
            name=asset_name,
            location=asset["location"],
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
                "asset_type": PSR_LABELS.get(asset["psr"], asset["psr"] or "unbekannt"),
                "location": asset["location"],
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
    best: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        key = f"{ev.get('mrid')}|{ev.get('asset_eic') or ev.get('asset_name')}"
        prev = best.get(key)
        if prev is None or int(ev.get("revision") or 0) >= int(prev.get("revision") or 0):
            best[key] = ev
    return list(best.values())


def keep_published(ev: Dict[str, Any]) -> bool:
    """Drop cancelled planned maintenance (A09) and withdrawn documents (A13)."""
    status = ev.get("status") or ""
    if status == "zurückgezogen":
        return False
    if status == "storniert" and ev.get("reason") == "Geplante Wartung":
        return False
    return True
