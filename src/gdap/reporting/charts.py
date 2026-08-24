"""Chart rendering.

:class:`ChartSpec` is transport-neutral (it is JSON in the API and in a report artifact). This
module is the only place that knows about Plotly, so swapping the visual library — or rendering
the same spec in a React front-end — does not touch the analytics layer.
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go

from gdap.core.contracts import ChartSpec
from gdap.observability.logging import get_logger

log = get_logger(__name__)

#: Colour-blind-safe qualitative palette (Okabe-Ito), consistent across every artifact.
PALETTE = [
    "#0072B2",
    "#E69F00",
    "#009E73",
    "#D55E00",
    "#CC79A7",
    "#56B4E9",
    "#F0E442",
    "#999999",
]

_LAYOUT = {
    "template": "plotly_white",
    "margin": {"l": 60, "r": 24, "t": 56, "b": 56},
    "font": {"family": "Inter, Segoe UI, system-ui, sans-serif", "size": 13},
    "colorway": PALETTE,
    "hovermode": "closest",
    "legend": {"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
}


def to_figure(spec: ChartSpec) -> go.Figure:
    """Build a Plotly figure from a chart spec. Unknown kinds degrade to a table-like bar."""
    builder = _BUILDERS.get(spec.kind, _bar)
    figure = builder(spec)
    figure.update_layout(title=spec.title, **_LAYOUT)
    return figure


def to_html(spec: ChartSpec, *, include_js: bool = False, div_id: str | None = None) -> str:
    """Render a chart as an embeddable ``<div>``; include the JS bundle once per document."""
    try:
        figure = to_figure(spec)
        return figure.to_html(
            full_html=False,
            include_plotlyjs="inline" if include_js else False,
            div_id=div_id,
            config={"displaylogo": False, "responsive": True},
        )
    except Exception as exc:  # a broken chart must not break the report
        log.error("chart_render_failed", title=spec.title, kind=spec.kind, error=str(exc))
        return (
            f'<div class="chart-error"><strong>{spec.title}</strong>: '
            f"chart could not be rendered ({type(exc).__name__})</div>"
        )


# ─────────────────────────────────────────── builders ──────────────────────────────────────


def _values(spec: ChartSpec, key: str | None) -> list[Any]:
    if key is None:
        return []
    return [row.get(key) for row in spec.data]


def _y_series(spec: ChartSpec) -> list[str]:
    if spec.y is None:
        return []
    return [spec.y] if isinstance(spec.y, str) else list(spec.y)


def _line(spec: ChartSpec) -> go.Figure:
    figure = go.Figure()
    x = _values(spec, spec.x)
    interval = spec.options.get("interval") or {}
    for name in _y_series(spec):
        y = _values(spec, name)
        if all(value is None for value in y):
            continue
        figure.add_trace(
            go.Scatter(
                x=x,
                y=y,
                name=name,
                mode="lines+markers" if len(x) <= 60 else "lines",
                line={"width": 2, "dash": "dot" if name in {"trend", "moving_avg"} else "solid"},
                connectgaps=False,
            )
        )
    lower, upper = interval.get("lower"), interval.get("upper")
    if lower and upper and spec.x:
        x_field = spec.x  # a prediction band needs an x axis to be drawn against
        band_x = [row[x_field] for row in spec.data if row.get(upper) is not None]
        figure.add_trace(
            go.Scatter(
                x=band_x + band_x[::-1],
                y=[row[upper] for row in spec.data if row.get(upper) is not None]
                + [row[lower] for row in spec.data if row.get(lower) is not None][::-1],
                fill="toself",
                fillcolor="rgba(0,114,178,0.12)",
                line={"color": "rgba(0,0,0,0)"},
                name="prediction interval",
                hoverinfo="skip",
            )
        )
    figure.update_xaxes(title=spec.x)
    return figure


def _area(spec: ChartSpec) -> go.Figure:
    figure = _line(spec)
    figure.update_traces(fill="tozeroy")
    return figure


def _bar(spec: ChartSpec) -> go.Figure:
    figure = go.Figure()
    x = _values(spec, spec.x)
    for name in _y_series(spec) or ["value"]:
        figure.add_trace(go.Bar(x=x, y=_values(spec, name), name=name))
    figure.update_xaxes(title=spec.x)
    figure.update_yaxes(title=_y_series(spec)[0] if _y_series(spec) else None)
    return figure


def _hbar(spec: ChartSpec) -> go.Figure:
    category: str = spec.y if isinstance(spec.y, str) else (spec.series or "label")
    value_field: str = spec.x or "value"
    rows = sorted(spec.data, key=lambda row: row.get(value_field) or 0)
    values = [row.get(value_field) for row in rows]
    colors = (
        ["#D55E00" if (value or 0) < 0 else "#0072B2" for value in values]
        if spec.options.get("diverging")
        else None
    )
    figure = go.Figure(
        go.Bar(
            x=values,
            y=[row.get(category) for row in rows],
            orientation="h",
            marker={"color": colors} if colors else None,
        )
    )
    figure.update_xaxes(title=value_field)
    return figure


def _histogram(spec: ChartSpec) -> go.Figure:
    """Bins are precomputed upstream, so this is a bar chart over the bin edges."""
    figure = go.Figure(
        go.Bar(
            x=[row.get("bin") for row in spec.data],
            y=[row.get("rows") for row in spec.data],
            marker={"line": {"width": 0}},
        )
    )
    figure.update_layout(bargap=0.02)
    figure.update_xaxes(title=spec.x)
    figure.update_yaxes(title="rows")
    return figure


def _scatter(spec: ChartSpec) -> go.Figure:
    color_field = spec.options.get("color_field")
    x_field: str = spec.x or ""
    y_field: str = spec.y if isinstance(spec.y, str) else ""
    if color_field:
        groups: dict[Any, list[dict[str, Any]]] = {}
        for row in spec.data:
            groups.setdefault(row.get(color_field), []).append(row)
        figure = go.Figure()
        for key, rows in groups.items():
            figure.add_trace(
                go.Scattergl(
                    x=[row.get(x_field) for row in rows],
                    y=[row.get(y_field) for row in rows],
                    mode="markers",
                    name=f"{color_field}={key}",
                    marker={"size": 8 if key else 5, "opacity": 0.9 if key else 0.45},
                )
            )
    else:
        figure = go.Figure(
            go.Scattergl(
                x=_values(spec, x_field),
                y=_values(spec, y_field),
                mode="markers",
                marker={"size": 6, "opacity": 0.6},
            )
        )
    figure.update_xaxes(title=x_field)
    figure.update_yaxes(title=y_field)
    return figure


def _box(spec: ChartSpec) -> go.Figure:
    figure = go.Figure()
    for name in _y_series(spec):
        figure.add_trace(go.Box(y=_values(spec, name), name=name, boxpoints="outliers"))
    return figure


def _heatmap(spec: ChartSpec) -> go.Figure:
    value_field = spec.options.get("value_field", "value")
    xs = list(dict.fromkeys(row.get("x") for row in spec.data))
    ys = list(dict.fromkeys(row.get("y") for row in spec.data))
    lookup = {(row.get("x"), row.get("y")): row.get(value_field) for row in spec.data}
    matrix = [[lookup.get((x, y)) for x in xs] for y in ys]
    figure = go.Figure(
        go.Heatmap(
            z=matrix,
            x=xs,
            y=ys,
            colorscale="RdBu",
            zmid=0 if spec.options.get("scale") == "diverging" else None,
            texttemplate="%{z:.2f}" if len(xs) * len(ys) <= 100 else None,
        )
    )
    return figure


def _pie(spec: ChartSpec) -> go.Figure:
    label_field: str = spec.y if isinstance(spec.y, str) else (spec.series or "label")
    value_field: str = spec.x or "value"
    return go.Figure(
        go.Pie(
            labels=[row.get(label_field) for row in spec.data],
            values=[row.get(value_field) for row in spec.data],
            hole=0.45,
        )
    )


_BUILDERS = {
    "line": _line,
    "area": _area,
    "bar": _bar,
    "hbar": _hbar,
    "histogram": _histogram,
    "scatter": _scatter,
    "box": _box,
    "heatmap": _heatmap,
    "pie": _pie,
}
