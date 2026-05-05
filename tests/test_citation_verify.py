"""Unit tests for the CiteVerify adapter."""

from types import SimpleNamespace

from reviewer.citation_verify import _normalize_for_citeverify, review_citations


def test_normalize_for_citeverify_promotes_references_heading():
    text = "# Paper\n\nA cited claim [1].\n\nReferences\n\n[1] Author, 2024, Title"

    normalized = _normalize_for_citeverify(text)

    assert "## References\n" in normalized


def test_review_citations_converts_citeverify_result(monkeypatch):
    captured = {}

    def fake_run_pipeline(report_path, **kwargs):
        captured["kwargs"] = kwargs
        captured["content"] = report_path.read_text()
        return SimpleNamespace(
            summary=SimpleNamespace(
                total_claims=1,
                total_references=1,
                exact_match=0,
                minor_hallucination=0,
                major_hallucination=1,
                corrections_applied=0,
                alignment_pairs=1,
                supports=0,
                contradicts=0,
                insufficient_evidence=1,
            ),
            claims=[
                SimpleNamespace(
                    claim_id="paper-1",
                    text="The method improves accuracy.",
                    source_text="The method improves accuracy [1].",
                    missing_inline_ids=[],
                )
            ],
            citations={
                "1": SimpleNamespace(
                    original_text="Invented Author, 2024, Imaginary Paper",
                    hallucination=SimpleNamespace(
                        label="major_hallucination",
                        score=0.0,
                        confidence="high",
                        reasoning="No matching source was found.",
                        matched_source=None,
                    ),
                    corrected=None,
                    alignments=[],
                )
            },
            claim_citation_pairs=[
                SimpleNamespace(
                    claim_id="paper-1",
                    citation_id="1",
                    detailed_verdict="insufficient_evidence",
                    stage="abstract",
                    abstract_reasoning="The abstract does not mention accuracy.",
                    abstract_text="This paper studies a different task.",
                    extracted_passages=[],
                )
            ],
        )

    fake_module = SimpleNamespace(run_pipeline=fake_run_pipeline)
    monkeypatch.setitem(__import__("sys").modules, "citeverify", fake_module)

    result = review_citations(
        "paper",
        "# Paper\n\nThe method improves accuracy [1].\n\nReferences\n\n[1] Invented Author, 2024, Imaginary Paper",
        model="openai/gpt-5.2",
        provider="openai",
        infer_citations=True,
        skip_alignment=False,
        try_web_search=True,
        use_full_text=False,
    )

    assert result.method == "citation_verify"
    assert result.model == "gpt-5.2"
    assert result.num_comments == 2
    assert "1 major" in result.overall_feedback
    assert "## References" in captured["content"]
    assert captured["kwargs"]["infer_citations"] is True
    assert captured["kwargs"]["try_web_search"] is True
    assert captured["kwargs"]["use_full_text"] is False
    assert captured["kwargs"]["llm_model"] == "gpt-5.2"
    assert {c.severity for c in result.comments} == {"major", "moderate"}

