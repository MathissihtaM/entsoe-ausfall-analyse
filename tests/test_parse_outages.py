from src.countries import GERMANY, fully_abroad, fully_abroad_eic, tso_from_asset_eic, tso_from_text, tso_label
from src.parse_outages import classify_voltage, keep_event, keep_published, label_unlocated, parse_unavailability_xml

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<Unavailability_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6:outagedocument:3:0">
  <mRID>v5ePJdMNzV_jEBjYnvEqTA</mRID>
  <revisionNumber>2</revisionNumber>
  <type>A78</type>
  <createdDateTime>2016-05-13T11:56:12Z</createdDateTime>
  <sender_MarketParticipant.mRID codingScheme="A01">10XDE-VE-TRANSM-W</sender_MarketParticipant.mRID>
  <unavailability_Time_Period.timeInterval>
    <start>2016-02-23T06:00Z</start>
    <end>2016-02-23T14:00Z</end>
  </unavailability_Time_Period.timeInterval>
  <TimeSeries>
    <mRID>1</mRID>
    <businessType>A53</businessType>
    <in_Domain.mRID codingScheme="A01">10YCZ-CEPS-----N</in_Domain.mRID>
    <out_Domain.mRID codingScheme="A01">10Y1001A1001A82H</out_Domain.mRID>
    <start_DateAndOrTime.date>2016-02-23</start_DateAndOrTime.date>
    <start_DateAndOrTime.time>06:00:00Z</start_DateAndOrTime.time>
    <end_DateAndOrTime.date>2016-02-23</end_DateAndOrTime.date>
    <end_DateAndOrTime.time>14:00:00Z</end_DateAndOrTime.time>
    <Asset_RegisteredResource>
      <mRID codingScheme="A01">10T-CZ-SK-00005Q</mRID>
      <name>Röhrsdorf-Hradec 380 kV</name>
      <asset_PSRType.psrType>B21</asset_PSRType.psrType>
      <location.name>Röhrsdorf-Hradec</location.name>
    </Asset_RegisteredResource>
    <Available_Period>
      <timeInterval>
        <start>2016-02-23T06:00Z</start>
        <end>2016-02-23T14:00Z</end>
      </timeInterval>
      <Point><position>1</position><quantity>1000</quantity></Point>
    </Available_Period>
  </TimeSeries>
  <Reason>
    <code>B19</code>
    <text>B19</text>
  </Reason>
</Unavailability_MarketDocument>
""".encode("utf-8")


def test_parse_sample():
    rows = parse_unavailability_xml(SAMPLE, country=GERMANY, doc_type="A78")
    assert len(rows) == 1
    ev = rows[0]
    assert ev["tso"] == "50Hertz"
    assert ev["reason"] == "Geplante Wartung"
    assert ev["voltage"] == "380 kV"
    assert ev["duration_h"] == 8.0
    assert ev["avail_mw"] == 1000.0
    assert ev["revision"] == 2
    assert "Tschechien" in ev["border"]


A80_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<Unavailability_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6:outagedocument:3:0">
  <mRID>c6GqBNWcdUknFUcTqn3XXg</mRID>
  <revisionNumber>2</revisionNumber>
  <type>A80</type>
  <createdDateTime>2016-05-13T11:44:39Z</createdDateTime>
  <sender_MarketParticipant.mRID codingScheme="A01">10XDE-VE-TRANSM-W</sender_MarketParticipant.mRID>
  <TimeSeries>
    <businessType>A53</businessType>
    <biddingZone_Domain.mRID codingScheme="A01">10YDE-VE-------2</biddingZone_Domain.mRID>
    <start_DateAndOrTime.date>2015-06-07</start_DateAndOrTime.date>
    <start_DateAndOrTime.time>22:00:00Z</start_DateAndOrTime.time>
    <end_DateAndOrTime.date>2015-12-31</end_DateAndOrTime.date>
    <end_DateAndOrTime.time>23:00:00Z</end_DateAndOrTime.time>
    <production_RegisteredResource.mRID codingScheme="A01">27W-PU-EPC2----S</production_RegisteredResource.mRID>
    <production_RegisteredResource.name>EPC2</production_RegisteredResource.name>
    <production_RegisteredResource.location.name>Počerady 2</production_RegisteredResource.location.name>
    <production_RegisteredResource.pSRType.psrType>B04</production_RegisteredResource.pSRType.psrType>
    <production_RegisteredResource.pSRType.powerSystemResources.nominalP unit="MAW">821</production_RegisteredResource.pSRType.powerSystemResources.nominalP>
    <Available_Period>
      <timeInterval><start>2015-06-07T22:00Z</start><end>2015-12-31T23:00Z</end></timeInterval>
      <Point><position>1</position><quantity>0</quantity></Point>
    </Available_Period>
  </TimeSeries>
  <Reason><code>B19</code></Reason>
</Unavailability_MarketDocument>
""".encode("utf-8")


def test_intra_zonal_name_becomes_unlocated():
    assert label_unlocated("intra_zonal") == "unlocated"
    assert label_unlocated("Intra-zonal") == "unlocated"
    assert label_unlocated("Neurath") == "Neurath"
    xml = A80_SAMPLE.replace(
        b"<production_RegisteredResource.name>EPC2</production_RegisteredResource.name>",
        b"<production_RegisteredResource.name>intra_zonal</production_RegisteredResource.name>",
    ).replace(
        "<production_RegisteredResource.location.name>Počerady 2</production_RegisteredResource.location.name>".encode("utf-8"),
        b"<production_RegisteredResource.location.name>intra_zonal</production_RegisteredResource.location.name>",
    )
    rows = parse_unavailability_xml(xml, country=GERMANY, doc_type="A80")
    assert rows[0]["asset_name"] == "unlocated"
    assert rows[0]["location"] == "unlocated"


def test_parse_generation_unit():
    rows = parse_unavailability_xml(A80_SAMPLE, country=GERMANY, doc_type="A80")
    assert len(rows) == 1
    ev = rows[0]
    assert ev["category"] == "Erzeugungseinheit"
    assert ev["asset_type"] == "Erdgas"
    assert ev["tso"] == "50Hertz"
    assert ev["nominal_mw"] == 821.0
    assert ev["reason"] == "Geplante Wartung"
    assert "Po" in ev["asset_name"]


def test_keep_published_drops_cancelled_maintenance_and_withdrawn():
    assert keep_published({"status": "storniert", "reason": "Geplante Wartung"}) is False
    assert keep_published({"status": "zurückgezogen", "reason": "Störung"}) is False
    assert keep_published({"status": "storniert", "reason": "Störung"}) is True
    assert keep_published({"status": "aktiv", "reason": "Geplante Wartung"}) is True


def test_voltage_classes():
    assert classify_voltage(xml_kv=380, type_code="B21", name="x", location="") == "380 kV"
    assert classify_voltage(xml_kv=None, type_code="B22", name="Kontek", location="") == "HGÜ"
    assert classify_voltage(xml_kv=None, type_code="B21", name="Leitung", location="") == "unbekannt"
    assert classify_voltage(xml_kv=None, type_code="B21", name="L-380-LAND-GROH-2", location="") == "380 kV"
    assert classify_voltage(xml_kv=None, type_code="B21", name="KAS_400-S2", location="") == "380 kV"


def test_baltic_cable_is_tennet_not_50hertz():
    assert tso_from_text("Herrenwyk-Kruseberg (Baltic cable)") == "TenneT"
    assert tso_from_text("50H/220/Baltic Eagle-Lubmin/283") == "50Hertz"
    label = tso_label(
        GERMANY,
        "10X1001A1001A450",
        ("10Y1001A1001A47J", "10Y1001A1001A82H"),
        asset_eic="10T-DE-SE-000015",
        asset_name="Herrenwyk-Kruseberg (Baltic cable)",
        location="",
    )
    assert label == "TenneT"


def test_asset_eic_d8_is_50hertz_d2_is_tennet():
    assert tso_from_asset_eic("11TD8L151------U") == "50Hertz"
    assert tso_from_asset_eic("11TD2L000000354T") == "TenneT"
    assert tso_from_asset_eic("11T-D2-D00000354") == "TenneT"
    assert tso_from_asset_eic("10T-DE-SE-000015") == ""


def test_generation_eic_issuer_fills_unknown_zone_but_not_explicit_zone():
    assert tso_from_asset_eic("11WD7BERG1S--KWB") == "Amprion"
    assert tso_from_asset_eic("11WD4AL1KU1-S--E") == "TransnetBW"
    assert tso_from_asset_eic("11W0-0000-0026-Y") == ""
    amprion_zone = tso_label(
        GERMANY,
        "10X1001A1001A450",
        (None, None, "10YDE-RWENET---I"),
        asset_eic="11WD4GKM-2CD----X",
        asset_name="intra_zonal",
    )
    assert amprion_zone == "Amprion"
    only_zone = tso_label(
        GERMANY,
        "10X1001A1001A450",
        (None, None, "10Y1001A1001A82H"),
        asset_eic="11WD4AL1KU1-S--E",
        asset_name="intra_zonal",
    )
    assert only_zone == "TransnetBW"


def test_shared_50h_ttg_line_is_not_forced_to_one_tso():
    assert tso_from_text("50H-TTG/380/ALTENFELD-REDWITZ/459") == "50Hertz/TenneT"
    assert tso_from_text("50H/380/Eula-Pulgar/587") == "50Hertz"
    assert tso_from_text("TTG/380/ELSFLETH-GANDERKESEE/RT") == "TenneT"


def test_a78_corridors_include_each_tso_against_neighbors():
    pairs = {(a.code, b.code) for a, b in GERMANY.a78_corridors()}
    assert ("DE_AMPRION", "FR") in pairs
    assert ("FR", "DE_AMPRION") in pairs
    assert ("DE_TENNET", "NL") in pairs
    assert ("DE_50HZ", "PL") in pairs
    assert ("DE_TRANSNET", "CH") in pairs
    assert ("DE_LU", "FR") in pairs
    assert tso_label(
        GERMANY,
        "10X1001A1001A450",
        ("10YFR-RTE------C", "10YDE-RWENET---I"),
        asset_eic="",
        asset_name="Taubental Süd",
    ) == "Amprion"


def test_fully_abroad_eic_drops_interior_and_third_country_links():
    drop = [
        "17T-FR-000000309",
        "12T-0000000124-J",
        "45T000000000109P",
        "50TL00000002565Y",
        "46TAC0000000176H",
        "46ASU0000000004A",
        "50A0000000017030",
        "10T-BE-FR-00008S",
        "10T-CH-FR-00005Q",
        "10T-DK-DK-00001Q",
        "10T-NO-SE-00005Q",
        "10T-NO-GB-00001I",
        "10T-CZ-SK-00005Q",
    ]
    keep = [
        "11TD8L151------U",
        "11W0-0000-0026-Y",
        "11A0-0000-0034-B",
        "10T-DE-FR-00002G",
        "10T-CH-DE-00009T",
        "10T-DE-PL-00001X",
        "10T-NO-DE-00002J",
        "10T-DE-SE-000015",
        "10T1001C--00064S",
        "",
    ]
    for eic in drop:
        assert fully_abroad_eic(eic), eic
        assert keep_event({"status": "aktiv", "reason": "Störung", "asset_eic": eic}, GERMANY) is False
    for eic in keep:
        assert fully_abroad_eic(eic) is False, eic
        assert keep_event({"status": "aktiv", "reason": "Störung", "asset_eic": eic}, GERMANY) is True


def test_fully_abroad_name_drops_neighbor_interior_keeps_interconnectors():
    assert fully_abroad("", "Westtirol - Buers 2", "")
    assert fully_abroad("10T-AT-DE-00001", "Westtirol - Buers 1", "")
    assert fully_abroad("", "Tauern-Lienz 451", "")
    assert fully_abroad("", "St. Peter Süd", "")
    assert fully_abroad("", "Buers Transformator 37", "")
    assert not fully_abroad("10T-CZ-DE-00001", "Hradec-Roehrsdorf 1", "CZ-DE")
    assert not fully_abroad("10T-CZ-DE-00002", "L-380-ETZ-HRA-441", "Etzenricht-Hradec")
    assert not fully_abroad("10T-AT-DE-00019", "St. Peter - Altheim 233", "")
    assert not fully_abroad("10T-CH-DE-00009", "380 kV Beznau-Tiengen (Aare West)", "")
    assert not fully_abroad("", "intra_zonal", "")
    assert not fully_abroad("11T0-0000-1671-Z", "Rheinfeld", "Opladen; St. Peter")
    assert keep_event(
        {"status": "aktiv", "reason": "Störung", "asset_eic": "10T-AT-DE-00001", "asset_name": "Westtirol - Buers 2"},
        GERMANY,
    ) is False
    assert keep_event(
        {"status": "aktiv", "reason": "Störung", "asset_eic": "10T-CZ-DE-00001", "asset_name": "Hradec-Roehrsdorf 1"},
        GERMANY,
    ) is True


def test_french_and_swiss_lines_are_not_assigned_by_country():
    rte = tso_label(
        GERMANY,
        "10X1001A1001A450",
        ("10YFR-RTE------C", "10Y1001A1001A82H"),
        asset_eic="17T-FR-00000",
        asset_name="L 400kV N0 1 AVELIN-MASTAING",
    )
    assert rte == "unbekannt"
    swiss = tso_label(
        GERMANY,
        "10X1001A1001A450",
        ("10YCH-SWISSGRIDZ", "10Y1001A1001A82H"),
        asset_eic="12T-000000000",
        asset_name="220 kV Giswil-Innertkirchen",
    )
    assert swiss == "unbekannt"
    tbw = tso_label(
        GERMANY,
        "10X1001A1001A450",
        ("10YFR-RTE------C", "10Y1001A1001A82H"),
        asset_eic="10T-DE-FR-00001",
        asset_name="Eichstetten-Vogelgrun",
    )
    assert tbw == "TransnetBW"
    amp = tso_label(
        GERMANY,
        "10X1001A1001A450",
        ("10YFR-RTE------C", "10Y1001A1001A82H"),
        asset_eic="10T-DE-FR-00002",
        asset_name="Ensdorf-Vigy 2",
    )
    assert amp == "Amprion"
    tiengen = tso_label(
        GERMANY,
        "10X1001A1001A450",
        ("10YCH-SWISSGRIDZ", "10Y1001A1001A82H"),
        asset_eic="10T-CH-DE-0001",
        asset_name="380 kV Beznau-Tiengen (Aare West)",
    )
    assert tiengen == "TransnetBW"
