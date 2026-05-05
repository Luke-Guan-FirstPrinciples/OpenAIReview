"""Citation verification adapter backed by CiteVerify.

This module keeps CiteVerify optional. OpenAIReview can run normally without
the extra package, but when the ``citation`` extra installs CiteVerify it can
expose citation hallucination and claim-citation checks as normal review
comments.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .models import Comment, ReviewResult
from .utils import locate_comment_in_document, split_into_paragraphs


DEFAULT_CITATION_MODEL = "gpt-5.2"


class CiteVerifyUnavailable(RuntimeError):
    """Raised when CiteVerify or one of its optional dependencies is missing."""


def _candidate_citeverify_paths(explicit_path: str | Path | None = None) -> list[Path]:
    paths: list[Path] = []
    if explicit_path:
        paths.append(Path(explicit_path).expanduser())
    env_path = os.environ.get("CITEVERIFY_PATH")
    if env_path:
        paths.append(Path(env_path).expanduser())
    return paths


def _ensure_citeverify(citeverify_path: str | Path | None = None):
    try:
        return importlib.import_module("citeverify")
    except ModuleNotFoundError as first_error:
        if first_error.name != "citeverify":
            raise CiteVerifyUnavailable(
                f"CiteVerify dependency missing: {first_error.name}. "
                "Install OpenAIReview with the citation extra."
            ) from first_error

    for path in _candidate_citeverify_paths(citeverify_path):
        if (path / "citeverify" / "__init__.py").exists():
            path_str = str(path)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)
            try:
                return importlib.import_module("citeverify")
            except ModuleNotFoundError as error:
                raise CiteVerifyUnavailable(
                    f"CiteVerify was found at {path}, but dependency "
                    f"{error.name!r} is missing."
                ) from error

    raise CiteVerifyUnavailable(
        "CiteVerify is not importable. Install OpenAIReview with the citation "
        "extra. For local development only, set CITEVERIFY_PATH or pass "
        "--citeverify-path pointing to a CiteVerify checkout."
    )


def _normalize_for_citeverify(text: str) -> str:
    """Make parsed paper/report text compatible with CiteVerify's parser."""
    if re.search(r"^##\s+References\s*$", text, flags=re.MULTILINE):
        return text

    lines = text.splitlines()
    normalized: list[str] = []
    changed = False
    heading_re = re.compile(
        r"^\s*(?:#{1,6}\s*)?(?:\d+(?:\.\d+)*\.?\s+)?"
        r"(references|bibliography)\s*$",
        flags=re.IGNORECASE,
    )
    for line in lines:
        if not changed and heading_re.match(line):
            normalized.append("## References")
            changed = True
        else:
            normalized.append(line)
    return "\n".join(normalized)


def _to_plain_data(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(k): _to_plain_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain_data(v) for v in value]
    if hasattr(value, "__dict__"):
        return {
            str(k): _to_plain_data(v)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
    return value


def _get_attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _map_provider(provider: str | None) -> str:
    if provider in (None, "", "auto"):
        return "openai"
    provider = provider.lower()
    if provider == "gemini":
        return "google"
    if provider in {"openai", "anthropic", "google"}:
        return provider
    raise ValueError(
        f"CiteVerify provider must be openai, anthropic, google, or gemini; got {provider!r}."
    )


def _strip_provider_prefix(model: str, provider: str) -> str:
    prefixes = {
        "openai": "openai/",
        "anthropic": "anthropic/",
        "google": "google/",
    }
    prefix = prefixes.get(provider)
    if prefix and model.startswith(prefix):
        return model[len(prefix):]
    return model


def _summary_text(summary: Any) -> str:
    return (
        f"Checked {_get_attr(summary, 'total_references', 0)} citations and "
        f"{_get_attr(summary, 'alignment_pairs', 0)} claim-citation pairs. "
        f"Hallucination labels: {_get_attr(summary, 'exact_match', 0)} exact, "
        f"{_get_attr(summary, 'minor_hallucination', 0)} minor, "
        f"{_get_attr(summary, 'major_hallucination', 0)} major. "
        f"Claim support: {_get_attr(summary, 'supports', 0)} supports, "
        f"{_get_attr(summary, 'contradicts', 0)} contradicts, "
        f"{_get_attr(summary, 'insufficient_evidence', 0)} insufficient evidence."
    )


def _matched_source_text(hallucination: Any) -> str:
    matched = _get_attr(hallucination, "matched_source")
    if not matched:
        return ""
    title = _get_attr(matched, "title", "") or ""
    authors = _get_attr(matched, "authors", "") or ""
    year = _get_attr(matched, "year", "") or ""
    url = _get_attr(matched, "url", "") or ""
    parts = [part for part in (authors, year, title, url) if part]
    return "; ".join(parts)


def _alignment_evidence(alignment: Any) -> str:
    reasoning = _get_attr(alignment, "passage_reasoning") or _get_attr(
        alignment, "abstract_reasoning", ""
    )
    passages = _get_attr(alignment, "extracted_passages", []) or []
    if passages:
        first = passages[0]
        passage_text = _get_attr(first, "text", "")
        if passage_text:
            return f"{reasoning}\n\nRelevant passage: {passage_text[:1000]}".strip()
    abstract = _get_attr(alignment, "abstract_text", "")
    if abstract:
        return f"{reasoning}\n\nAbstract: {abstract[:1000]}".strip()
    return str(reasoning or "")


def _locate(quote: str, paragraphs: list[str]) -> int | None:
    return locate_comment_in_document(quote, paragraphs, threshold=0.2)


def _comments_from_pipeline(result: Any, paragraphs: list[str]) -> list[Comment]:
    comments: list[Comment] = []
    claims_by_id = {
        _get_attr(claim, "claim_id", ""): claim
        for claim in (_get_attr(result, "claims", []) or [])
    }

    for claim in claims_by_id.values():
        missing = _get_attr(claim, "missing_inline_ids", []) or []
        for cid in missing:
            quote = _get_attr(claim, "source_text", "") or _get_attr(claim, "text", "")
            comments.append(Comment(
                title=f"Inline citation [{cid}] is missing from references",
                quote=quote,
                explanation=(
                    f"The text cites reference [{cid}], but CiteVerify did not find "
                    "that ID in the References section."
                ),
                comment_type="logical",
                paragraph_index=_locate(quote, paragraphs),
                claim=_get_attr(claim, "text", ""),
                rubric_dimension="claim_accuracy",
                confidence="high",
                severity="major",
                verification_status="verified",
            ))

    citations = _get_attr(result, "citations", {}) or {}
    for cid, report in citations.items():
        hallucination = _get_attr(report, "hallucination")
        label = _get_attr(hallucination, "label", "")
        if label not in {"major_hallucination", "minor_hallucination"}:
            continue

        quote = _get_attr(report, "original_text", "")
        correction = _get_attr(report, "corrected")
        rendered = _get_attr(correction, "rendered", "") if correction else ""
        matched = _matched_source_text(hallucination)
        severity = "major" if label == "major_hallucination" else "moderate"
        title = (
            f"Major hallucinated citation [{cid}]"
            if label == "major_hallucination"
            else f"Potentially incorrect citation metadata [{cid}]"
        )
        details = [
            f"CiteVerify classified this reference as `{label}`.",
            f"Score: {_get_attr(hallucination, 'score', 0)}.",
            str(_get_attr(hallucination, "reasoning", "") or "").strip(),
        ]
        if matched:
            details.append(f"Closest matched source: {matched}.")
        if rendered:
            details.append(f"Suggested correction: {rendered}.")

        comments.append(Comment(
            title=title,
            quote=quote,
            explanation="\n".join(part for part in details if part),
            comment_type="logical",
            paragraph_index=_locate(quote, paragraphs),
            evidence=matched or rendered,
            rubric_dimension="claim_accuracy",
            confidence=str(_get_attr(hallucination, "confidence", "") or "").lower(),
            severity=severity,
            verification_status="verified",
        ))

    for alignment in _get_attr(result, "claim_citation_pairs", []) or []:
        verdict = _get_attr(alignment, "detailed_verdict", "")
        if verdict not in {"contradicts", "insufficient_evidence"}:
            continue

        claim_id = _get_attr(alignment, "claim_id", "")
        claim = claims_by_id.get(claim_id)
        quote = (
            _get_attr(claim, "source_text", "")
            or _get_attr(claim, "text", "")
            or _get_attr(alignment, "claim", "")
        )
        cid = _get_attr(alignment, "citation_id", "")
        title = (
            f"Citation [{cid}] contradicts the cited claim"
            if verdict == "contradicts"
            else f"Citation [{cid}] does not provide enough evidence for the claim"
        )
        severity = "major" if verdict == "contradicts" else "moderate"
        comments.append(Comment(
            title=title,
            quote=quote,
            explanation=(
                f"CiteVerify's claim-evidence alignment verdict is `{verdict}` "
                f"using the `{_get_attr(alignment, 'stage', 'unknown')}` stage."
            ),
            comment_type="logical",
            paragraph_index=_locate(quote, paragraphs),
            claim=_get_attr(claim, "text", "") if claim else quote,
            evidence=_alignment_evidence(alignment),
            rubric_dimension="claim_accuracy",
            confidence="medium",
            severity=severity,
            verification_status="verified" if verdict == "contradicts" else "partially_verified",
        ))

    return comments


def review_citations(
    paper_slug: str,
    document_content: str,
    *,
    model: str = DEFAULT_CITATION_MODEL,
    provider: str | None = "openai",
    reasoning_effort: str | None = None,
    citeverify_path: str | Path | None = None,
    infer_citations: bool = False,
    skip_alignment: bool = False,
    steps_json_path: str | Path | None = None,
    try_web_search: bool = False,
    use_full_text: bool = True,
    verbose: bool = False,
) -> ReviewResult:
    """Run CiteVerify and return citation findings as an OpenAIReview result."""
    citeverify = _ensure_citeverify(citeverify_path)
    cv_provider = _map_provider(provider)
    cv_model = _strip_provider_prefix(model, cv_provider)

    normalized = _normalize_for_citeverify(document_content)
    paragraphs = split_into_paragraphs(document_content)

    with tempfile.TemporaryDirectory(prefix="openaireview-citeverify-") as tmpdir:
        report_path = Path(tmpdir) / f"{paper_slug or 'paper'}.md"
        report_path.write_text(normalized, encoding="utf-8")
        pipeline_result = citeverify.run_pipeline(
            report_path,
            infer_citations=infer_citations,
            steps_json_path=steps_json_path,
            skip_alignment=skip_alignment,
            try_web_search=try_web_search,
            use_full_text=use_full_text,
            llm_provider=cv_provider,
            llm_model=cv_model,
            abstract_compare_provider=cv_provider,
            abstract_compare_model=cv_model,
            passage_compare_provider=cv_provider,
            passage_compare_model=cv_model,
            fulltext_llm_provider=cv_provider,
            fulltext_llm_model=cv_model,
            reasoning_effort=reasoning_effort,
            verbose=verbose,
        )

    comments = _comments_from_pipeline(pipeline_result, paragraphs)
    summary = _get_attr(pipeline_result, "summary")
    return ReviewResult(
        method="citation_verify",
        paper_slug=paper_slug,
        comments=comments,
        overall_feedback=_summary_text(summary),
        verifier_outputs={"citation_verification": _to_plain_data(pipeline_result)},
        model=cv_model,
        reasoning_effort=reasoning_effort,
    )
