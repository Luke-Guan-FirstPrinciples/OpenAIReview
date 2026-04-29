"""Unit tests for data models."""

from reviewer.models import Comment, ReviewResult


def test_comment_to_dict():
    c = Comment(title="Bug", quote="x=1", explanation="wrong", comment_type="technical", paragraph_index=3)
    d = c.to_dict()
    assert d["title"] == "Bug"
    assert d["paragraph_index"] == 3


def test_comment_to_dict_no_paragraph():
    c = Comment(title="Bug", quote="x", explanation="y", comment_type="logical")
    d = c.to_dict()
    assert "paragraph_index" not in d


def test_review_result_num_comments():
    r = ReviewResult(method="test", paper_slug="slug")
    assert r.num_comments == 0
    r.comments.append(Comment(title="A", quote="q", explanation="e", comment_type="technical"))
    assert r.num_comments == 1


def test_review_result_to_dict():
    r = ReviewResult(method="progressive", paper_slug="paper1", model="test-model")
    r.comments.append(Comment(title="Issue", quote="q", explanation="e", comment_type="logical"))
    d = r.to_dict()
    assert d["method"] == "progressive"
    assert d["num_comments"] == 1
    assert len(d["comments"]) == 1


def test_grounded_comment_fields_to_dict():
    c = Comment(
        title="Unsupported baseline claim",
        quote="we outperform all baselines",
        explanation="The evidence is narrower than the claim.",
        comment_type="logical",
        paragraph_index=7,
        claim="The paper overstates its baseline comparison.",
        evidence="Table 2 only includes two baselines.",
        rubric_dimension="results",
        confidence="high",
        severity="major",
        verification_status="verified",
    )
    d = c.to_dict()
    assert d["claim"] == "The paper overstates its baseline comparison."
    assert d["evidence"] == "Table 2 only includes two baselines."
    assert d["rubric_dimension"] == "results"
    assert d["confidence"] == "high"
    assert d["severity"] == "major"
    assert d["verification_status"] == "verified"


def test_review_result_grounding_fields_to_dict():
    r = ReviewResult(
        method="grounded_progressive",
        paper_slug="paper1",
        final_review="## Summary\nGrounded review.",
        verifier_outputs={"refutation_checker": {"candidate_count": 3, "surviving_count": 1}},
    )
    d = r.to_dict()
    assert d["final_review"].startswith("## Summary")
    assert d["verifier_outputs"]["refutation_checker"]["candidate_count"] == 3
