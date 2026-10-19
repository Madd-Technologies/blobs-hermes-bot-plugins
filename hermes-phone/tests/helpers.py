import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SETTINGS = {"name": "Alex", "never_share": [{"label": "Home address", "value": "12 Orchard Lane, Pinecrest"},
                                            {"label": "Personal cell", "value": "(202) 555-0199"}],
            "do_not_call": ["202-555-0177"]}
CALLBACK = "+12025550123"


def septic(**extra):
    """The plan's example errand: three quotes for a septic pump-out."""
    raw = {
        "title": "Septic pump-out quotes",
        "mode": "quote",
        "goal": "Get a quote for pumping a residential septic tank.",
        "opening": "I'm calling to get a quote for pumping a septic tank.",
        "facts": [
            {"label": "Service area", "value": "Pinecrest, ZIP 00000"},
            {"label": "Home", "value": "4 bedrooms; tank probably 1,500 gallons", "estimate": True},
            {"label": "Availability", "value": "Weekdays after 10 am, Sunday afternoons"},
        ],
        "questions": ["Base pump-out price", "Disposal fee", "Locating or digging fee if the lid is buried", "Earliest date"],
        "record": [{"key": "base_price", "label": "Base pump-out price"}, {"key": "disposal_fee", "label": "Disposal fee"},
                   {"key": "earliest", "label": "Earliest date"}],
        "notes": "County records: built 2011, 4 bed, so likely 1,500 gal.",
        "calls": [{"name": "Pine Valley Septic", "number": "(202) 555-0101", "source": "their website", "why": "4.8 stars"},
                  {"name": "Harbor Pumping", "number": "1-202-555-0102"}],
    }
    raw.update(extra)
    return raw
