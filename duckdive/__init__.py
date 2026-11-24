"""DuckDive — turn a pandas DataFrame into a single self-contained HTML file
with a fully interactive, SQL-native data table. No server, no network, no
external CDNs: data + UI + logic in one file.

    from duckdive import DuckDive
    dd = DuckDive(df, title="Sales Q4")
    dd.save("report.html")
"""
from .core import DuckDive, DuckData, WASM_THRESHOLD
from .label_ui import LabelSqlError

__version__ = "0.2.0"
__all__ = ["DuckDive", "DuckData", "LabelSqlError", "WASM_THRESHOLD"]
