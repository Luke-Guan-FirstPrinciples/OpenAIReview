# Feature Provenance

OpenAIReview intentionally borrows useful review-system ideas from several research
repos without importing their full pipelines. This file records what was adopted,
what was changed, and what was deliberately left out.

## ReviewGrounder-Style Grounding

Implemented in `src/reviewer/method_grounded.py` as `grounded_progressive`.

What was adopted:

- Progressive candidate issue generation followed by verifier stages.
- Paper-grounded method/contribution extraction.
- Paper-grounded results/evaluation extraction.
- External related-work retrieval and synthesis for positioning questions.
- A final refutation checker that removes vague, duplicate, unsupported, or resolved
  candidate issues.

What was changed:

- The implementation uses OpenAIReview's provider-agnostic `chat()` wrapper instead
  of ReviewGrounder's service factory.
- Intermediate verifier outputs are stored in the existing review JSON under
  `methods[...].verifier_outputs`.
- The visualization renders these outputs generically as collapsible cards.

## Optional Novelty Delta Verifier

Implemented in `src/reviewer/method_grounded.py` and enabled with:

```bash
openaireview review paper.pdf --method grounded_progressive --novelty-delta
```

What was adopted from `eacl2026-assessing-paper-novelty`:

- A dedicated novelty/positioning pass instead of relying only on the generic
  related-work refiner.
- Explicit separation of:
  - author-claimed novelty,
  - closest related work,
  - contribution deltas,
  - overstated novelty risks,
  - missing comparisons or citations,
  - limitations of the available evidence.
- Cautious language around retrieved papers: retrieved related work is external
  context, not proof that the submission cited or discussed those works.

What was deliberately not adopted:

- GROBID TEI extraction as a hard dependency.
- Nougat or MinerU as required OCR engines.
- Full related-paper PDF download and OCR.
- LangChain-specific OpenAI calls.
- RankGPT/SPECTER2 retrieval code and its local path assumptions.
- The EACL repo's fixed `data/{submission_id}` directory contract.

Why:

OpenAIReview already supports multiple input paths: PDF OCR, arXiv HTML, LaTeX,
Markdown, DOCX, and plain text. Requiring GROBID plus a separate OCR stack would
make the novelty feature much harder to use. The optional novelty verifier instead
uses the text and related-work summaries already produced by `grounded_progressive`.

Tradeoff:

Without GROBID, the verifier usually does not have exact citation-context mappings
such as "the submission cites Paper X in sentence Y." It can still identify likely
closest work, weak deltas, and missing-comparison questions from retrieved metadata
and paper-grounded novelty claims. A future citation-context extractor can improve
this without making GROBID mandatory.

## When To Use `--novelty-delta`

Use it when contribution/novelty is central to the review, especially for:

- benchmark papers,
- method papers making strong originality claims,
- papers where related-work coverage is likely important,
- reviews where you want sharper questions about missing comparisons.

Skip it when speed/cost matters more than novelty analysis, or when reviewing a
paper whose contribution is mostly empirical validation, replication, or exposition.
