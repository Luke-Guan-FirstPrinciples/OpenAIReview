"""Unit tests for optional grounded novelty-delta verifier helpers."""

from reviewer import method_grounded
from reviewer.models import Comment, ReviewResult


def test_novelty_delta_verifier_builds_prompt_and_returns_dict(monkeypatch):
    captured = {}

    def fake_json_chat(prompt, result, model, reasoning_effort, max_tokens=8192):
        captured["prompt"] = prompt
        captured["max_tokens"] = max_tokens
        result.total_prompt_tokens += 10
        result.total_completion_tokens += 5
        return {
            "submission_novelty_claims": [
                {"claim": "new benchmark", "paper_evidence": "Abstract"}
            ],
            "confidence": "medium",
        }

    monkeypatch.setattr(method_grounded, "_json_chat", fake_json_chat)
    result = ReviewResult(method="grounded_progressive", paper_slug="paper")

    novelty = method_grounded._novelty_delta_verifier(
        paper_text="The paper claims a new benchmark.",
        method_insights={"novelty_claims_in_paper": [{"claim": "new benchmark"}]},
        results_analysis={"datasets": ["Benchmark-X"]},
        related_work={"related_papers": [{"title": "Prior Benchmark"}]},
        result=result,
        model="test-model",
        reasoning_effort=None,
    )

    assert novelty["confidence"] == "medium"
    assert "Retrieved related-work context" in captured["prompt"]
    assert "Prior Benchmark" in captured["prompt"]
    assert captured["max_tokens"] == 8192
    assert result.total_prompt_tokens == 10
    assert result.total_completion_tokens == 5


def test_grounded_progressive_stores_optional_novelty_delta(monkeypatch):
    full = ReviewResult(method="progressive_full", paper_slug="paper")
    full.comments.append(
        Comment(
            title="Weak novelty claim",
            quote="We introduce a new benchmark.",
            explanation="The novelty claim may need comparison.",
            comment_type="logical",
            paragraph_index=0,
        )
    )

    def fake_review_progressive(*args, **kwargs):
        return ReviewResult(method="progressive", paper_slug="paper"), full

    def fake_json_chat(prompt, result, model, reasoning_effort, max_tokens=8192):
        if "refutation checker" in prompt:
            return [
                {
                    "title": "Missing comparison",
                    "quote": "We introduce a new benchmark.",
                    "explanation": "The related work suggests a close benchmark comparison is needed.",
                    "type": "logical",
                    "paragraph_index": 0,
                    "claim": "The novelty positioning is under-supported.",
                    "evidence": "Novelty-delta analysis identifies Prior Benchmark.",
                    "rubric_dimension": "novelty",
                    "confidence": "high",
                    "severity": "moderate",
                    "verification_status": "verified",
                }
            ]
        return {"facts": []}

    def fake_related_work_searcher(*args, **kwargs):
        return {"related_papers": [{"title": "Prior Benchmark", "relation": "close task"}]}

    def fake_novelty_delta_verifier(*args, **kwargs):
        return {
            "closest_related_work": [{"title": "Prior Benchmark", "relation": "close task"}],
            "confidence": "high",
        }

    def fake_chat(*args, **kwargs):
        return "## Summary\nFinal review.", {"prompt_tokens": 3, "completion_tokens": 2}

    monkeypatch.setattr(method_grounded, "review_progressive", fake_review_progressive)
    monkeypatch.setattr(method_grounded, "_json_chat", fake_json_chat)
    monkeypatch.setattr(method_grounded, "_related_work_searcher", fake_related_work_searcher)
    monkeypatch.setattr(method_grounded, "_novelty_delta_verifier", fake_novelty_delta_verifier)
    monkeypatch.setattr(method_grounded, "chat", fake_chat)

    result = method_grounded.review_grounded_progressive(
        paper_slug="paper",
        document_content="We introduce a new benchmark.",
        model="test-model",
        enable_novelty_delta=True,
    )

    assert result.final_review.startswith("## Summary")
    assert result.verifier_outputs["novelty_delta"]["confidence"] == "high"
    assert result.comments[0].rubric_dimension == "novelty"
