from analysis.countries import GERMANY
from analysis.parse_outages import classify_voltage, keep_published, parse_unavailability_xml

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
    assert classify_voltage(xml_kv=380, psr="B21", name="x", location="") == "380 kV"
    assert classify_voltage(xml_kv=None, psr="B22", name="Kontek", location="") == "HGÜ"
    assert classify_voltage(xml_kv=None, psr="B21", name="Leitung", location="") == "unbekannt"
    assert classify_voltage(xml_kv=None, psr="B21", name="L-380-LAND-GROH-2", location="") == "380 kV"
    assert classify_voltage(xml_kv=None, psr="B21", name="KAS_400-S2", location="") == "380 kV"
