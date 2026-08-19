from app.countries import COUNTRIES, match_citizenship_text


def test_exact_match():
    assert match_citizenship_text("Georgia") == "Georgia"


def test_case_and_whitespace_insensitive_exact_match():
    assert match_citizenship_text("  georgia  ") == "Georgia"
    assert match_citizenship_text("RUSSIA") == "Russia"


def test_known_official_name_alias():
    assert match_citizenship_text("Russian Federation") == "Russia"
    assert match_citizenship_text("Czech Republic") == "Czechia"
    assert match_citizenship_text("USA") == "United States"


def test_none_or_empty_never_matches():
    assert match_citizenship_text(None) is None
    assert match_citizenship_text("") is None
    assert match_citizenship_text("   ") is None


def test_unrecognizable_text_returns_none_never_a_guess():
    assert match_citizenship_text("Not A Real Country Xyz123") is None


def test_every_entry_matches_itself():
    for country in COUNTRIES:
        assert match_citizenship_text(country) == country


def test_never_returns_a_country_outside_the_fixed_list():
    for text in ["Georgia", "Russian Federation", "Not A Real Country"]:
        result = match_citizenship_text(text)
        assert result is None or result in COUNTRIES
