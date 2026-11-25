"""Write phone-normalization fixtures from the desktop rules, for the Kotlin and Swift cores' parity tests.

    python tools/make_phone_fixtures.py > phone_fixtures.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from redtooth.phones import COUNTRIES, normalize  # noqa: E402

CASES = [
    ("(555) 014-2000", "US"), ("555.014.2000", "CA"), ("1 555 014 2000", "US"), ("+1 (555) 014-2000", "GB"),
    ("15550100", "US"), ("0049 30 1234567", "US"), ("011 44 20 7946 0958", "US"), ("+44 20 7946 0958", "US"),
    ("020 7946 0958", "GB"), ("07700 900123", "GB"), ("44 7700 900123", "GB"), ("00 1 555 014 2000", "GB"),
    ("06 12 34 56 78", "FR"), ("030 1234567", "DE"), ("49 30 1234567", "DE"), ("06 1234 5678", "IT"),
    ("392 123 4567", "IT"), ("612 34 56 78", "ES"), ("8 (912) 345-67-89", "RU"), ("810 49 30 1234567", "RU"),
    ("06 1 234 5678", "HU"), ("02 9876 5432", "AU"), ("0011 1 555 014 2000", "AU"), ("090-1234-5678", "JP"),
    ("010 1 555 014 2000", "JP"), ("010-1234-5678", "KR"), ("98765 43210", "IN"), ("0712 345678", "KE"),
    ("000 44 20 7946 0958", "KE"), ("55 1234 5678", "MX"), ("9123 4567", "SG"), ("0912 345 678", "TW"),
    ("tel:+33612345678", "US"), ("", "US"), ("n/a", "US"),
]
cases = [{"raw": r, "country": c, "e164": normalize(r, c)} for r, c in CASES]
table = [{"iso": c.iso, "name": c.name, "cc": c.cc, "trunk": c.trunk, "idd": c.idd} for c in COUNTRIES]
json.dump({"cases": cases, "countries": table}, sys.stdout, ensure_ascii=False, indent=1)
