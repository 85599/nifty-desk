"""Reconstruct the ODIN "Integrated Net Position" grid from Windows-OCR word boxes.

Windows OCR returns table columns as separate "lines", so rows are rebuilt from the word
bounding boxes. Column identity comes from clustering the *data* tokens by x (the header
row is too garbled to trust for cell edges) and then naming each cluster with the header's
keyword anchors, which are reliable for the few words that do read cleanly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# ODIN's standard layout: the seven value columns, left to right, after Strike/Option. Only used
# as a template once the header anchors confirm it (see _name_groups) - some brokers rearrange or
# drop columns, and filling by x order alone then writes the wrong column's number into the sheet.
NUMERIC_COLUMNS = ["netqty", "mtm", "buyqty", "buyavg", "sellqty", "sellavg", "mktprice"]

COLUMN_LABELS = {
    "symbol": "Symbol", "expiry": "Expiry", "strike": "Strike", "otype": "Type",
    "netqty": "Net Qty", "mtm": "MTM G/L", "buyqty": "Buy Qty", "buyavg": "Buy Avg",
    "sellqty": "Sell Qty", "sellavg": "Sell Avg", "mktprice": "Market Price",
}

# header words that survive OCR legibly enough to pin a column by x position.
# Deliberately excludes Buy/Sell: those titles read as bare "Bu"/"Sell" and collide.
HEADER_KEYS = {
    "symbol": ("mbol", "symbol", "smbol"),
    "expiry": ("serexp", "sexp", "expiry", "serex"),
    "strike": ("strike", "trike", "strikeprice", "strke", "stnke", "strtke", "srke"),
    "otype": ("option", "otion", "opttype"),
    "netqty": ("net", "netqty"),
    "mtm": ("mtm",),
    "mktprice": ("market", "mkt", "mrkt"),
}

def looks_like_expiry(token: str) -> bool:
    """'19OCT20...' and the OCR's '190CT20...' both count; plain numbers do not."""
    s = re.sub(r"[^A-Za-z0-9]", "", token.upper())
    if not re.match(r"^\d{2}", s) or len(s) < 4:
        return False
    frag = s[2:5].replace("0", "O").replace("1", "I")
    return any(mo.startswith(frag[:n]) and sum(c.isalpha() for c in frag[:n]) >= 2
               for mo in MONTHS for n in (3, 2))
TYPE_RE = re.compile(r"^[CBPDEF]{1,3}$")


@dataclass
class GridRow:
    cells: dict[str, str] = field(default_factory=dict)
    y: float = 0.0

    def raw(self, key: str) -> str:
        return self.cells.get(key, "").strip()


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9.]", "", text.lower())


# Windows OCR mis-reads the ODIN header titles constantly ("Strke", "otiont", "sbol"), so the
# header gate has to match corrupted spellings. HEADER_KEYS already tolerates these for naming
# columns; the gate must stay in step with it or a readable header is treated as absent.
STRIKE_WORDS = ("strike", "strke", "stnke", "strtke", "srke", "trike", "shike")
VALUE_WORDS = ("net", "mtm", "market", "mrkt")


def _cluster_rows(words: list[dict], tol: float) -> list[tuple[float, list[dict]]]:
    rows: list[list[dict]] = []
    centers: list[float] = []
    for w in sorted(words, key=lambda a: (a["y"] + a["h"] / 2, a["x"])):
        cy = w["y"] + w["h"] / 2
        if rows and abs(centers[-1] - cy) <= tol:
            rows[-1].append(w)
            centers[-1] = (centers[-1] * (len(rows[-1]) - 1) + cy) / len(rows[-1])
        else:
            rows.append([w])
            centers.append(cy)
    out = []
    for cy, r in zip(centers, rows):
        r.sort(key=lambda a: a["x"])
        out.append((cy, r))
    return out


def _cluster_columns(items: list[tuple[float, dict]], gap: float) -> list[list[dict]]:
    groups: list[list[dict]] = []
    last = None
    for cx, w in sorted(items, key=lambda a: a[0]):
        if groups and last is not None and cx - last <= gap:
            groups[-1].append(w)
        else:
            groups.append([w])
        last = cx
    return groups


def _header_anchors(header_words: list[dict]) -> dict[str, tuple[float, float]]:
    """x span per column, from whichever header titles still read cleanly."""
    anchors: dict[str, tuple[float, float]] = {}
    for w in header_words:
        blob = _squash(w["t"])
        if len(blob) < 3:
            continue
        for name, keys in HEADER_KEYS.items():
            if name in anchors:
                continue
            if any(k.startswith(blob) or blob.startswith(k) or k in blob for k in keys):
                anchors[name] = (w["x"], w["x"] + w["w"])
                break
    return anchors


def _span(group: list[dict]) -> tuple[float, float]:
    return min(w["x"] for w in group), max(w["x"] + w["w"] for w in group)


def _center(group: list[dict]) -> float:
    return sum(w["x"] + w["w"] / 2 for w in group) / len(group)


def _gap(a: tuple[float, float], b: tuple[float, float]) -> float:
    """0 when the two x ranges overlap, otherwise the distance between them."""
    return max(0.0, a[0] - b[1], b[0] - a[1])


def _match_by_anchors(groups: list[list[dict]], names: list[str],
                      anchors: dict[str, tuple[float, float]], tol: float) -> dict[int, str]:
    """Pin data clusters to the header columns that read cleanly: nearest overlap first, and never
    two matches that cross, since one row cannot reorder the columns the header itself lists."""
    ordered = sorted((n for n in names if n in anchors), key=lambda n: anchors[n][0])
    cands = []
    for i, g in enumerate(groups):
        lo, hi = _span(g)
        cx = (lo + hi) / 2
        for j, name in enumerate(ordered):
            a = anchors[name]
            d = _gap((lo, hi), a)
            if d <= tol:
                cands.append((d, abs(cx - (a[0] + a[1]) / 2), i, j, name))
    cands.sort()
    pairs: dict[int, tuple[int, str]] = {}
    used: set[int] = set()
    for _, _, i, j, name in cands:
        if i in pairs or j in used:
            continue
        if any((i - gi) * (j - gj) < 0 for gi, (gj, _) in pairs.items()):
            continue
        pairs[i] = (j, name)
        used.add(j)
    return {i: name for i, (_, name) in pairs.items()}


def _is_money(group: list[dict]) -> bool:
    """Mostly numbers with a non-zero fraction: a price or a value, never an index quantity."""
    nums = [v for v in (clean_number(w["t"]) for w in group) if v is not None]
    if not nums:
        return False
    return sum(1 for v in nums if abs(v % 1) > 0.01) / len(nums) >= 0.5


def _name_groups(groups: list[list[dict]], anchors: dict[str, tuple[float, float]],
                 width: float) -> tuple[dict[int, str], list[str]]:
    """Assign a column name to every x-cluster of data tokens, plus notes on anything the
    naming had to refuse. Wrong numbers are worse than missing ones in a risk sheet, so an
    unconfirmed layout is reported instead of guessed at."""
    named: dict[int, str] = {}
    notes: list[str] = []
    numeric: list[int] = []
    for i, g in enumerate(groups):
        texts = [w["t"] for w in g]
        share = lambda pred: sum(1 for t in texts if pred(t)) / len(texts)  # noqa: E731
        if share(looks_like_expiry) >= 0.5:
            named[i] = "expiry"
        elif share(lambda t: bool(TYPE_RE.match(t.replace(".", "")))) >= 0.5:
            named[i] = "otype"
        else:
            numeric.append(i)
    if not numeric:
        return named, notes

    numeric.sort(key=lambda i: _center(groups[i]))
    if "strike" in anchors:
        near = min(numeric, key=lambda i: _gap(_span(groups[i]), anchors["strike"]))
        if _gap(_span(groups[near]), anchors["strike"]) < 0.10 * width:
            named[near] = "strike"
            numeric.remove(near)
    elif numeric:
        named[numeric[0]] = "strike"
        numeric.remove(numeric[0])

    pinned = _match_by_anchors([groups[i] for i in numeric], NUMERIC_COLUMNS, anchors, 0.02 * width)
    for k, name in pinned.items():
        named[numeric[k]] = name

    # the x-order template is used only when the header puts every column it names where the
    # template says it is, or when nothing read cleanly and the cluster count matches exactly
    fits_template = all(NUMERIC_COLUMNS.index(named[numeric[k]]) == k for k in pinned)
    if fits_template and (pinned or len(numeric) == len(NUMERIC_COLUMNS)):
        for pos, name in enumerate(NUMERIC_COLUMNS):
            if pos < len(numeric) and numeric[pos] not in named:
                named[numeric[pos]] = name
    elif pinned or len(numeric) != len(NUMERIC_COLUMNS):
        notes.append("This grid does not follow the standard ODIN column layout (its columns are "
                     "rearranged, or some of them are hidden), so only the columns its own header "
                     "spells out were read. Show the rest in the terminal to bring them in too.")

    qty_group = next((i for i, n in named.items() if n == "netqty"), None)
    if qty_group is not None and _is_money(groups[qty_group]):
        del named[qty_group]
        notes.append("The column its header labels Net Qty holds money values instead of "
                     "quantities, so it was ignored. Check the capture is the Integrated Net "
                     "Position window with its own columns showing.")
    return named, notes


def parse_grid(ocr: dict) -> tuple[list[GridRow], list[str]]:
    """Return (rows, warnings). Rows are every position line found, unfiltered."""
    words = [w for line in ocr.get("lines", []) for w in line.get("words", []) if w.get("t", "").strip()]
    if not words:
        return [], ["OCR found no text in this image."]
    width = ocr.get("width") or max(w["x"] + w["w"] for w in words)
    scale = width / 1366.0
    rows = _cluster_rows(words, tol=max(4.0, 6.0 * scale))

    header_words, header_cy = None, 0.0
    for cy, r in rows:
        blob = _squash(" ".join(w["t"] for w in r))
        if any(v in blob for v in STRIKE_WORDS) and any(v in blob for v in VALUE_WORDS):
            header_words, header_cy = r, cy
            break
    if header_words is None:
        return [], [
            "Could not find the ODIN column header (Symbol / Strike Price / Net Qty / MTM G/L). "
            "Capture the Integrated Net Position window and try again."
        ]

    data_rows: list[tuple[float, list[dict]]] = []
    for cy, r in rows:
        if cy <= header_cy:
            continue
        first = r[0]["t"].replace(" ", "").upper()
        if first in {"(ALL)", "(AL)", "ALL)", "(A)"}:
            break
        if not re.match(r"^[A-Z][A-Z0-9&.\-]{1,}$", first):
            continue
        if not any(looks_like_expiry(w["t"]) for w in r[1:]):
            # every real position line carries an expiry; the window's status bar, trade log and
            # taskbar text do not. Some brokers' grids have no (ALL) footer row to stop on, so
            # this is what keeps that junk out of the column clustering.
            continue
        if not any(TYPE_RE.match(w["t"]) or any(c.isdigit() for c in w["t"]) for w in r[1:]):
            continue
        data_rows.append((cy, r))

    if not data_rows:
        return [], ["Header found but no position rows below it."]

    items = [(w["x"] + w["w"] / 2, w) for _, r in data_rows for w in r[1:]]
    groups = _cluster_columns(items, gap=max(28.0, 0.021 * width))
    anchors = _header_anchors(header_words)
    names, notes = _name_groups(groups, anchors, width)
    column_of = {id(w): names.get(i) for i, g in enumerate(groups) for w in g}

    parsed: list[GridRow] = []
    for cy, r in data_rows:
        grid = GridRow(y=cy)
        grid.cells["symbol"] = r[0]["t"]
        for w in r[1:]:
            name = column_of.get(id(w))
            if name:
                grid.cells[name] = (grid.cells.get(name, "") + " " + w["t"]).strip()
        parsed.append(grid)

    missing = [n for n in ["expiry", "strike", "otype", *NUMERIC_COLUMNS]
               if n not in set(names.values())]
    warnings = list(notes)
    if missing:
        warnings.append("Could not read these columns from the grid: "
                        + ", ".join(COLUMN_LABELS[m] for m in missing))
    return parsed, warnings


# ---------------------------------------------------------------- value cleaning

_LEAD_BAD = "_.-oO#@$&~—–‗"


def clean_number(text: str) -> float | None:
    s = text.strip().replace(",", "").replace(" ", "")
    if not s:
        return None
    neg = s[0] in "-_—–" or "(" in s
    s = re.sub(r"[^\d.]", "", s.lstrip(_LEAD_BAD).rstrip(")"))
    s = s.replace("O", "0").replace("o", "0")
    if s.count(".") > 1:
        s = s.replace(".", "", s.count(".") - 1)
    if s in ("", "."):
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if neg else value


def clean_symbol(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9&.\-]", "", text.upper())


def clean_option_type(text: str) -> str:
    s = text.strip().upper()
    if not s:
        return ""
    if s[0] in "CP":
        return "CE" if s[0] == "C" else "PE"
    if "CE" in s:
        return "CE"
    return "PE" if ("PE" in s or "PF" in s) else ""


def parse_expiry_token(text: str) -> tuple[int | None, int | None, str]:
    """'19OCT20...' / '190CT20..' -> (19, 10, '20'). Truncated years come back as digits seen."""
    s = re.sub(r"[^A-Za-z0-9]", "", text.upper())
    m = re.match(r"(\d{2})", s)
    if not m:
        return None, None, ""
    day = int(m.group(1))
    # OCR reads the letter O of the month as digit 0, so normalise only the month fragment
    rest = s[m.end():]
    frag = re.match(r"[A-Z]{1,3}", rest.replace("0", "O").replace("1", "I"))
    month, tail = None, rest
    if frag:
        tail = rest[frag.end():]
        cands = [i + 1 for i, mo in enumerate(MONTHS) if mo.startswith(frag.group(0))]
        month = cands[0] if len(cands) == 1 else None
    ym = re.search(r"\d{2,4}", tail)
    return day, month, (ym.group(0) if ym else "")


def _parse_nse_date(raw: str) -> tuple[int, int, int] | None:
    m = re.match(r"(\d{1,2})[-/ ](\d{1,2})[-/ ](\d{2,4})", raw)
    if m:
        y = int(m.group(3))
        return int(m.group(1)), int(m.group(2)), (2000 + y if y < 100 else y)
    m = re.match(r"(\d{1,2})[-/ ]([A-Za-z]{3,})[-/ ](\d{2,4})", raw)
    if not m:
        return None
    mon = m.group(2)[:3].upper()
    if mon not in MONTHS:
        return None
    y = int(m.group(3))
    return int(m.group(1)), MONTHS.index(mon) + 1, (2000 + y if y < 100 else y)


def resolve_expiry(token: str, expiry_dates: list[str] | None = None, today: str | None = None) -> str:
    """Match the (often truncated) terminal expiry against NSE's real expiry list."""
    day, month, year_digits = parse_expiry_token(token)
    if day is None or month is None:
        return token.strip()
    for raw in expiry_dates or []:
        parsed = _parse_nse_date(raw)
        if not parsed:
            continue
        d, mo, y = parsed
        # the terminal truncates on the right ("19OCT20.."), so the digits seen are a prefix
        if d == day and mo == month and (not year_digits or str(y).startswith(year_digits)):
            return raw.strip()          # hand back NSE's own spelling so every lookup matches
    if len(year_digits) == 4:
        year = int(year_digits)
    elif len(year_digits) == 2 and today and year_digits > today[2:4]:
        year = int(year_digits) + 2000
    else:
        year = int(today[:4]) if today else 0
    if not year:
        return f"{day:02d}-{MONTHS[month - 1]}"
    return f"{day:02d}-{MONTHS[month - 1]}-{year}"


def to_records(grid_rows: list[GridRow], expiry_dates: list[str] | None = None,
               today: str | None = None) -> list[dict[str, Any]]:
    """Turn grid lines into position records. Shorts arrive as minus in the terminal and
    are stored as a positive Qty plus a Side, which is how the sheet reads them."""
    records = []
    for g in grid_rows:
        net = clean_number(g.raw("netqty"))
        buy_q = clean_number(g.raw("buyqty")) or 0
        sell_q = clean_number(g.raw("sellqty")) or 0
        if net is None:
            net = (buy_q - sell_q) if (buy_q or sell_q) else None
        side = "" if net is None else ("Sell" if net < 0 else "Buy" if net > 0 else "Closed")
        token = g.raw("expiry")
        records.append({
            "symbol": clean_symbol(g.raw("symbol")),
            "expiry_raw": token,
            "expiry": resolve_expiry(token, expiry_dates, today) if token else "",
            "strike": clean_number(g.raw("strike")),
            "otype": clean_option_type(g.raw("otype")),
            "net_qty_raw": net,
            "qty": abs(int(net)) if net is not None else 0,
            "side": side,
            "mtm": clean_number(g.raw("mtm")),
            "buy_qty": int(buy_q) if buy_q else 0,
            "buy_avg": clean_number(g.raw("buyavg")),
            "sell_qty": int(sell_q) if sell_q else 0,
            "sell_avg": clean_number(g.raw("sellavg")),
            "mkt_price": clean_number(g.raw("mktprice")),
            "y": g.y,
        })
    _infer_missing_types(records)
    return records


def _infer_missing_types(records: list[dict[str, Any]]) -> None:
    """Recover an Option Type the OCR dropped.

    A row that carries a strike is an option, never a future, so a blank type there is a misread.
    When the opposite leg at the same symbol/expiry/strike was read cleanly, the blank one is the
    other side. Anything still unknown is flagged so the grid can ask for a manual fix rather than
    silently mis-placing it.
    """
    by_strike: dict[tuple, list[dict]] = {}
    for r in records:
        if r.get("strike"):
            by_strike.setdefault((r["symbol"], r["expiry"], r["strike"]), []).append(r)
    for grp in by_strike.values():
        known = {r["otype"] for r in grp if r["otype"] in ("CE", "PE")}
        if len(known) != 1:
            continue
        missing = "PE" if "CE" in known else "CE"
        for r in grp:
            if not r["otype"]:
                r["otype"] = missing
                r["otype_inferred"] = True
    for r in records:
        if r.get("strike") and r["otype"] not in ("CE", "PE"):
            r["otype_unclear"] = True
