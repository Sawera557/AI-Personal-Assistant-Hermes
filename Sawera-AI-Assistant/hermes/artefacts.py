"""Artefacts — deterministic, LLM-free live re-render of a persisted canvas.

An artefact row (hermes/db/artefacts.py) binds a title + a data source + a
render recipe. Opening it calls `sync_and_render(artefact_id)`, which:

  1. loads the row,
  2. rebuilds the correct per-business MatonClient from the STORED business_slug
     (no chat turn / no active_business in scope — this is why the slug is
     persisted on the row),
  3. re-fetches the live source (Sheets values / Docs text / Drive bytes) through
     the same Maton helpers the chat tools use,
  4. renders a self-contained inline-CSS HTML canvas (mirrors
     hermes/reviews_report.py's approach), escaping every source value,
  5. caches the HTML + last_synced_at and returns it.

CRITICAL: there is NO import of hermes.agent / anthropic anywhere in this module.
Opening an artefact must never call the LLM. (A test asserts zero Anthropic
calls.) The AI is only involved when an artefact is CREATED, via the
create_artefact agent tool in hermes/tools.py.
"""
from __future__ import annotations

import base64
import html as _html
import logging
import re
from typing import Any

from . import reviews_report
from .config import Config
from .db import artefacts as artefacts_db
from .maton import MatonClient

log = logging.getLogger("hermes.artefacts")

VALID_SOURCE_TYPES = {"sheets", "drive", "docs", "reviews"}
VALID_RENDERS = {"table", "kv", "raw", "dashboard", "reviews"}

_MAX_ROWS_DEFAULT = 500
_MAX_DOC_CHARS = 200_000


class ArtefactError(RuntimeError):
    """A source fetch failed in a way worth showing the user (bad range, deleted
    file, disconnected business …). Caught by sync_and_render → error canvas."""


# ─────────────────────────────────────────────────────────────────────────────
#  Fetch — one adapter per source_type, reusing the existing Maton read helpers.
#  Each returns a normalized dict the renderer understands:
#    rows  → {"kind":"rows",  "rows":[[...]], "range": str|None}
#    text  → {"kind":"text",  "text": str, "title": str}
#    embed → {"kind":"embed", "mime", "b64", "name", "web_link"}
#    link  → {"kind":"link",  "name", "mime", "web_link", "note"}
# ─────────────────────────────────────────────────────────────────────────────
def _describe_envelope(resp: dict[str, Any] | None, source_label: str) -> str:
    status = (resp or {}).get("status")
    data = (resp or {}).get("data")
    detail = ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            detail = err.get("message") or ""
        elif isinstance(err, str):
            detail = err
    msg = f"{source_label} request failed"
    if status:
        msg += f" (HTTP {status})"
    if detail:
        msg += f": {detail}"
    return msg


def _fetch_sheets(client: MatonClient, binding: dict[str, Any]) -> dict[str, Any]:
    sid = str(binding.get("spreadsheet_id") or "").strip()
    rng = str(binding.get("range") or "").strip()
    if not sid:
        raise ArtefactError("This artefact has no spreadsheet_id.")
    path = f"v4/spreadsheets/{sid}/values/{rng}" if rng else f"v4/spreadsheets/{sid}/values/A1:Z1000"
    resp = client.request(
        "google-sheets", "GET", path, connection_id=binding.get("connection_id"),
    )
    if not resp.get("ok"):
        raise ArtefactError(_describe_envelope(resp, "Google Sheets"))
    data = resp.get("data") or {}
    return {"kind": "rows", "rows": data.get("values") or [], "range": data.get("range") or rng or None}


def _docs_plaintext(doc: dict[str, Any] | None) -> str:
    """Flatten a Google Docs `documents.get` payload to plain text by walking
    body.content[].paragraph.elements[].textRun.content (which already carries
    its own newlines). Tables / section breaks are skipped for a plain render."""
    out: list[str] = []
    body = (doc or {}).get("body") or {}
    for element in body.get("content") or []:
        para = element.get("paragraph")
        if not para:
            continue
        for pe in para.get("elements") or []:
            tr = pe.get("textRun")
            if tr and tr.get("content"):
                out.append(tr["content"])
    return "".join(out)


def _fetch_docs(client: MatonClient, binding: dict[str, Any]) -> dict[str, Any]:
    did = str(binding.get("document_id") or "").strip()
    if not did:
        raise ArtefactError("This artefact has no document_id.")
    resp = client.request(
        "google-docs", "GET", f"v1/documents/{did}", connection_id=binding.get("connection_id"),
    )
    if not resp.get("ok"):
        raise ArtefactError(_describe_envelope(resp, "Google Docs"))
    doc = resp.get("data") or {}
    text = _docs_plaintext(doc)
    if len(text) > _MAX_DOC_CHARS:
        text = text[:_MAX_DOC_CHARS] + "\n\n… (truncated — document is longer than the display limit)"
    return {"kind": "text", "title": doc.get("title") or "", "text": text}


def _fetch_drive(client: MatonClient, binding: dict[str, Any]) -> dict[str, Any]:
    # Deferred import: tools imports this module (for the create_artefact
    # handler), so import drive_fetch lazily to avoid an import cycle.
    from .tools import drive_fetch

    fid = str(binding.get("file_id") or "").strip()
    if not fid:
        raise ArtefactError("This artefact has no file_id.")
    f = drive_fetch(client, fid)
    if not f["ok"]:
        raise ArtefactError(_describe_envelope(f["resp"], "Google Drive"))
    if f["readable"] and f["raw"]:
        return {
            "kind": "embed", "mime": f["mime"], "name": f["name"],
            "web_link": f["web_link"],
            "b64": base64.standard_b64encode(f["raw"]).decode("utf-8"),
        }
    return {"kind": "link", "name": f["name"], "mime": f["mime"],
            "web_link": f["web_link"], "note": f.get("note")}


def _fetch(source_type: str, client: MatonClient, binding: dict[str, Any]) -> dict[str, Any]:
    if source_type == "sheets":
        return _fetch_sheets(client, binding)
    if source_type == "docs":
        return _fetch_docs(client, binding)
    if source_type == "drive":
        return _fetch_drive(client, binding)
    raise ArtefactError(f"Unknown source type '{source_type}'.")


# ─────────────────────────────────────────────────────────────────────────────
#  Render — deterministic HTML. Every source value passes through _esc(); no
#  value is ever concatenated into markup unescaped (the top correctness rule).
# ─────────────────────────────────────────────────────────────────────────────
def _esc(value: Any) -> str:
    return _html.escape("" if value is None else str(value), quote=True)


def _clamp_int(value: Any, default: int) -> int:
    try:
        n = int(value)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def _default_render_for(source_type: str) -> str:
    return {"sheets": "table", "docs": "raw", "drive": "raw", "reviews": "reviews"}.get(source_type, "raw")


def _render_table(recipe: dict[str, Any], data: dict[str, Any]) -> tuple[str, str]:
    rows = [list(r) for r in (data.get("rows") or [])]
    options = recipe.get("options") or {}
    header_row = options.get("header_row", True)
    max_rows = _clamp_int(options.get("max_rows"), _MAX_ROWS_DEFAULT)
    if not rows:
        return ('<p class="muted">This source returned no rows.</p>', "0 rows")

    headers = None
    body_rows = rows
    if header_row:
        headers = rows[0]
        body_rows = rows[1:]

    # Optional column selection by header name or 0-based index.
    col_idx: list[int] | None = None
    cols = recipe.get("columns")
    if cols and headers is not None:
        name_to_idx = {str(h).strip().lower(): i for i, h in enumerate(headers)}
        picked: list[int] = []
        for c in cols:
            if isinstance(c, bool):
                continue
            if isinstance(c, int):
                picked.append(c)
            else:
                idx = name_to_idx.get(str(c).strip().lower())
                if idx is not None:
                    picked.append(idx)
        col_idx = picked or None

    def pick(row: list[Any]) -> list[Any]:
        if col_idx is None:
            return row
        return [row[i] if 0 <= i < len(row) else "" for i in col_idx]

    total = len(body_rows)
    shown = body_rows[:max_rows]
    ncol = len(pick(headers)) if headers is not None else max((len(r) for r in shown), default=0)

    thead = ""
    if headers is not None:
        thead = "<thead><tr>" + "".join(f"<th>{_esc(h)}</th>" for h in pick(headers)) + "</tr></thead>"
    trs = []
    for r in shown:
        cells = pick(r)
        cells = list(cells) + [""] * (ncol - len(cells))
        trs.append("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in cells) + "</tr>")
    table = f'<div class="scroll"><table>{thead}<tbody>{"".join(trs)}</tbody></table></div>'

    banner = ""
    if total > max_rows:
        banner = (f'<p class="banner">Showing the first {max_rows} of {total} rows — '
                  "narrow the range to see more.</p>")

    rng = data.get("range")
    subtitle = f"{total} row{'s' if total != 1 else ''}" + (f" · {rng}" if rng else "")
    return (banner + table, subtitle)


def _render_kv(recipe: dict[str, Any], data: dict[str, Any]) -> tuple[str, str]:
    rows = [list(r) for r in (data.get("rows") or [])]
    options = recipe.get("options") or {}
    max_rows = _clamp_int(options.get("max_rows"), _MAX_ROWS_DEFAULT)
    if not rows:
        return ('<p class="muted">This source returned no rows.</p>', "0 pairs")
    items = []
    for r in rows[:max_rows]:
        if not r:
            continue
        key = r[0]
        val = " ".join(str(x) for x in r[1:]) if len(r) > 1 else ""
        items.append(f'<div class="kv"><div class="k">{_esc(key)}</div>'
                     f'<div class="v">{_esc(val)}</div></div>')
    return (f'<div class="kvlist">{"".join(items)}</div>', f"{len(items)} pairs")


def _render_raw(source_type: str, recipe: dict[str, Any], data: dict[str, Any]) -> tuple[str, str]:
    kind = data.get("kind")
    if kind == "text":
        return (f'<pre class="doc">{_esc(data.get("text"))}</pre>', data.get("title") or "")
    if kind == "embed":
        mime = data.get("mime") or ""
        b64 = data.get("b64") or ""   # base64 charset is HTML-safe
        name = data.get("name") or ""
        if mime == "application/pdf":
            body = f'<embed class="embed" type="application/pdf" src="data:application/pdf;base64,{b64}">'
        else:
            body = f'<img class="embed" alt="{_esc(name)}" src="data:{_esc(mime)};base64,{b64}">'
        if data.get("web_link"):
            body += (f'<p class="muted"><a href="{_esc(data["web_link"])}" target="_blank" '
                     'rel="noopener">Open in Google Drive ↗</a></p>')
        return (body, name)
    if kind == "link":
        name = data.get("name") or "File"
        card = f'<div class="card"><div class="cardtitle">{_esc(name)}</div>'
        if data.get("note"):
            card += f'<p class="muted">{_esc(data["note"])}</p>'
        if data.get("web_link"):
            card += (f'<p><a href="{_esc(data["web_link"])}" target="_blank" '
                     'rel="noopener">Open in Google Drive ↗</a></p>')
        card += "</div>"
        return (card, "")
    if kind == "rows":
        return _render_table(recipe, data)
    return ('<p class="muted">Nothing to display.</p>', "")


# ─────────────────────────────────────────────────────────────────────────────
#  Dashboard — KPI cards + server-side SVG charts computed from the live rows.
#  Fully deterministic and JS-free: aggregation happens in Python and the charts
#  are hand-drawn SVG (same spirit as reviews_report), so opening stays LLM-free
#  and needs no external chart library.
# ─────────────────────────────────────────────────────────────────────────────
_PALETTE = ["#2f6bff", "#e0745a", "#2f9e6b", "#c98500", "#8b5cf6", "#12a5b8",
            "#d6455f", "#6d97ff", "#57b56a", "#e0a92a", "#b07cff", "#3ec5d8"]
_MAX_SLICES = 12


def _col_idx(headers: list[Any], name: Any) -> int | None:
    if name is None:
        return None
    if isinstance(name, bool):
        return None
    if isinstance(name, int):
        return name
    key = str(name).strip().lower()
    for i, h in enumerate(headers):
        if str(h).strip().lower() == key:
            return i
    return None


def _num(v: Any) -> float | None:
    """Best-effort number from a cell: strips currency symbols, commas, spaces."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else None


_MONTH_FMTS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d",
               "%d %b %Y", "%d %B %Y", "%b %Y", "%B %Y", "%m/%Y", "%Y-%m")


def _month_bucket(v: Any) -> str | None:
    from datetime import datetime
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in _MONTH_FMTS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m")
        except ValueError:
            continue
    m = re.match(r"(\d{4})[-/](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return None


def _fmt_num(n: Any) -> str:
    if n is None:
        return "—"
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    if isinstance(n, int):
        return f"{n:,}"
    try:
        return f"{n:,.2f}"
    except (TypeError, ValueError):
        return _esc(n)


def _truncate(s: Any, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _aggregate(rows: list[list[Any]], headers: list[Any], spec: dict[str, Any]) -> list[tuple[str, float]]:
    """Group rows by a column (or month bucket of a date column) and aggregate a
    value column. Returns [(label, value), …]."""
    gb = _col_idx(headers, spec.get("group_by"))
    vi = _col_idx(headers, spec.get("value"))
    agg = str(spec.get("agg") or ("count" if vi is None else "sum")).lower()
    bucket = spec.get("bucket")
    acc: dict[str, list[float]] = {}   # key -> [sum, count, max, min]
    order: list[str] = []
    for r in rows:
        if gb is None:
            key = "All"
        else:
            raw = r[gb] if gb < len(r) else ""
            key = _month_bucket(raw) if bucket == "month" else (str(raw).strip() or "—")
            if key is None:
                continue
        val = _num(r[vi]) if (vi is not None and vi < len(r)) else None
        if key not in acc:
            acc[key] = [0.0, 0, None, None]
            order.append(key)
        a = acc[key]
        if val is not None:
            a[0] += val
            a[2] = val if a[2] is None else max(a[2], val)
            a[3] = val if a[3] is None else min(a[3], val)
        a[1] += 1

    def value_of(a: list[float]) -> float:
        if agg == "count":
            return a[1]
        if agg == "avg":
            return a[0] / a[1] if a[1] else 0.0
        if agg == "max":
            return a[2] or 0.0
        if agg == "min":
            return a[3] or 0.0
        return a[0]

    items = [(k, value_of(acc[k])) for k in order]
    if bucket == "month":
        items.sort(key=lambda kv: kv[0])
    elif str(spec.get("sort") or "").lower() != "none":
        items.sort(key=lambda kv: kv[1], reverse=True)
    limit = spec.get("limit")
    if bucket != "month" and isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        items = items[:limit]
    return items[:_MAX_SLICES] if bucket != "month" else items


def _kpi_value(rows: list[list[Any]], headers: list[Any], spec: dict[str, Any]) -> Any:
    agg = str(spec.get("agg") or "count").lower()
    if agg == "count":
        return len(rows)
    ci = _col_idx(headers, spec.get("column"))
    if ci is None:
        return None
    nums = [_num(r[ci]) for r in rows if ci < len(r)]
    nums = [n for n in nums if n is not None]
    if not nums:
        return 0
    if agg == "sum":
        return sum(nums)
    if agg == "avg":
        return sum(nums) / len(nums)
    if agg == "max":
        return max(nums)
    if agg == "min":
        return min(nums)
    return len(nums)


def _svg_bar(items: list[tuple[str, float]]) -> str:
    if not items:
        return '<p class="muted">No data for this chart.</p>'
    n = len(items)
    rowh, top, W, label_w = 34, 8, 760, 190
    bar_x = label_w + 8
    bar_w_max = W - bar_x - 96
    H = top * 2 + n * rowh
    mx = max((v for _, v in items), default=0) or 1
    out = [f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="xMinYMin meet" role="img">']
    for i, (label, val) in enumerate(items):
        y = top + i * rowh
        cy = y + rowh / 2
        bw = max(2.0, bar_w_max * (val / mx))
        color = _PALETTE[i % len(_PALETTE)]
        out.append(f'<text x="{label_w}" y="{cy:.0f}" text-anchor="end" dominant-baseline="middle" class="c-lbl">{_esc(_truncate(label, 24))}</text>')
        out.append(f'<rect x="{bar_x}" y="{y + 6}" width="{bw:.1f}" height="{rowh - 12}" rx="5" fill="{color}"/>')
        out.append(f'<text x="{bar_x + bw + 7:.1f}" y="{cy:.0f}" dominant-baseline="middle" class="c-val">{_esc(_fmt_num(val))}</text>')
    out.append("</svg>")
    return "".join(out)


def _svg_doughnut(items: list[tuple[str, float]]) -> str:
    import math
    items = [(k, v) for k, v in items if v > 0]
    if not items:
        return '<p class="muted">No data for this chart.</p>'
    total = sum(v for _, v in items) or 1
    cx, cy, r, ir = 116, 116, 104, 62
    out = ['<svg viewBox="0 0 480 236" preserveAspectRatio="xMinYMin meet" role="img">']
    if len(items) == 1:
        color = _PALETTE[0]
        out.append(f'<circle cx="{cx}" cy="{cy}" r="{(r + ir) / 2:.0f}" fill="none" stroke="{color}" stroke-width="{r - ir}"/>')
    else:
        ang = -math.pi / 2
        for i, (_label, val) in enumerate(items):
            frac = val / total
            a2 = ang + frac * 2 * math.pi
            large = 1 if frac > 0.5 else 0
            x1, y1 = cx + r * math.cos(ang), cy + r * math.sin(ang)
            x2, y2 = cx + r * math.cos(a2), cy + r * math.sin(a2)
            xi1, yi1 = cx + ir * math.cos(a2), cy + ir * math.sin(a2)
            xi2, yi2 = cx + ir * math.cos(ang), cy + ir * math.sin(ang)
            color = _PALETTE[i % len(_PALETTE)]
            out.append(f'<path d="M{x1:.1f},{y1:.1f} A{r},{r} 0 {large} 1 {x2:.1f},{y2:.1f} '
                       f'L{xi1:.1f},{yi1:.1f} A{ir},{ir} 0 {large} 0 {xi2:.1f},{yi2:.1f} Z" fill="{color}"/>')
            ang = a2
    lx, ly = 252, 22
    for i, (label, val) in enumerate(items):
        color = _PALETTE[i % len(_PALETTE)]
        pct = 100 * val / total
        out.append(f'<rect x="{lx}" y="{ly + i * 23}" width="11" height="11" rx="2" fill="{color}"/>')
        out.append(f'<text x="{lx + 18}" y="{ly + i * 23 + 10}" class="c-lbl">{_esc(_truncate(label, 20))} · {pct:.0f}%</text>')
    out.append("</svg>")
    return "".join(out)


def _svg_line(items: list[tuple[str, float]]) -> str:
    if not items:
        return '<p class="muted">No data for this chart.</p>'
    n = len(items)
    W, H = 760, 280
    pad_l, pad_r, pad_t, pad_b = 58, 16, 16, 40
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    mx = max((v for _, v in items), default=0) or 1
    xs = [pad_l + (plot_w * (i / (n - 1)) if n > 1 else plot_w / 2) for i in range(n)]
    ys = [pad_t + plot_h * (1 - (v / mx)) for _, v in items]
    out = [f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="xMinYMin meet" role="img">']
    # horizontal gridlines + y labels at 0, 50%, 100%
    for f in (0.0, 0.5, 1.0):
        gy = pad_t + plot_h * (1 - f)
        out.append(f'<line x1="{pad_l}" y1="{gy:.0f}" x2="{W - pad_r}" y2="{gy:.0f}" class="c-grid"/>')
        out.append(f'<text x="{pad_l - 8}" y="{gy + 4:.0f}" text-anchor="end" class="c-axis">{_esc(_fmt_num(mx * f))}</text>')
    if n > 1:
        area = (f"M{xs[0]:.1f},{pad_t + plot_h:.1f} "
                + " ".join(f"L{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
                + f" L{xs[-1]:.1f},{pad_t + plot_h:.1f} Z")
        out.append(f'<path d="{area}" fill="{_PALETTE[0]}" opacity="0.12"/>')
        out.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))}" '
                   f'fill="none" stroke="{_PALETTE[0]}" stroke-width="2.5"/>')
    for i, (label, val) in enumerate(items):
        out.append(f'<circle cx="{xs[i]:.1f}" cy="{ys[i]:.1f}" r="3.5" fill="{_PALETTE[0]}"/>')
        if n <= 16:
            out.append(f'<text x="{xs[i]:.1f}" y="{H - pad_b + 18:.0f}" text-anchor="middle" class="c-axis">{_esc(_truncate(label, 10))}</text>')
    out.append("</svg>")
    return "".join(out)


def _render_dashboard(recipe: dict[str, Any], data: dict[str, Any]) -> tuple[str, str]:
    # Charts need tabular data; anything else falls back to a plain view.
    if data.get("kind") != "rows":
        return _render_raw("sheets", recipe, data)
    rows = [list(r) for r in (data.get("rows") or [])]
    options = recipe.get("options") or {}
    header_row = options.get("header_row", True)
    headers: list[Any] = []
    body_rows = rows
    if header_row and rows:
        headers = rows[0]
        body_rows = rows[1:]

    parts: list[str] = []
    desc = recipe.get("description") or recipe.get("subtitle")
    if desc:
        parts.append(f'<p class="dash-desc">{_esc(desc)}</p>')

    kpis = recipe.get("kpis") or []
    if kpis:
        cards = []
        for k in kpis:
            if not isinstance(k, dict):
                continue
            val = _kpi_value(body_rows, headers, k)
            disp = _fmt_num(val) if isinstance(val, (int, float)) else _esc(val)
            cards.append(f'<div class="kpi"><div class="kpi-v">{disp}</div>'
                         f'<div class="kpi-k">{_esc(k.get("label") or "")}</div></div>')
        if cards:
            parts.append(f'<div class="kpis">{"".join(cards)}</div>')

    chart_cards = []
    for ch in recipe.get("charts") or []:
        if not isinstance(ch, dict):
            continue
        items = _aggregate(body_rows, headers, ch)
        ctype = str(ch.get("type") or "bar").lower()
        if ctype in ("doughnut", "donut", "pie"):
            svg, wide = _svg_doughnut(items), False
        elif ctype == "line":
            svg, wide = _svg_line(items), True
        else:
            svg, wide = _svg_bar(items), True
        cls = "chart-card chart-wide" if wide else "chart-card"
        chart_cards.append(f'<div class="{cls}"><div class="chart-title">{_esc(ch.get("title") or "")}</div>{svg}</div>')
    if chart_cards:
        parts.append(f'<div class="charts">{"".join(chart_cards)}</div>')

    if recipe.get("table"):
        tbl, _ = _render_table(
            {"options": {"header_row": header_row, "max_rows": options.get("max_rows", 500)}},
            {"kind": "rows", "rows": rows, "range": data.get("range")},
        )
        parts.append(f'<div class="chart-card chart-wide"><div class="chart-title">Data</div>{tbl}</div>')

    if not parts:
        return _render_table(recipe, data)  # nothing configured → just the table

    rng = data.get("range")
    subtitle = str(desc) if desc else (f"{len(body_rows)} rows" + (f" · {rng}" if rng else ""))
    return ("".join(parts), subtitle)


def render_html(source_type: str, recipe: dict[str, Any] | None, data: dict[str, Any]) -> str:
    recipe = recipe or {}
    render = recipe.get("render") or _default_render_for(source_type)
    title = recipe.get("title") or "Artefact"
    if render == "dashboard":
        body, subtitle = _render_dashboard(recipe, data)
    elif render == "table":
        body, subtitle = _render_table(recipe, data)
    elif render == "kv":
        body, subtitle = _render_kv(recipe, data)
    else:
        body, subtitle = _render_raw(source_type, recipe, data)
    return _page(title, subtitle, body)


def render_error_canvas(title: str | None, message: str) -> str:
    body = (f'<div class="card err"><div class="cardtitle">Couldn\'t load this artefact</div>'
            f'<p class="muted">{_esc(message)}</p></div>')
    return _page(title or "Artefact", "", body)


# ─────────────────────────────────────────────────────────────────────────────
#  Orchestrator
# ─────────────────────────────────────────────────────────────────────────────
def sync_and_render(artefact_id: str) -> str | None:
    """Re-fetch the artefact's source and deterministically re-render its HTML.
    Caches the result. Returns None only if the artefact doesn't exist. On a
    fetch failure, records the error and serves the last-good cache if present,
    else a friendly error canvas. Never calls the LLM."""
    row = artefacts_db.get_render_inputs(artefact_id)
    if not row:
        return None

    title = row.get("title") or "Artefact"
    source_type = row.get("source_type")
    binding = row.get("source_binding") or {}
    recipe = row.get("render_recipe") or {}

    # The reviews wrapper delegates to the already-cached S3 report HTML.
    if source_type == "reviews":
        date = str(binding.get("date") or "").strip()
        html = reviews_report.load_report_html(date) if date else None
        if html is None:
            return render_error_canvas(
                title,
                "No review report has been generated for this date yet — it's created when the reviews job runs.",
            )
        return html

    # Rebuild the correct per-business Maton client from the STORED slug (no chat
    # turn in scope). Never substitute another business's key — parity with
    # tools._resolve_client's hard isolation rule.
    slug = row.get("business_slug")
    key = Config.maton_key_for_slug(slug)
    if not key:
        msg = (f"'{slug or 'This business'}' has no Maton connection configured — "
               "reconnect it to load this artefact.")
        artefacts_db.save_error(artefact_id, msg)
        return render_error_canvas(title, msg)

    client = MatonClient(api_key=key)
    try:
        data = _fetch(source_type, client, binding)
    except ArtefactError as exc:
        artefacts_db.save_error(artefact_id, str(exc))
        cached, _ = artefacts_db.get_cached_html(artefact_id)
        if cached:
            return cached  # serve last-good on a transient failure
        return render_error_canvas(title, str(exc))
    except Exception as exc:  # noqa: BLE001 — never 500 into the iframe
        log.warning("sync_and_render(%s) unexpected error: %s", artefact_id, exc)
        artefacts_db.save_error(artefact_id, f"Unexpected error: {exc}")
        cached, _ = artefacts_db.get_cached_html(artefact_id)
        return cached or render_error_canvas(title, f"Unexpected error: {exc}")

    html = render_html(source_type, recipe, data)
    artefacts_db.save_render(artefact_id, html)
    return html


# ─────────────────────────────────────────────────────────────────────────────
#  The self-contained page shell — inline CSS, theme-aware, no external requests.
#  __A_TITLE__ / __A_SUBTITLE__ / __A_BODY__ are filled in ONE regex pass so a
#  source value that happens to equal a token can never corrupt the shell.
# ─────────────────────────────────────────────────────────────────────────────
def _page(title: str, subtitle: str, body: str) -> str:
    mapping = {
        "__A_TITLE__": _esc(title),
        "__A_SUBTITLE__": _esc(subtitle),
        "__A_BODY__": body,  # already-built HTML; every value inside is escaped
    }
    return re.sub("|".join(map(re.escape, mapping)), lambda m: mapping[m.group(0)], _SHELL)


_SHELL = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>__A_TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{
    --page:#f6f7f9;--surface:#ffffff;--surface-2:#f1f3f6;--ink:#161a20;--ink-2:#3d4550;
    --muted:#7b8494;--line:#e5e8ee;--line-strong:#d6dae3;--accent:#2f6bff;--warn:#b6432b;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--maxw:1080px;color-scheme:light;}
  @media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){
    --page:#0d0f13;--surface:#161a20;--surface-2:#1d222b;--ink:#eef1f6;--ink-2:#c2c9d4;
    --muted:#8a93a3;--line:#262c36;--line-strong:#333b47;--accent:#6d97ff;--warn:#e0745a;color-scheme:dark;}}
  :root[data-theme="dark"]{
    --page:#0d0f13;--surface:#161a20;--surface-2:#1d222b;--ink:#eef1f6;--ink-2:#c2c9d4;
    --muted:#8a93a3;--line:#262c36;--line-strong:#333b47;--accent:#6d97ff;--warn:#e0745a;color-scheme:dark;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--page);color:var(--ink);font-family:var(--sans);line-height:1.55;-webkit-font-smoothing:antialiased}
  .wrap{max-width:var(--maxw);margin:0 auto;padding:26px 22px 64px}
  header.mast{padding:6px 0 20px;border-bottom:1px solid var(--line);margin-bottom:24px}
  h1{font-size:clamp(20px,3.2vw,28px);font-weight:650;letter-spacing:-.01em;margin:0}
  .subtitle{margin-top:6px;color:var(--muted);font-size:13.5px}
  .muted{color:var(--muted)}
  .scroll{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--surface)}
  table{width:100%;border-collapse:collapse;font-size:13.5px;min-width:min(100%,520px)}
  th,td{text-align:left;padding:9px 13px;border-bottom:1px solid var(--line);vertical-align:top;white-space:pre-wrap;word-break:break-word}
  thead th{position:sticky;top:0;background:var(--surface-2);font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:700;z-index:1}
  tbody tr:last-child td{border-bottom:0}
  tbody tr:nth-child(even){background:color-mix(in srgb,var(--surface-2) 55%,transparent)}
  .banner{background:color-mix(in srgb,var(--accent) 12%,transparent);border:1px solid color-mix(in srgb,var(--accent) 30%,transparent);color:var(--ink-2);border-radius:10px;padding:9px 13px;font-size:13px;margin:0 0 14px}
  pre.doc{white-space:pre-wrap;word-break:break-word;font-family:var(--mono);font-size:13px;line-height:1.6;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:20px;margin:0;color:var(--ink)}
  .kvlist{display:grid;gap:1px;background:var(--line);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .kv{display:grid;grid-template-columns:minmax(120px,240px) 1fr;gap:0;background:var(--surface)}
  .kv .k{padding:9px 13px;font-weight:600;color:var(--ink-2);border-right:1px solid var(--line);background:var(--surface-2)}
  .kv .v{padding:9px 13px;white-space:pre-wrap;word-break:break-word}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:20px}
  .card.err{border-color:color-mix(in srgb,var(--warn) 40%,var(--line))}
  .cardtitle{font-weight:650;margin-bottom:8px;font-size:15px}
  .embed{display:block;width:100%;max-width:100%;height:auto;border:1px solid var(--line);border-radius:12px;background:var(--surface)}
  embed.embed{height:min(82vh,900px)}
  a{color:var(--accent)}
  .dash-desc{color:var(--ink-2);font-size:14.5px;max-width:75ch;margin:0 0 18px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
  .kpi{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 18px;position:relative;overflow:hidden}
  .kpi::before{content:"";position:absolute;left:0;top:12px;bottom:12px;width:3px;border-radius:3px;background:var(--accent)}
  .kpi-v{font-size:26px;font-weight:650;letter-spacing:-.02em;line-height:1.05;font-variant-numeric:tabular-nums}
  .kpi-k{margin-top:7px;font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
  .charts{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
  .chart-card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:18px;min-width:0}
  .chart-card.chart-wide{grid-column:1/-1}
  .chart-title{font-weight:650;font-size:15px;margin-bottom:12px}
  .chart-card svg{width:100%;height:auto;display:block;overflow:visible}
  .c-lbl{fill:var(--ink-2);font-size:12.5px;font-family:var(--sans)}
  .c-val{fill:var(--ink);font-size:12px;font-family:var(--sans);font-variant-numeric:tabular-nums}
  .c-axis{fill:var(--muted);font-size:11px;font-family:var(--sans);font-variant-numeric:tabular-nums}
  .c-grid{stroke:var(--line);stroke-width:1}
  @media(max-width:680px){.charts{grid-template-columns:1fr}}
  @media (max-width:560px){.kv{grid-template-columns:1fr}.kv .k{border-right:0;border-bottom:1px solid var(--line)}}
</style></head><body>
<div class="wrap">
  <header class="mast">
    <h1>__A_TITLE__</h1>
    <div class="subtitle">__A_SUBTITLE__</div>
  </header>
  <main>__A_BODY__</main>
</div>
</body></html>"""
