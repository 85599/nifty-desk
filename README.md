# Nifty Desk

Drop a screenshot of your broker's **Integrated Net Position** grid and get back a
ready-to-use NIFTY options workbook — with live premiums filled in automatically.

Built for the ODIN / SynapseWave terminal, but the parser works off any screenshot of the
same grid.

## What it does

1. **Screenshot in** — drag & drop or `Ctrl+V`. Windows OCR reads the grid locally; the image
   never leaves your machine.
2. **Positions parsed** — non-NIFTY rows and fully-closed rows are filtered out. Net Qty that the
   terminal shows as minus becomes a positive Qty + a Side. Lot size is auto-detected from the
   quantities.
3. **Live rates** — press *Fetch rates* to pull premiums + spot from the public NSE option chain
   (no API key). The workbook then refreshes itself every 5 minutes.
4. **Excel out** — one `Sheet1` block per expiry, laid out exactly like a hand-made book:
   `STRIKE / QTY / PREMIUM` ladders at a 50-point gap, `CP IF <spot>` scenario columns at a
   100-point gap, CE block, PE block, futures line, grand `TOTAL`. Every scenario cell is a live
   IF formula, so editing a quantity or a rate recalculates the whole grid. Rates are written as
   round whole numbers. Each expiry prints on its own page.

## Run it

```bash
pip install -r requirements.txt
cp config.example.json config.json      # optional; sensible defaults are built in
python server.py
```

Then open `http://127.0.0.1:8765/`. On Windows you can double-click `Run Nifty Desk.bat`.

## Config

`config.json` (git-ignored) holds your working settings:

| key | meaning |
| --- | --- |
| `symbol` | instrument to keep rows for (default `NIFTY`) |
| `only_symbol` | hide non-matching rows |
| `drop_closed` | hide rows with net qty 0 |
| `step` | strike gap (50 or 100) |
| `span` | how far above/below spot the ladder reaches |
| `lot` | lot size; `0` = auto-detect from the screenshot |
| `output_dir` | where workbooks are written; blank = `./output` |

## Notes

- Requires Windows (uses the built-in Windows OCR via PowerShell).
- No credentials or market data are sent anywhere except the public NSE quote endpoint.
