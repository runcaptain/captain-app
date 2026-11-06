---
name: query-log-clustering
description: Group raw search log queries into intents (head, torso, tail) to build a representative query set.
tags: [data-mining, query-gen]
---
# Query log clustering

1. Normalize queries (lowercase, trim, collapse spaces). Count each.
2. Head = the queries that make up the first 50% of traffic. Torso = to 80%. Tail = the rest.
3. Sample a query set proportionally: about 40% head, 35% torso, 25% tail, so the backtest matches traffic.
4. Within the tail, group by the first content word to find intents the catalog may lack.
5. Keep the counts: they become traffic weights for simulated shoppers.
