---
name: zero-result-mining
description: Find the searches that return nothing, rank them by traffic, and sort them into fixable causes.
tags: [data-mining]
---
# Zero-result mining

1. From the search log, take searches with results = 0 over a fixed window. Group by normalized query (lowercase, trimmed).
2. Rank by count. The top 50 usually hold most zero-result traffic. Report that share.
3. Sort each into a cause: typo, synonym or vocabulary gap, product not carried, over-specific (too many words), numeric constraint ("under 50").
4. For "not carried", check the catalog with a LIKE search before concluding.
5. Save the ranked list as a dataset (`--save <name> --kind queries`) so it can seed a query set.

```sql
select lower(trim(query)) q, count(*) n
from search_log where results = 0
group by 1 order by n desc limit 200
```
