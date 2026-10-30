from app.ai.retranslate import en_entries

GLOSSARY = [
    {"source": "이연희", "target": "Lee Yeon-hee", "category": "character"},
    {"source": "흑뇌", "target": "Hắc Lôi", "category": "skill"},
    {"source": "팬틀러 기업", "target": "Pantler Corporation"},
    {"source": "팬틀러", "target": "Pantler"},
    {"source": "Nhanh nhẹn", "target": "Linh hoạt"},          # a Vietnamese “wrong rendering” rule, not a Korean term
]
BRIDGE = {"aliases": {"흑뇌": {"en": "Black Lightning", "votes": 4}, "팬틀러 기업": {"en": "Pantler", "votes": 3},
                      "팬틀러": {"en": "Pantler", "votes": 1}},
          "extra": [{"source": "Arachne", "target": "Arachne"}]}


def test_en_entries_find_terms_by_english_name():
    text = "Lee Yeonhee frowned. Black lightning crackled over Pantler's tower while the arachne fled."
    got = {e["source"]: e for e in en_entries(GLOSSARY, BRIDGE, text)}
    assert got["Lee Yeon-hee"]["target"] == "Lee Yeon-hee" and got["Lee Yeon-hee"]["ko"] == "이연희"   # hyphen-less spelling
    assert got["Black Lightning"]["target"] == "Hắc Lôi"                                           # alias, case-insensitive
    assert got["Pantler"]["target"] == "Pantler Corporation"                                       # one name: most votes wins
    assert got["Arachne"]["target"] == "Arachne"                                                   # term from an earlier EN chapter
    assert len(got) == 4                                                                            # VI rule entry not matched
    assert en_entries(GLOSSARY, BRIDGE, "Nothing related here.") == []


def test_en_entries_accept_common_alternative_spellings():
    bridge = {"aliases": {"흑뇌": {"en": "Black Lightning", "votes": 40, "counts": {"Black Lightning": 40, "Dark Thunder": 9, "Blak": 1}}}}
    assert [e["target"] for e in en_entries(GLOSSARY, bridge, "Dark Thunder struck.")] == ["Hắc Lôi"]
    assert en_entries(GLOSSARY, bridge, "Blak struck.") == []          # a one-off spelling is ignored
