"""Shared color tokens, sourced from the validated dataviz reference palette.

Categorical order is fixed and never re-sorted or cycled — the same entity
always gets the same slot across every chart in the app.
"""

CATEGORICAL = [
    "#2a78d6",  # 1 blue
    "#eb6834",  # 2 orange
    "#1baf7a",  # 3 aqua
    "#eda100",  # 4 yellow
    "#e87ba4",  # 5 magenta
    "#008300",  # 6 green
    "#4a3aa7",  # 7 violet
    "#e34948",  # 8 red
]

STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}

CHROME = {
    "surface": "#fcfcfb",
    "page": "#f9f9f7",
    "primary_ink": "#0b0b0b",
    "secondary_ink": "#52514e",
    "muted": "#898781",
    "gridline": "#e1e0d9",
    "baseline": "#c3c2b7",
}

# Ordinal ramp (blue, one hue light->dark) for the pipeline funnel — six open
# stages, lightest step no lighter than step 250 per the ordinal-ramp floor.
FUNNEL_RAMP = ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#1c5cab"]

# Forecast category -> color, held constant everywhere it appears (legend,
# bars, tables) so the color always identifies the same category.
FORECAST_CATEGORY_COLORS = {
    "Pipeline": CATEGORICAL[0],
    "Best Case": CATEGORICAL[1],
    "Commit": CATEGORICAL[2],
    "Closed Won": CATEGORICAL[3],
    "Closed Lost": CHROME["muted"],
    "Omitted": CHROME["muted"],
}

EARNINGS_COLORS = {
    "base": CATEGORICAL[0],
    "commission": CATEGORICAL[1],
}
