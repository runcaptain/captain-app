// captain-ui: data platform and cloud marks (Simple Icons, CC0; each its owner's trademark, see
// defaults/icons/LICENSES.md). Installed as icons/brand-<slug>.svg. A kind without a mark gets a neutral glyph.
//   import { Brand } from "./kit/brands.js";   Brand(c, "snowflake", 18)
import { svg } from "gpui-kit";
import { icon } from "./icons.js";

export const BRANDS = {
  googlecloud: { title: "Google Cloud", hex: "#4285F4" },
  snowflake: { title: "Snowflake", hex: "#29B5E8" },
  databricks: { title: "Databricks", hex: "#FF3621" },
  googlebigquery: { title: "Google BigQuery", hex: "#669DF6" },
  postgresql: { title: "PostgreSQL", hex: "#4169E1" },
  mysql: { title: "MySQL", hex: "#4479A1" },
  trino: { title: "Trino", hex: "#DD00A1" },
  presto: { title: "Presto", hex: "#5890FF" },
  clickhouse: { title: "ClickHouse", hex: "#FFCC01" },
  duckdb: { title: "DuckDB", hex: "#FFF000" },
  sqlite: { title: "SQLite", hex: "#003B57" },
  elasticsearch: { title: "Elasticsearch", hex: "#005571" },
  opensearch: { title: "OpenSearch", hex: "#005EB8" },
  algolia: { title: "Algolia", hex: "#003DFF" },
  modal: { title: "Modal", hex: "#7FEE64" },
};

// The names Captain uses for engines, clouds and connection kinds, to a mark's slug.
const ALIAS = {
  postgres: "postgresql", bigquery: "googlebigquery", gcp: "googlecloud", gcs: "googlecloud", google: "googlecloud",
  elastic: "elasticsearch", "modal-function": "modal",
};
// No mark available (removed from Simple Icons at the owner's request): a neutral glyph.
const CLOUDS = new Set(["aws", "azure", "s3", "amazons3", "amazonwebservices", "microsoftazure"]);

export function brandSlug(kind) {
  const k = String(kind ?? "").toLowerCase();
  const s = ALIAS[k] ?? k;
  return BRANDS[s] ? s : null;
}

const luma = (hex) => {
  const n = parseInt(hex.slice(1), 16);
  return 0.2126 * ((n >> 16) & 255) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255);
};

/** The mark for an engine, cloud or connection kind, `size` px square; a neutral glyph when there is none. */
export function Brand(c, kind, size = 16) {
  const slug = brandSlug(kind);
  if (!slug) {
    const k = String(kind ?? "").toLowerCase();
    return svg(icon(CLOUDS.has(k) ? "cloud" : "database")).size(size).flex_none().text_color(c.grey500);
  }
  const hex = BRANDS[slug].hex;
  // The brand's color, unless it would vanish on this background: then the text color.
  const unreadable = c.dark ? luma(hex) < 70 : luma(hex) > 190;
  return svg(`icons/brand-${slug}.svg`).size(size).flex_none().text_color(unreadable ? c.navy : hex);
}
