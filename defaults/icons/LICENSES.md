# Icons

- Brand icons: Simple Icons (https://simpleicons.org), CC0 1.0. Brands are their owners' trademarks.
- Amazon and Microsoft marks: the vendors' own icon packages, under the vendors' terms (not CC0). See "Vendor marks" below.
- Glyphs (terminal, bot, search, activity, ...), including icons in shipped mods: Lucide (https://lucide.dev), ISC, with MIT-licensed Feather-derived icons. Full notices: [LICENSE-Lucide.txt](LICENSE-Lucide.txt).
- Agent icons: from LobeHub icons (MIT) and the agents' own marks. Full notice: [LICENSE-LobeHub.txt](LICENSE-LobeHub.txt).

The release's THIRD-PARTY-NOTICES includes these notices and the full CC0 text. Trademark rights are not granted by these licenses.

## Data platform and cloud marks (brand-<name>.svg)

From Simple Icons 16.36.0 (CC0 1.0), shown in each brand's own color (or the text color when that color would vanish
on the background). Copied unaltered, except brand-mysql.svg: its viewBox is cropped to the dolphin, because the
wordmark under it is illegible at 14 px. Each mark is its owner's trademark and is used only to name the product
Captain connects to, under the owner's guidelines below. Kinds without a mark (Oracle, Typesense: not in Simple
Icons) show a neutral glyph.

| File | Brand | Source | Guidelines |
|---|---|---|---|
| brand-googlecloud.svg | Google Cloud | https://cloud.google.com | https://about.google/brand-resource-center |
| brand-googleanalytics.svg | Google Analytics | https://marketingplatform.google.com/about/analytics/ | https://about.google/brand-resource-center |
| brand-posthog.svg | PostHog | https://posthog.com/handbook/company/brand-assets | https://posthog.com/handbook/company/brand-assets |
| brand-snowflake.svg | Snowflake | https://www.snowflake.com/brand-guidelines/ | https://www.snowflake.com/brand-guidelines/ |
| brand-databricks.svg | Databricks | https://www.databricks.com | https://brand.databricks.com/Styleguide/Guide/ |
| brand-googlebigquery.svg | Google BigQuery | https://cloud.google.com/icons | https://about.google/brand-resource-center |
| brand-postgresql.svg | PostgreSQL | https://wiki.postgresql.org/wiki/Logo | https://www.postgresql.org/about/policies/trademarks/ |
| brand-mysql.svg | MySQL | https://www.mysql.com/about/legal/logos.html | https://www.mysql.com/about/legal/logos.html |
| brand-trino.svg | Trino | https://github.com/trinodb/docs.trino.io | |
| brand-presto.svg | Presto | https://github.com/prestodb/presto | |
| brand-clickhouse.svg | ClickHouse | https://github.com/ClickHouse/ClickHouse | |
| brand-duckdb.svg | DuckDB | https://duckdb.org | |
| brand-sqlite.svg | SQLite | https://github.com/sqlite/sqlite | |
| brand-elasticsearch.svg | Elasticsearch | https://www.elastic.co/brand | https://www.elastic.co/brand |
| brand-mlflow.svg | MLflow | https://github.com/mlflow/mlflow | https://mlflow.org |
| brand-opensearch.svg | OpenSearch | https://opensearch.org/trademark-brand-policy.html | https://opensearch.org/trademark-brand-policy.html |
| brand-algolia.svg | Algolia | https://algolia.frontify.com/d/1AZwVNcFZiu7/style-guide | https://algolia.frontify.com/d/1AZwVNcFZiu7/style-guide |
| brand-modal.svg | Modal | https://modal.com | |

## Vendor marks (brand-<name>-<size>.svg): AWS, Amazon Redshift, Amazon Athena, Azure, SQL Server

Amazon and Microsoft had their marks removed from Simple Icons, so these are the vendors' own files, downloaded from
the vendors' own sites on 2026-11-09. They are not CC0 and not ours to license: each stays its owner's property and
trademark, all rights reserved by the owner.

| Files | Mark | File in the vendor's package |
|---|---|---|
| brand-amazonaws-<size>.svg | AWS, for light backgrounds | Architecture-Group-Icons_07312026/AWS-Cloud-logo_32.svg |
| brand-amazonaws-dark-<size>.svg | AWS, for dark backgrounds | Architecture-Group-Icons_07312026/AWS-Cloud-logo_32_Dark.svg |
| brand-amazonredshift-<size>.svg | Amazon Redshift | Architecture-Service-Icons_07312026/Arch_Analytics/16 and 32/Arch_Amazon-Redshift_16.svg, _32.svg |
| brand-amazonathena-<size>.svg | Amazon Athena | Architecture-Service-Icons_07312026/Arch_Analytics/16 and 32/Arch_Amazon-Athena_16.svg, _32.svg |
| brand-microsoftazure-<size>.svg | Azure | Icons/other/10018-icon-service-Azure-A.svg |
| brand-microsoftsqlserver-<size>.svg | SQL Server | Icons/databases/10132-icon-service-SQL-Server.svg |

Sources:

- AWS Architecture Icons, release 07312026, from https://aws.amazon.com/architecture/icons/ (package:
  https://d1.awsstatic.com/onedam/marketing-channels/website/public/shared/architecture-icon-release/Icon-package_07312026.5846e92413caa21490223536cc97f1269e44fa92.zip,
  SHA-256 d2d166c453526471749d520e0db022c459abef759d2946cf2dd1d1c992dc6526).
  Trademark guidelines: https://aws.amazon.com/trademark-guidelines/.
- Azure Public Service Icons V25, from https://learn.microsoft.com/en-us/azure/architecture/icons/ (package:
  https://arch-center.azureedge.net/icons/Azure_Public_Service_Icons_V25.zip,
  SHA-256 f293d99252b3638557dd08ccbff9f2529083473d39b373fb2af29a7200351339).
  Trademark guidelines: https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks.

What was changed: only the width and height on each file's root tag, once per size the mark is drawn at (14, 15, 16,
18, 20, 24 and 32 px), by scripts/captain-brand-marks. The art is untouched: not recolored, cropped, flipped or
redrawn. The reason is sharpness. A full-color image is rasterized at the size its file declares and then scaled to
the size on screen in one step, so a file that declares the drawn size is drawn pixel for pixel. AWS draws simpler art
for small sizes: its "16" files are used up to 20 px and its "32" files above.

Usage basis: each mark is shown next to the service's name, only to name a service a person connects Captain to (a
database connection, a cloud login, a place to run work). It never stands for Captain or suggests the vendor
endorses it.

Open point for whoever owns legal review: the terms printed on both download pages cover a narrower use than this.
AWS: "We allow customers and partners to use these toolkits and assets to create architecture diagrams." Microsoft:
"Microsoft permits the use of these icons in architectural diagrams, training materials, or documentation. You can
copy, distribute, and display the icons only for the permitted use unless granted explicit permission by Microsoft."
Showing them in a product's connection list rests on referential trademark use under the guidelines linked above,
not on an explicit grant in those terms. Edgar chose the vendors' own marks on 2026-11-09 ("Use the proper logos for
the third parties"). If either vendor objects, removing the `full` entries in defaults/captain/kit/brands.js puts
the neutral glyph back.
