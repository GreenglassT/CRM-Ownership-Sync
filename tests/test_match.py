"""Classification behaviour on synthetic fixtures. No network."""
import pytest

from pipeline.match import build_proposals, score_pair

PARENT = {"account_id": "P1", "name": "Bellhaven Senior Living (Parent Account)", "parent_id": "", "parent_name": ""}
OTHER = {"account_id": "P2", "name": "Cedar Trail Communities (Parent Account)", "parent_id": "", "parent_name": ""}


def acct(**kw):
    base = dict(account_id="A", name="", parent_id="P1", parent_name="Bellhaven Senior Living (Parent Account)",
                billing_street="", billing_city="", billing_state="", billing_zip="", care_type="Assisted Living",
                status="Active", phone="", lifetime_revenue=0, outstanding_ar=0, chow_current_account="",
                duplicate_of_account="", note="")
    base.update(kw)
    return base


def loc(**kw):
    base = dict(slug="x", url="u", name="Bellhaven of X", street="1 Main St", city="Xtown", state="OH", zip="44000",
                care_offerings=["Assisted Living"], phone="(555) 000-0000", administrator="")
    base.update(kw)
    return base


def types(out):
    return sorted(p["type"] for p in out["proposals"])


def test_exact_match_confirms_with_no_proposals():
    out = build_proposals([loc()], [PARENT, acct(name="Bellhaven of X", billing_street="1 Main Street", billing_city="Xtown", billing_state="OH", billing_zip="44000", phone="555-000-0000")])
    assert types(out) == [] and len(out["confirmed"]) == 1


def test_rebrand_same_address_is_rename_not_create():
    out = build_proposals([loc()], [PARENT, acct(name="Sunny Acres Home", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000", phone="555-000-0000")])
    assert types(out) == ["UPDATE_NAME"]


def test_po_box_triggers_address_update():
    out = build_proposals([loc()], [PARENT, acct(name="Bellhaven of X", billing_street="PO Box 9", billing_city="Xtown", billing_state="OH", billing_zip="44000", phone="555-000-0000")])
    assert types(out) == ["UPDATE_ADDRESS"]


def test_wrong_parent_without_ar_reparents_directly():
    a = acct(name="Bellhaven of X", parent_id="P2", parent_name="Cedar Trail", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000", lifetime_revenue=50000, outstanding_ar=0)
    out = build_proposals([loc()], [PARENT, OTHER, a])
    assert types(out) == ["REPARENT"]
    assert out["proposals"][0]["actions"][0]["payload"] == {"parent_id": "P1"}


def test_wrong_parent_with_revenue_and_ar_is_chow_and_old_account_frozen():
    a = acct(name="Old Name", parent_id="P2", parent_name="Cedar Trail", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000", lifetime_revenue=50000, outstanding_ar=1200)
    out = build_proposals([loc()], [PARENT, OTHER, a])
    assert types(out) == ["CHOW"]          # no UPDATE_NAME on the frozen account
    p = out["proposals"][0]
    assert p["actions"][0]["op"] == "create" and p["actions"][0]["payload"]["parent_id"] == "P1"
    assert p["actions"][1]["payload"] == {"chow_current_account": {"$bind": "new_id"}}
    assert "note_append" not in p["actions"][1]   # old account: nothing but the CHOW pointer


def test_duplicates_keep_billing_history_copy():
    a = acct(account_id="A", name="Bellhaven of X", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000")
    b = acct(account_id="B", name="Bellhaven of X", billing_street="1 Main Street", billing_city="Xtown", billing_state="OH", billing_zip="44000", lifetime_revenue=9000)
    out = build_proposals([loc()], [PARENT, a, b])
    dups = [p for p in out["proposals"] if p["type"] == "DUPLICATE"]
    assert len(dups) == 1 and dups[0]["account"]["account_id"] == "A"
    assert dups[0]["actions"][0]["payload"] == {"duplicate_of_account": "B", "status": "Inactive"}


def test_duplicate_tie_breaks_on_website_agreement():
    # The copy whose phone matches the website gets the HIGHER id, so the id
    # tie-break alone would pick the wrong survivor; only the score term saves it.
    a = acct(account_id="A", name="Bellhaven of X", billing_street="1 Main Street", billing_city="Xtown", billing_state="OH", billing_zip="44000", phone="555-999-9999")
    b = acct(account_id="B", name="Bellhaven of X", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000", phone="555-000-0000")
    out = build_proposals([loc()], [PARENT, a, b])
    dups = [p for p in out["proposals"] if p["type"] == "DUPLICATE"]
    assert dups[0]["account"]["account_id"] == "A" and dups[0]["survivor"]["account_id"] == "B"


def test_false_friend_same_town_different_street_and_phone_is_create():
    a = acct(name="Union Square Senior Living", parent_id="P2", parent_name="Other", billing_street="240 Market St", billing_city="Xtown", billing_state="OH", billing_zip="44000", phone="555-111-1111")
    out = build_proposals([loc(name="Bellhaven at Union Square", street="118 Union Square Dr")], [PARENT, OTHER, a])
    assert types(out) == ["CREATE"]
    assert out["proposals"][0]["near_misses"][0]["name"] == "Union Square Senior Living"


def test_not_on_site_flags_needs_review_not_inactive():
    a = acct(name="Bellhaven of Gone", billing_city="Gone", billing_state="OH", billing_zip="40000", billing_street="9 Elm St")
    out = build_proposals([loc()], [PARENT, a, acct(account_id="Z", name="Bellhaven of X", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000")])
    ns = [p for p in out["proposals"] if p["type"] == "NOT_ON_SITE"]
    assert len(ns) == 1 and ns[0]["actions"][0]["payload"] == {"status": "Needs Review"}


def test_superseded_accounts_are_never_rematched():
    old = acct(account_id="OLD", name="Bellhaven of X", parent_id="P2", parent_name="Cedar Trail", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000", lifetime_revenue=1, outstanding_ar=1, chow_current_account="NEW")
    new = acct(account_id="NEW", name="Bellhaven of X", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000", phone="555-000-0000")
    out = build_proposals([loc()], [PARENT, OTHER, old, new])
    assert types(out) == [] and len(out["confirmed"]) == 1


def test_proposal_ids_are_stable_across_runs():
    crm = [PARENT, acct(name="Sunny Acres", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000")]
    a, b = build_proposals([loc()], crm), build_proposals([loc()], crm)
    assert [p["id"] for p in a["proposals"]] == [p["id"] for p in b["proposals"]]


def test_score_penalises_two_independent_contradictions():
    s_same, _ = score_pair(loc(), acct(billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000", phone="555-000-0000"))
    s_conf, _ = score_pair(loc(), acct(billing_street="77 Other Rd", billing_city="Xtown", billing_state="OH", billing_zip="44000", phone="555-999-9999"))
    assert s_same >= 0.8 and s_conf < 0.55


# --- fixes from the code review -------------------------------------------------

def test_street_short_forms_normalize():
    from pipeline.normalize import norm_street
    assert norm_street("3313 Wilmington Pk") == norm_street("3313 Wilmington Pike")
    assert norm_street("12 Grand Av") == norm_street("12 Grand Avenue")


def test_chow_successor_outranks_old_copy_with_revenue():
    old = acct(account_id="OLD", name="Bellhaven of X", parent_id="P2", parent_name="Cedar Trail", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000", lifetime_revenue=50000, outstanding_ar=1200, chow_current_account="NEW")
    new = acct(account_id="NEW", name="Bellhaven of X", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000", phone="555-000-0000")
    b = acct(account_id="B", name="Bellhaven of X", parent_id="P2", parent_name="Cedar Trail", billing_street="1 Main Street", billing_city="Xtown", billing_state="OH", billing_zip="44000", lifetime_revenue=9000)
    out = build_proposals([loc()], [PARENT, OTHER, old, new, b])
    dups = [p for p in out["proposals"] if p["type"] == "DUPLICATE"]
    assert [d["account"]["account_id"] for d in dups] == ["B"] and dups[0]["survivor"]["account_id"] == "NEW"


def test_low_confidence_match_asks_for_confirmation_first():
    a = acct(name="Sunny Acres", parent_id="P2", parent_name="Other", billing_street="1 Main Ave", billing_city="Xtown", billing_state="OH", billing_zip="44000")
    out = build_proposals([loc()], [PARENT, OTHER, a])
    assert types(out) == ["CONFIRM_MATCH"] and out["proposals"][0]["actions"] == []
    key = out["proposals"][0]["subject_key"]
    # approved link -> the field fixes appear; rejected link -> the account is excluded and CREATE fires
    out2 = build_proposals([loc()], [PARENT, OTHER, a], links={key: "approved"})
    assert "REPARENT" in types(out2) and "CONFIRM_MATCH" not in types(out2)
    out3 = build_proposals([loc()], [PARENT, OTHER, a], links={key: "rejected"})
    assert types(out3) == ["CREATE"]


def test_chow_survivor_defers_duplicates_to_next_run():
    old = acct(account_id="OLD", name="Old Name", parent_id="P2", parent_name="Cedar Trail", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000", lifetime_revenue=50000, outstanding_ar=1200)
    b = acct(account_id="B", name="Bellhaven of X", billing_street="1 Main Street", billing_city="Xtown", billing_state="OH", billing_zip="44000")
    out = build_proposals([loc()], [PARENT, OTHER, old, b])
    assert types(out) == ["CHOW"] and "B" in out["proposals"][0]["attention"]
    # next run: old superseded, successor present -> plain DUPLICATE with a concrete id
    old["chow_current_account"] = "NEW"
    new = acct(account_id="NEW", name="Bellhaven of X", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000", phone="555-000-0000")
    out2 = build_proposals([loc()], [PARENT, OTHER, old, new, b])
    dups = [p for p in out2["proposals"] if p["type"] == "DUPLICATE"]
    assert dups and dups[0]["changes"][0]["to"] == "NEW"


def test_incomplete_website_address_is_never_written():
    a = acct(name="Bellhaven of X", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000", phone="555-000-0000")
    out = build_proposals([loc(city="", state="", zip="")], [PARENT, a])
    assert "UPDATE_ADDRESS" not in types(out)
    out2 = build_proposals([loc(street="9 Elm St", city="", state="", zip="", phone="555-111-1111")], [PARENT, a])
    creates = [p for p in out2["proposals"] if p["type"] == "CREATE"]
    assert creates and creates[0]["attention"]


def test_parent_override_errors_are_descriptive(monkeypatch):
    from pipeline import config
    from pipeline.match import find_parent
    monkeypatch.setattr(config, "PARENT_ID_OVERRIDE", "NOPE")
    with pytest.raises(RuntimeError, match="NOPE"):
        find_parent([PARENT])
    monkeypatch.setattr(config, "PARENT_ID_OVERRIDE", "A")
    with pytest.raises(RuntimeError, match="not a parent"):
        find_parent([PARENT, acct(account_id="A", name="Facility")])


def test_proposal_id_tracks_the_write_payload(monkeypatch):
    # A CREATE's id must change when the address it would write changes, but not
    # when only the dated note line changes.
    import pipeline.match as m
    crm = [PARENT]
    a = build_proposals([loc(street="118 Union Square Dr")], crm)["proposals"][0]
    b = build_proposals([loc(street="999 Different Rd")], crm)["proposals"][0]
    assert a["type"] == b["type"] == "CREATE" and a["id"] != b["id"]
    monkeypatch.setattr(m, "today", lambda: "2031-01-01")
    c = build_proposals([loc(street="118 Union Square Dr")], crm)["proposals"][0]
    assert c["id"] == a["id"]
    # same for the successor a CHOW would create
    old = acct(name="Bellhaven of X", parent_id="P2", parent_name="Cedar Trail", billing_street="1 Main St", billing_city="Xtown", billing_state="OH", billing_zip="44000", lifetime_revenue=50000, outstanding_ar=1200)
    x = build_proposals([loc(care_offerings=["Assisted Living"])], [PARENT, OTHER, old])["proposals"][0]
    y = build_proposals([loc(care_offerings=["Memory Support"])], [PARENT, OTHER, old])["proposals"][0]
    assert x["type"] == y["type"] == "CHOW" and x["id"] != y["id"]
