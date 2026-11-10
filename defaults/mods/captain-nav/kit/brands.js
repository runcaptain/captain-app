// captain-ui: data platform and cloud marks, each its owner's trademark (sources: defaults/icons/LICENSES.md).
// Most are Simple Icons (CC0), installed as icons/brand-<slug>.svg and tinted one color. Amazon's and Microsoft's are
// their own full-color files (`full` below), installed once per drawn size as icons/brand-<slug>-<size>.svg.
// A kind without a mark gets a neutral glyph.
//   import { Brand } from "./kit/brands.js";   Brand(c, "snowflake", 18)
import { svg, image } from "gpui-kit";
import { icon } from "./icons.js";

export const BRANDS = {
  amazonaws: { title: "AWS", hex: "#232F3E", full: true, themed: true },
  googlecloud: { title: "Google Cloud", hex: "#4285F4" },
  microsoftazure: { title: "Azure", hex: "#0078D4", full: true },
  amazonredshift: { title: "Amazon Redshift", hex: "#8C4FFF", full: true },
  amazonathena: { title: "Amazon Athena", hex: "#8C4FFF", full: true },
  microsoftsqlserver: { title: "SQL Server", hex: "#0078D4", full: true },
  snowflake: { title: "Snowflake", hex: "#29B5E8" },
  databricks: { title: "Databricks", hex: "#FF3621" },
  googlebigquery: { title: "Google BigQuery", hex: "#669DF6" },
  postgresql: { title: "PostgreSQL", hex: "#4169E1" },
  mysql: { title: "MySQL", hex: "#4479A1" },
  posthog: { title: "PostHog", hex: "#000000" },
  googleanalytics: { title: "Google Analytics", hex: "#E37400" },
  trino: { title: "Trino", hex: "#DD00A1" },
  presto: { title: "Presto", hex: "#5890FF" },
  clickhouse: { title: "ClickHouse", hex: "#FFCC01" },
  duckdb: { title: "DuckDB", hex: "#FFF000" },
  sqlite: { title: "SQLite", hex: "#003B57" },
  elasticsearch: { title: "Elasticsearch", hex: "#005571" },
  opensearch: { title: "OpenSearch", hex: "#005EB8" },
  algolia: { title: "Algolia", hex: "#003DFF" },
  modal: { title: "Modal", hex: "#7FEE64" },
  mlflow: { title: "MLflow", hex: "#0194E2" },
};

// The names Captain uses for engines, clouds and connection kinds, to a mark's slug.
const ALIAS = {
  postgres: "postgresql", bigquery: "googlebigquery", gcp: "googlecloud", gcs: "googlecloud", google: "googlecloud",
  elastic: "elasticsearch", "modal-function": "modal",
  aws: "amazonaws", s3: "amazonaws", amazons3: "amazonaws", amazonwebservices: "amazonaws", azure: "microsoftazure",
  ga4: "googleanalytics", googleanalytics4: "googleanalytics",
  redshift: "amazonredshift", athena: "amazonathena", sqlserver: "microsoftsqlserver", mssql: "microsoftsqlserver",
};
// Amazon and Microsoft had their marks taken out of Simple Icons, so theirs are the vendors' own full-color files
// (Edgar, 2026-11-09: "Use the proper logos for the third parties"). svg() tints one color, so these go through
// image(), which rasterizes a file at the size the file declares, times two, and lets the GPU scale that to the size
// on screen in one step. A big file shrunk that way drew soft (the 2500 px and 512 px cloud marks did). So each mark
// ships once per drawn size, declaring exactly that size (scripts/captain-brand-marks), and is rasterized pixel for
// pixel on a 2x display. A size not in the list takes the next one up.
const FULL_SIZES = [14, 15, 16, 18, 20, 24, 32]; // keep in step with SIZES in scripts/captain-brand-marks
// This computer gets a glyph; anything else without a mark, a neutral one.
const GLYPH = { local: "monitor" };

export function brandSlug(kind) {
  const k = String(kind ?? "").toLowerCase();
  const s = ALIAS[k] ?? k;
  if (BRANDS[s]) return s;
  // A kind named after its vendor (a compute kind like aws-ecs, gcp-vertex, databricks-jobs): the vendor's mark.
  const head = k.split(/[-_]/)[0];
  const h = ALIAS[head] ?? head;
  return head !== k && BRANDS[h] ? h : null;
}

const luma = (hex) => {
  const n = parseInt(hex.slice(1), 16);
  return 0.2126 * ((n >> 16) & 255) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255);
};

/** A database's own name for itself ("DuckDB", "Databricks"), never the lowercase kind a person would read as code. */
export function engineTitle(kind) {
  const k = String(kind ?? "").trim();
  // A product's own name first: GA4 shares Google Analytics' mark but is called Google Analytics 4.
  const own = { redshift: "Redshift", athena: "Athena", mssql: "SQL Server", sqlserver: "SQL Server", oracle: "Oracle", ga4: "Google Analytics 4", posthog: "PostHog" };
  if (own[k.toLowerCase()]) return own[k.toLowerCase()];
  const slug = brandSlug(kind);
  if (slug && !/[-_]/.test(k)) return BRANDS[slug].title;
  return k ? `${k[0].toUpperCase()}${k.slice(1)}` : "Database";
}

/** The mark for an engine, cloud or connection kind, `size` px square; a neutral glyph when there is none. */
export function Brand(c, kind, size = 16) {
  const k = String(kind ?? "").toLowerCase();
  const slug = brandSlug(kind);
  if (!slug) return svg(icon(GLYPH[k] ?? "database")).size(size).flex_none().text_color(c.grey500);
  if (BRANDS[slug].full) {
    const file = FULL_SIZES.find((s) => s >= size) ?? FULL_SIZES.at(-1);
    // AWS publishes its mark for light and for dark backgrounds.
    return image(`icons/brand-${slug}${BRANDS[slug].themed && c.dark ? "-dark" : ""}-${file}.svg`).size(size).flex_none();
  }
  const hex = BRANDS[slug].hex;
  // The brand's color, unless it would vanish on this background: then the text color.
  const unreadable = c.dark ? luma(hex) < 70 : luma(hex) > 190;
  return svg(`icons/brand-${slug}.svg`).size(size).flex_none().text_color(unreadable ? c.navy : hex);
}
