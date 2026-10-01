import asyncio

from app import config
from app.services import legal_service


def _clear():
    legal_service.load_documents.cache_clear()


def test_hash_is_independent_of_key_order():
    assert legal_service.canonical_hash({"a": 1, "b": "é"}) == legal_service.canonical_hash({"b": "é", "a": 1})
    assert legal_service.canonical_hash({"a": 1}) != legal_service.canonical_hash({"a": 2})


def test_render_fills_nested_strings_and_keeps_empty_placeholders():
    raw = {"t": "{OPERATOR_NAME}", "rows": [["x {TUNNEL_PROVIDER}"]], "n": 3}
    out = legal_service.render(raw, {"OPERATOR_NAME": "Pearl's family", "TUNNEL_PROVIDER": ""})
    assert out == {"t": "Pearl's family", "rows": [["x {TUNNEL_PROVIDER}"]], "n": 3}
    assert legal_service.has_placeholders(out)
    assert not legal_service.has_placeholders(legal_service.render(raw, {"OPERATOR_NAME": "a", "TUNNEL_PROVIDER": "b"}))


def test_every_document_loads_and_languages_have_identical_structure():
    _clear()
    docs = legal_service.load_documents()
    assert set(docs) == {(k, lang) for k in legal_service.KIND_SCOPES for lang in legal_service.LANGUAGES}
    for kind in legal_service.KIND_SCOPES:
        en, zh = docs[(kind, "en")].document, docs[(kind, "zh-TW")].document
        assert [s["id"] for s in en["sections"]] == [s["id"] for s in zh["sections"]]
        for s_en, s_zh in zip(en["sections"], zh["sections"]):
            assert [b["type"] for b in s_en["blocks"]] == [b["type"] for b in s_zh["blocks"]]
            for b_en, b_zh in zip(s_en["blocks"], s_zh["blocks"]):
                if b_en["type"] == "list":
                    assert len(b_en["items"]) == len(b_zh["items"])
                if b_en["type"] == "table":
                    assert len(b_en["header"]) == len(b_zh["header"])
                    assert [len(r) for r in b_en["rows"]] == [len(r) for r in b_zh["rows"]]
        assert len(en["what_changed"]) == len(zh["what_changed"])


def test_core_privacy_section_has_anchor_id_data():
    _clear()
    for lang in legal_service.LANGUAGES:
        assert "data" in [s["id"] for s in legal_service.get_document("core", lang).document["sections"]]


def test_completeness_follows_fill_ins(monkeypatch):
    for name in ("OPERATOR_NAME", "OPERATOR_CONTACT", "TUNNEL_PROVIDER"):
        monkeypatch.setattr(config, name, "")
    _clear()
    assert not legal_service.get_document("core", "en").complete
    for name in ("OPERATOR_NAME", "OPERATOR_CONTACT", "TUNNEL_PROVIDER"):
        monkeypatch.setattr(config, name, "x")
    _clear()
    filled = legal_service.get_document("core", "en")
    assert filled.complete
    assert "{OPERATOR_NAME}" not in str(filled.document)
    _clear()


def test_changing_a_fill_in_changes_the_hash(monkeypatch):
    monkeypatch.setattr(config, "OPERATOR_NAME", "A")
    _clear()
    first = legal_service.get_document("core", "en").sha256
    monkeypatch.setattr(config, "OPERATOR_NAME", "B")
    _clear()
    assert legal_service.get_document("core", "en").sha256 != first
    _clear()


def test_normalise_language():
    assert legal_service.normalise_language("zh") == "zh-TW"
    assert legal_service.normalise_language("zh-tw") == "zh-TW"
    assert legal_service.normalise_language("zh-TW") == "zh-TW"
    assert legal_service.normalise_language("en-US") == "en"
    assert legal_service.normalise_language(None) == "en"


def test_register_documents_inserts_each_variant():
    _clear()
    calls = []

    class Conn:
        async def execute(self, sql, *args):
            calls.append(args)

    asyncio.run(legal_service.register_documents(Conn()))
    assert len(calls) == len(legal_service.KIND_SCOPES) * len(legal_service.LANGUAGES)
    assert all(len(args[3]) == 64 for args in calls)
