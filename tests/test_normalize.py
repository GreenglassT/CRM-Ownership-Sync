from pipeline.normalize import (norm_street, is_po_box, norm_phone, name_tokens, jaccard,
                                map_care, primary_care, street_number)


def test_street_abbreviations_collapse():
    assert norm_street("4850 Northwest Sylvania Avenue") == norm_street("4850 NW Sylvania Ave")
    assert norm_street("1250 Northwest Franklin St") == norm_street("1250 NW Franklin Street")
    assert norm_street("1125 Logan Boulevard") == norm_street("1125 Logan Blvd")


def test_street_drops_units_and_punct():
    assert norm_street("210 Orchard Lane, Suite 4") == "210 orchard ln"
    assert norm_street("210 Orchard Ln.") == "210 orchard ln"


def test_po_box():
    assert is_po_box("PO Box 517")
    assert is_po_box("P.O. Box 12")
    assert not is_po_box("3156 W Prospect Rd")


def test_street_number():
    assert street_number("118 Union Square Dr") == "118"
    assert street_number("PO Box 517") == ""


def test_phone():
    assert norm_phone("(734) 388-8242") == "7343888242"
    assert norm_phone("1-734-388-8242") == "7343888242"
    assert norm_phone("") == ""


def test_name_tokens_drop_generic():
    a = name_tokens("Bellhaven Healthcare Centre of Ashland")
    b = name_tokens("Bellhaven Health Care Center of Ashland")
    assert a == b == {"bellhaven", "ashland"}


def test_jaccard():
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard({"a"}, {"b"}) == 0.0
    assert jaccard(set(), {"b"}) == 0.0


def test_care_mapping():
    assert map_care(["Assisted Living", "Memory Support"]) == ["Memory Care", "Assisted Living"]
    assert primary_care(["Short-Term Rehabilitation & Nursing"]) == "Skilled Nursing"
    assert primary_care([]) == ""
