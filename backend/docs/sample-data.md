# Sample North Pole

Load from the app's **Load Sample North Pole** task, or run `cst demo seed` and
select `sample-north-pole` in the workspace switcher. This opt-in default sample
lives separately at `$CST_HOME/samples/sample-north-pole`. It never replaces an
existing user workspace or imports private organization demo data.

Everything is labeled **Sample**. Product names and descriptions are original;
prices (USD), inventory and specifications are synthetic, not verified commercial
offers. Do not treat sample safety descriptions as certified equipment ratings.

- Sample Gift Catalog: 50 products.
- Sample Workshop Supply: 50 products.
- Sample Polar Outfitter: 50 products.
- Six named Sample lists: stocking stuffers, unusual gifts, night shift, repairs,
  overnight sleigh supplies, and cold-weather field supplies. Each list is a generic
  dataset of product references, in the existing dataset library.
- Sample search queries: 30 exact, broad and constrained queries.
- Sample illustrative judgments: 18 positive query/product pairs covering 10
  queries, manually curated with explanations. Grade 3 is a strong match; 2 is
  useful with the stated qualification. Unlisted pairs are **unjudged**, not
  irrelevant. These labels are not measured results or a complete evaluation set.

No clicks, conversions, randomized shoppers or experiment performance are seeded.
The existing dataset schemas and Parquet snapshots are used. Product `id` and
`title` support existing engines; `name`, `catalog`, `description`, price, currency,
stock, category-specific nested `attributes`, `tags` and `demo_data` support
inspection and filtering. Missing optional attributes remain absent in the source
fixture and null in the corresponding Parquet struct fields.

## Repeatability and deletion

The versioned source fixture is `captain_search/samples/north_pole.json`, included
in the Python distribution. Data and IDs are deterministic. Repeated seeding
verifies ownership and file hashes and reuses the existing v1 snapshots. A failed
initial build leaves no workspace binding or partial target directory.

Choose **Remove Sample North Pole** in the New menu/palette or run
`cst demo remove-sample`. This deletes only the owned, unchanged sample directory
and its configuration binding, including sample-only dataset read history. Added/edited files, symlinks and conflicting
workspace bindings cause a refusal before deletion. Move work you want to keep to
another workspace before removing the sample. Removal is safe to repeat; a fresh
sample can then be loaded. Preview either command with global `--dry-run`.

The older simulated experiment demo is still available explicitly as
`cst demo seed --legacy`. Its behavior and simulation labels are unchanged.
