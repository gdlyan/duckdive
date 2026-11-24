"""duckdive_label: live SQL aggregate labels that recompute on filter changes.

SQL subset (validated at build time):
    SELECT <count|sum|avg|min|max>([DISTINCT] <column|*>) FROM <table>
    [WHERE <atom> AND <atom> ...]
atom := column IS [NOT] NULL | column <op> literal   (op: = != <> > < >= <=)
"""
import json
import re

_LABEL_RE = re.compile(
    r"^\s*SELECT\s+(count|sum|avg|min|max)\s*\(\s*(DISTINCT\s+)?(\"[\w ]+\"|[\w]+|\*)\s*\)"
    r"\s+FROM\s+(\"[\w ]+\"|[\w]+)\s*(?:WHERE\s+(.+?))?\s*;?\s*$",
    re.I | re.S,
)
_ATOM_NULL_RE = re.compile(r"^\s*(\"[^\"]+\"|[\w]+)\s+IS\s+(NOT\s+)?NULL\s*$", re.I)
_ATOM_CMP_RE = re.compile(
    r"^\s*(\"[^\"]+\"|[\w]+)\s*(=|!=|<>|>=|<=|>|<)\s*('(?:[^']|'')*'|-?\d+(?:\.\d+)?|true|false)\s*$",
    re.I,
)


class LabelSqlError(ValueError):
    pass


def _unq(ident: str) -> str:
    return ident[1:-1] if ident.startswith('"') else ident


def _split_and(where: str) -> list[str]:
    """Split WHERE on top-level AND (outside quotes/parens)."""
    parts, depth, quote, buf = [], 0, None, []
    i, n = 0, len(where)
    while i < n:
        ch = where[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                if where[i + 1:i + 2] == quote:  # '' escape
                    buf.append(quote)
                    i += 1
                else:
                    quote = None
        else:
            if ch == "'":
                quote = ch
                buf.append(ch)
            elif ch == "(":
                depth += 1
                buf.append(ch)
            elif ch == ")":
                depth -= 1
                buf.append(ch)
            elif depth == 0 and re.match(r"(?i)\s+AND\s+", where[i:]):
                m = re.match(r"(?i)\s+AND\s+", where[i:])
                parts.append("".join(buf))
                buf = []
                i += m.end()
                continue
            else:
                buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def parse_label_sql(sql: str, columns: list[str]) -> dict:
    """Validate label SQL against the restricted subset. Returns a spec dict."""
    m = _LABEL_RE.match(sql)
    if not m:
        raise LabelSqlError(
            "label SQL must be: SELECT <count|sum|avg|min|max>([DISTINCT] col|*) "
            "FROM <table> [WHERE atoms AND ...]"
        )
    agg, distinct, col, table, where = m.groups()
    agg = agg.lower()
    distinct = bool(distinct)
    col = _unq(col)
    table = _unq(table)
    if agg != "count" and col == "*":
        raise LabelSqlError(f"{agg}(*) is not allowed; use count(*) or {agg}(column)")
    known = set(columns)
    if col != "*" and col not in known:
        raise LabelSqlError(f"unknown column in label SQL: {col!r}")
    atoms = []
    if where:
        for atom in _split_and(where):
            mn = _ATOM_NULL_RE.match(atom)
            if mn:
                c = _unq(mn.group(1))
                if c not in known:
                    raise LabelSqlError(f"unknown column in label SQL: {c!r}")
                atoms.append({"col": c, "nullOp": "notnull" if mn.group(2) else "null"})
                continue
            mc = _ATOM_CMP_RE.match(atom)
            if not mc:
                raise LabelSqlError(f"unsupported WHERE atom in label SQL: {atom!r}")
            c, op, lit = _unq(mc.group(1)), mc.group(2), mc.group(3)
            if c not in known:
                raise LabelSqlError(f"unknown column in label SQL: {c!r}")
            if lit.startswith("'"):
                val = lit[1:-1].replace("''", "'")
                num = False
            elif lit.lower() in ("true", "false"):
                val = lit.lower() == "true"
                num = True
            else:
                val = float(lit) if "." in lit else int(lit)
                num = True
            atoms.append({"col": c, "op": op, "val": val, "num": num})
    return {"agg": agg, "distinct": distinct, "col": col, "table": table,
            "atoms": atoms, "where": (where or "").strip()}


def label_block(labels: list[dict]) -> str:
    """JS that recomputes labels in WASM mode (re-runs SQL via window.__ddq).

    JSON mode recomputation lives in table_ui._JS (client-side aggregation);
    this block only activates when the WASM runtime is present (__ddq).
    """
    if not labels:
        return ""
    specs = json.dumps(labels, ensure_ascii=False).replace("</", "<\\/")
    return (
        '<script id="duckdive-labels" type="application/json">' + specs + "</script>"
        """<script>
(function(){
  var LABELS=JSON.parse(document.getElementById('duckdive-labels').textContent);
  var t=null;
  function fmt(v,f){
    if(v==null)return '—';
    if(f==='percent')return (v*100).toLocaleString('ru-RU',{maximumFractionDigits:2})+'%';
    return Number(v).toLocaleString('ru-RU',{maximumFractionDigits:6});
  }
  async function recompute(){
    if(!window.__ddq)return; /* JSON mode handled by the table runtime */
    for(const sp of LABELS){
      const el=document.getElementById(sp.id);
      if(!el)continue;
      try{
        let sql=sp.sql;
        const w=sp.useFilters&&window.__ddTableApi?window.__ddTableApi.where():'';
        if(w)sql=sp.sqlf.split('__DDW__').join(w);
        const rows=await window.__ddq(sql);
        const k=rows.length?Object.keys(rows[0])[0]:null;
        el.textContent=fmt(k==null?null:rows[0][k],sp.fmt);
      }catch(e){console.warn('duckdive label:',e)}
    }
  }
  function sched(){clearTimeout(t);t=setTimeout(recompute,150)}
  document.addEventListener('dd-gfc-changed',sched);
  document.addEventListener('dd-filters-changed',sched);
  document.addEventListener('dd-table-ready',sched);
})();
</script>"""
    )
