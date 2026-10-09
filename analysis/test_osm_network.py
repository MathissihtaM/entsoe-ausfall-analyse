from analysis.osm_network import (
    _eic_tokens,
    _event_index,
    _keep_plant,
    _keep_substation,
    _lookup_plant,
    _build_eic_lookup,
    _match,
    _plant_query,
    _sub_query,
    _tokens,
    load_eic_plants,
)


def test_eic_tokens_expand_plant_names():
    toks = _eic_tokens("11WD7NEUR-B-KW-B")
    assert "neurath" in toks
    toks = _eic_tokens("11WD8JAEN1L----Z")
    assert "janschwalde" in toks


def test_zonal_event_not_osm_indexed():
    events = [
        {
            "reason": "Störung",
            "asset_name": "intra_zonal",
            "location": "intra_zonal",
            "asset_eic": "11WD7NEUR-B-KW-B",
        }
    ]
    items, index = _event_index(events)
    assert items == []
    assert index == {}


def test_overpass_queries_are_prefiltered():
    sub = _sub_query(47.0, 6.0, 55.0, 15.0)
    assert "voltage" in sub
    assert '["name"]({47.0,6.0,55.0,15.0});' not in sub
    plant = _plant_query(47.0, 6.0, 55.0, 15.0)
    assert "plant:source" in plant
    assert "plant:output:electricity" in plant


def test_keep_hv_substation_drops_distribution():
    assert _keep_substation({"substation": "minor_distribution"}) is False
    assert _keep_substation({"voltage": "380000"}) is True
    assert _keep_substation({"substation": "transmission"}) is True


def test_keep_plant_filters_solar():
    assert _keep_plant({"plant:source": "solar"}) is False
    assert _keep_plant({"plant:source": "coal"}) is True
    assert _keep_plant({"plant:output:electricity": "800 MW"}) is True


def test_zonal_generic_mitte_does_not_match_random_uw():
    events = [
        {
            "reason": "Störung",
            "asset_name": "intra_zonal",
            "location": "intra_zonal",
            "asset_eic": "11WD8MITT5G----0",
        }
    ]
    items, index = _event_index(events)
    assert _match("Umspannwerk Mitte", items, index) == []
    assert _match("Umspannwerk Dresden-Mitte", items, index) == []
    assert _match("Biomasseheizkraftwerk Berlin-Neukölln", items, index) == []


def test_single_station_token_matches():
    events = [{"reason": "Störung", "asset_name": "Garenfeld", "location": "", "asset_eic": ""}]
    items, index = _event_index(events)
    assert _match("Pumpspeicher Garenfeld", items, index) == [0]
    assert _tokens("Umspannwerk Borken") == ["borken"]


def test_vianden_from_short_eic_core():
    assert "vianden" in _eic_tokens("11WD72VIA2H-KW-0")


def test_eic_plant_lookup_from_opsd_csv(tmp_path):
    csv = tmp_path / "plants.csv"
    csv.write_text(
        "name_bnetza,name_uba,city,lat,lon,eic_code_plant,eic_code_block\n"
        "Neurath,Neurath,Grevenbroich,51.039,6.6,11WD7NEUR-B-KW-B,11WD7NEUR-B-KW-5\n"
        "Jänschwalde,Jänschwalde,Peitz,51.834,14.45,11WD8JAEN1L----Z,11WD8JAEN1L---AD\n",
        encoding="utf-8",
    )
    plants = load_eic_plants(csv)
    lookup = _build_eic_lookup(plants)
    neur = _lookup_plant("11WD7NEUR-B-KW-B", lookup)
    jaen = _lookup_plant("11WD8JAEN1L----Z", lookup)
    assert neur is not None and abs(neur["lat"] - 51.039) < 1e-6
    assert jaen is not None
    assert "jänschwalde" in jaen["name"].lower() or "janschwalde" in jaen["name"].lower()
    assert _lookup_plant("11W0-0000-0026-Y", lookup) is None
