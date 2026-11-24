"""DuckDive core: data registration (DuckDB in-memory), boot payload assembly,
render and save. Knows nothing about HTML rendering details.

    dd = DuckDive(df, title="Sales Q4")
    dd.save("report.html")

The output is a single self-contained .html: data + UI + logic, no server,
no network, no external CDNs. JSON mode up to ~50k rows; WASM mode
(duckdb_wasm=True) runs all operations as SQL in duckdb-wasm in the browser
and scales to millions of rows.
"""
import base64
import gzip
import json
import math
import os
import re
import tempfile
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from . import label_ui, table_ui
from .label_ui import label_block, parse_label_sql

_DEFAULT_TEMPLATE = Path(__file__).resolve().parent / "default_template.html"

#: datasets larger than this default to WASM mode when duckdb_wasm is not set
WASM_THRESHOLD = 50_000


def _validate_table_name(name: str) -> str:
    """Table names become SQL views and JS GFC source ids — keep them boring."""
    name = str(name)
    if not name.isidentifier() or name == "__rn" or name.endswith("__raw"):
        raise ValueError(
            f"invalid DuckDive table name {name!r}: use a Python-style identifier "
            "(letters/digits/underscore, not starting with a digit)"
        )
    return name


# ---------------------------------------------------------------------------
# Type-safe serialization
# ---------------------------------------------------------------------------

def _jsonable(v):
    """Convert numpy/datetime/decimal/... values into JSON-safe values."""
    import datetime
    import decimal

    if v is None or v is pd.NaT:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, np.generic):
        return _jsonable(v.item())
    if isinstance(v, (pd.Timestamp, datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, datetime.timedelta):
        return str(v)
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    if isinstance(v, np.ndarray):
        v = v.tolist()
    if isinstance(v, (list, tuple, dict)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return v


def _jsonable_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> list of row dicts with JSON-safe values (incl. __rn)."""
    df = df.copy()
    df["__rn"] = np.arange(1, len(df) + 1)
    cols = list(df.columns)
    out = []
    for row in df.itertuples(index=False, name=None):
        out.append({c: _jsonable(v) for c, v in zip(cols, row)})
    return out


def _gz_b64(text: str) -> str:
    return base64.b64encode(gzip.compress(text.encode("utf-8"), 6)).decode("ascii")


def _records_gz(df: pd.DataFrame) -> str:
    return _gz_b64(json.dumps(_jsonable_records(df), ensure_ascii=False,
                              separators=(",", ":")))


# ---------------------------------------------------------------------------
# Data registration
# ---------------------------------------------------------------------------

def _sql_lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float, np.integer, np.floating)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _register(con: duckdb.DuckDBPyConnection, name: str, obj) -> pd.DataFrame:
    """Register a DataFrame / Series / list / dict / data file as view `name`."""
    if isinstance(obj, (str, Path)):
        p = str(obj)
        ext = p.lower().rsplit(".", 1)[-1]
        q = p.replace("'", "''")
        if ext == "parquet":
            df = con.execute(f"SELECT * FROM read_parquet('{q}')").df()
        elif ext == "csv":
            df = con.execute(f"SELECT * FROM read_csv_auto('{q}', header=true)").df()
        elif ext in ("xlsx", "xls"):
            df = pd.read_excel(p)
        else:
            raise ValueError(f"unsupported data file extension: .{ext}")
    elif isinstance(obj, pd.Series):
        df = obj.to_frame(name=obj.name if obj.name is not None else "value")
    elif isinstance(obj, pd.DataFrame):
        df = obj.copy()
    elif isinstance(obj, (list, dict)):
        df = pd.DataFrame(obj)
    else:
        raise TypeError(f"unsupported data type: {type(obj).__name__}")
    con.register(f"{name}__raw", df)
    con.execute(f'CREATE OR REPLACE VIEW "{name}" AS SELECT * FROM "{name}__raw"')
    return df


class DuckData:
    """SQL access wrapper: dd.data.sql('SELECT ...') / dd.data.df"""

    def __init__(self, con: duckdb.DuckDBPyConnection, table: str = "t"):
        self._con = con
        self.table = table

    def sql(self, query: str, params: dict | None = None) -> pd.DataFrame:
        """Run arbitrary SQL against the in-memory DuckDB (local tool — the
        query is executed as-is; do not expose to untrusted input)."""
        return self._con.execute(query, params or {}).df()

    @property
    def df(self) -> pd.DataFrame:
        return self.sql(f'SELECT * FROM "{self.table}"')


# ---------------------------------------------------------------------------
# DuckDive
# ---------------------------------------------------------------------------

class DuckDive:
    """Turn a pandas DataFrame (or data file) into a self-contained HTML report.

    data: pd.DataFrame | pd.Series | list | dict | file path (.parquet/.csv/
          .xlsx/.xls) | dict of those (multiple tables; the first — or the one
          named 't' — becomes the primary table).
    template: path to an HTML template with {{ duckdive_table }} and friends.
    duckdb_wasm: True/False; None = auto (>WASM_THRESHOLD rows -> WASM).
            WASM mode also changes the surface area: SQL views are rebuilt on
            every cross-filter change ({{src}} = filtered base), standalone
            duckdive_filter/duckdive_toggles publish GFC atoms, and row atoms
            (selection/search) are available via use_filter_keys=. JSON mode
            ships rows once and applies the same owned-atom cross-filter rules
            in JS for in-memory views.
    params: values for :param placeholders in view/label SQL (row-level
            slicing, bound at build time).
    redact_columns: columns masked as '••••' before the data is embedded
            (build-time — raw values never reach the HTML).
    template_vars: extra variables for the Jinja2 pass of the template.
    """

    def __init__(self, data=None, template: str | None = None,
                 title: str | None = None, page_size: int = 20,
                 duckdb_wasm: bool | None = None,
                 params: dict | None = None,
                 redact_columns: list[str] | None = None,
                 template_vars: dict | None = None):
        self.title = title or "DuckDive report"
        self.page_size = page_size
        self.params = params or {}
        self.redact_columns = list(redact_columns or [])
        self.template_vars = template_vars or {}
        self.template_path = template
        self.boot: dict | None = None
        self.html: str | None = None

        self._con = duckdb.connect()
        self._tables: dict[str, pd.DataFrame] = {}
        if data is None:
            pass
        elif isinstance(data, dict) and not _looks_like_records(data):
            for name, obj in data.items():
                tname = _validate_table_name(str(name))
                self._tables[tname] = _register(self._con, tname, obj)
        else:
            self._tables["t"] = _register(self._con, "t", data)
        if not self._tables:
            raise ValueError("DuckDive: no data given")
        self.table = "t" if "t" in self._tables else next(iter(self._tables))
        self.data = DuckData(self._con, self.table)

        if duckdb_wasm is None:
            duckdb_wasm = len(self._tables[self.table]) > WASM_THRESHOLD
        self.duckdb_wasm = duckdb_wasm

    # -- schema ------------------------------------------------------------

    def _describe(self, table: str) -> list[dict]:
        rows = self._con.execute(f'DESCRIBE "{table}"').fetchall()
        return [{"name": r[0], "type": r[1]} for r in rows]

    # -- template ----------------------------------------------------------

    def _template_text(self) -> str:
        if self.template_path:
            return Path(self.template_path).read_text(encoding="utf-8")
        return _DEFAULT_TEMPLATE.read_text(encoding="utf-8")

    def _subst_params(self, sql: str) -> str:
        """Bind :name placeholders in SQL with literal values (build time)."""
        for k, v in self.params.items():
            sql = re.sub(rf":{re.escape(k)}\b", _sql_lit(v), sql)
        return sql

    # -- parquet for WASM mode ----------------------------------------------

    def _parquet_b64(self, table: str, redact: list[str]) -> str:
        redact = [c for c in redact if c in self._tables[table].columns]
        fd, tmp = tempfile.mkstemp(suffix=".parquet")
        os.close(fd)
        try:
            src = f'"{table}"'
            if redact:
                cols = self._describe(table)
                sel = ", ".join(
                    f"CASE WHEN \"{c['name']}\" IS NULL THEN NULL ELSE '••••' END "
                    f"AS \"{c['name']}\"" if c["name"] in redact else f'"{c["name"]}"'
                    for c in cols
                )
                src = f"(SELECT {sel} FROM \"{table}\")"
            self._con.execute(
                f"COPY (SELECT *, ROW_NUMBER() OVER () AS __rn FROM {src}) "
                f"TO '{tmp.replace(chr(39), chr(39) * 2)}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            return base64.b64encode(Path(tmp).read_bytes()).decode("ascii")
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -- render --------------------------------------------------------------

    def render(self) -> str:
        from jinja2 import Environment

        template_text = self._template_text()
        placeholders = table_ui.scan_placeholders(template_text)

        views, filters, toggles, labels_json, labels_wasm = [], [], [], [], []
        replacements: list[tuple[int, int, str]] = []
        redact = list(self.redact_columns)
        n_table = n_filter = n_toggle = n_label = 0

        base_cols = self._describe(self.table)
        base_col_names = [c["name"] for c in base_cols]

        for ph in placeholders:
            p = table_ui.parse_params(ph["params"])
            kind = ph["kind"]

            if kind == "duckdive_table":
                root = "duckdive-table" if n_table == 0 else f"duckdive-table-{n_table}"
                pfx = "" if n_table == 0 else f"dd{n_table + 1}"
                spec = {
                    "root": root,
                    "pfx": pfx,
                    "cross": p.get("cross_filters", p.get("cross", True)) is not False,
                    "page_size": int(p.get("page_size") or self.page_size),
                    "title": p.get("title"),
                    "name": p.get("name"),
                }
                if p.get("sql"):
                    spec["sql"] = self._subst_params(str(p["sql"]))
                if p.get("use_filter_keys"):
                    spec["use_filter_keys"] = str(p["use_filter_keys"])
                if p.get("filter_columns"):
                    spec["filter_columns"] = str(p["filter_columns"])
                cp = table_ui.normalize_cell_popup(p.get("cell_popup"))
                if cp:
                    spec["cell_popup"] = cp
                rc = p.get("redact_columns")
                if rc:
                    redact.extend(rc if isinstance(rc, list) else [rc])
                views.append(spec)
                replacements.append(
                    (ph["start"], ph["end"],
                     f'<div id="{root}" class="duckdive-root"></div>'))
                n_table += 1

            elif kind == "duckdive_label":
                sql = self._subst_params(str(p.get("sql", "")))
                spec = parse_label_sql(sql, base_col_names)
                lid = f"dd-label-{n_label}"
                n_label += 1
                use_filters = p.get("use_table_filters", True) is not False
                fmt = p.get("fmt")
                try:
                    value = self._con.execute(sql).fetchone()[0]
                except Exception as e:
                    raise label_ui.LabelSqlError(f"label SQL failed: {e}") from e
                initial = _fmt_label_value(value, fmt)
                base_sql = sql.strip().rstrip(";")
                sqlf = base_sql + (" AND (__DDW__)" if spec["where"]
                                   else " WHERE (__DDW__)")
                lbl = {"id": lid, "value": initial, "fmt": fmt,
                       "useFilters": use_filters, "sql": base_sql, "sqlf": sqlf,
                       "agg": spec["agg"], "col": spec["col"],
                       "distinct": spec["distinct"], "atoms": spec["atoms"]}
                labels_json.append(lbl)
                labels_wasm.append(lbl)
                replacements.append(
                    (ph["start"], ph["end"],
                     f'<span class="dd-label" id="{lid}">{initial}</span>'))

            elif kind == "duckdive_filter":
                col = str(p.get("column", ""))
                if col not in base_col_names:
                    raise ValueError(f"duckdive_filter: unknown column {col!r}")
                root = f"duckdive-filter-{n_filter}"
                n_filter += 1
                filters.append({"root": root, "column": col,
                                "name": f"flt_{col}",
                                "title": p.get("title") or col})
                replacements.append(
                    (ph["start"], ph["end"],
                     f'<span id="{root}" class="duckdive-root dd-filter-root"></span>'))

            elif kind == "duckdive_toggles":
                col = str(p.get("column", ""))
                if col not in base_col_names:
                    raise ValueError(f"duckdive_toggles: unknown column {col!r}")
                items = p.get("items") or []
                if isinstance(items, str):
                    items = json.loads(items)
                root = f"duckdive-toggles-{n_toggle}"
                n_toggle += 1
                toggles.append({"root": root, "column": col,
                                "name": f"tgl_{col}", "items": items})
                replacements.append(
                    (ph["start"], ph["end"],
                     f'<span id="{root}" class="dd-toggles"></span>'))

        if not views:
            views = [{"root": "duckdive-table", "pfx": "", "cross": True,
                      "page_size": self.page_size, "title": None, "name": None}]

        # build-time redaction: raw values never reach the HTML
        redact = sorted(set(redact))
        if redact:
            for tname, df in list(self._tables.items()):
                masked = df.copy()
                for c in redact:
                    if c in masked.columns:
                        masked[c] = masked[c].where(masked[c].isna(), "••••")
                _register(self._con, tname, masked)
                self._tables[tname] = masked

        if self.duckdb_wasm:
            from . import wasm_ui

            base_boot = {
                "title": self.title,
                "page_size": self.page_size,
                "columns": self._describe(self.table),
                "parquet": self._parquet_b64(self.table, []),  # already masked
                "wasm": True,
            }
            if len(self._tables) > 1:
                base_boot["tables"] = {
                    name: self._parquet_b64(name, [])
                    for name in self._tables if name != self.table
                }
            self.boot = dict(base_boot)
            self.boot["views"] = views
            if filters:
                self.boot["filters"] = filters
            if toggles:
                self.boot["toggles"] = toggles
            block = wasm_ui.wasm_table_block(views, base_boot,
                                             filters or None, toggles or None)
            block += label_block(labels_wasm)
        else:
            boot = {
                "title": self.title,
                "page_size": self.page_size,
                "columns": self._describe(self.table),
                "records_gz": _records_gz(self._tables[self.table]),
                "views": views,
                "labels": labels_json,
            }
            if filters:
                boot["filters"] = filters
            if toggles:
                boot["toggles"] = toggles
            for spec in views:
                if spec.get("sql"):
                    # JSON mode has no runtime view rebuild: {{src}} is the base
                    # table; cross-filtering applies via the shared filter pool
                    # on columns the view actually has.
                    vsql = spec["sql"].replace("{{src}}", f'"{self.table}"')
                    vdf = self._con.execute(vsql).df()
                    spec["columns"] = [{"name": c, "type": str(t)}
                                       for c, t in zip(vdf.columns, vdf.dtypes)]
                    spec["records_gz"] = _records_gz(vdf)
            self.boot = boot
            block = table_ui.table_block(boot)
            block += label_block([])  # JSON-mode labels live in table_ui._JS

        # replace placeholders (right to left to keep offsets valid)
        html = template_text
        for start, end, repl in sorted(replacements, reverse=True):
            html = html[:start] + repl + html[end:]

        env = Environment(autoescape=False)
        html = env.from_string(html).render(title=self.title, **self.template_vars)

        m = re.search(r"</body\s*>", html, re.I)
        if m:
            html = html[:m.start()] + block + html[m.start():]
        else:
            html += block
        self.html = html
        return html

    def save(self, path: str | os.PathLike) -> Path:
        html = self.html or self.render()
        p = Path(path)
        p.write_text(html, encoding="utf-8")
        return p

    # -- round-trip -----------------------------------------------------------

    @classmethod
    def read(cls, path: str | os.PathLike) -> "DuckDive":
        """Parse the boot JSON back out of a saved report (round-trip)."""
        html = Path(path).read_text(encoding="utf-8")
        m = re.search(
            r'<script id="duckdive-boot" type="application/json">(.*?)</script>',
            html, re.S)
        if not m:
            raise ValueError("not a DuckDive report: boot JSON not found")
        boot = json.loads(m.group(1))
        if boot.get("wasm"):
            fd, tmp = tempfile.mkstemp(suffix=".parquet")
            os.close(fd)
            try:
                Path(tmp).write_bytes(base64.b64decode(boot["parquet"]))
                con = duckdb.connect()
                df = con.execute(
                    f"SELECT * FROM read_parquet('{tmp}')").df()
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        else:
            recs = json.loads(gzip.decompress(
                base64.b64decode(boot["records_gz"])).decode("utf-8"))
            df = pd.DataFrame(recs)
        if "__rn" in df.columns:
            df = df.drop(columns=["__rn"])
        dd = cls(df, title=boot.get("title"),
                 page_size=boot.get("page_size", 20),
                 duckdb_wasm=bool(boot.get("wasm")))
        dd.boot = boot
        dd.html = html
        return dd


def _looks_like_records(d) -> bool:
    """Heuristic: a dict of scalars/lists is one table (records), not a
    multi-table mapping."""
    return not any(isinstance(v, (pd.DataFrame, pd.Series)) or
                   isinstance(v, (str, Path)) and
                   str(v).lower().rsplit(".", 1)[-1] in ("parquet", "csv", "xlsx", "xls")
                   for v in d.values())


def _fmt_label_value(v, fmt) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if fmt == "percent":
        return f"{f * 100:,.2f}".replace(",", " ").replace(".", ",") + "%"
    if f == int(f) and abs(f) < 1e15:
        return f"{int(f):,}".replace(",", " ")
    return f"{f:,.6f}".rstrip("0").rstrip(".")
