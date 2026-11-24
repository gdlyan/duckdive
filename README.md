# DuckDive

Turn a pandas DataFrame (or a data file) into a **single self-contained HTML file** with a fully interactive data table. No server, no network, no external JS CDNs — open the file in a browser and you get a working data explorer.

```python
from duckdive import DuckDive

dd = DuckDive(df, title="Sales Q4")
dd.save("report.html")          # one .html = data + UI + logic
```

## Principles

- **Zero infrastructure**: one `.html` file = data + UI + logic. Works offline from `file://`.
- **SQL-native**: data lives in DuckDB; views and labels are SQL.
- **Two scale modes**: JSON (default, up to ~50k rows) and WASM (`duckdb_wasm=True`, millions of rows — all operations run as SQL queries in duckdb-wasm inside a Web Worker).
- **Composable**: a template can contain multiple tables, standalone filters, toggle buttons and live SQL labels — all cross-filtering each other through the Global Filter Context (GFC).
- **Self-contained runtime**: SheetJS and the duckdb-wasm bundle are vendored in `assets/` and inlined into every report at build time.

## API

```python
DuckDive(data, template=None, title=None, page_size=20, duckdb_wasm=None,
         params=None, redact_columns=None, template_vars=None)

dd.save("report.html")
dd2 = DuckDive.read("report.html")   # round-trip: parses the boot JSON back out
dd.data.sql("SELECT * FROM t LIMIT 5")   # arbitrary SQL on the in-memory DuckDB
```

- `data`: `pd.DataFrame`, `pd.Series`, `list`, `dict`, a file path (`.parquet`, `.csv`, `.xlsx`, `.xls`), or a dict of those (`{"t1": df1, "t2": df2}`) for multi-table reports (SQL views can JOIN across them).
- `duckdb_wasm`: `None` = auto (>50,000 rows → WASM).
- `params`: values for `:param` placeholders in view/label SQL — bound at build time (row-level slicing: generate per-user HTML files).
- `redact_columns`: columns masked to `••••` **at build time** — raw values never reach the HTML.

## Template placeholders

Placeholders are custom (not Jinja2) and are scanned brace-aware, so nested `cell_popup={...}` works. Jinja2 is used afterwards for plain variables like `{{ title }}`.

| Placeholder | Result |
|---|---|
| `{{ duckdive_table }}` | interactive table of the base data |
| `{{ duckdive_table sql="SELECT region, sum(amount) AS total FROM {{src}} GROUP BY region" name="by_region" }}` | SQL view as a table; `{{src}}` = GFC-filtered base |
| `{{ duckdive_label sql="SELECT count(*) FROM t WHERE status='active'" }}` | live aggregate, recomputed on filter changes |
| `{{ duckdive_filter column="region" title="Region" }}` | standalone filter button |
| `{{ duckdive_toggles column="status" items='[{"id":"a","label":"Hide cancelled","value":"cancelled","on":true}]' }}` | exclusion toggle buttons |

Table parameters: `title=`, `page_size=`, `name=`, `cross_filters=false`, `use_filter_keys="src:col1,col2|*"`, `filter_columns="col1,col2"`, `cell_popup={"col": "x", "formatter": myJsFn(key="lit", num=123)}`, `redact_columns=["col"]`.

Label SQL is a validated subset: `SELECT <count|sum|avg|min|max>([DISTINCT] col|*) FROM t [WHERE atom AND ...]` where atoms are `col IS [NOT] NULL` or `col <op> literal` (`= != <> > < >= <=`). `fmt="percent"` renders fractions as percentages; `use_table_filters=false` pins the label to the unfiltered value.

## Table features

Polished card UI (sticky header, zebra/selected-row states, focus rings, custom scrollbars, reduced-motion aware), auto-typed columns (int/float/date/string), pagination with a jump-to-page control (no per-page DOM lists on huge tables), tri-state sorting, global search, column picker, export (CSV/TSV/XLSX/JSON of the filtered+sorted+visible subset, or selected rows), row selection with select-all, hover tooltips + pinned copyable tooltip, per-column filter popups (value lists with counts, numeric ranges, date ranges, include/exclude), removable filter badges plus incoming cross-filter badges, keyboard shortcuts (`Ctrl/Cmd+F` focuses search, `Esc` closes popups), dark mode (`prefers-color-scheme`), print stylesheet.

## Cross-filtering (GFC)

- **Owned column atoms**: every filter is published with its author (`name=` / component id). A component applies its own UI filters locally and receives only *foreign* atoms from the shared pool — own atoms are never fed back into the same base, so feedback loops and double-filtering are structurally impossible. JSON mode now follows the same ownership rules as WASM mode.
- **Row atoms**: selection/search in a named table publishes a temp key table; subscribers (`use_filter_keys="orders:region"`) get an `EXISTS` predicate. Selection survives view rebuilds (key-based capture/restore; `__rn` from the base parquet is stable).
- `filter_columns=` is an allowlist restricting which foreign column atoms a component accepts; atoms on columns a component doesn't have are skipped automatically. Incoming atoms are visible as dashed badges (`← source: column`) next to a table's own badges.
- Build-time label values are computed before any client-side filter exists; labels with `use_table_filters=true` are recomputed by the runtime as soon as the table/GFC is ready.

## Security notes (by design, read before shipping reports)

- The HTML file **is** the data: anyone with the file has all rows. Use `redact_columns` / `params` to produce masked or per-recipient files.
- No network calls at runtime, no secrets, in-memory DuckDB only; JSON payloads are `</`-escaped; all cell text is HTML-escaped; label SQL is validated against a strict subset.
- `DuckData.sql()` executes arbitrary SQL — fine for a local tool, not for untrusted input.

## Not implemented (documented gaps)

- `duckdive_chart` (Chart.js/SVG charts) — planned; labels cover the KPI case for now.
- Virtual DOM scrolling — WASM mode pages via SQL `LIMIT/OFFSET`, so this is rarely needed.
- `sqlglot` label parsing — the regex subset is strict and sufficient.
- Incremental `dd.update()` patching of existing HTML.

## Layout

```
.
├── duckdive/
│   ├── __init__.py
│   ├── core.py                # DuckDive, DuckData, _register, _jsonable, boot, render/save/read
│   ├── table_ui.py            # placeholder scanner, _CSS, JSON-mode _JS, popup, icons, table_block
│   ├── label_ui.py            # label SQL subset parser, label_block
│   ├── wasm_ui.py             # bundle loader, worker shim, WASM-mode _JS (GFC), wasm_table_block
│   ├── default_template.html
│   └── assets/
│       ├── xlsx.full.min.js   # SheetJS (Excel export), inlined
│       └── wasm/
│           ├── duckdb-mvp.wasm                 # duckdb-wasm binary (gzip+base64 at build time)
│           ├── duckdb-browser-mvp.worker.js    # web worker
│           └── duckdb-bundle.js                # duckdb-wasm API + apache-arrow + tslib +
│                                               #   flatbuffers, esbuild-bundled, import-free
├── examples/
│   ├── minimal.py             # build a small report from a DataFrame
│   ├── index.html             # links to the prebuilt demos
│   └── demo-*.html            # prebuilt self-contained reports
├── pyproject.toml
└── uv.lock
```

## Development (VS Code / uv)

```bash
uv sync
uv run python examples/minimal.py
```

Open `examples/minimal.html` or any `examples/demo-*.html` in a browser — no server is needed.

The generated HTML embeds the data and all runtime assets, so a report stays
fully offline and can be sent as a single file.
