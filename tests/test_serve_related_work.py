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
