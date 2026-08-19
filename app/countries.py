"""Static country reference data for the policyholder's citizenship field.

English common names -- this is what OCR is asked to normalize citizenship
to (see app.ocr.provider's prompt) and what /policyholder's citizenship
<select> offers. match_citizenship_text() decides whether an OCR-read
string safely corresponds to exactly one of these: same conservative
philosophy as app.ocr.parser's manufacturer/model matching (exact match
after normalization, or a high-confidence fuzzy match with a clear margin
over the runner-up) -- anything less certain returns None rather than
guessing or inventing a new country, per the task's explicit requirement.
"""

import difflib

COUNTRIES: tuple[str, ...] = (
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Antigua and Barbuda", "Argentina", "Armenia",
    "Australia", "Austria", "Azerbaijan", "Bahamas", "Bahrain", "Bangladesh", "Barbados", "Belarus", "Belgium",
    "Belize", "Benin", "Bhutan", "Bolivia", "Bosnia and Herzegovina", "Botswana", "Brazil", "Brunei", "Bulgaria",
    "Burkina Faso", "Burundi", "Cabo Verde", "Cambodia", "Cameroon", "Canada", "Central African Republic", "Chad",
    "Chile", "China", "Colombia", "Comoros", "Congo", "Costa Rica", "Cote d'Ivoire", "Croatia", "Cuba", "Cyprus",
    "Czechia", "Denmark", "Djibouti", "Dominica", "Dominican Republic", "Ecuador", "Egypt", "El Salvador",
    "Equatorial Guinea", "Eritrea", "Estonia", "Eswatini", "Ethiopia", "Fiji", "Finland", "France", "Gabon",
    "Gambia", "Georgia", "Germany", "Ghana", "Greece", "Grenada", "Guatemala", "Guinea", "Guinea-Bissau", "Guyana",
    "Haiti", "Honduras", "Hungary", "Iceland", "India", "Indonesia", "Iran", "Iraq", "Ireland", "Israel", "Italy",
    "Jamaica", "Japan", "Jordan", "Kazakhstan", "Kenya", "Kiribati", "Kosovo", "Kuwait", "Kyrgyzstan", "Laos",
    "Latvia", "Lebanon", "Lesotho", "Liberia", "Libya", "Liechtenstein", "Lithuania", "Luxembourg", "Madagascar",
    "Malawi", "Malaysia", "Maldives", "Mali", "Malta", "Marshall Islands", "Mauritania", "Mauritius", "Mexico",
    "Micronesia", "Moldova", "Monaco", "Mongolia", "Montenegro", "Morocco", "Mozambique", "Myanmar", "Namibia",
    "Nauru", "Nepal", "Netherlands", "New Zealand", "Nicaragua", "Niger", "Nigeria", "North Korea",
    "North Macedonia", "Norway", "Oman", "Pakistan", "Palau", "Palestine", "Panama", "Papua New Guinea",
    "Paraguay", "Peru", "Philippines", "Poland", "Portugal", "Qatar", "Romania", "Russia", "Rwanda",
    "Saint Kitts and Nevis", "Saint Lucia", "Saint Vincent and the Grenadines", "Samoa", "San Marino",
    "Sao Tome and Principe", "Saudi Arabia", "Senegal", "Serbia", "Seychelles", "Sierra Leone", "Singapore",
    "Slovakia", "Slovenia", "Solomon Islands", "Somalia", "South Africa", "South Korea", "South Sudan", "Spain",
    "Sri Lanka", "Sudan", "Suriname", "Sweden", "Switzerland", "Syria", "Taiwan", "Tajikistan", "Tanzania",
    "Thailand", "Timor-Leste", "Togo", "Tonga", "Trinidad and Tobago", "Tunisia", "Turkey", "Turkmenistan",
    "Tuvalu", "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom", "United States", "Uruguay",
    "Uzbekistan", "Vanuatu", "Vatican City", "Venezuela", "Vietnam", "Yemen", "Zambia", "Zimbabwe",
)

# A small, deliberately short list of well-known OFFICIAL/ISO names that
# differ from the common name this module otherwise offers -- NOT a general
# alias mechanism, just enough to recognize the same country's formal name
# without silently "inventing" a country that isn't in COUNTRIES. Anything
# not listed here still goes through the normal fuzzy match below.
_KNOWN_ALIASES: dict[str, str] = {
    "russian federation": "Russia",
    "republic of korea": "South Korea",
    "korea, republic of": "South Korea",
    "democratic people's republic of korea": "North Korea",
    "czech republic": "Czechia",
    "ivory coast": "Cote d'Ivoire",
    "united states of america": "United States",
    "usa": "United States",
    "great britain": "United Kingdom",
    "uk": "United Kingdom",
    "people's republic of china": "China",
    "republic of moldova": "Moldova",
    "kingdom of the netherlands": "Netherlands",
}

_FUZZY_MATCH_THRESHOLD = 0.92
_FUZZY_MATCH_MARGIN = 0.05  # best match must clear the runner-up by this much


def _normalize(value: str) -> str:
    return " ".join(value.split()).strip().casefold()


def match_citizenship_text(text: str | None) -> str | None:
    """Returns the exact COUNTRIES entry text safely corresponds to, or
    None -- never a guess, and never a country outside this fixed list."""
    if not text:
        return None
    target = _normalize(text)
    if not target:
        return None

    alias = _KNOWN_ALIASES.get(target)
    if alias is not None:
        return alias

    normalized = [(_normalize(country), country) for country in COUNTRIES]

    exact = [country for norm, country in normalized if norm == target]
    if len(exact) == 1:
        return exact[0]

    scored = sorted(
        ((difflib.SequenceMatcher(None, target, norm).ratio(), country) for norm, country in normalized),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best_country = scored[0]
    if best_score < _FUZZY_MATCH_THRESHOLD:
        return None
    if len(scored) > 1 and (best_score - scored[1][0]) < _FUZZY_MATCH_MARGIN:
        return None  # too close to the runner-up to be confident
    return best_country
