---
name: backtest-readout
description: Read a backtest run honestly - evidence labels, noise, independence and coverage.
tags: [backtest]
---
# Backtest readout

1. Lead with the primary metric's verdict (win, loss or noise) and its CI.
2. Say the evidence label of every number: SIMULATED, LLM_GRADED, HUMAN_GRADED or REAL_TRAFFIC.
3. A gain under 0.003 nDCG@10, or a CI that crosses zero, is noise until replicated.
4. If the run is "not independent", the click metrics and nDCG are one signal. Do not cite them as two.
5. If judged@10 is under 90%, nDCG is understated for that arm. Say so.
6. Report revenue per visitor next to conversion: a ranker raised simulated conversion 12% while cutting revenue 12% before.
7. Simulated results never justify a revenue claim.
