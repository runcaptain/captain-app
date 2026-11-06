---
name: judge-calibration
description: Check an LLM judge against human labels before trusting its nDCG.
tags: [judging]
---
# Judge calibration

LLM judges order systems roughly right but overstate absolute nDCG (company learning 2026-09-30: kappa 0.54 to 0.69,
nDCG 0.89 judged vs 0.78 human). So:

1. Import human labels: `cst judge import labels.csv --name <set>-human` (columns query_id, product_id, grade).
2. Grade the same pairs with the LLM judge.
3. `cst judge calibrate --llm <llm-set> --human <set>-human`. Report quadratic kappa, exact agreement and mean grade bias.
4. Under kappa 0.6, do not use the LLM judge for decisions. Either way, a live deploy needs HUMAN_GRADED evidence.
