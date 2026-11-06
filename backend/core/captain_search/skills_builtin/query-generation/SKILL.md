---
name: query-generation
description: Generate test queries from the catalog by failure class, then seal a holdout before tuning.
tags: [query-gen]
---
# Query generation by failure class

Use `cst queries gen --catalog <ref> --classes head,brand,multi-attr,numeric,model-code,typo,zero-result --n 2000 --seed 7 --name <set>`.

Classes: head (category words), brand + type, multi-attribute (3 title words), numeric limit ("under 50"),
model code with separators varied, typo (swapped letters), zero-result (off-catalog products).

Then seal: `cst queries seal <set>@v1 --holdout 0.3`. Tune only on `:train`. Report on `:holdout`. A changed
holdout breaks the seal and blocks live deploys.
