# Agent quality evaluation

The labelled suite at `backend/evals/agent_quality_suite.json` measures the
customer-facing routing and retrieval path without using production messages as
ground truth.

## Metrics

- **Intent accuracy** is exact agreement between the expected route and the
  route selected by the agent. The current labels are `greeting`, `pricing`,
  `knowledge`, `order_status`, and `handoff`.
- **Country recall** is micro recall over the active structured catalogue. For
  each destination question, every active product whose normalized destination
  matches the labelled country is relevant. The score is matched relevant
  products divided by all relevant products.
- **Product recall** is micro recall over labelled product-name and alias
  questions. Out-of-stock products remain relevant because the correct answer
  is “product exists; currently out of stock”.
- **Retrieval Top-1 / Top-3 accuracy** is the percentage of questions whose
  labelled source appears at rank 1 or within ranks 1–3 after query rewriting,
  metadata filtering, and retrieval ranking.

Country and product recall do not measure precision. Retrieval correctness uses
stable source URL fragments instead of mutable page titles, and duplicate chunks
from the same source do not make an incorrect source correct.

## Run locally

Run the deterministic baseline:

```powershell
.\.venv\Scripts\python.exe scripts\run_quality_evaluation.py --show-failures
```

Run the configured live chat model and publish the result to the Analytics page:

```powershell
.\.venv\Scripts\python.exe scripts\run_quality_evaluation.py --live-model --save-latest --show-failures
```

The latest tenant-scoped report is stored under `data/evaluations/`. The
authenticated endpoint `GET /api/quality-evaluation/latest` and the Analytics
page read that report. A report always includes the suite version, timestamp,
model mode, embedding provider, aggregate metrics, and per-case evidence.

## Reading the score

Always compare reports produced by the same suite version. Top-3 can be high
while Top-1 is low; that means the correct evidence was recalled but the final
ranking still needs improvement. Do not treat a Top-3 hit as a Top-1 pass.

## Current retrieval ranking

The retrieval path normalizes simplified/traditional text, applies destination
metadata as a hard filter, recalls candidates from local BM25 and vector
similarity, normalizes both score families, and reranks them with title,
destination, and structured-catalog signals. Structured product pages receive a
large boost only for direct price, stock, purchase, or rental questions;
comparison, policy, pickup/return, and how-to questions prefer the matching guide.
The final Top-K contains at most one chunk per source URL. A candidate below the
configured final fused-score threshold is discarded even when a raw vector
collision happens to clear the cosine threshold; if no trusted source remains,
the conversation enters a real human-handoff state.
