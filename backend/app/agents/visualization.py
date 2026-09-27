"""Deterministic chart specs for tabular datasets.

The data analyst attaches these specs to its result; the final Markdown
response carries them as fenced ```chart blocks (JSON) which the frontend
renders as interactive SVG charts. Chat history stores only message text, so
embedding the spec in the text keeps charts visible when a chat is reopened.
"""

from __future__ import annotations

import json
import re
from typing import Any

import numpy as np
import pandas as pd

CHART_FENCE = "chart"
MAX_CHARTS_PER_TABLE = 6
MAX_CHARTS_TOTAL = 8
MAX_CATEGORIES = 12
MAX_HEATMAP_COLUMNS = 8
MAX_SCATTER_POINTS = 400
MAX_TREND_POINTS = 60
HISTOGRAM_BINS = 20
# Axis label per resample period; a week is labelled by its first day.
PERIOD_LABELS = {"D": "%Y-%m-%d", "W": "%Y-%m-%d", "M": "%Y-%m", "Y": "%Y"}

SUM_KEYWORDS = ("total", "sales", "revenue", "amount", "quantity", "qty", "income", "cogs", "profit", "cost", "count")
TARGET_KEYWORDS = ("outcome", "target", "label", "class", "rating", "churn", "default", "status", "gender", "type")
CHART_BLOCK_RE = re.compile(r"```chart\s*\n.*?```\s*", re.DOTALL)


def build_charts(dataframe: pd.DataFrame, profile: dict[str, Any], question: str = "") -> list[dict[str, Any]]:
    """Pick the most informative charts for one table. Columns named in the
    question are preferred everywhere a column has to be chosen."""
    if dataframe.empty or not len(dataframe.columns):
        return []
    # String, unique column labels so every lookup below returns a Series.
    dataframe = dataframe.copy(deep=False)
    dataframe.columns = [str(column) for column in dataframe.columns]
    dataframe = dataframe.loc[:, ~dataframe.columns.duplicated()]
    rows = len(dataframe)
    mentioned = _mentioned_columns(dataframe, question)
    numeric = [
        column
        for column in dataframe.select_dtypes(include="number").columns
        if not _is_identifier(dataframe[column], rows) and dataframe[column].nunique(dropna=True) > 1
    ]
    continuous = [column for column in numeric if dataframe[column].nunique(dropna=True) > 10]
    categorical = _categorical_columns(dataframe, rows, numeric, mentioned)
    measure = _primary_measure(continuous, mentioned)
    group_column = _group_column(dataframe, categorical, mentioned, measure) if measure else None
    label = _table_label(profile)

    charts: list[dict[str, Any]] = []
    builders = [
        lambda: _trend_chart(dataframe, measure, label),
        lambda: _group_chart(dataframe, group_column, measure, label),
        *[
            (lambda column=column: _distribution_chart(dataframe[column], column, label))
            for column in _prioritize(continuous, [*mentioned, *([measure] if measure else [])])[:2]
        ],
        *[
            (lambda column=column: _category_chart(dataframe[column], column, label))
            for column in [item for item in categorical if item != group_column][:2]
        ],
        lambda: _scatter_chart(dataframe, profile, mentioned, continuous, label),
        lambda: _heatmap_chart(dataframe, numeric, profile, mentioned, label),
        lambda: _missing_chart(profile, label),
    ]
    for build in builders:
        if len(charts) >= MAX_CHARTS_PER_TABLE:
            break
        try:
            chart = build()
        except Exception:
            # A chart is a nice-to-have; bad data in one column never fails the analysis.
            chart = None
        if chart and not any(existing["title"] == chart["title"] for existing in charts):
            charts.append(chart)
    return charts


def render_chart_blocks(charts: list[dict[str, Any]]) -> str:
    """Markdown section with one fenced ```chart block per spec."""
    blocks = [
        f"```{CHART_FENCE}\n{json.dumps(chart, ensure_ascii=True, separators=(',', ':'), allow_nan=False)}\n```"
        for chart in charts[:MAX_CHARTS_TOTAL]
    ]
    return "## Visualizations\n\n" + "\n\n".join(blocks) if blocks else ""


def charts_from_metadata(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    charts = (metadata or {}).get("charts")
    return [chart for chart in charts if isinstance(chart, dict)] if isinstance(charts, list) else []


def strip_chart_blocks(text: str) -> str:
    """Chart JSON is for the UI only; keep it out of LLM prompts."""
    return CHART_BLOCK_RE.sub("", text).replace("## Visualizations\n\n", "").strip()


# ---------------------------------------------------------------- builders


def _trend_chart(dataframe: pd.DataFrame, measure: str | None, label: str) -> dict[str, Any] | None:
    date_column, dates = _date_column(dataframe)
    if date_column is None:
        return None
    frame = pd.DataFrame({"date": dates})
    if measure is not None:
        frame["value"] = pd.to_numeric(dataframe[measure], errors="coerce")
    frame = frame.dropna()
    if frame["date"].nunique() < 3:
        return None
    span_days = (frame["date"].max() - frame["date"].min()).days
    if span_days <= MAX_TREND_POINTS:
        period, period_name = "D", "day"
    elif span_days <= MAX_TREND_POINTS * 7:
        period, period_name = "W", "week"
    elif span_days <= MAX_TREND_POINTS * 31:
        period, period_name = "M", "month"
    else:
        period, period_name = "Y", "year"
    grouped = frame.groupby(frame["date"].dt.to_period(period))
    if measure is None:
        series = grouped.size()
        title = f"Records per {period_name}"
        y_label = "Rows"
    else:
        series = grouped["value"].agg("sum" if _is_additive(measure) else "mean")
        title = f"{_aggregate_title(measure)} per {period_name}"
        y_label = measure
    series = series.tail(MAX_TREND_POINTS)
    return {
        "type": "line",
        "title": title,
        "subtitle": f"{label} · by {date_column}" + (" (week starting)" if period == "W" else ""),
        "x_label": date_column,
        "y_label": y_label,
        "x": [item.start_time.strftime(PERIOD_LABELS[period]) for item in series.index],
        "y": [_number(value) for value in series.values],
    }


def _group_chart(
    dataframe: pd.DataFrame, column: str | None, measure: str | None, label: str
) -> dict[str, Any] | None:
    if measure is None or column is None:
        return None
    aggregate = "sum" if _is_additive(measure) else "mean"
    values = (
        pd.to_numeric(dataframe[measure], errors="coerce")
        .groupby(dataframe[column].astype(str))
        .agg(aggregate)
        .dropna()
        .sort_values(ascending=False)
        .head(MAX_CATEGORIES)
    )
    if len(values) < 2:
        return None
    return {
        "type": "bar",
        "title": f"{_aggregate_title(measure)} by {column}",
        "subtitle": label,
        "value_label": measure,
        "categories": [str(item) for item in values.index],
        "values": [_number(value) for value in values.values],
    }


def _distribution_chart(series: pd.Series, column: str, label: str) -> dict[str, Any] | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    values = values[np.isfinite(values)]
    if values.nunique() < 2:
        return None
    counts, edges = np.histogram(values, bins=HISTOGRAM_BINS)
    return {
        "type": "histogram",
        "title": f"Distribution of {column}",
        "subtitle": f"{label} · {len(values):,} values, median {_number(values.median())}",
        "x_label": column,
        "y_label": "Rows",
        "bins": [
            {"start": _number(edges[index]), "end": _number(edges[index + 1]), "count": int(count)}
            for index, count in enumerate(counts)
        ],
    }


def _category_chart(series: pd.Series, column: str, label: str) -> dict[str, Any] | None:
    counts = series.dropna().astype(str).value_counts()
    if len(counts) < 2:
        return None
    top = counts.head(MAX_CATEGORIES)
    categories = [str(item) for item in top.index]
    values = [int(value) for value in top.values]
    other = int(counts.iloc[MAX_CATEGORIES:].sum())
    if other:
        categories.append("Other")
        values.append(other)
    return {
        "type": "bar",
        "title": f"Rows by {column}",
        "subtitle": f"{label} · {len(counts):,} distinct values",
        "value_label": "Rows",
        "categories": categories,
        "values": values,
    }


def _scatter_chart(
    dataframe: pd.DataFrame,
    profile: dict[str, Any],
    mentioned: list[str],
    continuous: list[str],
    label: str,
) -> dict[str, Any] | None:
    mentioned_continuous = [column for column in mentioned if column in continuous]
    if len(mentioned_continuous) >= 2:
        # "plot Glucose vs BMI": the first named column goes on the y axis.
        right, left = mentioned_continuous[:2]
    else:
        pair = next(
            (
                item
                for item in profile.get("correlations", [])
                if abs(item["coefficient"]) >= 0.5 and item["left"] in continuous and item["right"] in continuous
            ),
            None,
        )
        if pair is None:
            return None
        left, right = pair["left"], pair["right"]
    frame = dataframe[[left, right]].apply(pd.to_numeric, errors="coerce").dropna()
    if len(frame) < 3:
        return None
    coefficient = frame[left].corr(frame[right])
    if len(frame) > MAX_SCATTER_POINTS:
        # Fixed seed: the same dataset always renders the same sample.
        frame = frame.sample(MAX_SCATTER_POINTS, random_state=0).sort_index()
    return {
        "type": "scatter",
        "title": f"{right} vs {left}",
        "subtitle": f"{label} · correlation {coefficient:+.2f}" if pd.notna(coefficient) else label,
        "x_label": left,
        "y_label": right,
        "points": [[_number(x), _number(y)] for x, y in frame.itertuples(index=False)],
    }


def _heatmap_chart(
    dataframe: pd.DataFrame,
    numeric: list[str],
    profile: dict[str, Any],
    mentioned: list[str],
    label: str,
) -> dict[str, Any] | None:
    if len(numeric) < 3:
        return None
    ranked: list[str] = [column for column in mentioned if column in numeric]
    for item in profile.get("correlations", []):
        ranked.extend(column for column in (item["left"], item["right"]) if column in numeric)
    ranked.extend(numeric)
    columns = list(dict.fromkeys(ranked))[:MAX_HEATMAP_COLUMNS]
    matrix = dataframe[columns].corr()
    return {
        "type": "heatmap",
        "title": "Correlation matrix",
        "subtitle": f"{label} · Pearson r between numeric columns",
        "labels": columns,
        "matrix": [[_number(round(float(value), 2)) if pd.notna(value) else None for value in row] for row in matrix.values],
    }


def _missing_chart(profile: dict[str, Any], label: str) -> dict[str, Any] | None:
    rows = profile.get("rows") or 0
    missing = list(profile.get("missing_by_column", {}).items())[:MAX_CATEGORIES]
    if not rows or not missing:
        return None
    return {
        "type": "bar",
        "title": "Missing values by column",
        "subtitle": label,
        "value_label": "% missing",
        "unit": "%",
        "categories": [str(column) for column, _ in missing],
        "values": [_number(round(100.0 * count / rows, 2)) for _, count in missing],
    }


# ----------------------------------------------------------------- helpers


def _table_label(profile: dict[str, Any]) -> str:
    label = str(profile.get("file_name", "dataset"))
    if profile.get("sheet_name") not in (None, "data"):
        label += f" / {profile['sheet_name']}"
    return label


def _mentioned_columns(dataframe: pd.DataFrame, question: str) -> list[str]:
    text = question.lower()
    if not text.strip():
        return []
    hits = [
        (text.find(str(column).lower()), str(column))
        for column in dataframe.columns
        if len(str(column)) >= 2 and re.search(rf"(?<![a-z0-9]){re.escape(str(column).lower())}(?![a-z0-9])", text)
    ]
    return [column for _, column in sorted(hits)]


def _prioritize(columns: list[str], mentioned: list[str]) -> list[str]:
    return [column for column in mentioned if column in columns] + [column for column in columns if column not in mentioned]


def _is_identifier(series: pd.Series, rows: int) -> bool:
    name = str(series.name).lower()
    if re.search(r"(^|[^a-z])(id|uuid|index)([^a-z]|$)", name):
        return True
    unique = series.nunique(dropna=True)
    if rows >= 20 and unique >= 0.95 * rows:
        if not pd.api.types.is_numeric_dtype(series):
            return True
        return pd.api.types.is_integer_dtype(series) and series.is_monotonic_increasing
    return False


def _categorical_columns(dataframe: pd.DataFrame, rows: int, numeric: list[str], mentioned: list[str]) -> list[str]:
    candidates = []
    for column in dataframe.columns:
        series = dataframe[column]
        unique = series.nunique(dropna=True)
        if unique < 2 or _is_identifier(series, rows):
            continue
        if pd.api.types.is_numeric_dtype(series):
            # Low-cardinality numeric codes (0/1 outcomes, 1-5 ratings) are
            # categories, but only worth a chart when they look like a target.
            if column in numeric and unique <= 10 and (
                column in mentioned or any(keyword in str(column).lower() for keyword in TARGET_KEYWORDS)
            ):
                candidates.append(str(column))
            continue
        if pd.api.types.is_datetime64_any_dtype(series) or _looks_like_dates(series):
            continue
        if unique <= 50 or column in mentioned:
            candidates.append(str(column))
    # 3-12 groups read best as bars; two-value splits next; long tails last.
    candidates.sort(key=lambda column: _category_rank(dataframe[column].nunique(dropna=True)))
    return _prioritize(candidates, mentioned)


def _category_rank(unique: int) -> tuple[int, int]:
    if 3 <= unique <= MAX_CATEGORIES:
        return (0, unique)
    return (1, unique) if unique == 2 else (2, unique)


def _group_column(
    dataframe: pd.DataFrame, categorical: list[str], mentioned: list[str], measure: str | None
) -> str | None:
    """Category to break the primary measure down by: a named one, else the
    one with the most groups that still fit a readable bar chart."""
    usable = [
        column
        for column in categorical
        if column != measure and 2 <= dataframe[column].nunique(dropna=True) <= MAX_CATEGORIES
    ]
    named = [column for column in mentioned if column in usable]
    if named:
        return named[0]
    return max(usable, key=lambda column: dataframe[column].nunique(dropna=True), default=None)


def _aggregate_title(measure: str) -> str:
    if not _is_additive(measure):
        return f"Average {measure}"
    return measure if measure.lower().startswith("total") else f"Total {measure}"


def _primary_measure(continuous: list[str], mentioned: list[str]) -> str | None:
    for column in mentioned:
        if column in continuous:
            return column
    for keyword in SUM_KEYWORDS:
        for column in continuous:
            if keyword in column.lower():
                return column
    return continuous[0] if continuous else None


def _is_additive(column: str) -> bool:
    return any(keyword in column.lower() for keyword in SUM_KEYWORDS)


def _date_column(dataframe: pd.DataFrame) -> tuple[str | None, pd.Series | None]:
    for column in dataframe.columns:
        series = dataframe[column]
        if pd.api.types.is_datetime64_any_dtype(series):
            return str(column), series
    for column in dataframe.columns:
        series = dataframe[column]
        if _looks_like_dates(series):
            parsed = _parse_dates(series)
            if parsed.notna().mean() >= 0.9:
                return str(column), parsed
    return None, None


def _looks_like_dates(series: pd.Series) -> bool:
    if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
        return False
    name = str(series.name).lower()
    sample = series.dropna().astype(str).head(50)
    if sample.empty or not sample.str.contains(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}").all():
        return False
    return "date" in name or "time" in name or "day" in name or _parse_dates(sample).notna().mean() >= 0.9


def _parse_dates(series: pd.Series) -> pd.Series:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.to_datetime(series, errors="coerce")


def _number(value: Any) -> float | int | None:
    if value is None or pd.isna(value):
        return None
    numeric = float(value)
    if not np.isfinite(numeric):
        return None
    if numeric.is_integer():
        return int(numeric)
    return round(numeric, 4)
