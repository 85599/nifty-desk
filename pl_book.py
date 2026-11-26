"""The daily profit-loss file: one PRO block per day, shaped like the hand-made TEMP sheet.

Their flow is a rolling one-file-per-month book. The Excel built at 15:40 (market close) freezes
that day's book as a snapshot; the next day the desk writes a block into `NIFTY PL <MON>.xlsx` that
puts the carried book on the left (quantity + carry-left rate), today's live rate next to it, and
whatever moved today on the right. Every cell is the same formula the hand-made sheet uses:

    AMT.        =(CUR.RATE-CL.RATE)*QTY
    net qty     =QTY(today)+QTY(carried)
    AMT.(today) =(CUR.RATE-rate the change was done at)*QTY(change)
    TOTAL       =SUM(left)+SUM(right)

so the block reads as the day's realised + unrealised profit, and overtyping a rate or a quantity
still recalculates it. Quantities here are the terminal's own sign - buy positive, sell negative -
which is the opposite of Sheet1, where a short is written positive so the CP IF formulas work.
"""
from __future__ import annotations

import calendar
import datetime as dt
import glob
import json
import os
import re

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Border, Font, Side

import build_book

SHEET = "TEMP"
SNAP_DIR = "snapshots"
BLOCK_GAP = 4                      # blank rows between one PRO block and the next
MARKER = "pl-written.json"         # in the snapshots folder; dated closes are matched by pattern
CLOSE_TIME = dt.time(15, 35)       # the desk calls it a close from here on (market ends 15:30)

WIDTHS = {"A": 15.1, "C": 14.9, "D": 15.9, "E": 16.4, "G": 18.1,
          "I": 17.7, "J": 16.3, "K": 14.1, "L": 16.3, "M": 19.6}
HEAD = ("AG1", "QTY", "CL.RATE", "CUR.RATE", "AMT.")
CLOSE_DAY = re.compile(r"\d{4}-\d{2}-\d{2}$")

TITLE_FONT = Font(name="Arial", size=11, bold=True)
BOLD = Font(name="Arial", size=10, bold=True)
THIN = Side(style="thin")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def avg_rate(p: dict) -> int:
    """The rate the open quantity is carried at - the terminal's own Net Avg.

    Not a blend of every fill: the part of a leg already squared off booked its profit, so only the
    quantity still held counts. Blending all fills instead makes the block disagree with the
    terminal's MTM by whatever those closed rounds earned.
    """
    qty = abs(int(p.get("qty") or 0))
    bq, ba = int(p.get("buy_qty") or 0), float(p.get("buy_avg") or 0)
    sq, sa = int(p.get("sell_qty") or 0), float(p.get("sell_avg") or 0)
    if qty and (bq or sq):
        held = ((sq * sa - bq * ba) if (p.get("side") or "Sell").lower().startswith("s")
                else (bq * ba - sq * sa))
        return build_book.round_rate(held / qty)
    return build_book.round_rate(p.get("mkt_price") or 0)


def buy_book(positions: list[dict]) -> dict[tuple[str, float, str], tuple[int, int]]:
    """(expiry, strike, type) -> (quantity, rate), signed the way the P&L sheet reads it.

    A buy is positive and a sell negative, so (CUR.RATE - CL.RATE) x QTY comes out as a profit when
    it should. A row with no strike is the futures line and is keyed as FUT.
    """
    book: dict[tuple[str, float, str], list] = {}
    for p in positions:
        qty = int(p.get("qty") or 0)
        if not qty:
            continue
        sign = -1 if (p.get("side") or "Sell").lower().startswith("s") else 1
        kind = (p.get("otype") or "").upper()
        strike = round(float(p.get("strike") or 0), 2)
        if kind not in ("CE", "PE"):
            if strike:
                continue                      # an option whose type is unknown is not a future
            kind = "FUT"
        key = (p.get("expiry") or "", strike, kind)
        held = book.setdefault(key, [0, 0])
        held[0] += sign * qty
        held[1] = avg_rate(p) or held[1]
    return {k: (v[0], v[1]) for k, v in book.items()}


def side_rates(positions: list[dict]) -> dict[tuple[str, float, str], dict[str, int]]:
    """key -> {"buy": rate, "sell": rate} for the fills the terminal shows on each side.

    The right half of a block prices the day's change at the rate it was actually done at, so a
    row that was both bought and sold needs the two averages apart: `buy_book` blends them into one
    carry-left rate, which is right for the left half and wrong for the right one.
    """
    out: dict[tuple[str, float, str], dict[str, int]] = {}
    for p in positions:
        if not int(p.get("qty") or 0):
            continue
        kind = (p.get("otype") or "").upper()
        strike = round(float(p.get("strike") or 0), 2)
        if kind not in ("CE", "PE"):
            if strike:
                continue
            kind = "FUT"
        key = (p.get("expiry") or "", strike, kind)
        rates = out.setdefault(key, {})
        for qty_field, rate_field, name in (("buy_qty", "buy_avg", "buy"),
                                            ("sell_qty", "sell_avg", "sell")):
            if p.get(qty_field) and p.get(rate_field):
                rates[name] = build_book.round_rate(p[rate_field])
    return out


def snap_dir(out_dir: str) -> str:
    return os.path.join(out_dir, SNAP_DIR)


def save_snapshot(out_dir: str, positions: list[dict], chains: dict[str, dict], cfg: dict,
                  when: dt.datetime | None = None) -> str:
    """Freeze the book as this day's close. Rewritten on every build, so the day's last write wins."""
    when = when or dt.datetime.now()
    os.makedirs(snap_dir(out_dir), exist_ok=True)
    book = buy_book(positions)
    legs = [{"expiry": e, "strike": s, "kind": k, "qty": q, "rate": r}
            for (e, s, k), (q, r) in sorted(book.items())]
    spot = next((c.get("spot") for _e, c in sorted((chains or {}).items()) if (c or {}).get("spot")), 0)
    data = {"date": when.date().isoformat(), "as_of": when.strftime("%Y-%m-%d %H:%M"),
            "symbol": cfg.get("symbol", "NIFTY"), "spot": build_book.round_rate(spot),
            "legs": legs}
    path = os.path.join(snap_dir(out_dir), f"{when.date().isoformat()}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    opening = os.path.join(snap_dir(out_dir), f"{when.date().isoformat()}-open.json")
    if not os.path.exists(opening):
        # the day's first build, kept whole so a block can be written before any close exists
        with open(opening, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
    return path


def previous_snapshot(out_dir: str, day: dt.date) -> tuple[str, dict] | None:
    """The book the P&L block is measured from.

    Normally the latest close strictly before today. On the very first day the desk has no earlier
    close at all, so the day's own opening build is used - the block then shows what has moved since
    this morning instead of refusing to exist.
    """
    for path in sorted(glob.glob(os.path.join(snap_dir(out_dir), "*.json")), reverse=True):
        stamp = os.path.splitext(os.path.basename(path))[0]
        if not CLOSE_DAY.match(stamp) or stamp >= day.isoformat():
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        return stamp, data
    opening = os.path.join(snap_dir(out_dir), f"{day.isoformat()}-open.json")
    try:
        with open(opening, encoding="utf-8") as fh:
            return f"{day.isoformat()} (open)", json.load(fh)
    except (OSError, ValueError):
        return None


def was_written(out_dir: str, day: dt.date) -> bool:
    try:
        with open(os.path.join(snap_dir(out_dir), MARKER), encoding="utf-8") as fh:
            return day.isoformat() in (json.load(fh) or {}).get("days", [])
    except (OSError, ValueError):
        return False


def mark_written(out_dir: str, day: dt.date) -> None:
    path = os.path.join(snap_dir(out_dir), MARKER)
    days = []
    try:
        with open(path, encoding="utf-8") as fh:
            days = list((json.load(fh) or {}).get("days", []))
    except (OSError, ValueError):
        pass
    if day.isoformat() not in days:
        days.append(day.isoformat())
    os.makedirs(snap_dir(out_dir), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"days": days}, fh, indent=1)


def pl_path(out_dir: str, cfg: dict, day: dt.date) -> str:
    """One rolling file per month, named the way theirs is: `NIFTY PL DEC.xlsx`."""
    os.makedirs(out_dir, exist_ok=True)
    symbol = cfg.get("symbol", "NIFTY")
    return os.path.join(out_dir, f"{symbol} PL {calendar.month_abbr[day.month].upper()}.xlsx")


def _day_label(day: dt.date, expiry_label: str) -> str:
    base = f"PRO ( {day.day:02d} {calendar.month_abbr[day.month].lower()} )"
    return f"{base} {expiry_label}" if expiry_label else base


def _pl_block(ws, first: int, expiry: str, prev: dict, today: dict, sides: dict, chain: dict,
              spot: float, cfg: dict, day: dt.date, expiry_label: str) -> int:
    """Write one day's block: label, header, CE ladder, the futures line, PE ladder, TOTAL."""
    step = int(cfg.get("step") or 50)
    span = float(cfg.get("span") or 600)
    rates = build_book.leg_rates(chain)

    strikes = {kind: {s for (e, s, k) in set(prev) | set(today) if e == expiry and k == kind}
               for kind in ("CE", "PE")}
    ladder = sorted(set(build_book.ladder_strikes(spot, step, span)) | strikes["CE"] | strikes["PE"])

    ws.cell(row=first, column=7, value=_day_label(day, expiry_label)).font = TITLE_FONT
    head = first + 3
    for base in (1, 9):                      # the same five columns twice: carried book, today's move
        for offset, title in enumerate(HEAD):
            ws.cell(row=head, column=base + offset, value=title)
    for col in list(range(1, 6)) + list(range(9, 14)):
        cell = ws.cell(row=head, column=col)
        cell.font = BOLD
        cell.border = BOX

    row = head + 1
    top = row
    for kind in ("CE", "PE"):
        if kind == "PE":
            # the futures line sits between the two sides, exactly like the hand-made sheet
            row = _leg_row(ws, row, (expiry, 0.0, "FUT"), prev, today, sides, rates, spot,
                           cfg["symbol"])
        for strike in ladder:
            row = _leg_row(ws, row, (expiry, strike, kind), prev, today, sides, rates, spot,
                           f"{strike:g} {kind}")
    total = row
    ws.cell(row=total, column=1, value="TOTAL")
    ws.cell(row=total, column=9, value="TOTAL")
    ws.cell(row=total, column=5, value=f"=SUM(E{top}:E{row - 1})")
    ws.cell(row=total, column=13, value=f"=SUM(M{top}:M{row - 1})")
    ws.cell(row=total, column=7, value=f"=E{total}+M{total}")
    for col in list(range(1, 6)) + list(range(7, 8)) + list(range(9, 14)):
        cell = ws.cell(row=total, column=col)
        cell.font = BOLD
        cell.border = BOX
    return total


def _leg_row(ws, row: int, key: tuple, prev: dict, today: dict, sides: dict, rates: dict,
             spot: float, label: str) -> int:
    """One strike line: carried book on the left, today's change on the right, net quantity between."""
    expiry, strike, kind = key
    p_qty, p_rate = prev.get(key, (0, 0))
    t_qty, t_rate = today.get(key, (0, 0))
    live = (rates.get(strike, {}) or {}).get(kind) if kind in ("CE", "PE") else None
    # a future has no premium so the index level is its rate, taken before its own average or the
    # row would re-price at the carry rate and always show zero; an option never takes spot
    if kind == "FUT":
        cur = build_book.round_rate(spot or t_rate or p_rate)
    else:
        cur = build_book.round_rate(live or t_rate or p_rate)

    carried = p_qty
    # a row with no carried quantity is priced at today's own rate, so AMT. stays 0 instead of
    # pretending the position came in free
    cl = p_rate if (carried and p_rate) else cur
    delta = t_qty - carried
    # the change is priced at the fill on that side, not the row's blended average
    done = (sides.get(key) or {}).get("buy" if delta > 0 else "sell") or (t_rate if delta else 0) or cur

    ws.cell(row=row, column=1, value=label)
    ws.cell(row=row, column=2, value=carried)
    ws.cell(row=row, column=3, value=cl)
    ws.cell(row=row, column=4, value=cur)
    ws.cell(row=row, column=5, value=f"=(D{row}-C{row})*B{row}")
    ws.cell(row=row, column=7, value=f"=J{row}+B{row}")
    ws.cell(row=row, column=9, value=f"=A{row}")
    ws.cell(row=row, column=10, value=delta)
    ws.cell(row=row, column=11, value=done)
    ws.cell(row=row, column=12, value=f"=D{row}")
    ws.cell(row=row, column=13, value=f"=(L{row}-K{row})*J{row}")
    return row + 1


def build_pl(path: str, snapshot: dict, positions: list[dict], chains: dict[str, dict],
             cfg: dict, day: dt.date | None = None) -> dict:
    """Append today's PRO block(s) to the month's P&L file, one block per expiry in the book."""
    day = day or dt.date.today()
    prev = {(l["expiry"], round(float(l["strike"]), 2), l["kind"]): (int(l["qty"]), int(l["rate"]))
            for l in (snapshot or {}).get("legs", [])}
    today = buy_book(positions)
    sides = side_rates(positions)

    if os.path.exists(path):
        wb = load_workbook(path)
        ws = wb[SHEET] if SHEET in wb.sheetnames else wb[wb.sheetnames[0]]
        first = (ws.max_row or 0) + BLOCK_GAP
        fresh = False
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = SHEET
        first = 1
        fresh = True
        for col, width in WIDTHS.items():
            ws.column_dimensions[col].width = width

    # only expiries the book actually holds get a block: fetching live rates quotes several, and an
    # all-zero page for one of them is not a day's P&L
    expiries = sorted({k[0] for k in prev} | {k[0] for k in today})
    show_expiry = len(expiries) > 1
    blocks, row = [], first
    for expiry in expiries:
        chain = (chains or {}).get(expiry) or {}
        spot = chain.get("spot") or (snapshot or {}).get("spot") or cfg.get("spot") or 0
        label = build_book.expiry_label(expiry) if show_expiry else ""
        last = _pl_block(ws, row, expiry, prev, today, sides, chain, spot, cfg, day, label)
        blocks.append({"expiry": expiry, "first": row, "last": last})
        row = last + BLOCK_GAP

    wb.save(path)
    return {"path": path, "blocks": len(blocks), "fresh_file": fresh, "day": day.isoformat(),
            "carried_from": (snapshot or {}).get("date") or (snapshot or {}).get("as_of") or "",
            "as_of": (snapshot or {}).get("as_of") or "",
            "expiries": [b["expiry"] for b in blocks],
            "rows": sum(b["last"] - b["first"] + 1 for b in blocks)}


def is_close(now: dt.datetime) -> bool:
    """The auto block is only worth writing once the market has gone, so its rates are the close ones."""
    return now.weekday() < 5 and now.time() >= CLOSE_TIME
