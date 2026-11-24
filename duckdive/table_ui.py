"""duckdive table UI: placeholder scanner, CSS/JS assets, JSON-mode runtime.

core.py knows nothing about HTML; this module knows nothing about data loading.
wasm_ui.py reuses _CSS, _POPUP_SRC, _xlsx_src(), _json_escape_script(),
_inject_icons() from here.
"""
import json
import re
from pathlib import Path

_ASSETS = Path(__file__).resolve().parent / "assets"
_XLSX_CACHE: str | None = None

_PLACEHOLDER_KINDS = (
    "duckdive_table",
    "duckdive_label",
    "duckdive_filter",
    "duckdive_toggles",
)


def _xlsx_src() -> str:
    """SheetJS, loaded once from assets/xlsx.full.min.js (inlined into HTML)."""
    global _XLSX_CACHE
    if _XLSX_CACHE is None:
        _XLSX_CACHE = (_ASSETS / "xlsx.full.min.js").read_text(encoding="utf-8")
    return _XLSX_CACHE


def _json_escape_script(s: str) -> str:
    """JSON-encode a string for embedding as a JS string literal inside <script>."""
    return (
        json.dumps(s, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


# ---------------------------------------------------------------------------
# Placeholder scanning (brace-aware, quote-aware)
# ---------------------------------------------------------------------------

def scan_placeholders(text: str) -> list[dict]:
    """Find all {{ duckdive_* ... }} placeholders.

    Returns a list of {start, end, kind, params} (params = raw param text).
    Nested {} / [] inside params and quoted strings are handled correctly, so
    e.g. {{ duckdive_table cell_popup={"col": "x", "formatter": fn(a=1)} }}
    scans as one placeholder.
    """
    out = []
    i, n = 0, len(text)
    while True:
        j = text.find("{{", i)
        if j < 0:
            break
        k = j + 2
        while k < n and text[k] in " \t":
            k += 1
        m = re.match(r"[a-z_][a-z0-9_]*", text[k:])
        if not m or m.group(0) not in _PLACEHOLDER_KINDS:
            i = j + 2
            continue
        kind = m.group(0)
        p = k + m.end()
        depth, quote, esc = 0, None, False
        q = p
        end = -1
        while q < n:
            ch = text[q]
            if quote:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == quote:
                    quote = None
            else:
                if depth == 0 and ch == "}" and text[q + 1:q + 2] == "}":
                    end = q + 2
                    break
                if ch in "\"'":
                    quote = ch
                elif ch in "{[":
                    depth += 1
                elif ch in "}]":
                    depth -= 1
            q += 1
        if end < 0:
            break  # unbalanced — stop scanning
        out.append({"start": j, "end": end, "kind": kind, "params": text[p:q]})
        i = end
    return out


def _split_top(s: str, sep: str) -> list[str]:
    """Split on sep at bracket depth 0, outside quotes."""
    parts, depth, quote, esc, buf = [], 0, None, False, []
    for ch in s:
        if quote:
            buf.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "{[(":
            depth += 1
            buf.append(ch)
        elif ch in "}])":
            depth -= 1
            buf.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_value(s: str, i: int):
    """Parse one value starting at s[i]. Returns (value, next_index)."""
    n = len(s)
    ch = s[i]
    if ch in "\"'":
        q = ch
        i += 1
        buf = []
        while i < n:
            c = s[i]
            if c == "\\" and i + 1 < n:
                buf.append(s[i + 1])
                i += 2
                continue
            if c == q:
                return "".join(buf), i + 1
            buf.append(c)
            i += 1
        return "".join(buf), i
    if ch in "{[":
        depth, quote, esc, start = 0, None, False, i
        while i < n:
            c = s[i]
            if quote:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == quote:
                    quote = None
            else:
                if c in "\"'":
                    quote = c
                elif c in "{[":
                    depth += 1
                elif c in "}]":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        return _parse_composite(s[start:i]), i
            i += 1
        return s[start:], n
    m = re.match(r"[^\s]+", s[i:])
    tok = m.group(0)
    i += len(tok)
    if tok in ("true", "True"):
        return True, i
    if tok in ("false", "False"):
        return False, i
    if re.fullmatch(r"-?\d+", tok):
        return int(tok), i
    if re.fullmatch(r"-?\d+\.\d+", tok):
        return float(tok), i
    return tok, i


_FUNCALL_RE = re.compile(r"^([A-Za-z_$][\w$]*)\((.*)\)$", re.S)


def _parse_scalar(v: str):
    """Scalar inside a composite: string / number / bool / fn(k=v,...) / nested."""
    v = v.strip()
    if not v:
        return ""
    if v[0] in "\"'":
        val, _ = _parse_value(v, 0)
        return val
    if v[0] in "{[":
        return _parse_composite(v)
    m = _FUNCALL_RE.match(v)
    if m:
        opts = {}
        for part in _split_top(m.group(2), ","):
            if not part.strip():
                continue
            k, _, val = part.partition("=")
            opts[k.strip()] = _parse_scalar(val)
        return {"__fn__": m.group(1), "opts": opts}
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def _parse_composite(txt: str):
    """Parse {...} / [...] param values; JSON first, tolerant fallback after."""
    try:
        return json.loads(txt)
    except Exception:
        pass
    txt = txt.strip()
    if txt.startswith("{"):
        out = {}
        for part in _split_top(txt[1:-1], ","):
            if not part.strip():
                continue
            k, _, v = part.partition(":")
            out[k.strip().strip("\"'")] = _parse_scalar(v)
        return out
    if txt.startswith("["):
        return [_parse_scalar(p) for p in _split_top(txt[1:-1], ",") if p.strip()]
    return txt


def parse_params(s: str) -> dict:
    """Parse `key=value key2=value2 ...` placeholder params."""
    params = {}
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", s[i:])
        if not m:
            break
        key = m.group(0)
        i += m.end()
        while i < n and s[i].isspace():
            i += 1
        if i < n and s[i] == "=":
            i += 1
            while i < n and s[i].isspace():
                i += 1
            val, i = _parse_value(s, i)
            params[key] = val
        else:
            params[key] = True
    return params


def normalize_cell_popup(cp) -> list[dict]:
    """cell_popup={"col": "x", "formatter": fn(a=1)} -> [{col, formatter, opts}]."""
    if not cp:
        return []
    items = cp if isinstance(cp, list) else [cp]
    out = []
    for it in items:
        if not isinstance(it, dict) or "col" not in it:
            continue
        fmt = it.get("formatter")
        if isinstance(fmt, dict) and "__fn__" in fmt:
            out.append({"col": it["col"], "formatter": fmt["__fn__"],
                        "opts": fmt.get("opts") or {}})
        elif isinstance(fmt, str):
            out.append({"col": it["col"], "formatter": fmt,
                        "opts": it.get("opts") or {}})
        else:
            out.append({"col": it["col"], "formatter": None,
                        "opts": it.get("opts") or {}})
    return out


# ---------------------------------------------------------------------------
# Icons (inline SVG, single-quoted attrs — they land inside JS string literals)
# ---------------------------------------------------------------------------

_ICONS = {
    "columns": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5'><rect x='1.5' y='2.5' width='13' height='11' rx='1.5'/><path d='M6.3 2.5v11M9.7 2.5v11'/></svg>",
    "download": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><path d='M8 2.5v7.5M4.8 7.2 8 10.5l3.2-3.3M3 13.5h10'/></svg>",
    "eye": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5'><path d='M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8Z'/><circle cx='8' cy='8' r='2'/></svg>",
    "eyeoff": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round'><path d='M3 3l10 10M6.6 4C7 3.7 7.5 3.5 8 3.5c4 0 6.5 4.5 6.5 4.5a11 11 0 0 1-2.1 2.7M9.9 12.1A6 6 0 0 1 8 12.5C4 12.5 1.5 8 1.5 8a11.6 11.6 0 0 1 2.4-2.9'/></svg>",
    "table": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5'><rect x='1.5' y='2.5' width='13' height='11' rx='1.5'/><path d='M1.5 6h13M6 6v7.5'/></svg>",
    "grid": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5'><rect x='1.5' y='1.5' width='5.6' height='5.6' rx='1'/><rect x='8.9' y='1.5' width='5.6' height='5.6' rx='1'/><rect x='1.5' y='8.9' width='5.6' height='5.6' rx='1'/><rect x='8.9' y='8.9' width='5.6' height='5.6' rx='1'/></svg>",
    "braces": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round'><path d='M5.5 2.5c-1.5 0-2 .8-2 2v2c0 1-.5 1.5-1.5 1.5 1 0 1.5.5 1.5 1.5v2c0 1.2.5 2 2 2M10.5 2.5c1.5 0 2 .8 2 2v2c0 1 .5 1.5 1.5 1.5-1 0-1.5.5-1.5 1.5v2c0 1.2-.5 2-2 2'/></svg>",
    "chd": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M4 6.5 8 10.5 12 6.5'/></svg>",
    "chl": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M10 3.5 5.5 8 10 12.5'/></svg>",
    "chr": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M6 3.5 10.5 8 6 12.5'/></svg>",
    "chdl": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M8 3.5 3.5 8 8 12.5M12.5 3.5 8 8l4.5 4.5'/></svg>",
    "chdr": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M3.5 3.5 8 8l-4.5 4.5M8 3.5 12.5 8 8 12.5'/></svg>",
    "search": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round'><circle cx='7' cy='7' r='4.5'/><path d='m10.5 10.5 3.5 3.5'/></svg>",
    "x": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round'><path d='M4 4l8 8M12 4l-8 8'/></svg>",
    "copy": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5'><rect x='5.5' y='5.5' width='8' height='9' rx='1.5'/><path d='M10.5 3.5v-1a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 2.5V10A1.5 1.5 0 0 0 4 11.5h1'/></svg>",
    "check": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='m3 8.5 3.5 3.5L13 4.5'/></svg>",
    "filter": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linejoin='round'><path d='M2 3h12l-4.7 5.5v4L6.7 14V8.5Z'/></svg>",
    "updown": "<svg viewBox='0 0 16 16' fill='currentColor' opacity='.45'><path d='M8 2.5 11 6.5H5ZM8 13.5l3-4H5Z'/></svg>",
    "upwide": "<svg viewBox='0 0 16 16' fill='currentColor'><path d='M8 3 12 9.5H4Z'/></svg>",
    "downwide": "<svg viewBox='0 0 16 16' fill='currentColor'><path d='M8 13 4 6.5h8Z'/></svg>",
}


def _inject_icons(js: str) -> str:
    """Replace __ICON_*__ placeholders with inline SVG markup."""
    for name, svg in _ICONS.items():
        js = js.replace("__ICON_" + name.upper() + "__", svg)
    return js


# ---------------------------------------------------------------------------
# CSS (shared by JSON and WASM modes) — CSS vars, dark mode, print stylesheet
# ---------------------------------------------------------------------------

_CSS = r"""
.duckdive-root{
  --dd-bg:#ffffff; --dd-text:#242428; --dd-muted:#6f6f77; --dd-faint:#a0a0a8;
  --dd-border:#e6e6ea; --dd-soft:#f6f6f8; --dd-zebra:#fafafc; --dd-hover:#f0f0f4;
  --dd-accent:#e8590c; --dd-accent-soft:#fff3e8; --dd-accent-text:#d9480f;
  --dd-info:#1971c2; --dd-info-bg:#e7f3fe; --dd-info-border:#a5d8ff;
  --dd-warn-bg:#fff8e1; --dd-warn-border:#ffe08a; --dd-warn-text:#8d6e00;
  --dd-danger:#d64545; --dd-pop-bg:#ffffff;
  --dd-shadow:0 10px 32px rgba(20,20,25,.12);
  --dd-ring:0 0 0 3px rgba(232,89,12,.16);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
  font-size:13px;line-height:1.45;color:var(--dd-text);background:var(--dd-bg);
  border:1px solid var(--dd-border);border-radius:14px;overflow:hidden;margin:14px 0;
  box-shadow:0 1px 2px rgba(20,20,25,.04),0 10px 28px rgba(20,20,25,.05);
}
.duckdive-root *{box-sizing:border-box}
.duckdive-root button{font-family:inherit}
.duckdive-root svg{width:14px;height:14px;flex:none}
.duckdive-root button:focus-visible,.duckdive-root input:focus-visible,.duckdive-root select:focus-visible{outline:0;box-shadow:var(--dd-ring)}

/* topbar */
.dd-topbar{display:flex;align-items:center;gap:10px;padding:10px 12px;border-bottom:1px solid var(--dd-border);background:linear-gradient(180deg,var(--dd-bg),var(--dd-soft))}
.dd-topbar .spacer,.dd-pager .bbspace{flex:1}
.dd-srch{display:flex;align-items:center;gap:7px;flex:0 1 340px;min-width:150px;border:1px solid var(--dd-border);border-radius:999px;padding:6px 11px;color:var(--dd-faint);background:var(--dd-bg);transition:border-color .15s,box-shadow .15s}
.dd-srch:focus-within{border-color:var(--dd-accent);color:var(--dd-accent);box-shadow:var(--dd-ring)}
.dd-srch input{border:0;outline:0;flex:1;font-size:13px;background:transparent;color:var(--dd-text);min-width:0}
.dd-srch input::placeholder{color:var(--dd-faint)}
.dd-srch .qx{display:none;border:0;background:none;cursor:pointer;color:var(--dd-muted);padding:2px;line-height:1;border-radius:50%}
.dd-srch .qx:hover{background:var(--dd-hover);color:var(--dd-text)}
.dd-srch .qx.has{display:inline-flex}
.dd-srch .qx svg{width:11px;height:11px}
.dd-iconbtn{display:inline-flex;align-items:center;gap:7px;padding:6px 12px;border:1px solid var(--dd-border);border-radius:999px;background:var(--dd-bg);cursor:pointer;font-size:12px;font-weight:550;color:var(--dd-muted);line-height:1.35;white-space:nowrap;transition:background .15s,border-color .15s,color .15s,transform .05s}
.dd-iconbtn:hover{background:var(--dd-hover);color:var(--dd-text);border-color:#d8d8de}
.dd-iconbtn:active{transform:translateY(1px)}
.dd-iconbtn.open,.dd-iconbtn.on{color:var(--dd-accent-text);background:var(--dd-accent-soft);border-color:var(--dd-accent)}

/* badges */
.dd-badges{display:flex;flex-wrap:wrap;gap:7px;padding:9px 12px 0}
.dd-badges:empty{padding:0}
.dd-badge{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border:1px solid var(--dd-info-border);border-radius:999px;background:var(--dd-info-bg);color:var(--dd-info);font-size:11.5px;font-weight:550;max-width:360px;box-shadow:0 1px 0 rgba(20,20,25,.03)}
.dd-badge span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dd-badge .x{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;cursor:pointer;font-size:13px;line-height:1;opacity:.72}
.dd-badge .x:hover{opacity:1;background:rgba(25,113,194,.12)}
.dd-badge-in{background:var(--dd-warn-bg);border-color:var(--dd-warn-border);color:var(--dd-warn-text);border-style:dashed}
.dd-clear{cursor:pointer;color:var(--dd-accent-text);font-size:11.5px;font-weight:600;padding:4px 7px;align-self:center;border-radius:6px}
.dd-clear:hover{text-decoration:underline;background:var(--dd-accent-soft)}

/* table */
.dd-tscroll{overflow:auto;max-height:72vh;scrollbar-width:thin;scrollbar-color:var(--dd-faint) transparent}
.dd-tscroll::-webkit-scrollbar{height:10px;width:10px}
.dd-tscroll::-webkit-scrollbar-thumb{background:var(--dd-border);border-radius:8px;border:2px solid transparent;background-clip:content-box}
.dd-tscroll::-webkit-scrollbar-thumb:hover{background:var(--dd-faint);border:2px solid transparent;background-clip:content-box}
.duckdive-root table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px}
.duckdive-root th{position:sticky;top:0;background:var(--dd-soft);text-align:left;padding:0;border-bottom:1px solid var(--dd-border);z-index:3;box-shadow:inset 0 -1px 0 var(--dd-border)}
.duckdive-root th .hwrap{display:flex;align-items:center;gap:5px;padding:9px 12px;min-height:38px}
.duckdive-root th .cn{cursor:pointer;user-select:none;display:inline-flex;align-items:center;gap:5px;font-weight:650;letter-spacing:.01em;white-space:nowrap;color:var(--dd-text)}
.duckdive-root th .cn:hover{color:var(--dd-accent-text)}
.duckdive-root th .srt{display:inline-flex;opacity:.5;transition:opacity .15s,color .15s}
.duckdive-root th .srt-on{opacity:1;color:var(--dd-accent)}
.duckdive-root th .fb{display:inline-flex;cursor:pointer;color:var(--dd-faint);border-radius:6px;padding:3px;margin-left:auto;transition:background .15s,color .15s}
.duckdive-root th .fb:hover{background:var(--dd-border);color:var(--dd-text)}
.duckdive-root th .fb.on{color:var(--dd-accent-text);background:var(--dd-accent-soft)}
.duckdive-root td{padding:8px 12px;border-bottom:1px solid var(--dd-soft);max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:top;background-clip:padding-box}
.duckdive-root td.num{text-align:right;font-variant-numeric:tabular-nums}
.duckdive-root tbody tr:nth-child(even){background:var(--dd-zebra)}
.duckdive-root tbody tr:hover{background:var(--dd-hover)}
.duckdive-root tbody tr.sel{background:var(--dd-accent-soft)}
.duckdive-root tbody tr.sel:hover{background:var(--dd-accent-soft)}
.dd-empty{color:var(--dd-faint);padding:28px 12px;text-align:center;font-size:12.5px}
.dd-selcol{width:34px;min-width:34px;text-align:center}
.dd-selcol input{cursor:pointer;accent-color:var(--dd-accent)}

/* pager */
.dd-pager{display:flex;align-items:center;gap:12px;padding:8px 12px;border-top:1px solid var(--dd-border);flex-wrap:wrap;background:var(--dd-bg)}
.rowcount{color:var(--dd-muted);font-size:12px;white-space:nowrap}
.rowcount .hid{color:var(--dd-faint)}
.rowlink{border:0;background:none;color:var(--dd-accent-text);cursor:pointer;font-size:12px;font-weight:600;padding:2px 4px;border-radius:5px}
.rowlink:hover{text-decoration:underline;background:var(--dd-accent-soft)}
.pgctl{display:flex;align-items:center;gap:5px}
.pgbtn,.szbtn{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--dd-border);background:var(--dd-bg);border-radius:8px;cursor:pointer;color:var(--dd-muted);font-size:12px;padding:5px 9px;line-height:1.3;transition:background .15s,color .15s,border-color .15s}
.pgbtn:hover:not(:disabled),.szbtn:hover{background:var(--dd-hover);color:var(--dd-text)}
.pgbtn:disabled{opacity:.35;cursor:default}
.pgbtn.pgnum{font-weight:700;color:var(--dd-accent-text);background:var(--dd-accent-soft);border-color:var(--dd-accent);min-width:38px;justify-content:center}
.pgbtn svg,.szbtn svg{width:11px;height:11px}
.pginfo{color:var(--dd-muted);font-size:12px;display:inline-flex;align-items:center;gap:5px}
.dd-jump{display:flex;gap:6px;padding:6px 8px 8px;border-bottom:1px solid var(--dd-soft);margin-bottom:4px}
.dd-jump input{width:76px;border:1px solid var(--dd-border);border-radius:7px;padding:5px 7px;font-size:12.5px;background:var(--dd-bg);color:var(--dd-text);outline:0}
.dd-jump button{border:1px solid var(--dd-accent);background:var(--dd-accent);color:#fff;border-radius:7px;padding:5px 10px;font-size:12px;cursor:pointer}
.dd-jump button:hover{filter:brightness(1.05)}

/* menus & popups */
.dd-menu{position:fixed;z-index:1000;background:var(--dd-pop-bg);border:1px solid var(--dd-border);border-radius:12px;box-shadow:var(--dd-shadow);padding:6px;min-width:140px;overflow:hidden}
.dd-menulabel{font-size:10.5px;font-weight:700;color:var(--dd-faint);padding:6px 10px 4px;text-transform:uppercase;letter-spacing:.06em}
.dd-menuitem{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;cursor:pointer;color:var(--dd-text)}
.dd-menuitem:hover{background:var(--dd-hover)}
.dd-menuitem svg{color:var(--dd-muted)}
.dd-menuitem .lbl{font-size:12.5px;white-space:nowrap}
.dd-menuitem .desc{font-size:11px;color:var(--dd-faint);margin-left:auto;padding-left:18px;white-space:nowrap}
.dd-pop{position:fixed;z-index:1000;background:var(--dd-pop-bg);border:1px solid var(--dd-border);border-radius:12px;box-shadow:var(--dd-shadow);width:292px;display:flex;flex-direction:column;max-height:420px;overflow:hidden}
.dd-pop .srch{padding:9px 11px;border-bottom:0}
.dd-pop .srch input{width:100%;border:1px solid var(--dd-border);border-radius:8px;padding:6px 9px;font-size:12.5px;outline:0;background:var(--dd-bg);color:var(--dd-text)}
.dd-pop .srch input:focus{border-color:var(--dd-accent);box-shadow:var(--dd-ring)}
.dd-pop .vals{overflow-y:auto;min-height:44px;padding:5px}
.dd-pop .val{display:flex;align-items:center;gap:9px;padding:6px 8px;border-radius:7px;cursor:pointer;font-size:12.5px}
.dd-pop .val:hover{background:var(--dd-hover)}
.dd-pop .val input{accent-color:var(--dd-accent);cursor:pointer}
.dd-pop .val span:nth-child(2){overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dd-pop .val .cnt{margin-left:auto;color:var(--dd-faint);font-size:11px;font-variant-numeric:tabular-nums}
.dd-pop .sep{border-top:1px solid var(--dd-soft);margin:4px 0}
.dd-pop .cvrow{display:flex;align-items:center;gap:9px;padding:6px 8px;border-radius:7px;cursor:pointer;font-size:12.5px}
.dd-pop .cvrow:hover{background:var(--dd-hover)}
.dd-pop .cvrow.act{color:var(--dd-accent-text);font-weight:600}
.dd-pop .cvrow input{accent-color:var(--dd-accent);cursor:pointer}
.dd-pop .cvrow .typ{margin-left:auto;color:var(--dd-faint);font-size:10.5px;border:1px solid var(--dd-border);border-radius:5px;padding:1px 5px;background:var(--dd-bg)}
.dd-pop .load{color:var(--dd-faint);font-size:12px;padding:12px;text-align:center}
.dd-pop .num,.dd-pop .date{display:flex;gap:7px;padding:9px 11px;align-items:center}
.dd-pop .num select,.dd-pop .num input,.dd-pop .date input{border:1px solid var(--dd-border);border-radius:8px;padding:6px 8px;font-size:12.5px;background:var(--dd-bg);color:var(--dd-text);outline:0;min-width:0}
.dd-pop .num select:focus,.dd-pop .num input:focus,.dd-pop .date input:focus{border-color:var(--dd-accent);box-shadow:var(--dd-ring)}
.dd-pop .num input{width:96px}
.dd-pop .date label{display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--dd-muted);flex:1}
.dd-pop .mode{display:flex;gap:16px;padding:7px 13px;font-size:12.5px;color:var(--dd-text)}
.dd-pop .mode label{display:flex;align-items:center;gap:6px;cursor:pointer}
.dd-pop .mode input{accent-color:var(--dd-accent)}
.dd-pop .acts{display:flex;gap:7px;padding:9px 11px;border-top:1px solid var(--dd-soft);background:var(--dd-bg)}
.dd-pop .acts .b,.dd-pop .acts .bp{border-radius:8px;padding:6px 13px;font-size:12px;cursor:pointer}
.dd-pop .acts .b{border:1px solid var(--dd-border);background:var(--dd-bg);color:var(--dd-muted)}
.dd-pop .acts .b:hover{background:var(--dd-hover);color:var(--dd-text)}
.dd-pop .acts .bp{border:1px solid var(--dd-accent);background:var(--dd-accent);color:#fff;margin-left:auto;font-weight:650}
.dd-pop .acts .bp:hover{filter:brightness(1.06)}
.dd-pop2{max-height:300px}

/* tooltip */
.dd-tip{position:fixed;z-index:1100;background:#242428;color:#fafafa;font-size:12px;line-height:1.55;padding:9px 11px;border-radius:10px;max-width:440px;max-height:280px;overflow:auto;box-shadow:0 12px 34px rgba(0,0,0,.28);white-space:pre-wrap;word-break:break-word}
.dd-tip .tipacts{display:flex;gap:7px;margin-top:7px;border-top:1px solid rgba(255,255,255,.16);padding-top:7px}
.dd-tip .tipacts button{border:0;background:rgba(255,255,255,.13);color:#fafafa;border-radius:7px;cursor:pointer;padding:4px 8px;display:inline-flex}
.dd-tip .tipacts button:hover{background:rgba(255,255,255,.24)}
.dd-tip .tipacts button.ok{background:#2b8a3e}
.dd-tip .tipacts svg{width:12px;height:12px}

/* cell popup modal */
.dd-cpop{position:fixed;inset:0;z-index:1200;background:rgba(15,15,20,.42);display:flex;align-items:center;justify-content:center;padding:28px;backdrop-filter:blur(2px)}
.dd-cpop-box{background:var(--dd-pop-bg,#fff);color:var(--dd-text,#242428);border-radius:14px;box-shadow:0 20px 70px rgba(0,0,0,.32);max-width:780px;width:100%;max-height:82vh;display:flex;flex-direction:column;overflow:hidden;border:1px solid var(--dd-border,#e6e6ea)}
.dd-cpop-head{display:flex;align-items:center;gap:8px;padding:11px 15px;border-bottom:1px solid var(--dd-border,#e6e6ea);background:var(--dd-soft,#f6f6f8)}
.dd-cpop-head .t{font-weight:650;font-size:13px}
.dd-cpop-head button{border:1px solid var(--dd-border,#e6e6ea);background:var(--dd-bg,#fff);color:var(--dd-muted,#6f6f77);border-radius:8px;cursor:pointer;padding:5px 9px;display:inline-flex;align-items:center;gap:6px;font-size:12px;margin-left:auto}
.dd-cpop-head button:hover{background:var(--dd-hover,#f0f0f4)}
.dd-cpop-head button.ok{color:#2b8a3e;border-color:#2b8a3e}
.dd-cpop-body{padding:16px;overflow:auto;font-size:13px;line-height:1.6}
.dd-cpopup-raw{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;margin:0}

/* standalone filter + toggles */
.dd-filter-root{display:inline-block;vertical-align:middle;margin-left:10px}
.dd-toggles{display:inline-flex;gap:7px;vertical-align:middle;margin-left:10px;flex-wrap:wrap}
.dd-tbtn,.dd-fbtn{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border:1px solid var(--dd-border,#e6e6ea);border-radius:999px;background:var(--dd-bg,#fff);cursor:pointer;font-size:12px;color:var(--dd-muted,#5c5c64);font-family:inherit;line-height:1.4;transition:background .15s,border-color .15s,color .15s}
.dd-tbtn:hover,.dd-fbtn:hover{background:var(--dd-hover,#f0f0f4);color:var(--dd-text,#242428)}
.dd-tbtn.on,.dd-fbtn.on{color:var(--dd-info,#1971c2);background:var(--dd-info-bg,#e7f3fe);border-color:var(--dd-info-border,#a5d8ff);font-weight:650}
.dd-tbtn svg,.dd-fbtn svg{width:13px;height:13px}

/* labels */
.dd-label{font-variant-numeric:tabular-nums;font-weight:650}

/* dark mode */
@media (prefers-color-scheme: dark){
  .duckdive-root{
    --dd-bg:#17171a; --dd-text:#e7e7ea; --dd-muted:#a6a6ad; --dd-faint:#77777f;
    --dd-border:#37373d; --dd-soft:#202024; --dd-zebra:#1b1b1f; --dd-hover:#26262b;
    --dd-accent:#ff922b; --dd-accent-soft:#3a2814; --dd-accent-text:#ffa94d;
    --dd-info:#74c0fc; --dd-info-bg:#16324a; --dd-info-border:#2c5f8a;
    --dd-warn-bg:#3a2f0e; --dd-warn-border:#8d6e00; --dd-warn-text:#ffe082;
    --dd-danger:#ff8787; --dd-pop-bg:#1d1d21;
    --dd-shadow:0 12px 36px rgba(0,0,0,.55);
    --dd-ring:0 0 0 3px rgba(255,146,43,.2);
    box-shadow:0 1px 2px rgba(0,0,0,.35),0 14px 34px rgba(0,0,0,.28);
  }
  .dd-tip{background:#f4f4f5;color:#242428}
  .dd-tip .tipacts{border-top-color:rgba(0,0,0,.12)}
  .dd-tip .tipacts button{background:rgba(0,0,0,.08);color:#242428}
  .dd-tip .tipacts button:hover{background:rgba(0,0,0,.16)}
  .dd-tip .tipacts button.ok{background:#2b8a3e;color:#fff}
}

@media (prefers-reduced-motion: reduce){
  .duckdive-root *{transition:none!important;animation:none!important}
}

/* print: table only, all rows visible */
@media print{
  .dd-topbar,.dd-pager,.dd-badges,.dd-tbtn,.dd-fbtn,.dd-tip,.dd-cpop,.dd-menu,.dd-pop{display:none!important}
  .duckdive-root{border:0;overflow:visible;box-shadow:none;border-radius:0}
  .dd-tscroll{max-height:none;overflow:visible}
  .duckdive-root td{max-width:none;white-space:normal}
  .duckdive-root th{position:static}
  .dd-selcol{display:none}
}
"""


# ---------------------------------------------------------------------------
# JSON-mode runtime (records embedded as gzip+base64 JSON; all ops in JS)
# ---------------------------------------------------------------------------

_JS = r"""
(async function(){
const B=JSON.parse(document.getElementById('duckdive-boot').textContent);
const escH=s=>(s??'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const escA=s=>escH(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const fmtN=n=>n==null||n===''?'—':Number(n).toLocaleString('ru-RU');
const ICONS={cols:"__ICON_COLUMNS__",dl:"__ICON_DOWNLOAD__",eye:"__ICON_EYE__",eyeoff:"__ICON_EYEOFF__",table:"__ICON_TABLE__",grid:"__ICON_GRID__",braces:"__ICON_BRACES__",chd:"__ICON_CHD__",chl:"__ICON_CHL__",chr:"__ICON_CHR__",chdl:"__ICON_CHDL__",chdr:"__ICON_CHDR__",search:"__ICON_SEARCH__",x:"__ICON_X__",copy:"__ICON_COPY__",check:"__ICON_CHECK__",filter:"__ICON_FILTER__",updown:"__ICON_UPDOWN__",upwide:"__ICON_UPWIDE__",downwide:"__ICON_DOWNWIDE__"};
window.__ddFilters=window.__ddFilters||{};
window.__ddFiltersSrc=window.__ddFiltersSrc||{};
window.__ddTglExcl=window.__ddTglExcl||{};

async function gunzip(b64){
  const CH=1<<22,parts=[];
  for(let i=0;i<b64.length;i+=CH)parts.push(atob(b64.slice(i,i+CH)));
  const u8=parts.map(p=>{const b=new Uint8Array(p.length);for(let j=0;j<p.length;j++)b[j]=p.charCodeAt(j);return b});
  const ds=new Blob(u8).stream().pipeThrough(new DecompressionStream('gzip'));
  return await new Response(ds).text();
}
const BASE=JSON.parse(await gunzip(B.records_gz));
window.__ddBase=BASE;

const colType={};(B.columns||[]).forEach(c=>colType[c.name]=(c.type||'').toUpperCase());
const isNumC=n=>/INT|HUGEINT|DOUBLE|FLOAT|DEC|REAL/.test(colType[n]||'');
const isDateC=n=>/DATE|TIME/.test(colType[n]||'');

/* filter-spec matcher — mirrors the SQL colCond semantics */
function colMatch(v,f,isNum,isDate){
  const vals=f.values||[];
  if(vals.length||f.blank){
    let ok=false;
    if(vals.length)ok=vals.some(x=>String(x)===String(v));
    if(!ok&&f.blank)ok=(v==null||v==='');
    if(f.op==='notin'){
      if((v==null||v==='')&&!f.blank)return true; /* nulls survive exclusion */
      ok=!ok;
    }
    if(!ok)return false;
  }
  const hasRange=f.cmp&&((f.min??'')!==''||(f.cmp==='between'&&(f.max??'')!==''));
  if(hasRange){
    if(v==null||v==='')return f.op==='notin';
    const num=isNum;
    const x=num?Number(v):String(v);
    const mn=f.min,mx=f.max;
    let ok=true;
    if(f.cmp==='between'){
      if(mn!==''&&mn!=null)ok=ok&&(num?x>=Number(mn):x>=String(mn));
      if(mx!==''&&mx!=null)ok=ok&&(num?x<=Number(mx):x<=String(mx));
    }else{
      const y=num?Number(mn):String(mn);
      ok={eq:x===y,gt:x>y,gte:x>=y,lt:x<y,lte:x<=y}[f.cmp]??true;
    }
    if(f.op==='notin')ok=!ok;
    if(!ok)return false;
  }
  return true;
}
function tglPass(r,cols){
  const ex=window.__ddTglExcl||{};
  for(const col in ex){
    if(cols&&!cols.includes(col))continue;
    if(ex[col]&&ex[col].length&&ex[col].includes(String(r[col])))return false;
  }
  return true;
}

/* ── duckdive_table instance (JSON mode) ── */
async function initInstance(cfg){
const root=document.getElementById(cfg.root);
if(!root)return;
const P=cfg.pfx??'';
const $=id=>document.getElementById(P+id);
const COLS=(cfg.columns||B.columns).filter(c=>c.name!=='__rn');
COLS.forEach(c=>{if(!colType[c.name])colType[c.name]=(c.type||'').toUpperCase()});
const RECS=cfg.records_gz?JSON.parse(await gunzip(cfg.records_gz)):BASE;
const cross=cfg.cross!==false;
const myName=cfg.name||cfg.root;
/* own UI filters stay local; cross filters are published into the shared
   pool with an author and applied to other components only (GFC semantics,
   same as wasm mode — no feedback loops, no cross-contamination) */
const state={filters:{},page:1,pageSize:cfg.page_size||B.page_size||20,sortCol:null,sortDir:'asc',colvis:new Set(COLS.map(c=>c.name)),search:'',sel:new Set()};
const CPOPUP={};(cfg.cell_popup||[]).forEach(cp=>{if(cp&&cp.col)CPOPUP[cp.col]=cp});
const hasCol=c=>COLS.some(cc=>cc.name===c);
const isNum=n=>isNumC(n),isDate=n=>isDateC(n);
const PSIZES=[5,10,25,50,100];
let total=0,pageRows=[],pop=null,popOutside=null,tip=null,tipTimer=null,pinCell=null;

function changed(){document.dispatchEvent(new CustomEvent('dd-filters-changed',{detail:{src:cfg.root}}))}
function publishCol(col,f){
  if(!cross)return;
  if(f){window.__ddFilters[col]=f;window.__ddFiltersSrc[col]=myName}
  else if(window.__ddFiltersSrc[col]===myName){delete window.__ddFilters[col];delete window.__ddFiltersSrc[col]}
}
function foreignEntries(){
  if(!cross)return [];
  return Object.entries(window.__ddFilters).filter(([c])=>window.__ddFiltersSrc[c]!==myName&&hasCol(c));
}

function pass(r,excl){
  for(const[c,f]of Object.entries(state.filters)){
    if(c===excl||!hasCol(c))continue;
    if(!colMatch(r[c],f,isNum(c),isDate(c)))return false;
  }
  for(const[c,f]of foreignEntries()){
    if(c===excl)continue;
    if(!colMatch(r[c],f,isNum(c),isDate(c)))return false;
  }
  if(!tglPass(r,null))return false;
  if(state.search){
    const s=state.search.toLowerCase();
    if(!visibleCols().some(c=>String(r[c.name]??'').toLowerCase().includes(s)))return false;
  }
  return true;
}
function filteredRows(){return RECS.filter(r=>pass(r,null))}
function sortedRows(rows){
  if(!state.sortCol)return rows;
  const c=state.sortCol,num=isNum(c),dir=state.sortDir==='desc'?-1:1;
  return rows.slice().sort((a,b)=>{
    const x=a[c],y=b[c];
    if(x==null||x==='')return 1;
    if(y==null||y==='')return -1;
    const d=num?Number(x)-Number(y):String(x).localeCompare(String(y));
    return d*dir||(a.__rn>b.__rn?1:-1);
  });
}
function refresh(){
  const rows=sortedRows(filteredRows());
  total=rows.length;
  const pages=Math.max(1,Math.ceil(total/state.pageSize));
  if(state.page>pages)state.page=pages;
  pageRows=rows.slice((state.page-1)*state.pageSize,state.page*state.pageSize);
  render();
  if(cfg.idx===0){
    window.__ddLabelSearch=state.search;
    if(!window.__ddTableReady){window.__ddTableReady=true;document.dispatchEvent(new CustomEvent('dd-table-ready'))}
    if(window.__ddLabelsRecompute)window.__ddLabelsRecompute();
  }
}

/* ── popups/tooltips ── */
function hideTip(){pinCell=null;if(tip){tip.remove();tip=null}if(tipTimer){clearTimeout(tipTimer);tipTimer=null}}
function fallbackCopy(txt,done){
  const ta=document.createElement('textarea');
  ta.value=txt;ta.style.position='fixed';ta.style.opacity='0';
  document.body.appendChild(ta);ta.select();
  try{document.execCommand('copy')}catch(e){}
  ta.remove();done();
}
function showTip(td,pin){
  hideTip();
  const txt=td.textContent;
  if(!txt)return;
  if(!pin&&td.scrollWidth<=td.clientWidth+1&&td.scrollHeight<=td.clientHeight+1)return;
  tipTimer=setTimeout(()=>{
    tip=document.createElement('div');
    tip.className='dd-tip';
    if(pin){
      tip.innerHTML='<div class="tiptxt"></div><div class="tipacts"><button class="cp" title="Copy">'+ICONS.copy+'</button><button class="cl" title="Close">'+ICONS.x+'</button></div>';
      tip.querySelector('.tiptxt').textContent=txt;
      tip.querySelector('.cp').onclick=e=>{
        e.stopPropagation();
        const btn=e.currentTarget;
        const done=()=>{btn.classList.add('ok');btn.innerHTML=ICONS.check;setTimeout(()=>{btn.classList.remove('ok');btn.innerHTML=ICONS.copy},1200)};
        if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(txt).then(done,()=>fallbackCopy(txt,done));
        else fallbackCopy(txt,done);
      };
      tip.querySelector('.cl').onclick=e=>{e.stopPropagation();hideTip()};
    }else tip.textContent=txt;
    document.body.appendChild(tip);
    const r=td.getBoundingClientRect(),tr=tip.getBoundingClientRect();
    let left=Math.max(8,Math.min(r.left,window.innerWidth-tr.width-8));
    let top=r.bottom+4;
    if(top+tr.height>window.innerHeight-8)top=Math.max(8,r.top-tr.height-4);
    tip.style.left=left+'px';tip.style.top=top+'px';
    if(pin)pinCell=td;
    tip.addEventListener('mouseleave',()=>{if(!pinCell)hideTip()});
  },150);
}
function closePop(){
  hideTip();
  if(pop){pop.remove();pop=null}
  if(popOutside){document.removeEventListener('click',popOutside);popOutside=null}
  root.querySelectorAll('.dd-iconbtn.open,.pgbtn.open').forEach(b=>b.classList.remove('open'));
}
function anchorMenu(menu,btn,estW){
  const r=btn.getBoundingClientRect();
  let left=Math.max(8,Math.min(r.right-estW,window.innerWidth-estW-8));
  let top=r.bottom+4;
  const h=menu.offsetHeight||280;
  if(top+h>window.innerHeight-8)top=Math.max(8,r.top-h-4);
  menu.style.left=left+'px';menu.style.top=top+'px';
  setTimeout(()=>{document.removeEventListener('click',outside);popOutside=outside;document.addEventListener('click',outside)},0);
  function outside(e){if(pop&&!pop.contains(e.target)&&e.target.isConnected){closePop();document.removeEventListener('click',outside)}}
}

/* ── export / columns / pager menus ── */
function baseName(){return (B.title||'duckdive').replace(/[^\wа-яА-ЯёЁ]+/g,'_').slice(0,40)}
function downloadBlob(text,mime,ext){
  const blob=new Blob([text],{type:mime});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;a.download=baseName()+ext;document.body.appendChild(a);a.click();
  a.remove();setTimeout(()=>URL.revokeObjectURL(url),5000);
}
function toDelim(rows,cs,sep){
  return rows.map(r=>cs.map(c=>{
    const v=r[c.name];
    const s=v==null?'':String(v);
    return /["\n\r;,]|^\s|\s$/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;
  }).join(sep)).join('\r\n');
}
function exportData(fmt){
  const cs=visibleCols();
  let rows=sortedRows(filteredRows());
  if(state.sel.size)rows=rows.filter(r=>state.sel.has(r.__rn));
  if(fmt==='xlsx'){
    const data=rows.map(r=>{const o={};cs.forEach(c=>{let v=r[c.name];
      if(typeof v==='string'&&v.length>32767)v=v.slice(0,32766)+'…';o[c.name]=v});return o});
    const wb=XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb,XLSX.utils.json_to_sheet(data),'Data');
    XLSX.writeFile(wb,baseName()+'.xlsx');
    return;
  }
  const hdr=Object.fromEntries(cs.map(c=>[c.name,c.name]));
  if(fmt==='csv')downloadBlob('﻿'+toDelim([hdr],cs,',')+'\r\n'+toDelim(rows,cs,','),'text/csv;charset=utf-8','.csv');
  else if(fmt==='tsv')downloadBlob(toDelim([hdr],cs,'\t')+'\r\n'+toDelim(rows,cs,'\t'),'text/tab-separated-values;charset=utf-8','.tsv');
  else if(fmt==='json')downloadBlob(JSON.stringify(rows,null,2),'application/json','.json');
}
function openExportMenu(ev){
  closePop();
  const btn=ev.currentTarget;
  btn.classList.add('open');
  pop=document.createElement('div');
  pop.className='dd-menu';
  pop.innerHTML=`<div class="dd-menulabel">Download data</div>
    <div class="dd-menuitem" data-fmt="csv">${ICONS.table}<span class="lbl">CSV</span><span class="desc">Comma-separated values</span></div>
    <div class="dd-menuitem" data-fmt="tsv">${ICONS.grid}<span class="lbl">TSV</span><span class="desc">Best for Excel and Google Sheets</span></div>
    <div class="dd-menuitem" data-fmt="xlsx">${ICONS.dl}<span class="lbl">Excel</span><span class="desc">Self-contained .xlsx</span></div>
    <div class="dd-menuitem" data-fmt="json">${ICONS.braces}<span class="lbl">JSON</span><span class="desc">Raw JSON data</span></div>`;
  document.body.appendChild(pop);
  anchorMenu(pop,btn,260);
  pop.querySelectorAll('.dd-menuitem').forEach(el=>el.onclick=()=>{
    closePop();exportData(el.dataset.fmt);
  });
}
function openColvis(ev){
  closePop();
  const btn=ev.currentTarget;
  btn.classList.add('open');
  pop=document.createElement('div');
  pop.className='dd-pop';
  pop.innerHTML=`<div class="srch"><input id="${P}dd-cv-q" placeholder="Search columns..."></div>
    <div class="vals dd-pop2" id="${P}dd-cv-list"></div>`;
  document.body.appendChild(pop);
  const r=btn.getBoundingClientRect();
  let left=Math.max(8,Math.min(r.right-260,window.innerWidth-268));
  let top=r.bottom+4;
  if(top+320>window.innerHeight-8)top=Math.max(8,window.innerHeight-330);
  pop.style.left=left+'px';pop.style.top=top+'px';
  const list=$('dd-cv-list');
  function typeTag(n){const t=colType[n]||'';return /INT|HUGEINT/.test(t)?'int':/DOUBLE|FLOAT|DEC|REAL/.test(t)?'float':/DATE/.test(t)?'date':/TIME/.test(t)?'time':'str'}
  function renderList(qs){
    const cols=COLS.filter(c=>!qs||c.name.toLowerCase().includes(qs));
    const anyVisible=visibleCols().length>0;
    list.innerHTML=`<div class="cvrow act" id="${P}dd-cv-all" ${COLS.length===state.colvis.size?'style="opacity:.45;pointer-events:none"':''}>${ICONS.eye}<span>Show all</span></div>
      <div class="cvrow act" id="${P}dd-cv-none" ${!anyVisible?'style="opacity:.45;pointer-events:none"':''}>${ICONS.eyeoff}<span>Hide all</span></div>
      <div class="sep"></div>`
      +(cols.map(c=>{
        const vis=state.colvis.has(c.name);
        return `<div class="cvrow" data-c="${escA(c.name)}"><input type="checkbox" ${vis?'checked':''}><span>${escH(c.name)}</span><span class="typ">${typeTag(c.name)}</span></div>`;
      }).join('')||'<div class="load">No results.</div>');
    const toggle=c=>{
      if(state.colvis.has(c))state.colvis.delete(c);else state.colvis.add(c);
      renderList($('dd-cv-q').value.toLowerCase());
      refresh();
    };
    list.querySelectorAll('.cvrow[data-c]').forEach(el=>{
      const cb=el.querySelector('input');
      cb.onchange=()=>toggle(el.dataset.c);
      el.onclick=e=>{if(e.target!==cb){e.preventDefault();cb.checked=!cb.checked;toggle(el.dataset.c)}};
    });
    list.querySelector('#'+P+'dd-cv-all').onclick=()=>{
      COLS.forEach(c=>state.colvis.add(c.name));
      renderList($('dd-cv-q').value.toLowerCase());refresh();
    };
    list.querySelector('#'+P+'dd-cv-none').onclick=()=>{
      state.colvis.clear();
      renderList($('dd-cv-q').value.toLowerCase());refresh();
    };
  }
  renderList('');
  const q=$('dd-cv-q');
  q.oninput=()=>renderList(q.value.toLowerCase());
  setTimeout(()=>{document.removeEventListener('click',outside);popOutside=outside;document.addEventListener('click',outside)},0);
  function outside(e){if(pop&&!pop.contains(e.target)&&e.target.isConnected){closePop();document.removeEventListener('click',outside)}}
}
function openPageMenu(ev){
  closePop();
  const btn=ev.currentTarget;
  pop=document.createElement('div');
  pop.className='dd-menu';
  const pages=Math.max(1,Math.ceil(total/state.pageSize));
  /* no one DOM node per page (1M rows => tens of thousands of nodes):
     jump input + a small window around the current page */
  const near=new Set([1,2,3,pages-2,pages-1,pages,state.page-2,state.page-1,state.page,state.page+1,state.page+2]);
  const list=[...near].filter(p=>p>=1&&p<=pages).sort((a,b)=>a-b);
  let prev=0,items='';
  for(const pn of list){
    if(pn-prev>1)items+='<div class="dd-menulabel" style="text-align:center;padding-top:2px">…</div>';
    items+=`<div class="dd-menuitem" data-p="${pn}"><span class="lbl"${pn===state.page?' style="font-weight:700;color:var(--dd-accent-text)"':''}>${fmtN(pn)}</span></div>`;
    prev=pn;
  }
  pop.innerHTML=`<div class="dd-menulabel">Page</div>
    <div class="dd-jump"><input type="number" min="1" max="${pages}" value="${state.page}"><button>Go</button></div>`+items;
  document.body.appendChild(pop);
  anchorMenu(pop,btn,180);
  const inp=pop.querySelector('input');
  const go=()=>{const pn=Math.max(1,Math.min(pages,Number(inp.value)||1));state.page=pn;closePop();refresh()};
  pop.querySelector('.dd-jump button').onclick=go;
  inp.onkeydown=e=>{if(e.key==='Enter')go()};
  pop.querySelectorAll('.dd-menuitem').forEach(el=>el.onclick=()=>{
    state.page=Number(el.dataset.p);closePop();refresh();
  });
  inp.focus();inp.select();
}
function openSizeMenu(ev){
  closePop();
  const btn=ev.currentTarget;
  pop=document.createElement('div');
  pop.className='dd-menu';
  const sizes=[...new Set([...PSIZES,state.pageSize])].sort((a,b)=>a-b);
  pop.innerHTML=`<div class="dd-menulabel">Rows per page</div>`
    +sizes.map(sz=>`<div class="dd-menuitem" data-sz="${sz}"><span class="lbl"${sz===state.pageSize?' style="font-weight:600;background:var(--dd-accent-soft)"':''}>${sz}</span></div>`).join('');
  document.body.appendChild(pop);
  anchorMenu(pop,btn,120);
  pop.querySelectorAll('.dd-menuitem').forEach(el=>el.onclick=()=>{
    state.pageSize=Number(el.dataset.sz);state.page=1;closePop();refresh();
  });
}
"""
_JS += r"""
/* ── filter popup (values computed in JS) ── */
function openPopup(col,ev){
  closePop();
  pop=document.createElement('div');
  pop.className='dd-pop';
  pop.dataset.col=col;
  const kind=isNum(col)?'num':(isDate(col)?'date':'values');
  pop.dataset.kind=kind;
  const f=state.filters[col]||{};
  const cmp0=f.cmp&&f.cmp!=='none'?f.cmp:(kind==='num'?'between':(kind==='date'?'between':null));
  pop._d={vals:new Set((f.values||[]).map(String)),blank:!!f.blank,mode:f.op==='notin'?'notin':'in',cmp:cmp0,min:f.min??'',max:f.max??''};
  let inner='';
  if(kind==='num'){
    inner=`<div class="num">
      <select name="op">
        <option value="none"${cmp0?'':' selected'}>&mdash;</option>
        <option value="eq"${cmp0==='eq'?' selected':''}>=</option>
        <option value="gt"${cmp0==='gt'?' selected':''}>&gt;</option>
        <option value="gte"${cmp0==='gte'?' selected':''}>&ge;</option>
        <option value="lt"${cmp0==='lt'?' selected':''}>&lt;</option>
        <option value="lte"${cmp0==='lte'?' selected':''}>&le;</option>
        <option value="between"${cmp0==='between'?' selected':''}>&ndash;</option>
      </select>
      <input type="number" data-k="min" value="${escA(f.min??'')}" placeholder="Value">
      <input type="number" data-k="max" value="${escA(f.max??'')}" placeholder="Max" style="display:${cmp0==='between'?'':'none'}">
    </div>`;
  }else if(kind==='date'){
    inner=`<div class="date"><label>From <input type="date" data-k="min" value="${escA(f.min??'')}"></label>
      <label>To <input type="date" data-k="max" value="${escA(f.max??'')}"></label></div>`;
  }
  inner+=`<div class="mode"><label><input type="radio" name="m" value="in" ${f.op!=='notin'?'checked':''}> Include</label>
      <label><input type="radio" name="m" value="notin" ${f.op==='notin'?'checked':''}> Exclude</label></div>
      <div class="srch"><input class="qs" placeholder="Search values..."></div>
      <div class="vals"></div>`;
  pop.innerHTML=`<div class="srch" style="font-weight:600;border-bottom:1px solid var(--dd-soft);padding:8px 10px">${escH(col)}</div>`+inner
    +`<div class="acts"><button class="b act-clear">Clear</button><button class="b act-close">Close</button>
      <button class="bp act-apply">Apply</button></div>`;
  document.body.appendChild(pop);
  const r=ev.target.getBoundingClientRect();
  let left=r.left-100,top=r.bottom+6;
  left=Math.max(8,Math.min(left,window.innerWidth-(pop.offsetWidth||300)-8));
  if(top+380>window.innerHeight-8)top=Math.max(8,window.innerHeight-390);
  pop.style.left=left+'px';pop.style.top=top+'px';
  renderVals(col,f);
  pop.querySelectorAll('input[data-k],select[data-k]').forEach(inp=>{
    pop._d[inp.dataset.k]=inp.value;
    inp.oninput=()=>{pop._d[inp.dataset.k]=inp.value};
  });
  const opSel=pop.querySelector('select[name=op]');
  if(opSel){
    const mx=pop.querySelector('input[data-k=max]');
    opSel.onchange=()=>{pop._d.cmp=opSel.value==='none'?null:opSel.value;if(mx)mx.style.display=opSel.value==='between'?'':'none'};
  }
  pop.querySelectorAll('input[name=m]').forEach(rb=>rb.onchange=()=>{pop._d.mode=rb.value});
  pop.querySelector('.act-apply').onclick=()=>applyPop();
  pop.querySelector('.act-close').onclick=closePop;
  pop.querySelector('.act-clear').onclick=()=>{delete state.filters[col];publishCol(col,null);state.page=1;closePop();refresh();changed()};
  setTimeout(()=>{document.removeEventListener('click',outside);popOutside=outside;document.addEventListener('click',outside)},0);
  function outside(e){if(pop&&!pop.contains(e.target)&&e.target.isConnected){closePop();document.removeEventListener('click',outside)}}
}
function renderVals(col,f){
  const wrap=pop.querySelector('.vals');
  const m=new Map();let blanks=0;
  for(const r of RECS){
    if(!pass(r,col))continue;
    const v=r[col];
    if(v==null||v===''){blanks++;continue}
    const k=String(v);m.set(k,(m.get(k)||0)+1);
  }
  const opts=[...m.entries()].sort((a,b)=>b[1]-a[1]).slice(0,300);
  const checked=v=>f.values&&f.values.map(String).includes(v);
  const blankRow=blanks?`<div class="val" data-v="__dd_blank__"><input type="checkbox" ${f.blank?'checked':''}>
     <span>(Blanks)</span><span class="cnt">${fmtN(blanks)}</span></div>`:'';
  wrap.innerHTML=(opts.map(([v,c])=>
    `<div class="val" data-v="${escA(v)}"><input type="checkbox" ${checked(v)?'checked':''}>
     <span>${escH(v)}</span><span class="cnt">${fmtN(c)}</span></div>`).join('')
    ||'<div class="load">No values</div>')+blankRow;
  wrap.querySelectorAll('input').forEach(cb=>cb.onchange=()=>{
    const v=cb.closest('.val').dataset.v,d=pop._d;
    if(v==='__dd_blank__'){d.blank=cb.checked;return}
    if(cb.checked)d.vals.add(v);else d.vals.delete(v);
  });
  const qs=pop.querySelector('.qs');
  if(qs)qs.oninput=()=>{const s=qs.value.toLowerCase();
    pop.querySelectorAll('.val').forEach(el=>{el.style.display=el.dataset.v.toLowerCase().includes(s)?'':'none'})};
}
function applyPop(){
  const col=pop.dataset.col,d=pop._d,kind=pop.dataset.kind;
  const cmp=kind==='date'?'between':d.cmp;
  const hasVals=d.vals.size||d.blank;
  const hasRange=cmp&&((d.min??'')!==''||(cmp==='between'&&(d.max??'')!==''));
  let nf=null;
  if(hasVals||hasRange)nf={op:d.mode,values:[...d.vals],blank:!!d.blank,cmp:hasRange?cmp:null,min:d.min??'',max:d.max??''};
  if(nf)state.filters[col]=nf;else delete state.filters[col];
  publishCol(col,nf);
  state.page=1;closePop();refresh();changed();
}

/* ── render ── */
function badgeLabel(c,f){
  if(f.op==='in'||f.op==='notin'){
    let s=(f.op==='notin'?'excl. ':'')+c+': ';
    const p=[];
    if(f.values&&f.values.length)p.push(f.values.join(', '));
    if(f.blank)p.push('(Blanks)');
    s+=p.join(', ');
    if(f.cmp&&f.cmp!=='between')s+=' '+({eq:'=',gt:'>',gte:'\u2265',lt:'<',lte:'\u2264'}[f.cmp]||'')+' '+f.min;
    else if(f.cmp==='between')s+=' '+[f.min,f.max].filter(x=>x!==''&&x!=null).join(' \u2013 ');
    return s;
  }
  return c+' '+[f.min,f.max].filter(x=>x!==''&&x!=null).join(' – ');
}
function renderBadges(){
  const el=root.querySelector('.dd-badges');
  if(!el)return;
  const entries=Object.entries(state.filters).filter(([c,f])=>hasCol(c));
  const incoming=foreignEntries();
  el.innerHTML=entries.map(([c,f])=>
    `<span class="dd-badge"><span>${escH(badgeLabel(c,f))}</span><span class="x" data-c="${escA(c)}">&times;</span></span>`).join('')
    + incoming.map(([c,f])=>`<span class="dd-badge dd-badge-in" title="Cross-filter from component &quot;${escA(window.__ddFiltersSrc[c]||'')}&quot;">&larr;${escH(window.__ddFiltersSrc[c]||'')}: ${escH(badgeLabel(c,f))}</span>`).join('')
    + (entries.length?`<span class="dd-clear" id="${P}dd-clear">&times; Clear all</span>`:"");
  el.querySelectorAll('.x').forEach(x=>x.onclick=()=>{delete state.filters[x.dataset.c];publishCol(x.dataset.c,null);state.page=1;refresh();changed()});
  const cl=$('dd-clear');
  if(cl)cl.onclick=()=>{const ks=Object.keys(state.filters);ks.forEach(k=>{delete state.filters[k];publishCol(k,null)});state.page=1;refresh();changed()};
}
function bottomLeft(nCols,hidden){
  const n=state.sel.size;
  if(n>0){
    const allSel=n===total;
    return `<span>${fmtN(n)} selected</span> `
      +(pageRows.length&&pageRows.every(r=>state.sel.has(r.__rn))&&!allSel
        ?`<button class="rowlink" id="${P}dd-selallrows">Select all ${fmtN(total)}</button> `
        :'')
      +`<button class="rowlink" id="${P}dd-selclear">Reset</button>`;
  }
  return `${fmtN(total)} rows · ${fmtN(nCols)} cols${hidden?` <span class="hid">+${hidden} hidden</span>`:''}`;
}
function visibleCols(){return COLS.filter(c=>state.colvis.has(c.name))}
function tableHtml(cs){
  const heads=`<th class="dd-selcol"><input type="checkbox" id="${P}dd-selall"></th>`+cs.map(c=>{
    const f=state.filters[c.name];
    const srt=state.sortCol===c.name
      ?(state.sortDir==='asc'?ICONS.upwide:ICONS.downwide)
      :ICONS.updown;
    const srtCls=state.sortCol===c.name?' srt-on':'';
    return `<th><span class="hwrap"><span class="cn" data-c="${escA(c.name)}">${escH(c.name)}<span class="srt${srtCls}">${srt}</span></span><span class="fb${f?' on':''}" data-c="${escA(c.name)}" title="Filter">${ICONS.filter}</span></span></th>`;
  }).join('');
  const body=pageRows.length?pageRows.map((r,pagei)=>{
    const tds='<td class="dd-selcol"><input type="checkbox" class="dd-selrow" data-i="'+r.__rn+'"'+(state.sel.has(r.__rn)?' checked':'')+'></td>'
      +cs.map(c=>{
        const v=r[c.name];
        if(v==null||v==='')return '<td></td>';
        return `<td${isNum(c.name)?' class="num"':''}>${escH(typeof v==='number'?fmtN(v):v)}</td>`;
      }).join('');
    return '<tr data-dd-ri="'+pagei+'"'+(state.sel.has(r.__rn)?' class="sel"':'')+'>'+tds+'</tr>';
  }).join(''):'<tr><td class="dd-empty" colspan="'+(cs.length+1)+'">No rows</td></tr>';
  return `<div class="dd-badges"></div>
    <div class="dd-tscroll"><table><thead><tr>${heads}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function pagerHtml(cs){
  const pages=Math.max(1,Math.ceil(total/state.pageSize));
  const hidden=COLS.length-cs.length;
  return `<div class="dd-pager">
      <span class="rowcount">${bottomLeft(cs.length,hidden)}</span>
      <div class="pgctl">
        <button class="szbtn" id="${P}dd-sz">${state.pageSize} / page${ICONS.chd}</button>
        <button class="pgbtn" id="${P}dd-first" ${state.page<=1?'disabled':''} title="First page">${ICONS.chdl}</button>
        <button class="pgbtn" id="${P}dd-prev" ${state.page<=1?'disabled':''} title="Previous page">${ICONS.chl}</button>
        <span class="pginfo">Page <button class="pgbtn pgnum" id="${P}dd-page">${state.page}</button> of ${fmtN(pages)}</span>
        <button class="pgbtn" id="${P}dd-next" ${state.page>=pages?'disabled':''} title="Next page">${ICONS.chr}</button>
        <button class="pgbtn" id="${P}dd-last" ${state.page>=pages?'disabled':''} title="Last page">${ICONS.chdr}</button>
      </div>
      <div class="bbspace"></div>
    </div>`;
}
function topHtml(){
  return `<div class="dd-topbar">
      <div class="dd-srch">${ICONS.search}<input id="${P}dd-q" type="text" placeholder="Search..." value="" autocomplete="off"><button class="qx" id="${P}dd-qx" title="Clear">${ICONS.x}</button></div>
      <div class="spacer"></div>
      <button class="dd-iconbtn" id="${P}dd-cv">${ICONS.cols}Columns</button>
      <button class="dd-iconbtn" id="${P}dd-xlsx">${ICONS.dl}Export</button>
    </div>`;
}
function bindTable(){
  const selAll=$('dd-selall');
  if(selAll){
    const pageIdx=pageRows.map(r=>r.__rn);
    const onPg=pageIdx.filter(i=>state.sel.has(i)).length;
    selAll.checked=pageIdx.length>0&&onPg===pageIdx.length;
    selAll.indeterminate=onPg>0&&onPg<pageIdx.length;
    selAll.onchange=()=>{
      if(selAll.checked)pageIdx.forEach(i=>state.sel.add(i));else pageIdx.forEach(i=>state.sel.delete(i));
      render();
    };
  }
  root.querySelectorAll('.dd-selrow').forEach(cb=>cb.onchange=()=>{
    const i=Number(cb.dataset.i);
    if(cb.checked)state.sel.add(i);else state.sel.delete(i);
    render();
  });
  const sa=$('dd-selallrows');
  if(sa)sa.onclick=()=>{filteredRows().forEach(r=>state.sel.add(r.__rn));render()};
  const sc=$('dd-selclear');
  if(sc)sc.onclick=()=>{state.sel.clear();render()};
  root.querySelectorAll('.cn').forEach(el=>el.onclick=()=>{
    const c=el.dataset.c;
    if(state.sortCol!==c){state.sortCol=c;state.sortDir='asc'}
    else if(state.sortDir==='asc')state.sortDir='desc';
    else state.sortCol=null;
    state.page=1;refresh();
  });
  root.querySelectorAll('.fb').forEach(el=>el.onclick=e=>{e.stopPropagation();openPopup(el.dataset.c,e)});
  $('dd-first').onclick=()=>{state.page=1;refresh()};
  $('dd-prev').onclick=()=>{if(state.page>1){state.page--;refresh()}};
  $('dd-next').onclick=()=>{if(state.page<Math.ceil(total/state.pageSize)){state.page++;refresh()}};
  $('dd-last').onclick=()=>{state.page=Math.ceil(total/state.pageSize);refresh()};
  $('dd-page').onclick=e=>{e.stopPropagation();openPageMenu(e)};
  $('dd-sz').onclick=e=>{e.stopPropagation();openSizeMenu(e)};
}
function bindTop(){
  const q=$('dd-q'),qx=$('dd-qx');
  let deb=null;
  q.oninput=()=>{qx.classList.toggle('has',!!q.value);clearTimeout(deb);deb=setTimeout(()=>{
    state.search=q.value;state.page=1;refresh();
  },300)};
  q.onkeydown=e=>{if(e.key==='Escape'){q.value='';qx.classList.remove('has');state.search='';refresh()}};
  qx.onclick=()=>{q.value='';qx.classList.remove('has');state.search='';refresh()};
  $('dd-cv').onclick=e=>{e.stopPropagation();openColvis(e)};
  $('dd-xlsx').onclick=e=>{e.stopPropagation();openExportMenu(e)};
}
function render(){
  const cs=visibleCols();
  hideTip();
  const sc0=root.querySelector('.dd-tscroll');
  const sl0=sc0?sc0.scrollLeft:0;
  if(!$('dd-tbl')){
    root.innerHTML=topHtml()+`<div id="${P}dd-tbl"></div><div id="${P}dd-pagerbox"></div>`;
    bindTop();
  }
  $('dd-tbl').innerHTML=tableHtml(cs);
  $('dd-pagerbox').innerHTML=pagerHtml(cs);
  renderBadges();
  bindTable();
  const sc1=root.querySelector('.dd-tscroll');
  if(sc1&&sl0)sc1.scrollLeft=sl0;
}

/* ── cell popups & hover tooltips ── */
function colByCell(td){
  const table=td.closest('table');
  const th=table&&table.querySelectorAll('thead th')[td.cellIndex];
  const cn=th&&th.querySelector('.cn');
  return cn?(cn.dataset.c||cn.textContent.trim()):null;
}
function openCellPopup(td,cp){
  const tr=td.closest('tr');
  const ri=tr?Number(tr.dataset.ddRi):NaN;
  const row=(pageRows&&Number.isInteger(ri))?pageRows[ri]:null;
  const val=window.__ddGetValue?__ddGetValue(row,cp.col):(td.textContent||'');
  let html;
  const fn=cp.formatter?window[cp.formatter]:null;
  if(typeof fn==='function'){
    try{html=fn(row,cp.col,cp.opts||{})}
    catch(err){console.error('duckdive: cell_popup formatter failed:',err);html=null}
  }
  if(html==null)html='<pre class="dd-cpopup-raw">'+escH(val)+'</pre>';
  if(window.__ddPopup)__ddPopup.open({html:html,raw:String(val)});
}
root.addEventListener('mouseover',e=>{
  if(pinCell)return;
  const td=e.target.closest('.duckdive-root td');
  if(!td||td.classList.contains('dd-selcol')){if(!pinCell)hideTip();return}
  if(CPOPUP[colByCell(td)])return;
  showTip(td,false);
});
root.addEventListener('mouseleave',()=>{if(!pinCell)hideTip()});
root.addEventListener('click',e=>{
  const td=e.target.closest('.duckdive-root td');
  if(!td||td.classList.contains('dd-selcol'))return;
  e.stopPropagation();
  const cp=CPOPUP[colByCell(td)];
  if(cp){hideTip();openCellPopup(td,cp);return}
  if(pinCell===td){hideTip();return}
  showTip(td,true);
});
document.addEventListener('click',e=>{
  if(pinCell&&!pinCell.contains(e.target)&&!(tip&&tip.contains(e.target))&&!e.target.closest('.duckdive-root td'))hideTip();
});
document.addEventListener('keydown',e=>{
  if(e.key!=='Escape')return;
  if(pop)closePop();
  else if(pinCell)hideTip();
});
document.addEventListener('dd-filters-changed',e=>{if(!e.detail||e.detail.src!==cfg.root)refresh()});
(window.__ddInstances=window.__ddInstances||[]).push({name:cfg.name,root:cfg.root,refresh:refresh});
refresh();
}
"""

_JS += r"""
/* ── duckdive_filter: standalone selector (JSON mode) ── */
function initFilter(cfg){
  const root=document.getElementById(cfg.root);
  if(!root)return;
  const col=cfg.column,myName=cfg.name||('flt_'+col);
  const meta=(B.columns||[]).find(c=>c.name===col);
  if(!meta){console.warn('duckdive_filter: column not found:',col);root.style.display='none';return}
  const isN=isNumC(col),isD=isDateC(col);
  let f=null,pop=null,popOutside=null;
  function apply(nf){
    f=nf;
    if(f){window.__ddFilters[col]=f;window.__ddFiltersSrc[col]=myName}
    else if(window.__ddFiltersSrc[col]===myName){delete window.__ddFilters[col];delete window.__ddFiltersSrc[col]}
    renderBtn();
    document.dispatchEvent(new CustomEvent('dd-filters-changed',{detail:{src:cfg.root}}));
  }
  function closePop(){
    if(pop){pop.remove();pop=null}
    if(popOutside){document.removeEventListener('click',popOutside);popOutside=null}
  }
  function renderBtn(){
    root.innerHTML=`<button class="dd-fbtn${f?' on':''}" title="Filter: ${escA(cfg.title||col)}">${ICONS.filter}<span>${escH(cfg.title||col)}</span></button>`;
    root.querySelector('button').onclick=e=>{e.stopPropagation();openPop(e)};
  }
  function openPop(ev){
    closePop();
    pop=document.createElement('div');
    pop.className='dd-pop';
    const kind=isN?'num':(isD?'date':'values');
    pop._d={vals:new Set(f?(f.values||[]).map(String):[]),blank:f?!!f.blank:false,mode:f&&f.op==='notin'?'notin':'in',
      cmp:f&&f.cmp?f.cmp:(kind==='values'?null:'between'),min:(f&&f.min)??'',max:(f&&f.max)??''};
    let inner='';
    if(kind==='num'){
      const cmp0=pop._d.cmp||'between';
      inner=`<div class="num">
        <select name="op">
          <option value="none"${cmp0?'':' selected'}>&mdash;</option>
          <option value="eq"${cmp0==='eq'?' selected':''}>=</option>
          <option value="gt"${cmp0==='gt'?' selected':''}>&gt;</option>
          <option value="gte"${cmp0==='gte'?' selected':''}>&ge;</option>
          <option value="lt"${cmp0==='lt'?' selected':''}>&lt;</option>
          <option value="lte"${cmp0==='lte'?' selected':''}>&le;</option>
          <option value="between"${cmp0==='between'?' selected':''}>&ndash;</option>
        </select>
        <input type="number" data-k="min" value="${escA(pop._d.min)}" placeholder="Value">
        <input type="number" data-k="max" value="${escA(pop._d.max)}" placeholder="Max" style="display:${cmp0==='between'?'':'none'}">
      </div>`;
    }else if(kind==='date'){
      inner=`<div class="date"><label>From <input type="date" data-k="min" value="${escA(pop._d.min)}"></label>
        <label>To <input type="date" data-k="max" value="${escA(pop._d.max)}"></label></div>`;
    }
    inner+=`<div class="mode"><label><input type="radio" name="m" value="in" ${(f&&f.op==='notin')?'':'checked'}> Include</label>
        <label><input type="radio" name="m" value="notin" ${f&&f.op==='notin'?'checked':''}> Exclude</label></div>`
      +(kind==='values'?`<div class="srch"><input class="qs" placeholder="Search values..."></div>`:'')
      +`<div class="vals"></div>`;
    pop.innerHTML=`<div class="srch" style="font-weight:600;border-bottom:1px solid var(--dd-soft,#f4f4f5);padding:8px 10px">${escH(cfg.title||col)}</div>`+inner
      +`<div class="acts"><button class="b act-clear">Clear</button><button class="b act-close">Close</button>
        <button class="bp act-apply">Apply</button></div>`;
    document.body.appendChild(pop);
    const r=ev.target.getBoundingClientRect();
    let left=r.left-100,top=r.bottom+6;
    left=Math.max(8,Math.min(left,window.innerWidth-(pop.offsetWidth||300)-8));
    if(top+380>window.innerHeight-8)top=Math.max(8,window.innerHeight-390);
    pop.style.left=left+'px';pop.style.top=top+'px';
    pop.querySelectorAll('input[data-k],select[data-k]').forEach(inp=>{
      inp.oninput=()=>{pop._d[inp.dataset.k]=inp.value};
      inp.onchange=()=>{pop._d[inp.dataset.k]=inp.value};
    });
    const opSel=pop.querySelector('select[name=op]');
    if(opSel){const mx=pop.querySelector('input[data-k=max]');
      opSel.onchange=()=>{pop._d.cmp=opSel.value==='none'?null:opSel.value;if(mx)mx.style.display=opSel.value==='between'?'':'none'}}
    pop.querySelectorAll('input[name=m]').forEach(rb=>rb.onchange=()=>{pop._d.mode=rb.value});
    pop.querySelector('.act-apply').onclick=()=>{
      const d=pop._d,cmp=kind==='date'?'between':d.cmp;
      const hasVals=d.vals.size||d.blank;
      const hasRange=cmp&&((d.min??'')!==''||(cmp==='between'&&(d.max??'')!==''));
      closePop();
      apply(hasVals||hasRange?{op:d.mode,values:[...d.vals],blank:!!d.blank,cmp:hasRange?cmp:null,min:d.min??'',max:d.max??''}:null);
    };
    pop.querySelector('.act-close').onclick=closePop;
    pop.querySelector('.act-clear').onclick=()=>{closePop();apply(null)};
    setTimeout(()=>{document.removeEventListener('click',outside);popOutside=outside;document.addEventListener('click',outside)},0);
    function outside(e){if(pop&&!pop.contains(e.target)&&!root.contains(e.target)){closePop();document.removeEventListener('click',outside)}}
    /* value list with counts (own column filter excluded, toggles applied) */
    const m=new Map();let blanks=0;
    for(const r of BASE){
      let ok=true;
      for(const[c,ff]of Object.entries(window.__ddFilters)){
        if(c===col)continue;
        if(!colMatch(r[c],ff,isNumC(c),isDateC(c))){ok=false;break}
      }
      if(ok)ok=tglPass(r,null);
      if(!ok)continue;
      const v=r[col];
      if(v==null||v===''){blanks++;continue}
      const k=String(v);m.set(k,(m.get(k)||0)+1);
    }
    const opts=[...m.entries()].sort((a,b)=>b[1]-a[1]).slice(0,300);
    const wrap=pop.querySelector('.vals');
    const blankRow=blanks?`<div class="val" data-v="__dd_blank__"><input type="checkbox" ${pop._d.blank?'checked':''}>
       <span>(Blanks)</span><span class="cnt">${fmtN(blanks)}</span></div>`:'';
    wrap.innerHTML=(opts.map(([v,c])=>
      `<div class="val" data-v="${escA(v)}"><input type="checkbox" ${pop._d.vals.has(v)?'checked':''}>
       <span>${escH(v)}</span><span class="cnt">${fmtN(c)}</span></div>`).join('')
      ||'<div class="load">No values</div>')+blankRow;
    wrap.querySelectorAll('input').forEach(cb=>cb.onchange=()=>{
      const v=cb.closest('.val').dataset.v,d=pop._d;
      if(v==='__dd_blank__'){d.blank=cb.checked;return}
      if(cb.checked)d.vals.add(v);else d.vals.delete(v);
    });
    const qs=pop.querySelector('.qs');
    if(qs)qs.oninput=()=>{const s=qs.value.toLowerCase();
      pop.querySelectorAll('.val').forEach(el=>{el.style.display=el.dataset.v.toLowerCase().includes(s)?'':'none'})};
  }
  document.addEventListener('dd-filters-changed',e=>{if(!e.detail||e.detail.src!==cfg.root)renderBtn()});
  renderBtn();
}

/* ── duckdive_toggles: exclusion toggle buttons (JSON mode) ── */
function initToggles(cfg){
  const root=document.getElementById(cfg.root);
  if(!root)return;
  const col=cfg.column;
  const meta=(B.columns||[]).find(c=>c.name===col);
  if(!meta){console.warn('duckdive_toggles: column not found:',col);root.style.display='none';return}
  const items=(cfg.items||[]).filter(it=>it&&it.id!=null&&it.label!=null&&it.value!=null);
  const state=window.__ddToggles=window.__ddToggles||{};
  items.forEach(it=>{if(typeof state[it.id]!=='boolean')state[it.id]=it.on===true});
  function republish(){
    const ex=items.filter(it=>state[it.id]).map(it=>String(it.value));
    if(ex.length)window.__ddTglExcl[col]=ex;else delete window.__ddTglExcl[col];
    document.dispatchEvent(new CustomEvent('dd-filters-changed',{detail:{src:cfg.root}}));
  }
  function renderBtns(){
    root.innerHTML=items.map(it=>
      `<button class="dd-tbtn${state[it.id]?' on':''}" data-t="${escA(it.id)}" title="${escA(it.label)}">${escH(it.label)}</button>`).join('');
    root.querySelectorAll('.dd-tbtn').forEach(btn=>{
      btn.onclick=e=>{e.stopPropagation();
        const id=btn.dataset.t;
        state[id]=!state[id];
        renderBtns();republish();
      };
    });
  }
  renderBtns();
  if(items.some(it=>state[it.id]))republish();
}

/* ── duckdive_label: live aggregates (JSON mode; WASM handled in label_block) ── */
const LABELS=B.labels||[];
function labelVal(sp){
  let rows=BASE;
  for(const a of sp.atoms){
    rows=rows.filter(r=>{
      const v=r[a.col];
      if(a.nullOp)return a.nullOp==='null'?(v==null):(v!=null);
      if(v==null)return false;
      const num=isNumC(a.col);
      const x=num?Number(v):String(v);
      const y=num?Number(a.val):String(a.val);
      return({'=':x===y,'!=':x!==y,'<>':x!==y,'>':x>y,'<':x<y,'>=':x>=y,'<=':x<=y})[a.op]??true;
    });
  }
  if(sp.useFilters){
    rows=rows.filter(r=>{
      for(const[c,f]of Object.entries(window.__ddFilters)){
        if(!colMatch(r[c],f,isNumC(c),isDateC(c)))return false;
      }
      return tglPass(r,null);
    });
    const s=(window.__ddLabelSearch||'').toLowerCase();
    if(s)rows=rows.filter(r=>Object.keys(r).some(k=>k!=='__rn'&&String(r[k]??'').toLowerCase().includes(s)));
  }
  let v;
  if(sp.agg==='count'){
    v=sp.col==='*'?rows.length:(sp.distinct?new Set(rows.map(r=>r[sp.col])).size:rows.filter(r=>r[sp.col]!=null).length);
  }else{
    const vals=rows.map(r=>Number(r[sp.col])).filter(x=>isFinite(x));
    if(!vals.length)v=null;
    else if(sp.agg==='sum')v=vals.reduce((a,b)=>a+b,0);
    else if(sp.agg==='avg')v=vals.reduce((a,b)=>a+b,0)/vals.length;
    else if(sp.agg==='min')v=Math.min.apply(null,vals);
    else v=Math.max.apply(null,vals);
  }
  return v;
}
function fmtLabel(v,fmt){
  if(v==null)return '—';
  if(fmt==='percent')return (v*100).toLocaleString('ru-RU',{maximumFractionDigits:2})+'%';
  return Number(v).toLocaleString('ru-RU',{maximumFractionDigits:6});
}
let lblT=null;
function labelsRecompute(){
  if(window.__ddq)return; /* wasm mode: label_block does the recompute */
  for(const sp of LABELS){
    const el=document.getElementById(sp.id);
    if(!el)continue;
    try{el.textContent=fmtLabel(labelVal(sp),sp.fmt)}catch(e){console.warn('duckdive label:',e)}
  }
}
window.__ddLabelsRecompute=labelsRecompute;
document.addEventListener('dd-filters-changed',()=>{clearTimeout(lblT);lblT=setTimeout(labelsRecompute,150)});

/* ── keyboard: Ctrl/Cmd+F focuses the first table search ── */
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&(e.key==='f'||e.key==='F')){
    const q=document.querySelector('.duckdive-root .dd-srch input');
    if(q){e.preventDefault();q.focus();q.select()}
  }
});

/* ── boot ── */
const VIEWS=B.views&&B.views.length?B.views:[{}];
for(let i=0;i<VIEWS.length;i++){
  const vw=VIEWS[i];
  await initInstance({idx:i,root:vw.root||'duckdive-table',pfx:vw.pfx??(i?'dd'+(i+1):''),
    cross:vw.cross!==false,name:vw.name||null,columns:vw.columns,records_gz:vw.records_gz,
    page_size:vw.page_size,title:vw.title,cell_popup:vw.cell_popup});
}
const FILTERS=B.filters||[];
for(let i=0;i<FILTERS.length;i++){
  try{initFilter(FILTERS[i])}catch(e){console.warn('duckdive: filter init failed:',e)}
}
const TOGGLES=B.toggles||[];
for(let i=0;i<TOGGLES.length;i++){
  try{initToggles(TOGGLES[i])}catch(e){console.warn('duckdive: toggles init failed:',e)}
}
labelsRecompute();
})();
"""


# ---------------------------------------------------------------------------
# Cell-popup modal (shared by both modes)
# ---------------------------------------------------------------------------

_POPUP_SRC = r"""
window.__ddPopup=(function(){
  let ov=null;
  function close(){if(ov){ov.remove();ov=null;document.removeEventListener('keydown',onKey)}}
  function onKey(e){if(e.key==='Escape')close()}
  function fallbackCopy(txt,done){
    const ta=document.createElement('textarea');
    ta.value=txt;ta.style.position='fixed';ta.style.opacity='0';
    document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy')}catch(e){}
    ta.remove();done();
  }
  function open(o){
    close();
    ov=document.createElement('div');
    ov.className='dd-cpop';
    ov.innerHTML="<div class='dd-cpop-box'><div class='dd-cpop-head'><span class='t'>Cell value</span>"
      +"<button class='dd-cpop-copy'>__ICON_COPY__ Copy</button>"
      +"<button class='dd-cpop-x' title='Close'>__ICON_X__</button></div>"
      +"<div class='dd-cpop-body'></div></div>";
    ov.querySelector('.dd-cpop-body').innerHTML=o.html;
    ov.addEventListener('click',e=>{if(e.target===ov)close()});
    const cb=ov.querySelector('.dd-cpop-copy');
    cb.onclick=e=>{
      e.stopPropagation();
      const done=()=>{cb.classList.add('ok');cb.innerHTML="__ICON_CHECK__ Copied";setTimeout(()=>{cb.classList.remove('ok');cb.innerHTML="__ICON_COPY__ Copy"},1200)};
      if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(o.raw||'').then(done,()=>fallbackCopy(o.raw||'',done));
      else fallbackCopy(o.raw||'',done);
    };
    ov.querySelector('.dd-cpop-x').onclick=close;
    document.addEventListener('keydown',onKey);
    document.body.appendChild(ov);
  }
  return {open:open,close:close};
})();
"""


def table_block(boot: dict) -> str:
    """JSON-mode boot block: <style> + boot JSON + xlsx.js + popup + main JS."""
    js = _inject_icons(_JS)
    payload = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    return (
        f"<style>{_CSS}</style>"
        f'<script id="duckdive-boot" type="application/json">{payload}</script>'
        f"<script>{_xlsx_src()}</script>"
        f"<script>{_inject_icons(_POPUP_SRC)}</script>"
        f"<script>{js}</script>"
    )
