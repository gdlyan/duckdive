from pathlib import Path

import pandas as pd

from duckdive import DuckDive

df = pd.DataFrame(
    {
        "region": ["north", "south", "north", "west"],
        "amount": [120, 85, 240, 60],
    }
)

out = Path(__file__).with_suffix(".html")
DuckDive(df, title="DuckDive example").save(out)
print(out)
