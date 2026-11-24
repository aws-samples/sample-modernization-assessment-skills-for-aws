#!/usr/bin/env python3
"""Shared visual building blocks for the assessment HTML reports (standard library only).

Charts are inline SVG rendered from the canonical JSON artifacts; nothing is inferred and no
JavaScript or external asset is used. Status colours are semantic indicators for PASS/WARN/FAIL/
UNKNOWN judgements and are separate from the single editorial accent (AWS smile orange).
English drafts are localized through the aws-mcp-setup output-language workflow.

This module is kept byte-identical in aws-architecture-review and aws-modernization-pathway so
each skill stays portable; tests/test_visual_reports.py guards the two copies against drift.
"""
import html
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re

SANS = "'Amazon Ember','Helvetica Neue',Helvetica,Arial,sans-serif"
MONO = "'Amazon Ember Mono',ui-monospace,SFMono-Regular,Menlo,monospace"
INK, MUTED, SOFT, ACCENT, LINK, PAPER2 = "#232F3E", "#545B64", "#7D8998", "#EC7211", "#0972D3", "#F2F3F3"
RULE = "rgba(35,47,62,0.12)"
STATUS = {  # judgement -> (label, colour, tint)
    "PASS": ("Pass", "#7AA116", "rgba(122,161,22,0.14)"),
    "WARN": ("Warn", "#EC7211", "rgba(236,114,17,0.14)"),
    "FAIL": ("Fail", "#DD344C", "rgba(221,52,76,0.14)"),
    "UNKNOWN": ("Unknown", "#7D8998", "rgba(125,137,152,0.18)"),
    "NOT_APPLICABLE": ("Not applicable", "#8D99A8", "rgba(141,153,168,0.22)"),
}
STATUS_ORDER = ("FAIL", "WARN", "UNKNOWN", "PASS", "NOT_APPLICABLE")
SEVERITY = {"HIGH": ("High", "#DD344C"), "MEDIUM": ("Medium", "#EC7211"), "LOW": ("Low", "#7AA116"), "INFO": ("Info", "#7D8998")}
PATHWAY_STATUS = {"candidate": ("Candidate", "#7AA116"), "conditional": ("Conditional", "#EC7211"),
                  "not_recommended": ("Not recommended", "#687078"), "not_assessed": ("Not assessed", "#8D99A8")}


def esc(value):
    return html.escape(str(value), quote=True)


def num(value):
    return f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)


def css():
    return f"""
*{{box-sizing:border-box}} body{{margin:0;background:#fff;color:{INK};font-family:{SANS};font-size:15px;line-height:1.65}}
a{{color:{LINK};text-decoration:none}} a:hover{{text-decoration:underline}}
.hero{{background:{INK};color:#fff;padding:40px 0 32px;border-bottom:4px solid #FF9900}}
.hero .wrap{{display:flex;flex-direction:column;gap:18px}}
.hero h1{{font-size:30px;font-weight:700;margin:6px 0 10px;letter-spacing:-0.01em}} .hero p{{margin:0;color:#B6BEC9;max-width:760px}}
.eyebrow{{font-family:{MONO};font-size:11px;letter-spacing:0.16em;text-transform:uppercase;color:#FF9900;margin:0}}
.meta{{display:flex;flex-wrap:wrap;gap:8px}}
.meta span{{font-family:{MONO};font-size:11px;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.18);border-radius:4px;padding:4px 10px;color:#fff;white-space:nowrap}}
.wrap{{max-width:1240px;margin:0 auto;padding:0 32px}}
nav.toc{{position:sticky;top:0;z-index:5;background:rgba(255,255,255,0.96);border-bottom:1px solid {RULE}}}
[id]{{scroll-margin-top:56px}} /* fragment targets land below the sticky menu; browsers open folded <details> ancestors themselves */
nav.toc .wrap{{display:flex;gap:4px;overflow-x:auto;padding:8px 32px}}
nav.toc a{{font-family:{MONO};font-size:11px;color:{MUTED};padding:6px 10px;border-radius:4px;white-space:nowrap}} nav.toc a:hover{{background:{PAPER2};text-decoration:none;color:{INK}}}
section{{padding:40px 0 8px}} section h2{{font-size:22px;font-weight:700;margin:0 0 6px}} section h3{{font-size:16px;margin:28px 0 10px}} section h4{{font-size:14px;margin:18px 0 6px}}
.lead{{color:{MUTED};margin:0 0 20px;max-width:900px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}}
.kpi{{border:1px solid {RULE};border-radius:6px;padding:14px 16px;background:#fff;border-top:3px solid {INK}}}
.kpi .n{{font-size:30px;font-weight:700;line-height:1.1}} .kpi .l{{font-family:{MONO};font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:{MUTED};margin-top:4px}}
details.kpi-list{{padding:0}} details.kpi-list summary{{padding:14px 16px;list-style:none;cursor:pointer;display:block}} details.kpi-list summary::-webkit-details-marker{{display:none}} details.kpi-list summary .hint{{display:block;font-family:{MONO};font-size:10px;color:{LINK};margin-top:6px}}
details.kpi-list[open]{{grid-column:1 / -1}} details.kpi-list ul{{margin:0;padding:0 16px 12px 32px;max-height:320px;overflow:auto;columns:2;column-gap:24px}} details.kpi-list li{{line-height:1.6;break-inside:avoid}} details.kpi-list li a{{font-family:{MONO};font-size:11.5px}}
.ev-link{{font-family:{MONO};border-bottom:1px dotted {LINK}}}
.evidence-table{{table-layout:fixed}} .evidence-table th:nth-child(1),.evidence-table td:nth-child(1){{width:72px}} .evidence-table th:nth-child(2),.evidence-table td:nth-child(2){{width:200px}} .evidence-table th:nth-child(4),.evidence-table td:nth-child(4){{width:110px}} .evidence-table th:nth-child(5),.evidence-table td:nth-child(5){{width:104px}} .evidence-table th:nth-child(6),.evidence-table td:nth-child(6){{width:64px}} .evidence-table th:nth-child(8),.evidence-table td:nth-child(8){{width:130px}}
.evidence-table td:nth-child(1) code{{white-space:nowrap}} .evidence-table td:nth-child(8) code{{font-size:11px}}
.evidence-table td{{font-size:12.5px;word-break:break-word}} .evidence-table code{{white-space:normal;word-break:break-word}}
.kpi.fail{{border-top-color:#DD344C}} .kpi.warn{{border-top-color:#EC7211}} .kpi.pass{{border-top-color:#7AA116}} .kpi.unknown{{border-top-color:#7D8998}} .kpi.accent{{border-top-color:#FF9900}} .kpi.link{{border-top-color:{LINK}}}
.grid{{display:grid;gap:16px}} .g2{{grid-template-columns:1.15fr 0.85fr}} .g3{{grid-template-columns:repeat(3,1fr)}}
@media (max-width:900px){{.g2,.g3{{grid-template-columns:1fr}}}}
.card{{border:1px solid {RULE};border-radius:6px;padding:18px 20px;background:#fff}} .card h3{{margin:0 0 6px;font-size:15px}} .card h4{{margin:0 0 8px;font-size:15px}} .card.consider p{{margin:6px 0;font-size:13px}} .card.consider p b{{color:{MUTED};font-weight:600;margin-right:4px}}
.card .sub{{font-family:{MONO};font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:{MUTED};margin:0 0 12px}} .card svg{{width:100%;height:auto;display:block}}
.chip{{display:inline-block;font-family:{MONO};font-size:10px;letter-spacing:0.04em;padding:2px 8px;border-radius:3px;border:1px solid transparent;white-space:nowrap;vertical-align:middle}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}} th,td{{text-align:left;vertical-align:top;padding:9px 10px;border-bottom:1px solid {RULE}}}
th{{font-family:{MONO};font-size:10px;letter-spacing:0.1em;text-transform:uppercase;color:{MUTED};background:{PAPER2};font-weight:600}}
code{{font-family:{MONO};font-size:12px;background:{PAPER2};padding:1px 5px;border-radius:3px;word-break:break-all}}
.finding{{border:1px solid {RULE};border-left-width:4px;border-radius:6px;padding:12px 16px;margin:10px 0;background:#fff}}
.finding header{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:6px}} .finding header strong{{font-size:14px}}
.finding p{{margin:4px 0;font-size:13.5px}} .finding .ev{{font-family:{MONO};font-size:11px;color:{MUTED};margin-top:6px;word-break:break-all}}
details{{border:1px solid {RULE};border-radius:6px;margin:10px 0;background:#fff}} details summary{{cursor:pointer;padding:12px 16px;font-weight:600;list-style:none;display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
details summary::-webkit-details-marker{{display:none}} details summary::before{{content:'\\25B8';color:{MUTED};font-size:12px}} details[open] summary::before{{content:'\\25BE'}} details .body{{padding:0 16px 12px}}
.legend{{display:flex;flex-wrap:wrap;gap:14px;font-family:{MONO};font-size:11px;color:{MUTED};margin-top:10px}} .legend i{{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:-2px;margin-right:6px}}
.pathcard{{border:1px solid {RULE};border-top:4px solid {MUTED};border-radius:6px;padding:16px 18px;background:#fff;display:flex;flex-direction:column;gap:8px}}
.pathcard h3{{margin:0;font-size:16px;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}} .pathcard p{{margin:0;font-size:13px;color:{MUTED}}} .pathcard ul{{margin:4px 0 0;padding-left:18px;font-size:12.5px}}
.callout{{border-left:4px solid {ACCENT};background:rgba(255,153,0,0.06);padding:12px 16px;border-radius:0 6px 6px 0;margin:16px 0}} .callout p{{margin:0}}
figure{{margin:16px 0}} figure svg,figure img{{width:100%;height:auto;display:block}} figcaption{{font-family:{MONO};font-size:11px;color:{MUTED};margin-top:6px}}
.opt{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}} .opt>div{{border:1px solid {RULE};border-top-width:3px;border-radius:6px;padding:10px 12px;font-size:12.5px}} .opt>div>b{{display:block;font-size:13px;margin-bottom:4px}}
.option-table{{table-layout:fixed}} .small{{font-size:12.5px;color:{MUTED}}} ul.plain{{margin:0;padding:0;list-style:none}} ul.plain li{{line-height:1.5}}
.grades{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:16px 0}}
.grade-tile{{display:block;border:1px solid {RULE};border-top:4px solid {MUTED};border-radius:6px;padding:12px 14px;background:#fff;color:{INK};text-decoration:none}} a.grade-tile:hover{{background:{PAPER2};text-decoration:none}}
.g-head{{display:flex;justify-content:space-between;align-items:center;gap:8px}} .g-name{{font-weight:700;font-size:14px}} .g-counts{{font-family:{MONO};font-size:11px;color:{MUTED};margin:6px 0}}
.g-drivers{{margin:0;padding-left:16px;font-size:11.5px;color:{MUTED}}} .g-drivers li{{line-height:1.5}}
.g-why{{font-size:11.5px;color:{INK};margin:0 0 6px}}
figure.zoomable a.zoom-open{{display:block;cursor:zoom-in}} figure.zoomable .zoom-close{{display:none}}
figure.zoomable:target{{position:fixed;inset:0;z-index:1000;margin:0;background:#fff;overflow:auto;padding:56px 24px 24px}} figure.zoomable:target a.zoom-open{{cursor:default}}
figure.zoomable:target svg{{width:max(1800px,200%);max-width:none}} figure.zoomable:target .zoom-close{{display:inline-block;position:fixed;top:12px;right:24px;z-index:1001;background:{INK};color:#fff;padding:6px 14px;border-radius:16px;font-size:13px;text-decoration:none}}
.specialist{{border:1px solid {RULE};border-top:4px solid {ACCENT};border-radius:6px;padding:16px 20px;margin:18px 0;background:#fff}} .specialist h3{{margin:0 0 8px;font-size:17px}} .specialist h3 .chips{{display:inline-flex;gap:6px;flex-wrap:wrap;vertical-align:middle;margin-left:6px}} .specialist h4{{margin:12px 0 6px;font-size:14px}} .specialist .rec{{border-left:3px solid {RULE};padding:4px 14px;margin:12px 0}} .specialist .rec h4 .chips{{display:inline-flex;gap:6px;flex-wrap:wrap;margin-left:6px;vertical-align:middle}} .specialist .rec p{{margin:4px 0}} .specialist .ref{{font-family:{MONO};font-size:11px;color:{SOFT};margin-top:4px}}
table.glance th{{width:180px;white-space:nowrap}} table.glance ol.glance-list{{margin:0;padding-left:18px}} table.glance ol.glance-list li{{margin:3px 0}}
table.fields{{margin:6px 0 4px}} table.fields th{{width:150px;white-space:nowrap;vertical-align:top;background:{PAPER2}}} table.fields td p{{margin:2px 0}} table.fields table.inner{{margin:0}} table.fields table.inner th{{width:auto;white-space:normal;background:transparent}}
nav.toc a.other{{margin-left:auto;color:{ACCENT};font-weight:600}}
.issue{{border:1px solid {RULE};border-left:5px solid {MUTED};border-radius:6px;padding:14px 18px;margin:12px 0;background:#fff}} .issue h3{{margin:0 0 6px;font-size:16px}} .issue p{{margin:4px 0}} .issue h3 .chips{{display:inline-flex;gap:6px;flex-wrap:wrap;vertical-align:middle;margin-left:6px}} .issue h3 .chip{{font-size:12px;padding:4px 10px}} .quickwins{{margin-top:18px}} .quickwins ul.plain li{{margin:6px 0;font-size:14px}} .quickwins .chip{{margin-left:4px}} .issue .why{{color:{MUTED}}} .issue .act{{font-weight:600}} .issue .ref{{font-family:{MONO};font-size:11px;color:{SOFT};margin-top:6px}}
footer{{border-top:1px solid {RULE};margin-top:40px;padding:20px 0 40px;font-family:{MONO};font-size:11px;color:{MUTED}}}
@media print{{nav.toc{{display:none}} .hero{{-webkit-print-color-adjust:exact;print-color-adjust:exact}} details{{page-break-inside:avoid}}}}
"""


def chip(label, color, tint=None):
    return f'<span class="chip" style="color:{color};background:{tint or color + "1f"};border-color:{color}">{esc(label)}</span>'


def status_chip(status):
    label, color, tint = STATUS[status]
    return chip(f"{label} · {status}", color, tint)


def pathway_chip(status):
    label, color = PATHWAY_STATUS[status]
    return chip(label, color)


def kpi(value, label, kind="", items=None, item_label=None):
    """A count tile. With `items` (list of (href, text)) the tile is a <details> that opens the list behind the number."""
    head = f'<div class="n">{esc(num(value))}</div><div class="l">{esc(label)}</div>'
    if not items:
        return f'<div class="kpi {kind}">{head}</div>'
    rows = "".join(f'<li><a href="{esc(h)}">{esc(t)}</a></li>' if h else f'<li>{esc(t)}</li>' for h, t in items)
    return (f'<details class="kpi {kind} kpi-list"><summary>{head}<span class="hint">{esc(item_label or "show list")}</span></summary>'
            f'<ul class="small">{rows}</ul></details>')


def evidence_links(ids):
    """Evidence IDs rendered as links into the evidence appendix."""
    ids = [i for i in (ids or []) if i]
    if not ids:
        return "additional evidence required"
    return ", ".join(f'<a href="#ev-{esc(i)}" class="ev-link" title="what this evidence is">{esc(i)}</a>' for i in ids)


def legend(items):
    return '<div class="legend">' + "".join(f'<span><i style="background:{c}"></i>{esc(l)}</span>' for l, c in items) + "</div>"


def svg_open(width, height, slug, title, desc):
    return (f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" '
            f'aria-labelledby="{slug}-title {slug}-desc" data-chart="{slug}">'
            f'<title id="{slug}-title">{esc(title)}</title><desc id="{slug}-desc">{esc(desc)}</desc>')


def text(x, y, value, size=11, fill=INK, weight=None, mono=False, anchor="start"):
    weight_attr = f' font-weight="{weight}"' if weight else ""
    return (f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}"{weight_attr} '
            f'font-family="{MONO if mono else SANS}" text-anchor="{anchor}">{esc(value)}</text>')


def score_bars(scores, slug="pillar-scores", width=640):
    """Horizontal 0-100 bars per pillar; the lowest judged score carries the accent, unassessed pillars stay dashed."""
    rows = list(scores.items())
    row_h, top, label_x = 40, 20, 150
    plot = width - label_x - 100
    height = top + row_h * len(rows) + 30
    judged = [(k, v["score"]) for k, v in rows if v.get("score") is not None]
    focal = min(judged, key=lambda kv: kv[1])[0] if judged else None
    out = [svg_open(width, height, slug, "Local pillar scores",
                    "Horizontal bars compare the local 0-100 score of each Well-Architected pillar; evidence coverage is listed beside each bar.")]
    for grid in range(0, 101, 25):
        x = label_x + plot * grid / 100
        out.append(f'<line x1="{x:.1f}" y1="{top - 4}" x2="{x:.1f}" y2="{height - 24}" stroke="rgba(35,47,62,0.08)" stroke-width="0.8"/>')
        out.append(text(f"{x:.1f}", height - 8, str(grid), 10, MUTED, mono=True, anchor="middle"))
    for index, (name, value) in enumerate(rows):
        y = top + index * row_h
        out.append(text(label_x - 10, y + 23, name, 12, INK, 600, anchor="end"))
        out.append(f'<rect x="{label_x}" y="{y + 9}" width="{plot}" height="22" rx="3" fill="{PAPER2}"/>')
        if value.get("score") is None:
            out.append(f'<rect x="{label_x}" y="{y + 9}" width="{plot}" height="22" rx="3" fill="none" stroke="{SOFT}" stroke-dasharray="4,3"/>')
            out.append(text(label_x + 10, y + 24, "Unassessed - no judged evidence", 11, SOFT, mono=True))
        else:
            bar = max(3.0, plot * value["score"] / 100)
            fill, stroke, label_fill = (("rgba(255,153,0,0.18)", ACCENT, ACCENT) if name == focal else ("rgba(84,91,100,0.18)", MUTED, INK))
            out.append(f'<rect x="{label_x}" y="{y + 9}" width="{bar:.1f}" height="22" rx="3" fill="{fill}" stroke="{stroke}" stroke-width="1"/>')
            inside = bar > 60
            out.append(text(f"{label_x + bar - 8 if inside else label_x + bar + 8:.1f}", y + 24, num(value["score"]), 12, label_fill, 700, mono=True,
                            anchor="end" if inside else "start"))
        coverage = value.get("evidence_coverage_percent")
        out.append(text(width - 4, y + 24, f"evidence {num(coverage)}%" if coverage is not None else "evidence -", 10, MUTED, mono=True, anchor="end"))
    out.append("</svg>")
    return "".join(out)


def stacked_status(scores, slug="status-stack", width=640):
    rows = list(scores.items())
    row_h, top, label_x = 38, 10, 150
    plot = width - label_x - 50
    height = top + row_h * len(rows) + 8
    out = [svg_open(width, height, slug, "Judgement distribution per pillar",
                    "Stacked bars show the share of fail, warn, unknown, pass and not-applicable checks for each pillar.")]
    for index, (name, value) in enumerate(rows):
        y = top + index * row_h
        total = sum(value["counts"].values()) or 1
        x = float(label_x)
        out.append(text(label_x - 10, y + 22, name, 12, INK, 600, anchor="end"))
        for status in STATUS_ORDER:
            count = value["counts"].get(status, 0)
            if not count:
                continue
            w = plot * count / total
            opacity = 0.9 if status != "NOT_APPLICABLE" else 0.6
            out.append(f'<rect x="{x:.1f}" y="{y + 6}" width="{w:.1f}" height="26" fill="{STATUS[status][1]}" opacity="{opacity}"/>')
            if w > 22:
                out.append(text(f"{x + w / 2:.1f}", y + 23, str(count), 12, "#fff", 700, mono=True, anchor="middle"))
            x += w
        out.append(text(width - 2, y + 22, f"{total}", 10, MUTED, mono=True, anchor="end"))
    out.append("</svg>")
    return "".join(out)


def donut(counts, slug="judgement-donut", title="Judgement distribution", center_label="checks"):
    total = sum(counts.get(s, 0) for s in STATUS_ORDER)
    cx = cy = 110
    radius, stroke = 86, 26
    out = [svg_open(220, 220, slug, title, f"Donut chart of the judgement distribution across {total} checks.")]
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="{PAPER2}" stroke-width="{stroke}"/>')
    angle = -90.0
    for status in STATUS_ORDER:
        count = counts.get(status, 0)
        if not count or not total:
            continue
        end = angle + 360 * count / total
        large = 1 if end - angle > 180 else 0
        x0, y0 = cx + radius * math.cos(math.radians(angle)), cy + radius * math.sin(math.radians(angle))
        x1, y1 = cx + radius * math.cos(math.radians(end - 0.01)), cy + radius * math.sin(math.radians(end - 0.01))
        opacity = 0.9 if status != "NOT_APPLICABLE" else 0.6
        out.append(f'<path d="M{x0:.2f},{y0:.2f} A{radius},{radius} 0 {large},1 {x1:.2f},{y1:.2f}" fill="none" stroke="{STATUS[status][1]}" stroke-width="{stroke}" opacity="{opacity}"/>')
        angle = end
    out.append(text(cx, cy + 2, str(total), 34, INK, 700, anchor="middle"))
    out.append(text(cx, cy + 22, center_label, 10, MUTED, mono=True, anchor="middle"))
    out.append("</svg>")
    return "".join(out)


def severity_bars(checks, slug="severity", width=640):
    """FAIL/WARN counts grouped by severity (local priority, not HRI/MRI)."""
    severities = ("HIGH", "MEDIUM", "LOW")
    data = {s: {"FAIL": 0, "WARN": 0} for s in severities}
    for check in checks:
        if check["status"] in data["HIGH"] and check.get("severity") in data:
            data[check["severity"]][check["status"]] += 1
    maximum = max([v for d in data.values() for v in d.values()] + [1])
    label_x, row_h, top = 90, 64, 12
    plot = width - label_x - 110
    height = top + row_h * len(severities) + 6
    out = [svg_open(width, height, slug, "Fail and warn counts by severity",
                    "Grouped bars count fail and warn judgements for high, medium and low local severity.")]
    for index, severity in enumerate(severities):
        y = top + index * row_h
        out.append(text(label_x - 10, y + 32, SEVERITY[severity][0], 12, INK, 600, anchor="end"))
        for offset, status in enumerate(("FAIL", "WARN")):
            count = data[severity][status]
            w = plot * count / maximum if count else 0
            yy = y + 4 + offset * 26
            out.append(f'<rect x="{label_x}" y="{yy}" width="{plot}" height="22" rx="3" fill="{PAPER2}"/>')
            if count:
                out.append(f'<rect x="{label_x}" y="{yy}" width="{w:.1f}" height="22" rx="3" fill="{STATUS[status][1]}" opacity="0.9"/>')
            out.append(text(f"{label_x + w + 8:.1f}", yy + 16, f"{STATUS[status][0]} {count}", 11, INK if count else SOFT, 600, mono=True))
    out.append("</svg>")
    return "".join(out)


def count_bars(pairs, slug, title, desc, focal=None, width=640, suffix=""):
    """Simple horizontal count bars for up to eight categories."""
    maximum = max([v for _, v in pairs] + [1])
    label_x, row_h, top = 190, 34, 8
    plot = width - label_x - 70
    height = top + row_h * max(len(pairs), 1) + 6
    out = [svg_open(width, height, slug, title, desc)]
    for index, (name, value) in enumerate(pairs):
        y = top + index * row_h
        w = plot * value / maximum
        fill, stroke = (("rgba(255,153,0,0.18)", ACCENT) if name == focal else ("rgba(84,91,100,0.18)", MUTED))
        out.append(text(label_x - 10, y + 21, name, 12, INK, 600, anchor="end"))
        out.append(f'<rect x="{label_x}" y="{y + 6}" width="{max(w, 3):.1f}" height="22" rx="3" fill="{fill}" stroke="{stroke}"/>')
        out.append(text(f"{label_x + w + 8:.1f}", y + 21, f"{num(value)}{suffix}", 12, INK, 700, mono=True))
    out.append("</svg>")
    return "".join(out)


def heat_cell(score):
    if score is None:
        return (f'<td style="text-align:center;background:repeating-linear-gradient(135deg,{PAPER2} 0 6px,#fff 6px 12px);'
                f'color:{SOFT};font-family:{MONO}">-</td>')
    alpha = 0.1 + 0.17 * (score - 1)
    return f'<td style="text-align:center;background:rgba(255,153,0,{alpha:.2f});font-family:{MONO};font-weight:700">{num(score)}</td>'


def gantt(rows, slug="roadmap", width=1100, focal=None):
    """Ordinal wave bars: rows are (label, start, end, note). No calendar dates are implied."""
    if not rows:
        return ""
    columns = max(end for _, _, end, _ in rows) + 1
    label_x, row_h, top = 230, 48, 44
    plot = width - label_x - 40
    pitch = plot / columns
    height = top + row_h * len(rows) + 16
    out = [svg_open(width, height, slug, "Roadmap wave order",
                    "Gantt-style bars place each wave in order of execution. Positions are ordinal and do not represent calendar dates.")]
    for column in range(columns):
        x = label_x + column * pitch
        out.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" y2="{height - 10}" stroke="rgba(35,47,62,0.08)" stroke-width="0.8"/>')
        out.append(text(f"{x + pitch / 2:.1f}", top - 16, f"Order {column + 1}", 11, MUTED, mono=True, anchor="middle"))
    out.append(f'<line x1="{label_x}" y1="{top - 8}" x2="{width - 40}" y2="{top - 8}" stroke="rgba(35,47,62,0.25)"/>')
    for index, (label, start, end, note) in enumerate(rows):
        y = top + index * row_h
        out.append(text(label_x - 12, y + 26, label, 13, INK, 600, anchor="end"))
        fill, stroke, label_fill = (("rgba(255,153,0,0.14)", ACCENT, ACCENT) if label == focal else ("rgba(84,91,100,0.15)", MUTED, INK))
        x0 = label_x + start * pitch + 4
        w = (end - start + 1) * pitch - 8
        out.append(f'<rect x="{x0:.1f}" y="{y + 8}" width="{w:.1f}" height="30" rx="4" fill="{fill}" stroke="{stroke}"><title>{esc(note)}</title></rect>')
        limit = max(3, int((w - 24) / 6.8))  # approximate glyph budget; the full note stays in the rect title
        shown = note if len(note) <= limit else note[: limit - 1].rstrip() + "\u2026"
        out.append(text(f"{x0 + 12:.1f}", y + 28, shown, 12, label_fill, 600))
    out.append("</svg>")
    return "".join(out)


def toc(items):
    return '<nav class="toc"><div class="wrap">' + "".join(f'<a href="#{esc(anchor)}">{esc(label)}</a>' for anchor, label in items) + "</div></nav>"


def hero(eyebrow, title, lead, meta):
    chips = "".join(f"<span>{esc(m)}</span>" for m in meta)
    return (f'<div class="hero"><div class="wrap"><div><p class="eyebrow">{esc(eyebrow)}</p><h1>{esc(title)}</h1>'
            f'<p>{esc(lead)}</p></div><div class="meta">{chips}</div></div></div>')


def page(title, body, lang="en"):
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>'
            f"<style>{css()}</style></head><body>{body}</body></html>\n")


# ---------------------------------------------------------------------------
# Run metadata (LLM model, inference time, tokens) — declared by the agent, never measured here
# ---------------------------------------------------------------------------
RUN_METADATA_SCHEMA = "1.0"
RUN_METADATA_FIELDS = ("inference_seconds", "input_tokens", "output_tokens", "requests")


def _non_negative(value, name, integer=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"run metadata {name} must be a non-negative number or null")
    if integer and int(value) != value:
        raise ValueError(f"run metadata {name} must be an integer or null")
    return int(value) if integer else float(value)


def load_run_metadata(path):
    """Validate an agent-recorded run-metadata.json and compute totals.

    The helpers cannot observe the model, inference time or token usage of the agent that drives them, so
    these values are declared inputs. Missing values stay null and are reported as "not recorded"; the
    loader never fills gaps. Returns None when path is None.
    """
    if path is None:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != RUN_METADATA_SCHEMA:
        raise ValueError("unsupported run metadata schema")
    model = data.get("model_id")
    if model is not None and (not isinstance(model, str) or not 1 <= len(model.strip()) <= 128):
        raise ValueError("run metadata model_id must be a short string or null")
    client = data.get("agent_client")
    if client is not None and (not isinstance(client, str) or not 1 <= len(client) <= 64):
        raise ValueError("run metadata agent_client must be a short string or null")
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("run metadata entries must be a list")
    unique, seen = [], set()
    for entry in entries:  # the same client session imported twice under two labels is one record, not two
        key = (entry.get("source"), entry.get("inference_seconds"), entry.get("requests")) if isinstance(entry, dict) else id(entry)
        if key in seen:
            continue
        seen.add(key); unique.append(entry)
    entries = unique
    totals = {f: 0 for f in RUN_METADATA_FIELDS}
    missing = {f: 0 for f in RUN_METADATA_FIELDS}
    models = set()
    clean = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("label"), str) or not entry["label"].strip():
            raise ValueError("each run metadata entry needs a label")
        breakdown = entry.get("breakdown")
        if breakdown is not None and not isinstance(breakdown, dict):
            raise ValueError("run metadata breakdown must be an object")
        item = {"label": entry["label"], "source": entry.get("source") or "declared", "recorded_at": entry.get("recorded_at"),
                "model_id": entry.get("model_id") or model,
                "breakdown": {str(k): v for k, v in (breakdown or {}).items() if isinstance(v, (int, float, str, list)) and not isinstance(v, bool)}}
        for extra in item["breakdown"].get("models", []) if isinstance(item["breakdown"].get("models"), list) else []:
            if isinstance(extra, str):
                models.add(extra)
        if item["model_id"]:
            models.add(item["model_id"])
        for field in RUN_METADATA_FIELDS:
            value = _non_negative(entry.get(field), field, integer=field != "inference_seconds")
            item[field] = value
            if value is None:
                missing[field] += 1
            else:
                totals[field] += value
        clean.append(item)
    if not entries:
        for field in RUN_METADATA_FIELDS:
            totals[field] = None
    else:
        for field in RUN_METADATA_FIELDS:
            if missing[field] == len(entries):
                totals[field] = None
    total_tokens = (None if totals["input_tokens"] is None and totals["output_tokens"] is None
                    else (totals["input_tokens"] or 0) + (totals["output_tokens"] or 0))
    return {"schema_version": RUN_METADATA_SCHEMA, "agent_client": client, "model_id": model,
            "models_seen": sorted(models), "entries": clean,
            "totals": dict(totals, total_tokens=total_tokens),
            "partial_fields": sorted(f for f in RUN_METADATA_FIELDS if 0 < missing[f] < len(entries)),
            "notes": [n for n in data.get("notes", []) if isinstance(n, str)],
            "source_path": str(path)}


def fmt_seconds(value):
    if value is None:
        return "not recorded"
    value = float(value)
    if value < 60:
        return f"{value:.1f} s"
    minutes, seconds = divmod(value, 60)
    if minutes < 60:
        return f"{int(minutes)} min {seconds:.0f} s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)} h {int(minutes)} min"


def fmt_int(value):
    return "not recorded" if value is None else f"{int(value):,}"


def run_metadata_rows(meta):
    """Label/value pairs for Markdown and HTML; English draft, localized later."""
    if meta is None:
        return [("LLM model", "not recorded"), ("Total inference time", "not recorded"), ("Total tokens", "not recorded"),
                ("Source", "run-metadata.json not supplied; record it with run_metadata.py")]
    totals = meta["totals"]
    models = ", ".join(meta["models_seen"]) if meta["models_seen"] else (meta["model_id"] or "not recorded")
    tokens = fmt_int(totals["total_tokens"])
    if totals["total_tokens"] is not None:
        tokens += f" (input {fmt_int(totals['input_tokens'])} · output {fmt_int(totals['output_tokens'])})"
    partial = f" · partial: {', '.join(meta['partial_fields'])} missing in some entries" if meta["partial_fields"] else ""
    credits = [float(e["breakdown"]["credits"]) for e in meta["entries"] if isinstance(e["breakdown"].get("credits"), (int, float))]
    # Kiro CLI meters usage in credits and does not expose token counts; show the unit the client actually records.
    usage_row = ("Kiro credits", f"{sum(credits):.1f}") if credits and totals["total_tokens"] is None else ("Total tokens", tokens)
    rows = [("LLM model", models), ("Agent client", meta["agent_client"] or "not recorded"),
            ("Total inference time", fmt_seconds(totals["inference_seconds"]) + (f" over {fmt_int(totals['requests'])} model requests" if totals["requests"] is not None else "")),
            usage_row, ("Entries", f"{len(meta['entries'])} recorded segment(s){partial}")]
    if credits and totals["total_tokens"] is not None:
        rows.insert(4, ("Kiro credits", f"{sum(credits):.1f}"))
    # Input tokens are mostly cache reads (the context re-sent with every request) and sub-agents are separate sessions;
    # both are counted, and said so, because they are where the usage goes.
    cache_reads = sum(int(e["breakdown"].get(k) or 0) for e in meta["entries"] for k in ("cache_read_input", "cached_input") if isinstance(e["breakdown"].get(k), (int, float)))
    subagents = sum(int(e["breakdown"].get("subagents") or 0) for e in meta["entries"] if isinstance(e["breakdown"].get("subagents"), (int, float)))
    sub_requests = sum(int(e["breakdown"].get("subagent_requests") or 0) for e in meta["entries"] if isinstance(e["breakdown"].get("subagent_requests"), (int, float)))
    if totals["total_tokens"] is not None and (cache_reads or subagents):
        detail = []
        if cache_reads and totals["input_tokens"]:
            detail.append(f"cache reads {fmt_int(cache_reads)} ({100 * cache_reads // max(1, totals['input_tokens'])}% of input: the context re-sent with each request)")
        if subagents:
            detail.append(f"{subagents} sub-agent session(s) with {fmt_int(sub_requests)} requests included")
        rows.insert(len(rows) - 1, ("Token composition", " · ".join(detail)))
    tier = model_tier(models)
    if tier in {"reduced", "unsuitable"}:
        rows.append(("Model guidance", f"{models} is below the recommended tier for this assessment ({MODEL_RECOMMENDED}); "
                                        "key issues and recommendations are shallower than the method is designed for. Re-run with the recommended model before delivery."))
    return rows


MODEL_TIERS = {"frontier": ("opus",), "reduced": ("sonnet", "fable"), "unsuitable": ("haiku", "nova-lite", "nova-micro", "mini", "flash")}
MODEL_RECOMMENDED = "claude-opus-5.5"  # keep in step with skills/moda-skill/assets/models.json


def model_tier(model_id):
    text = (model_id or "").lower()
    for tier, families in MODEL_TIERS.items():
        if any(f in text for f in families):
            return tier
    return "unknown"


def fmt_breakdown(breakdown):
    parts = [f"{k} {fmt_int(v) if isinstance(v, int) else f'{v:.1f}'}" for k, v in breakdown.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return "; ".join(parts) if parts else "-"


def run_metadata_html(meta):
    rows = run_metadata_rows(meta)
    kpis = ""
    if meta is not None:
        t = meta["totals"]
        kpis = ('<div class="kpis">' + kpi(meta["models_seen"][0] if len(meta["models_seen"]) == 1 else (len(meta["models_seen"]) or "-"), "LLM model" if len(meta["models_seen"]) <= 1 else "models used", "link")
                + kpi(fmt_seconds(t["inference_seconds"]) if t["inference_seconds"] is not None else "-", "total inference time", "accent")
                + (kpi(f"{sum(float(e['breakdown'].get('credits') or 0) for e in meta['entries']):.1f}", "Kiro credits", "accent")
                   if t["total_tokens"] is None and any(e["breakdown"].get("credits") is not None for e in meta["entries"])
                   else kpi(fmt_int(t["total_tokens"]) if t["total_tokens"] is not None else "-", "total tokens"))
                + kpi(fmt_int(t["requests"]) if t["requests"] is not None else "-", "model requests") + "</div>")
    table = "".join(f"<tr><th style=\"width:200px\">{esc(a)}</th><td>{esc(b)}</td></tr>" for a, b in rows)
    entries = ""
    if meta and meta["entries"]:
        entries = ('<details><summary class="small">Recorded segments</summary><div class="body"><table><thead><tr><th>Label</th><th>Model</th><th>Inference time</th><th>Input tokens</th><th>Output tokens</th><th>Requests</th><th>Breakdown</th><th>Source</th></tr></thead><tbody>'
                   + "".join(f"<tr><td>{esc(e['label'])}</td><td>{esc(e['model_id'] or '-')}</td><td>{esc(fmt_seconds(e['inference_seconds']))}</td><td>{esc(fmt_int(e['input_tokens']))}</td><td>{esc(fmt_int(e['output_tokens']))}</td><td>{esc(fmt_int(e['requests']))}</td><td class=\"small\">{esc(fmt_breakdown(e['breakdown']))}</td><td>{esc(e['source'])}</td></tr>" for e in meta["entries"])
                   + "</tbody></table></div></details>")
    notes = "".join(f"<li>{esc(n)}</li>" for n in (meta["notes"] if meta else []))
    return (f'<section id="provenance"><h2>Run provenance</h2><p class="lead">Model, elapsed time, requests and (where the client exposes them) tokens or credits are read from the client session records or declared by the agent and recorded in run-metadata.json. The report helper never estimates missing values. These figures measure the AI assistant\'s own usage while it ran the collection and drafting steps; the architect\'s review and the evidence-collection wall time are not in them.</p>'
            f"{kpis}<table><tbody>{table}</tbody></table>{entries}" + (f"<ul class=\"small\">{notes}</ul>" if notes else "") + "</section>")


def run_metadata_markdown(meta):
    lines = ["## Run provenance", "", "Declared by the agent and recorded in run-metadata.json; the helper does not measure model usage.", ""]
    lines += [f"- {a}: {b}" for a, b in run_metadata_rows(meta)]
    if meta and meta["entries"]:
        lines += ["", "| Segment | Model | Inference time | Input tokens | Output tokens | Requests | Breakdown | Source |", "|---|---|---|---:|---:|---:|---|---|"]
        lines += [f"| {e['label']} | {e['model_id'] or '-'} | {fmt_seconds(e['inference_seconds'])} | {fmt_int(e['input_tokens'])} | {fmt_int(e['output_tokens'])} | {fmt_int(e['requests'])} | {fmt_breakdown(e['breakdown'])} | {e['source']} |" for e in meta["entries"]]
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Qualitative pillar grades: a documented, deterministic reading of the judgement counts
# ---------------------------------------------------------------------------
# The grade answers "how urgent is this pillar" for a reader who does not want a 0-100 score.
# It is derived only from the judged checks' statuses and severities, so it can always be
# explained by pointing at the findings that produced it. The rubric (first rule that matches):
#   critical  - at least one FAIL with HIGH severity
#   weak      - any FAIL, or three or more WARN
#   watch     - one or two WARN, or no judged checks but UNKNOWN items remain
#   sound     - every judged check passed and at least one check was judged
#   unassessed- nothing judged and nothing unknown (only NOT_APPLICABLE), or no checks at all
# "Reliability: critical (5 fail, 1 unknown)" is therefore a statement about evidence, not a
# compliance verdict. Scores stay available for the method appendix and pathway ranking.
GRADES = {
    "critical": ("Fix first", "#DD344C", "rgba(221,52,76,0.12)", "At least one high-severity item to fix. Worth settling before any modernization step."),
    "weak": ("Needs work", "#EC7211", "rgba(236,114,17,0.12)", "Failures or several warnings. Worth planning in the first wave."),
    "watch": ("Watch", "#D4A017", "rgba(212,160,23,0.14)", "Minor warnings, or open questions the configuration cannot answer. Confirm with evidence before relying on it."),
    "sound": ("Sound", "#7AA116", "rgba(122,161,22,0.12)", "Every judged check passed and no question is open. Keep as is and re-check on change."),
    "unassessed": ("Unassessed", "#7D8998", "rgba(125,137,152,0.14)", "No judged evidence yet."),
}
GRADE_ORDER = ("critical", "weak", "watch", "sound", "unassessed")


def grade_pillar(checks, pillar):
    """Return (grade_key, reasons) for one pillar using the rubric above; reasons are the driving checks."""
    items = [c for c in checks if c.get("pillar") == pillar]
    fails = [c for c in items if c["status"] == "FAIL"]
    warns = [c for c in items if c["status"] == "WARN"]
    unknowns = [c for c in items if c["status"] == "UNKNOWN"]
    passes = [c for c in items if c["status"] == "PASS"]
    high_fails = [c for c in fails if c.get("severity") == "HIGH"]
    if high_fails:
        key = "critical"
    elif fails or len(warns) >= 3:
        key = "weak"
    elif warns or unknowns:
        key = "watch"  # open questions mean the pillar is not fully assessed, even when every judged check passed
    elif passes:
        key = "sound"
    else:
        key = "unassessed"
    severity_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
    ordered = sorted(fails, key=lambda c: severity_rank.get(c.get("severity"), 9)) + sorted(warns, key=lambda c: severity_rank.get(c.get("severity"), 9))
    drivers, seen = [], set()
    for c in ordered:  # one entry per rule; repeated resource-level checks collapse into their rule
        if c["rule_id"] not in seen:
            seen.add(c["rule_id"]); drivers.append(c)
    return key, {"fails": len(fails), "warns": len(warns), "unknowns": len(unknowns), "passes": len(passes),
                 "high_fails": len(high_fails), "drivers": drivers[:5]}


def grade_all(checks, pillars):
    return {p: grade_pillar(checks, p) for p in pillars}


def grade_chip(key):
    label, color, tint, _ = GRADES[key]
    return chip(label, color, tint)


def grade_counts_text(info):
    parts = []
    if info["fails"]:
        parts.append(f"{info['fails']} fail" + (f" ({info['high_fails']} high)" if info["high_fails"] else ""))
    if info["warns"]:
        parts.append(f"{info['warns']} warn")
    if info["unknowns"]:
        parts.append(f"{info['unknowns']} unknown")
    if info["passes"]:
        parts.append(f"{info['passes']} pass")
    return ", ".join(parts) or "no checks"


def grade_rubric_html():
    rows = "".join(f'<tr><td>{grade_chip(k)}</td><td>{esc(GRADES[k][3])}</td><td class="small">{esc(rule)}</td></tr>'
                   for k, rule in (("critical", "one or more FAIL with HIGH severity"), ("weak", "any FAIL, or 3+ WARN"),
                                   ("watch", "1-2 WARN, or any open question (UNKNOWN)"), ("sound", "all judged checks PASS and no open question"),
                                   ("unassessed", "no judged or unknown checks")))
    return ('<table><thead><tr><th>Grade</th><th>Meaning</th><th>Rule (first match wins)</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            '<p class="small">Grades are derived only from the judged checks listed in this report; every grade can be traced to the findings that produced it. '
            'They are not AWS Well-Architected Tool ratings. The numeric local score (PASS 1 · WARN 0.5 · FAIL 0, weighted) remains in the method appendix for pathway ranking.</p>')


def grade_reason(key, info):
    """Why the tile has its grade, in one line on the tile itself, so one high-severity failure among many passes is not
    read as 'the whole pillar is failing'."""
    if key == "critical":
        return f"Set by {info['high_fails']} high-severity failure(s); {info['passes']} check(s) passed."
    if key == "weak":
        return (f"Set by {info['fails']} failure(s)" if info["fails"] else f"Set by {info['warns']} warnings") + f"; {info['passes']} check(s) passed."
    if key == "watch":
        return "Set by minor warnings or open questions; nothing high-severity failed."
    if key == "sound":
        return "Every judged check passed."
    return "No judged evidence yet."


def grade_tiles(grades, anchors=None, pillar_labels=None, rule_labels=None):
    """One tile per pillar: grade, counts and top drivers (in words, not rule codes), linking to the pillar's findings."""
    out = []
    for pillar, (key, info) in grades.items():
        label, color, tint, _ = GRADES[key]
        name = (pillar_labels or {}).get(pillar, pillar)
        words = rule_labels or (lambda c: c["rule_id"])
        drivers = "".join(f'<li>{esc(words(c))}</li>' for c in info["drivers"][:3]) or (
            f'<li>{info["unknowns"]} open question(s), nothing failed</li>' if info["unknowns"] else "<li>-</li>")
        href = f' href="#{esc((anchors or {}).get(pillar, ""))}"' if anchors and pillar in anchors else ""
        tag = "a" if href else "div"
        out.append(f'<{tag}{href} class="grade-tile" style="border-top-color:{color}"><div class="g-head"><span class="g-name">{esc(name)}</span>{grade_chip(key)}</div>'
                   f'<div class="g-counts">{esc(grade_counts_text(info))}</div><p class="g-why">{esc(grade_reason(key, info))}</p><ul class="g-drivers">{drivers}</ul></{tag}>')
    return '<div class="grades">' + "".join(out) + "</div>"


# ---------------------------------------------------------------------------
# SVG guard. Diagrams are embedded in reports only after a tag and attribute scan; html.parser performs no DTD or
# entity processing, so no XML parser is loaded at all. Callers embed the original text, never a re-serialization.
# ---------------------------------------------------------------------------
class _SvgScan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.elements = []  # (tag, {attribute: value}) in document order; names lower-cased, prefixes dropped

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag.rsplit(":", 1)[-1].lower(), {k.lower(): (v or "") for k, v in attrs}))

    handle_startendtag = handle_starttag


def scan_svg(text):
    """Return the element list of an SVG document, or raise ValueError when it declares a DTD or entities, is not an
    SVG, or carries active content (script, foreignObject, event handlers, references outside # and data:image/)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if len(text) > 25_000_000:
        raise ValueError("SVG larger than 25 MB")
    head = text[:4096]
    if re.search(r"<!(?:DOCTYPE|ENTITY)", text, re.I) or ("<?xml-stylesheet" in head and "http" in head):
        raise ValueError("SVG declares a DTD or entities; refused")
    scan = _SvgScan()
    scan.feed(text)
    scan.close()
    if not scan.elements or scan.elements[0][0] != "svg":
        raise ValueError("diagram must be an SVG")
    for tag, attrs in scan.elements:
        if tag in {"script", "foreignobject", "iframe", "embed", "object"}:
            raise ValueError("diagram contains active content")
        for key, value in attrs.items():
            if key.startswith("on"):
                raise ValueError("diagram contains event handlers")
            if key.rsplit(":", 1)[-1] == "href" and not value.startswith(("#", "data:image/")):
                raise ValueError("diagram contains external references")
    return scan.elements
