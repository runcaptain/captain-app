---
name: catalog-profiling
description: Profile a product catalog before any search work (coverage of titles, brands, categories, prices, missing fields).
tags: [data-mining]
---
# Catalog profiling

Goal: know what the index can and cannot answer before tuning anything.

1. Count rows and distinct ids. Duplicated ids break judgments, so flag them first.
2. For each of title, brand, category, price, description: the share of rows where it is null or empty.
3. Top 20 categories and brands by row count, and the long tail (how many have under 5 products).
4. Price: min, median, p95, max, and rows with price 0 or null.
5. Title length distribution (words). Titles under 3 words rank badly on multi-attribute queries.

SQL shape (adapt names):
```sql
select count(*) n, count(distinct id) ids,
       avg(case when brand is null or brand = '' then 1 else 0 end) brand_missing,
       median(price) p50, quantile_cont(price, 0.95) p95
from products
```
Report: one line per finding, with the number from the query. Never state a number you did not query.
