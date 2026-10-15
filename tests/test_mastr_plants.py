from pathlib import Path

from src.mastr_plants import (
    api_filter,
    attach_coords_from_snapshot,
    attach_weic_from_details,
    fetch_api,
    parse_mastr_xml,
    parse_weic_html,
    plants_from_api,
    refresh_mastr_ref,
    row_has_weic,
    write_mastr_csv,
    zenodo_coord_index,
)


def test_api_filter_is_10_mw():
    assert "10000" in api_filter()
    assert "Bruttoleistung der Einheit" in api_filter()


def test_row_has_weic_detects_field():
    assert row_has_weic({"Weic": "11WD7NEUR-B-KW-B"})
    assert not row_has_weic({"EinheitName": "Neurath", "Bruttoleistung": 1100000})


def test_plants_from_api_skips_without_weic():
    rows = [
        {
            "Weic": "11WD7NEUR-B-KW-B",
            "KraftwerkName": "Neurath",
            "Ort": "Grevenbroich",
            "Breitengrad": 51.039,
            "Laengengrad": 6.6,
            "Bruttoleistung": 1100000,
            "EnergietraegerName": "Braunkohle",
        },
        {
            "EinheitName": "Ohne EIC",
            "Breitengrad": 51.0,
            "Laengengrad": 6.5,
            "Bruttoleistung": 20000,
        },
        {
            "Weic": "11W0-0000-0026-Y",
            "EinheitName": "Anonym",
            "Breitengrad": 51.0,
            "Laengengrad": 6.5,
            "Bruttoleistung": 20000,
        },
        {
            "Weic": "11WD8BOXB1L---Q2",
            "EinheitName": "Boxberg Block Q",
            "Bruttoleistung": 893000,
        },
    ]
    plants = plants_from_api(rows)
    assert len(plants) == 2
    assert plants[0]["eic_code_plant"] == "11WD7NEUR-B-KW-B"
    assert plants[0]["name_bnetza"] == "Neurath"
    assert plants[1]["eic_code_plant"] == "11WD8BOXB1L---Q2"
    assert plants[1]["lat"] == ""


def test_fetch_api_pages(monkeypatch):
    pages = {
        1: {
            "Total": 2,
            "Data": [
                {"Id": 1, "Bruttoleistung": 20000, "Weic": "11WD7NEUR-B-KW-1"},
                {"Id": 2, "Weic": "11WD7NEUR-B-KW-B", "Bruttoleistung": 30000},
            ],
        }
    }

    def fake_page(page, page_size=5000, filt=None, session=None):
        return pages[page]

    monkeypatch.setattr("src.mastr_plants.fetch_api_page", fake_page)
    rows, has_weic = fetch_api()
    assert len(rows) == 2
    assert has_weic is True


def test_fetch_api_pages_without_weic(monkeypatch):
    calls = []

    def fake_page(page, page_size=5000, filt=None, session=None):
        calls.append(page)
        n = 5 if page == 1 else 3
        return {"Total": 8, "Data": [{"Id": page * 10 + i, "Bruttoleistung": 20000} for i in range(n)]}

    monkeypatch.setattr("src.mastr_plants.fetch_api_page", fake_page)
    rows, has_weic = fetch_api(page_size=5)
    assert has_weic is False
    assert len(rows) == 8
    assert calls == [1, 2]


def test_parse_xml_and_refresh_fallback(tmp_path: Path, monkeypatch):
    xml = tmp_path / "EinheitenVerbrennung.xml"
    xml.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
        <EinheitenVerbrennung>
          <Einheit>
            <NameStromerzeugungseinheit>Neurath Block F</NameStromerzeugungseinheit>
            <Weic>11WD7NEUR-B-KW-B</Weic>
            <Bruttoleistung>1100000</Bruttoleistung>
            <Breitengrad>51.039</Breitengrad>
            <Laengengrad>6.6</Laengengrad>
            <Ort>Grevenbroich</Ort>
            <Bundesland>Nordrhein-Westfalen</Bundesland>
          </Einheit>
          <Einheit>
            <NameStromerzeugungseinheit>Klein</NameStromerzeugungseinheit>
            <Weic>11WD7KLEI-B-KW-1</Weic>
            <Bruttoleistung>500</Bruttoleistung>
            <Breitengrad>51.0</Breitengrad>
            <Laengengrad>6.5</Laengengrad>
          </Einheit>
        </EinheitenVerbrennung>
        """,
        encoding="utf-8",
    )
    plants = parse_mastr_xml(xml)
    assert len(plants) == 1
    assert plants[0]["eic_code_plant"] == "11WD7NEUR-B-KW-B"
    assert abs(float(plants[0]["capacity_net_bnetza"]) - 1100) < 0.01

    dest = tmp_path / "mastr.csv"
    cache = tmp_path / "xml"
    cache.mkdir()
    (cache / "EinheitenVerbrennung.xml").write_bytes(xml.read_bytes())

    monkeypatch.setattr("src.mastr_plants.fetch_api", lambda **kwargs: ([], False))
    path = refresh_mastr_ref(dest=dest, cache_dir=cache)
    text = path.read_text(encoding="utf-8")
    assert "11WD7NEUR-B-KW-B" in text
    assert "Neurath" in text


def test_parse_weic_from_detail_html():
    html = (
        '<td class="display-template-label" title="W-EIC-Code der Stromerzeugungseinheit">'
        "W-EIC-Code der Stromerzeugungseinheit:</td>"
        '<td class="display-template-value " title="W-EIC-Code der Stromerzeugungseinheit">'
        "11WD8BOXB1L---Q2</td>"
    )
    assert parse_weic_html(html) == "11WD8BOXB1L---Q2"
    assert parse_weic_html("<html>kein code</html>") == ""


def test_attach_weic_from_details_uses_cache(tmp_path: Path, monkeypatch):
    cache = tmp_path / "weic_by_id.json"
    rows = [{"Id": 1817131, "Bruttoleistung": 893000, "EinheitName": "Boxberg Block Q"}]
    calls = []

    def fake_fetch(unit_id, session=None):
        calls.append(unit_id)
        return "11WD8BOXB1L---Q2"

    monkeypatch.setattr("src.mastr_plants.fetch_detail_weic", fake_fetch)
    n = attach_weic_from_details(rows, cache_path=cache, workers=1)
    assert n == 1
    assert rows[0]["Weic"] == "11WD8BOXB1L---Q2"
    rows2 = [{"Id": 1817131, "Bruttoleistung": 893000}]
    n2 = attach_weic_from_details(rows2, cache_path=cache, workers=1)
    assert n2 == 0
    assert rows2[0]["Weic"] == "11WD8BOXB1L---Q2"
    assert calls == [1817131]


def test_zenodo_coord_index_and_attach(tmp_path: Path, monkeypatch):
    csv_path = tmp_path / "bnetza_mastr_combustion_raw.csv"
    csv_path.write_text(
        "EinheitMastrNummer,Weic,Bruttoleistung,Breitengrad,Laengengrad\n"
        "SEE900001,11WD8BOXB1L---Q2,893000,51.419203,14.567034\n"
        "SEE900002,11WD8BOXB1L---R0,675000,51.411378,14.561926\n"
        "SEE900003,,500,51.0,14.5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("src.mastr_plants.download_zenodo_table", lambda key, cache_dir, session=None: csv_path)
    index = zenodo_coord_index(cache_dir=tmp_path, tables=("bnetza_mastr_combustion_raw.csv.zip",))
    assert index["SEE900001"]["lat"] == 51.419203
    assert index["11WD8BOXB1L---R0"]["lon"] == 14.561926
    assert "SEE900003" not in index  # unter 10 MW

    rows = [
        {"Id": 1, "MaStRNummer": "SEE900001", "Breitengrad": None, "Laengengrad": None},
        {"Id": 2, "MaStRNummer": "SEE999999", "Weic": "11WD8BOXB1L---R0", "Breitengrad": "", "Laengengrad": ""},
        {"Id": 3, "MaStRNummer": "SEE900001", "Breitengrad": 52.0, "Laengengrad": 13.0},
    ]
    n = attach_coords_from_snapshot(rows, index)
    assert n == 2
    assert rows[0]["Breitengrad"] == 51.419203
    assert rows[1]["Laengengrad"] == 14.561926
    assert rows[2]["Breitengrad"] == 52.0  # vorhandene Koordinaten bleiben


def test_write_csv_opsd_schema(tmp_path: Path):
    dest = tmp_path / "out.csv"
    write_mastr_csv([{
        "name_bnetza": "Neurath",
        "name_uba": "",
        "city": "Grevenbroich",
        "lat": 51.039,
        "lon": 6.6,
        "eic_code_plant": "11WD7NEUR-B-KW-B",
        "eic_code_block": "",
        "state": "Nordrhein-Westfalen",
        "country": "DE",
        "capacity_net_bnetza": 1100,
        "energy_source": "Braunkohle",
        "network_operator": "Amprion",
    }], dest)
    header = dest.read_text(encoding="utf-8").splitlines()[0]
    assert "eic_code_plant" in header
    assert "lat" in header
