import sys
from types import SimpleNamespace

from reviewer import serve


def test_reference_candidates_extracts_numbered_references():
    text = """## References

[1] Ada Lovelace and Alan Turing. Computing machinery for proofs. 2024.

[2] Grace Hopper. Reliable compilers for theorem proving. 2023.
"""

    refs = serve._reference_candidates(text)

    assert len(refs) == 2
    assert "Computing machinery" in refs[0]
    assert "Reliable compilers" in refs[1]


def test_extract_reference_text_prefers_references_heading():
    paragraphs = [
        {"index": 0, "text": "Main paper body."},
        {"index": 1, "text": "## References"},
        {"index": 2, "text": "[1] Prior Work. 2024."},
    ]

    text = serve._extract_reference_text(paragraphs)

    assert "Main paper body" not in text
    assert "Prior Work" in text


def test_connected_papers_api_key_accepts_camel_case_alias(monkeypatch):
    monkeypatch.delenv("CONNECTED_PAPERS_API_KEY", raising=False)
    monkeypatch.setenv("ConnectedPapers_API_KEY", "camel-case-key")

    assert serve._connected_papers_api_key() == "camel-case-key"


def test_connected_papers_uses_python_client(monkeypatch):
    class FakeClient:
        def __init__(self, access_token):
            self.access_token = access_token

        def get_graph_sync(self, paper_id):
            assert self.access_token == "key"
            assert paper_id == "paper-1"
            return SimpleNamespace(
                status="OLD_GRAPH",
                remaining_requests=4,
                graph_json=SimpleNamespace(
                    common_references=[
                        SimpleNamespace(id="ref-1", title="Foundational Work", year=2020, authors=["Ada"])
                    ],
                    common_citations=[
                        {"paperId": "cite-1", "title": "Follow Up", "year": 2024, "authors": ["Grace"]}
                    ],
                ),
            )

    fake_module = SimpleNamespace(ConnectedPapersClient=FakeClient)
    monkeypatch.setitem(sys.modules, "connectedpapers", fake_module)
    monkeypatch.setenv("CONNECTED_PAPERS_API_KEY", "key")

    payload = serve._connected_papers(["paper-1"])

    assert payload["available"] is True
    assert payload["seedPaperId"] == "paper-1"
    assert payload["common_references"][0]["paperId"] == "ref-1"
    assert payload["common_citations"][0]["paperId"] == "cite-1"
