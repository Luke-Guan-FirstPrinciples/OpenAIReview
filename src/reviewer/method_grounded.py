"""Grounded progressive review with ReviewGrounder-style verification stages."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date
from typing import Any

from .client import chat
from .method_progressive import review_progressive
from .models import ReviewResult
from .utils import count_tokens, parse_comments_from_list, truncate_text


MAX_PAPER_TOKENS_FOR_VERIFIERS = 40_000
MAX_CANDIDATE_ISSUES = 80

METHOD_INSIGHT_MINER_PROMPT = """\
You are a method insight miner for an academic paper review system.

Extract method/contribution facts from the paper. Treat the paper as the source of truth.
Focus on core contributions, assumptions, algorithms, equations, implementation details,
methodological limitations, and novelty claims made by the authors.

Return ONLY JSON:
{{
  "core_contributions": [{{"claim": "...", "evidence": "section/equation/snippet"}}],
  "method_summary": [{{"point": "...", "evidence": "..."}}],
  "assumptions_and_scope": [{{"item": "...", "evidence": "..."}}],
  "novelty_claims_in_paper": [{{"claim": "...", "evidence": "..."}}],
  "method_risks": [{{"risk": "...", "evidence": "..."}}]
}}

Paper:
{paper_text}
"""

RESULTS_ANALYZER_PROMPT = """\
You are a results analyzer for an academic paper review system.

Extract experiment/evaluation facts from the paper. Treat the paper as the source of truth.
Focus on datasets, metrics, baselines, tables, figures, quantitative claims, ablations,
statistical evidence, and limitations of the evaluation.

Return ONLY JSON:
{{
  "datasets": ["..."],
  "metrics": ["..."],
  "baselines": ["..."],
  "key_results": [{{"claim": "...", "evidence": "table/figure/section/snippet"}}],
  "evaluation_risks": [{{"risk": "...", "evidence": "..."}}],
  "missing_or_unclear_experimental_details": [{{"item": "...", "evidence": "..."}}]
}}

Paper:
{paper_text}
"""

RELATED_KEYWORD_PROMPT = """\
Generate 3-5 short Semantic Scholar search queries for finding related work that would
help assess the novelty and positioning of this paper.

Return ONLY JSON:
{{"queries": ["...", "..."]}}

Paper excerpt:
{paper_text}
"""

RELATED_SYNTHESIS_PROMPT = """\
You are a related-work searcher. Given the target paper and retrieved paper metadata,
summarize how the retrieved work should inform novelty, positioning, or missing-baseline
comments. Be careful: retrieved papers are external context and may not be cited by the
target paper.

Return ONLY JSON:
{{
  "search_queries": {queries_json},
  "related_papers": [
    {{"title": "...", "year": 2024, "relation": "...", "url": "..."}}
  ],
  "positioning_notes": [
    {{"note": "...", "evidence": "retrieved paper title or target-paper quote"}}
  ]
}}

Target paper excerpt:
{paper_text}

Retrieved metadata:
{retrieved_json}
"""

REFUTATION_CHECKER_PROMPT = """\
You are a refutation checker for candidate academic-review issues.

Your job is to verify, sharpen, or discard candidate issues produced by a first-pass
reviewer. Be strict. Keep an issue only if it is grounded in the paper and useful to
authors. Drop duplicates, vague comments, formatting-only comments, and comments resolved
by context.

For each surviving issue, return all required fields:
- title
- quote: exact quote from the paper when possible
- explanation: concise but specific reasoning
- type: "technical" or "logical"
- paragraph_index: preserve the candidate index if available, else null
- claim: the review claim being made
- evidence: concrete paper evidence; include section/table/equation/snippet if available
- rubric_dimension: one of "method", "results", "novelty", "soundness", "reproducibility", "clarity", "claim_accuracy"
- confidence: "high", "medium", or "low"
- severity: "major", "moderate", or "minor"
- verification_status: "verified", "partially_verified", or "needs_clarification"

Use the method insights, results analysis, and related-work notes when they are relevant.
Do not use related work to assert that the target paper cited or compared against a work
unless the target text says so. Phrase external-context issues as positioning questions or
missing-comparison suggestions.

Return ONLY a JSON array of surviving issues.

Today's date: {current_date}

Candidate issues:
{candidates_json}

Method insights:
{method_insights_json}

Results analysis:
{results_analysis_json}

Related-work notes:
{related_work_json}

Paper:
{paper_text}
"""

FINAL_REVIEW_PROMPT = """\
You are a senior academic reviewer. Write a final review from verified, localized issues
and verifier outputs. Preserve OpenAIReview's factual, quote-grounded style while making
the result read like a coherent peer review.

Use Markdown with these sections:
## Summary
## Strengths
## Weaknesses
## Questions and Suggestions
## Bottom Line

Keep it concise and evidence-grounded. Mention only claims supported by the verified
issues or verifier outputs.

Verified issues:
{issues_json}

Method insights:
{method_insights_json}

Results analysis:
{results_analysis_json}

Related-work notes:
{related_work_json}
"""


def _extract_json(text: str) -> Any:
    """Extract the first parseable JSON object or array from an LLM response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        return value
    return None


def _json_chat(
    prompt: str,
    result: ReviewResult,
    model: str,
    reasoning_effort: str | None,
    max_tokens: int = 8192,
) -> Any:
    response, usage = chat(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        temperature=0.0,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
    )
    result.raw_responses.append(response)
    result.total_prompt_tokens += usage["prompt_tokens"]
    result.total_completion_tokens += usage["completion_tokens"]
    parsed = _extract_json(response)
    return parsed if parsed is not None else {}


def _paper_for_verifiers(document_content: str) -> str:
    if count_tokens(document_content) <= MAX_PAPER_TOKENS_FOR_VERIFIERS:
        return document_content
    return truncate_text(document_content, MAX_PAPER_TOKENS_FOR_VERIFIERS)


def _search_semantic_scholar(query: str, limit: int = 5) -> list[dict]:
    params = urllib.parse.urlencode({
        "query": query,
        "limit": limit,
        "fields": "title,abstract,year,citationCount,url,authors",
    })
    req = urllib.request.Request(
        f"https://api.semanticscholar.org/graph/v1/paper/search?{params}",
        headers={"User-Agent": "openaireview/grounded-progressive"},
    )
    api_key = os.environ.get("S2_API_KEY")
    if api_key:
        req.add_header("x-api-key", api_key)
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    papers = []
    for p in payload.get("data", []):
        papers.append({
            "title": p.get("title", ""),
            "abstract": p.get("abstract", ""),
            "year": p.get("year"),
            "citationCount": p.get("citationCount"),
            "url": p.get("url", ""),
            "authors": [a.get("name", "") for a in p.get("authors", [])[:4]],
        })
    return papers


def _related_work_searcher(
    paper_text: str,
    result: ReviewResult,
    model: str,
    reasoning_effort: str | None,
) -> dict:
    keyword_obj = _json_chat(
        RELATED_KEYWORD_PROMPT.format(paper_text=paper_text[:12000]),
        result,
        model,
        reasoning_effort,
        max_tokens=1024,
    )
    queries = keyword_obj.get("queries", []) if isinstance(keyword_obj, dict) else []
    queries = [str(q) for q in queries if str(q).strip()][:5]

    retrieved: list[dict] = []
    errors: list[str] = []
    for query in queries:
        try:
            for paper in _search_semantic_scholar(query, limit=5):
                if paper.get("title") and paper["title"] not in {p.get("title") for p in retrieved}:
                    retrieved.append(paper)
        except Exception as exc:
            errors.append(f"{query}: {exc}")

    if not retrieved:
        return {
            "search_queries": queries,
            "related_papers": [],
            "positioning_notes": [],
            "errors": errors,
        }

    synthesis = _json_chat(
        RELATED_SYNTHESIS_PROMPT.format(
            queries_json=json.dumps(queries),
            paper_text=paper_text[:12000],
            retrieved_json=json.dumps(retrieved[:15], ensure_ascii=False),
        ),
        result,
        model,
        reasoning_effort,
        max_tokens=4096,
    )
    if isinstance(synthesis, dict):
        if errors:
            synthesis["errors"] = errors
        return synthesis
    return {"search_queries": queries, "related_papers": retrieved[:15], "errors": errors}


def _normalize_grounded_comments(items: Any) -> list:
    if not isinstance(items, list):
        return []
    comments = parse_comments_from_list([x for x in items if isinstance(x, dict)])
    allowed_status = {"verified", "partially_verified", "needs_clarification"}
    allowed_conf = {"high", "medium", "low"}
    allowed_sev = {"major", "moderate", "minor"}
    for c in comments:
        if c.verification_status not in allowed_status:
            c.verification_status = "needs_clarification"
        if c.confidence not in allowed_conf:
            c.confidence = "medium"
        if c.severity not in allowed_sev:
            c.severity = "moderate"
        if not c.rubric_dimension:
            c.rubric_dimension = "claim_accuracy"
        if not c.claim:
            c.claim = c.title
        if not c.evidence:
            c.evidence = c.quote
    return comments


def review_grounded_progressive(
    paper_slug: str,
    document_content: str,
    model: str = "anthropic/claude-opus-4-6",
    reasoning_effort: str | None = None,
    ocr: bool = False,
) -> ReviewResult:
    """Run progressive review, then ground and verify candidate issues."""
    grounded = ReviewResult(
        method="grounded_progressive",
        paper_slug=paper_slug,
        model=model,
        reasoning_effort=reasoning_effort,
    )

    print("  Grounded progressive: generating candidate issues...")
    _consolidated, full = review_progressive(
        paper_slug=paper_slug,
        document_content=document_content,
        model=model,
        reasoning_effort=reasoning_effort,
        ocr=ocr,
    )
    grounded.total_prompt_tokens += full.total_prompt_tokens
    grounded.total_completion_tokens += full.total_completion_tokens
    grounded.raw_responses.extend(full.raw_responses)

    paper_text = _paper_for_verifiers(document_content)
    candidates = [c.to_dict() for c in full.comments[:MAX_CANDIDATE_ISSUES]]

    print("  Grounding: method insight miner...")
    method_insights = _json_chat(
        METHOD_INSIGHT_MINER_PROMPT.format(paper_text=paper_text),
        grounded,
        model,
        reasoning_effort,
    )

    print("  Grounding: results analyzer...")
    results_analysis = _json_chat(
        RESULTS_ANALYZER_PROMPT.format(paper_text=paper_text),
        grounded,
        model,
        reasoning_effort,
    )

    print("  Grounding: related work searcher...")
    related_work = _related_work_searcher(
        paper_text,
        grounded,
        model,
        reasoning_effort,
    )

    print(f"  Grounding: refutation checker over {len(candidates)} candidate issues...")
    verified_items = _json_chat(
        REFUTATION_CHECKER_PROMPT.format(
            current_date=date.today().isoformat(),
            candidates_json=json.dumps(candidates, ensure_ascii=False, indent=2),
            method_insights_json=json.dumps(method_insights, ensure_ascii=False, indent=2),
            results_analysis_json=json.dumps(results_analysis, ensure_ascii=False, indent=2),
            related_work_json=json.dumps(related_work, ensure_ascii=False, indent=2),
            paper_text=paper_text,
        ),
        grounded,
        model,
        reasoning_effort,
        max_tokens=16384,
    )
    grounded.comments = _normalize_grounded_comments(verified_items)

    print("  Grounding: final review...")
    final_prompt = FINAL_REVIEW_PROMPT.format(
        issues_json=json.dumps([c.to_dict() for c in grounded.comments], ensure_ascii=False, indent=2),
        method_insights_json=json.dumps(method_insights, ensure_ascii=False, indent=2),
        results_analysis_json=json.dumps(results_analysis, ensure_ascii=False, indent=2),
        related_work_json=json.dumps(related_work, ensure_ascii=False, indent=2),
    )
    final_review, usage = chat(
        messages=[{"role": "user", "content": final_prompt}],
        model=model,
        temperature=0.0,
        max_tokens=8192,
        reasoning_effort=reasoning_effort,
    )
    grounded.raw_responses.append(final_review)
    grounded.total_prompt_tokens += usage["prompt_tokens"]
    grounded.total_completion_tokens += usage["completion_tokens"]
    grounded.final_review = final_review.strip()
    grounded.overall_feedback = grounded.final_review
    grounded.verifier_outputs = {
        "method_insight_miner": method_insights,
        "results_analyzer": results_analysis,
        "related_work_searcher": related_work,
        "refutation_checker": {
            "candidate_count": len(candidates),
            "surviving_count": len(grounded.comments),
        },
    }
    return grounded
