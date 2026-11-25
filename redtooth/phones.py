"""Phone numbers: one canonical form (E.164, "+15550142000") on every platform.

A number written with "+" or an international prefix is already unambiguous. A number written the
way people dial at home ("(555) 014-2000", "020 7946 0958", "06 12 34 56 78") needs a country: that
comes from the user's setting (detected from the computer's region, changeable at setup or in the
panel). Each user normalizes *their own* contacts with *their own* country, and everything that
crosses the network is already E.164, so two people in different countries still agree on a number.

The rules here are mirrored exactly by the Android (Kotlin) and iOS (Swift) cores; the shared test
fixtures come from tools/make_phone_fixtures.py.
"""
from __future__ import annotations

import ctypes
import locale
import os
import re
import sys
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Country:
    iso: str  # ISO 3166-1 alpha-2
    name: str
    cc: str  # calling code, digits only
    trunk: str = "0"  # national prefix dropped in international form ("" = none, e.g. Italy keeps its 0)
    idd: str = "00"  # prefix for dialing abroad from there


# Calling code, national (trunk) prefix and international prefix per country.
COUNTRIES: list[Country] = [
    Country("US", "United States", "1", "1", "011"),
    Country("CA", "Canada", "1", "1", "011"),
    Country("PR", "Puerto Rico", "1", "1", "011"),
    Country("JM", "Jamaica", "1", "1", "011"),
    Country("TT", "Trinidad and Tobago", "1", "1", "011"),
    Country("BS", "Bahamas", "1", "1", "011"),
    Country("BB", "Barbados", "1", "1", "011"),
    Country("DO", "Dominican Republic", "1", "1", "011"),
    Country("MX", "Mexico", "52", ""),
    Country("GT", "Guatemala", "502", ""),
    Country("SV", "El Salvador", "503", ""),
    Country("HN", "Honduras", "504", ""),
    Country("NI", "Nicaragua", "505", ""),
    Country("CR", "Costa Rica", "506", ""),
    Country("PA", "Panama", "507", ""),
    Country("CU", "Cuba", "53", "0", "119"),
    Country("HT", "Haiti", "509", ""),
    Country("CO", "Colombia", "57", "", "009"),
    Country("VE", "Venezuela", "58"),
    Country("EC", "Ecuador", "593"),
    Country("PE", "Peru", "51"),
    Country("BO", "Bolivia", "591", "0", "0010"),
    Country("BR", "Brazil", "55", "0", "0014"),
    Country("PY", "Paraguay", "595"),
    Country("UY", "Uruguay", "598"),
    Country("AR", "Argentina", "54"),
    Country("CL", "Chile", "56", "", "00"),
    Country("GB", "United Kingdom", "44"),
    Country("IE", "Ireland", "353"),
    Country("FR", "France", "33"),
    Country("BE", "Belgium", "32"),
    Country("NL", "Netherlands", "31"),
    Country("LU", "Luxembourg", "352", ""),
    Country("DE", "Germany", "49"),
    Country("AT", "Austria", "43"),
    Country("CH", "Switzerland", "41"),
    Country("IT", "Italy", "39", ""),
    Country("SM", "San Marino", "378", ""),
    Country("VA", "Vatican City", "379", ""),
    Country("MT", "Malta", "356", ""),
    Country("ES", "Spain", "34", ""),
    Country("PT", "Portugal", "351", ""),
    Country("AD", "Andorra", "376", ""),
    Country("MC", "Monaco", "377", ""),
    Country("DK", "Denmark", "45", ""),
    Country("NO", "Norway", "47", ""),
    Country("SE", "Sweden", "46"),
    Country("FI", "Finland", "358"),
    Country("IS", "Iceland", "354", ""),
    Country("EE", "Estonia", "372", ""),
    Country("LV", "Latvia", "371", ""),
    Country("LT", "Lithuania", "370", "0"),
    Country("PL", "Poland", "48", ""),
    Country("CZ", "Czechia", "420", ""),
    Country("SK", "Slovakia", "421"),
    Country("HU", "Hungary", "36", "06"),
    Country("SI", "Slovenia", "386"),
    Country("HR", "Croatia", "385"),
    Country("BA", "Bosnia and Herzegovina", "387"),
    Country("RS", "Serbia", "381"),
    Country("ME", "Montenegro", "382"),
    Country("MK", "North Macedonia", "389"),
    Country("AL", "Albania", "355"),
    Country("XK", "Kosovo", "383"),
    Country("GR", "Greece", "30", ""),
    Country("CY", "Cyprus", "357", ""),
    Country("BG", "Bulgaria", "359"),
    Country("RO", "Romania", "40"),
    Country("MD", "Moldova", "373"),
    Country("UA", "Ukraine", "380"),
    Country("BY", "Belarus", "375", "8", "810"),
    Country("RU", "Russia", "7", "8", "810"),
    Country("KZ", "Kazakhstan", "7", "8", "810"),
    Country("GE", "Georgia", "995"),
    Country("AM", "Armenia", "374"),
    Country("AZ", "Azerbaijan", "994"),
    Country("TR", "Turkey", "90"),
    Country("IL", "Israel", "972"),
    Country("PS", "Palestine", "970"),
    Country("LB", "Lebanon", "961"),
    Country("JO", "Jordan", "962"),
    Country("SY", "Syria", "963"),
    Country("IQ", "Iraq", "964"),
    Country("IR", "Iran", "98"),
    Country("SA", "Saudi Arabia", "966"),
    Country("AE", "United Arab Emirates", "971"),
    Country("QA", "Qatar", "974", ""),
    Country("BH", "Bahrain", "973", ""),
    Country("KW", "Kuwait", "965", ""),
    Country("OM", "Oman", "968", ""),
    Country("YE", "Yemen", "967"),
    Country("EG", "Egypt", "20"),
    Country("MA", "Morocco", "212"),
    Country("DZ", "Algeria", "213"),
    Country("TN", "Tunisia", "216", ""),
    Country("LY", "Libya", "218"),
    Country("NG", "Nigeria", "234", "0", "009"),
    Country("GH", "Ghana", "233"),
    Country("SN", "Senegal", "221", ""),
    Country("CI", "Côte d’Ivoire", "225", ""),
    Country("CM", "Cameroon", "237", ""),
    Country("KE", "Kenya", "254", "0", "000"),
    Country("UG", "Uganda", "256", "0", "000"),
    Country("TZ", "Tanzania", "255", "0", "000"),
    Country("RW", "Rwanda", "250", ""),
    Country("ET", "Ethiopia", "251"),
    Country("SO", "Somalia", "252"),
    Country("SD", "Sudan", "249"),
    Country("CD", "DR Congo", "243"),
    Country("AO", "Angola", "244", ""),
    Country("ZM", "Zambia", "260"),
    Country("ZW", "Zimbabwe", "263"),
    Country("MZ", "Mozambique", "258", ""),
    Country("NA", "Namibia", "264"),
    Country("BW", "Botswana", "267", ""),
    Country("ZA", "South Africa", "27"),
    Country("MU", "Mauritius", "230", ""),
    Country("MG", "Madagascar", "261"),
    Country("PK", "Pakistan", "92"),
    Country("AF", "Afghanistan", "93"),
    Country("IN", "India", "91"),
    Country("BD", "Bangladesh", "880"),
    Country("LK", "Sri Lanka", "94"),
    Country("NP", "Nepal", "977"),
    Country("MV", "Maldives", "960", ""),
    Country("UZ", "Uzbekistan", "998", "", "00"),
    Country("KG", "Kyrgyzstan", "996"),
    Country("TJ", "Tajikistan", "992", "", "810"),
    Country("TM", "Turkmenistan", "993", "8", "810"),
    Country("MN", "Mongolia", "976", "0", "001"),
    Country("CN", "China", "86"),
    Country("HK", "Hong Kong", "852", "", "001"),
    Country("MO", "Macau", "853", ""),
    Country("TW", "Taiwan", "886", "0", "002"),
    Country("JP", "Japan", "81", "0", "010"),
    Country("KR", "South Korea", "82", "0", "001"),
    Country("VN", "Vietnam", "84"),
    Country("TH", "Thailand", "66", "0", "001"),
    Country("KH", "Cambodia", "855", "0", "001"),
    Country("LA", "Laos", "856"),
    Country("MM", "Myanmar", "95"),
    Country("MY", "Malaysia", "60"),
    Country("SG", "Singapore", "65", "", "001"),
    Country("ID", "Indonesia", "62", "0", "001"),
    Country("PH", "Philippines", "63"),
    Country("BN", "Brunei", "673", ""),
    Country("AU", "Australia", "61", "0", "0011"),
    Country("NZ", "New Zealand", "64"),
    Country("FJ", "Fiji", "679", ""),
    Country("PG", "Papua New Guinea", "675", ""),
]
BY_ISO = {c.iso: c for c in COUNTRIES}
DEFAULT_COUNTRY = "US"

_current = os.environ.get("REDTOOTH_COUNTRY", "").upper() or DEFAULT_COUNTRY


def set_country(iso: Optional[str]) -> str:
    """Country used for numbers written without a country code. Unknown codes leave it unchanged."""
    global _current
    if iso and iso.upper() in BY_ISO:
        _current = iso.upper()
    return _current


def current_country() -> Country:
    return BY_ISO.get(_current, BY_ISO[DEFAULT_COUNTRY])


def normalize(raw: str, country: Optional[str] = None) -> str:
    """E.164 for any way a number is written. Same algorithm in Phones.kt and Phones.swift."""
    if not raw:
        return ""
    c = BY_ISO.get((country or _current).upper()) or BY_ISO[DEFAULT_COUNTRY]
    m = re.search(r"[+\d]", raw)  # skip labels like "tel:" or "Mobile: "
    s = raw[m.start():] if m else ""
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if s.startswith("+"):
        return "+" + digits
    # Dialed abroad from this country: its own international prefix, or 00, which most of the world uses.
    for idd in (c.idd, "00"):
        if idd and digits.startswith(idd) and len(digits) > len(idd) + 6:
            return "+" + digits[len(idd):]
    if c.cc == "1":  # North America: 10 digits, or 1 + 10 digits
        if len(digits) == 11 and digits.startswith("1"):
            return "+" + digits
        return "+1" + digits
    if c.trunk and digits.startswith(c.trunk):
        return "+" + c.cc + digits[len(c.trunk):]
    # Written with the country code but no "+" (e.g. "44 7700 900123") in a country whose
    # national numbers always start with the trunk prefix.
    if c.trunk and digits.startswith(c.cc) and len(digits) >= len(c.cc) + 8:
        return "+" + digits
    return "+" + c.cc + digits


def country_for_phone(e164: str, prefer: Optional[str] = None) -> Optional[str]:
    """Best guess at a country from an E.164 number (+44… → GB). Shared codes (+1, +7) prefer `prefer`."""
    digits = e164.lstrip("+")
    hits = [c for c in COUNTRIES if digits.startswith(c.cc)]
    if not hits:
        return None
    longest = max(len(c.cc) for c in hits)
    hits = [c for c in hits if len(c.cc) == longest]
    if prefer and any(c.iso == prefer.upper() for c in hits):
        return prefer.upper()
    return hits[0].iso


def detect_country() -> str:
    """The computer's region setting (Windows: Region › Country or region; macOS/Linux: locale)."""
    if sys.platform.startswith("win"):
        try:
            buf = ctypes.create_unicode_buffer(16)
            if ctypes.windll.kernel32.GetUserDefaultGeoName(buf, 16) and buf.value.upper() in BY_ISO:  # Windows 10 1709+
                return buf.value.upper()
        except Exception:
            pass
    if sys.platform == "darwin":
        try:
            import subprocess

            out = subprocess.run(["defaults", "read", "-g", "AppleLocale"], capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"_([A-Z]{2})", out)
            if m and m.group(1) in BY_ISO:
                return m.group(1)
        except Exception:
            pass
    for v in (os.environ.get("LC_ALL"), os.environ.get("LC_CTYPE"), os.environ.get("LANG"), _locale_name()):
        m = re.search(r"[_-]([A-Z]{2})\b", v or "")
        if m and m.group(1) in BY_ISO:
            return m.group(1)
        # Windows locale names: "English_United States"
        if v and "_" in v:
            name = v.split("_", 1)[1].split(".")[0].lower()
            for c in COUNTRIES:
                if c.name.lower() == name:
                    return c.iso
    return DEFAULT_COUNTRY


def _locale_name() -> str:
    try:
        return locale.getlocale()[0] or ""
    except Exception:
        return ""


def parse_country(text: str, default: Optional[str] = None) -> Optional[str]:
    """Accept "GB", "gb", "United Kingdom", "uk", "+44", "44"."""
    t = text.strip()
    if not t:
        return default
    u = t.upper()
    if u in BY_ISO:
        return u
    if u in ("UK", "ENGLAND", "SCOTLAND", "WALES", "BRITAIN"):
        return "GB"
    if u in ("USA", "AMERICA", "UNITED STATES OF AMERICA"):
        return "US"
    if re.fullmatch(r"\+?\d{1,3}", t):
        return country_for_phone("+" + t.lstrip("+") + "0000000", prefer=default)
    low = t.lower()
    for c in COUNTRIES:
        if c.name.lower() == low:
            return c.iso
    for c in COUNTRIES:
        if c.name.lower().startswith(low):
            return c.iso
    return None


def flag(iso: str) -> str:
    return "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in iso.upper()) if len(iso) == 2 else ""
