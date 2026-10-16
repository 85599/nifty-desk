"""Local trading desk: ODIN screenshot -> OCR -> review -> live NSE rates -> Excel.

Run with `python server.py`. Nothing leaves the machine except the NSE quote request.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser

from flask import Flask, jsonify, request, send_from_directory

import build_book
import nse_live
import parse_grid

FROZEN = getattr(sys, "frozen", False)
# PyInstaller unpacks read-only data files to a temp dir at startup
BUNDLE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def writable_root() -> str:
    """Homes output/ and config.json. Next to the exe, unless that dir is read-only."""
    if not FROZEN:
        return os.path.dirname(os.path.abspath(__file__))
    beside = os.path.dirname(os.path.abspath(sys.executable))
    probe = os.path.join(beside, ".niftydesk-write-test")
    try:
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        return beside
    except OSError:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        fallback = os.path.join(base, "NiftyDesk")
        os.makedirs(fallback, exist_ok=True)
        return fallback


ROOT = writable_root()
WEB = os.path.join(BUNDLE, "web")
OCR_PS = os.path.join(BUNDLE, "ocr", "winocr.ps1")
OUTPUT_DIR = os.path.join(ROOT, "output")
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")

DEFAULT_CONFIG = {
    "symbol": "NIFTY",
    "only_symbol": True,
    "drop_closed": True,
    "step": 50,
    "span": 600,             # ladder reaches this far above and below spot, per expiry page
    "lot": 0,                # 0 = detect from the screenshot's quantities
    "expiry": "",
    "output_dir": OUTPUT_DIR,
    "ocr_scale": 2.0,
}

STATE: dict = {"chains": {}, "chain": {}, "records": [], "warnings": [], "image": "",
               "config": dict(DEFAULT_CONFIG),
               "auto": {"enabled": True, "seconds": 300, "last_ts": 0.0, "last": "", "note": "",
                        "path": ""}}
LOCK = threading.Lock()
app = Flask(__name__, static_folder=None)


def config_path() -> str:
    return os.path.join(ROOT, "config.json")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    STATE["config"] = cfg
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(config_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass


def run_ocr(image_path: str, scale: float) -> dict:
    out_json = os.path.join(tempfile.gettempdir(), f"odin_ocr_{uuid.uuid4().hex}.json")
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", OCR_PS,
           "-Path", image_path, "-OutJson", out_json, "-Scale", str(float(scale))]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise RuntimeError("OCR timed out. Is this a very large image?")
    finally:
        pass
    if not os.path.exists(out_json):
        detail = (done.stderr or done.stdout or "").strip()[:400]
        raise RuntimeError(f"Windows OCR did not run. {detail}")
    with open(out_json, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    os.remove(out_json)
    return data


def detect_lot(records: list[dict], fallback: int = 75) -> int:
    qtys = [r["qty"] for r in records if r.get("qty")]
    if not qtys:
        return fallback
    lot = 0
    for q in qtys:
        lot = math.gcd(lot, q)
    return lot if 1 <= lot <= 200 else fallback


def nse() -> nse_live.NseClient:
    client = STATE.get("nse")
    if client is None:
        client = STATE["nse"] = nse_live.NseClient()
    return client


def strike_map(chains: dict[str, dict]) -> dict:
    """'{expiry}|{strike}' -> {CE: {...}, PE: {...}}, the shape the browser table looks up."""
    out = {}
    for exp, chain in chains.items():
        for k, legs in (chain or {}).get("strikes", {}).items():
            out[f"{exp}|{k:g}"] = {side: {f: leg.get(f) for f in ("last", "bid", "ask", "oi")}
                                   for side, leg in legs.items()}
    return out


def normalise(rows: list[dict]) -> list[dict]:
    """Terminal rows -> the shape the workbook writer wants, dropping anything with no quantity."""
    out: list[dict] = []
    for raw in rows:
        p = dict(raw)
        p["strike"] = float(p.get("strike") or 0)
        p["qty"] = int(abs(float(p.get("qty") or 0)))
        p["side"] = p.get("side") or ("Sell" if p["qty"] and float(p.get("net_qty_raw") or 0) < 0 else "Buy")
        for key in ("buy_avg", "sell_avg", "mkt_price", "mtm", "buy_qty", "sell_qty"):
            value = p.get(key)
            p[key] = None if value in ("", None) else (int(float(value)) if key.endswith("qty") else float(value))
        if p["qty"]:
            out.append(p)
    return out


def fetch_chains(symbol: str, primary: str, extra=()) -> tuple[dict, list, str, list]:
    """Quote every expiry the book holds. Raises when the primary expiry cannot be quoted."""
    client = nse()
    dates = client.expiry_dates(symbol)
    primary = primary or (dates[0] if dates else "")
    if not primary:
        raise nse_live.NseError("NSE sent no expiry list.")
    chains, errors = {}, []
    for exp in [primary] + sorted({e for e in extra if e and e != primary}):
        try:
            chains[exp] = client.chain(symbol, exp)
        except nse_live.NseError as exc:
            if exp == primary:
                raise
            errors.append(f"{exp}: {exc}")
    return chains, dates, primary, errors


def book_path(cfg: dict, chains: dict) -> str:
    """One rolling workbook per book, named after the expiry it is priced for, so the refresher
    always rewrites the same file instead of piling up dated copies."""
    out_dir = cfg.get("output_dir") or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    expiry = cfg.get("expiry") or (sorted(chains)[0] if chains else "book")
    return os.path.join(out_dir, f"nifty {expiry}.xlsx")


def write_book() -> tuple[str, dict] | None:
    """Rebuild the workbook from what the desk currently holds. Raises PermissionError if Excel
    has the file open - the caller decides whether that is an error or just a skipped refresh."""
    positions, chains = STATE.get("records") or [], STATE.get("chains") or {}
    if not positions or not chains:
        return None
    cfg = STATE["config"]
    spot = next((c.get("spot") for _e, c in sorted(chains.items()) if (c or {}).get("spot")), 0)
    path = book_path(cfg, chains)
    summary = build_book.build_workbook(
        path, positions, chains,
        {"symbol": cfg["symbol"], "spot": spot, "step": int(cfg.get("step") or 50),
         "span": float(cfg.get("span") or 600)})
    STATE["auto"]["path"] = path
    return path, summary


def auto_loop() -> None:
    """Pull fresh NSE rates and rewrite the workbook on a timer, so the file is never stale."""
    while True:
        time.sleep(5)
        st = STATE["auto"]
        if not st["enabled"] or time.time() - st["last_ts"] < st["seconds"]:
            continue
        st["last_ts"] = time.time()
        cfg, records = STATE["config"], STATE.get("records") or []
        if not records:
            continue
        try:
            with LOCK:
                chains, _dates, primary, errors = fetch_chains(
                    cfg["symbol"], cfg.get("expiry"),
                    [r["expiry"] for r in records if r.get("expiry")])
                STATE["chains"], STATE["chain"] = chains, chains.get(primary) or {}
            write_book()
            st["last"] = dt.datetime.now().strftime("%H:%M")
            st["note"] = "; ".join(errors)[:180]
        except PermissionError:
            st["note"] = "Excel has the workbook open, so this refresh only updated the rates " \
                         "on the desk - close the file and it will be rewritten in the next cycle."
        except Exception as exc:
            st["note"] = str(exc)[:180]


@app.get("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.get("/<path:name>")
def assets(name):
    return send_from_directory(WEB, name)


@app.post("/api/ocr")
def api_ocr():
    upload = request.files.get("image")
    if upload is None or not upload.filename:
        return jsonify(error="No image received."), 400
    suffix = os.path.splitext(upload.filename)[1].lower() or ".png"
    if suffix not in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff"}:
        return jsonify(error=f"Unsupported image type {suffix}."), 400
    tmp = os.path.join(tempfile.gettempdir(), f"odin_in_{uuid.uuid4().hex}{suffix}")
    upload.save(tmp)
    cfg = STATE["config"]
    try:
        ocr = run_ocr(tmp, cfg.get("ocr_scale", 2.0))
        dates: list[str] = []
        try:
            dates = nse().expiry_dates(cfg["symbol"])
        except Exception:
            dates = []
        grid_rows, warnings = parse_grid.parse_grid(ocr)
        today = dt.date.today().isoformat()
        records = parse_grid.to_records(grid_rows, dates, today)
        if cfg.get("only_symbol", True):
            kept = [r for r in records if r["symbol"].startswith(cfg["symbol"])]
            dropped = len(records) - len(kept)
            records = kept
            if dropped:
                warnings.append(f"{dropped} non-{cfg['symbol']} row(s) hidden "
                                f"(untick 'NIFTY only' to see them).")
        if cfg.get("drop_closed", True):
            open_rows = [r for r in records if r["qty"]]
            closed = len(records) - len(open_rows)
            if closed:
                warnings.append(f"{closed} fully closed row(s) hidden (qty 0).")
            records = open_rows
        with LOCK:
            STATE["records"] = records
            STATE["warnings"] = warnings
            STATE["image"] = os.path.basename(tmp)
            # a new screenshot is a new book - old quotes no longer apply to it, and the
            # refresher should price this one straight away rather than waiting out the timer
            STATE["chains"] = {}
            STATE["chain"] = {}
            STATE["auto"]["last_ts"] = 0.0
        return jsonify(records=records, warnings=warnings,
                       detected_lot=detect_lot(records),
                       image="/api/image/" + os.path.basename(tmp))
    except Exception as exc:
        return jsonify(error=str(exc)), 500
    finally:
        pass


@app.get("/api/image/<name>")
def api_image(name):
    return send_from_directory(tempfile.gettempdir(), name)


@app.get("/api/expiries")
def api_expiries():
    cfg = STATE["config"]
    try:
        return jsonify(expiry_dates=nse().expiry_dates(cfg["symbol"]))
    except Exception as exc:
        return jsonify(expiry_dates=[], error=str(exc))


@app.post("/api/live")
def api_live():
    body = request.get_json(silent=True) or {}
    cfg = STATE["config"]
    symbol = body.get("symbol") or cfg["symbol"]
    extra = [e for e in (body.get("expiries") or []) if e]
    extra += [r["expiry"] for r in STATE["records"] if r.get("expiry")]
    try:
        chains, dates, primary, errors = fetch_chains(symbol, body.get("expiry") or cfg.get("expiry"), extra)
    except nse_live.NseError as exc:
        return jsonify(error=str(exc)), 502
    except Exception as exc:
        return jsonify(error=f"Could not get live rates: {exc}"), 502

    main_chain = chains[primary]
    with LOCK:
        STATE["chains"] = chains
        STATE["chain"] = main_chain
        cfg["expiry"] = main_chain["expiry"]
        STATE["config"] = cfg
        save_config(cfg)
        STATE["auto"]["last_ts"] = time.time()
    touched = write_book()
    return jsonify(
        symbol=main_chain["symbol"], expiry=main_chain["expiry"], spot=main_chain["spot"],
        timestamp=main_chain["timestamp"], as_of=main_chain["as_of"],
        atm=nse_live.atm_strike(main_chain["spot"], main_chain["strikes"], cfg["step"]),
        n_strikes=len(main_chain["strikes"]), expiry_dates=dates,
        quoted_expiries=sorted(chains), errors=errors, strikes=strike_map(chains),
        wrote=(touched[0] if touched else ""),
    )


@app.post("/api/build")
def api_build():
    body = request.get_json(silent=True) or {}
    cfg = dict(STATE["config"])
    cfg.update({k: v for k, v in body.items() if k in DEFAULT_CONFIG})
    cfg.update({k: v for k, v in (body.get("config") or {}).items() if k in DEFAULT_CONFIG})
    with LOCK:
        STATE["config"] = cfg
    positions = normalise(body.get("positions") or STATE["records"])
    if not positions:
        return jsonify(error="Nothing to write - drop a screenshot first."), 400
    with LOCK:
        STATE["records"] = positions
    if not STATE.get("chains"):
        try:
            chains, _, _, _ = fetch_chains(cfg["symbol"], cfg.get("expiry"),
                                           [r["expiry"] for r in positions if r.get("expiry")])
            with LOCK:
                STATE["chains"] = chains
                STATE["chain"] = chains[cfg.get("expiry") or sorted(chains)[0]]
                cfg["expiry"] = STATE["chain"]["expiry"]
                STATE["config"] = cfg
                save_config(cfg)
        except Exception as exc:
            return jsonify(error=f"Could not get live rates: {exc}"), 502
    try:
        path, summary = write_book()
    except PermissionError:
        return jsonify(error="Excel has that file open. Close it and try again."), 423
    except Exception as exc:
        return jsonify(error=f"Could not write the workbook: {exc}"), 500
    save_config(cfg)
    return jsonify(summary=summary, path=path)


@app.post("/api/open")
def api_open():
    path = (request.get_json(silent=True) or {}).get("path") or ""
    if not path or not os.path.exists(path):
        return jsonify(error="File not found."), 404
    try:
        os.startfile(path)  # noqa: S606 - local convenience
        return jsonify(ok=True)
    except OSError as exc:
        return jsonify(error=str(exc)), 500


@app.get("/api/state")
def api_state():
    chain = STATE.get("chain") or {}
    return jsonify(config=STATE["config"], warnings=STATE["warnings"],
                   records=STATE["records"],
                   strikes=strike_map(STATE.get("chains") or {}),
                   live={k: chain.get(k) for k in ("symbol", "expiry", "spot", "timestamp")}
                   if chain else None,
                   quoted_expiries=sorted(STATE.get("chains") or {}),
                   auto=auto_status(),
                   has_chain=bool(chain))


@app.get("/api/health")
def api_health():
    ok = os.path.exists(OCR_PS)
    return jsonify(ok=ok, python=sys.version.split()[0], ocr_script=ok)


@app.post("/api/auto")
def api_auto():
    """Turn the 5-minute live refresh on or off, or ask for one right now."""
    body = request.get_json(silent=True) or {}
    st = STATE["auto"]
    if "enabled" in body:
        st["enabled"] = bool(body["enabled"])
    if body.get("seconds"):
        st["seconds"] = max(60, int(body["seconds"]))
    if body.get("now"):
        st["last_ts"] = 0.0
    return jsonify(auto=auto_status())


def auto_status() -> dict:
    st = STATE["auto"]
    due = st["last_ts"] + st["seconds"] - time.time()
    return {"enabled": st["enabled"], "seconds": st["seconds"], "last": st["last"],
            "note": st["note"], "path": st["path"], "due_in": max(0, int(due)) if st["enabled"] else None}


def main() -> None:
    load_config()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    threading.Thread(target=auto_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8765))
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  Nifty desk tool  ->  {url}\n  Ctrl+C to stop\n")
    if not os.environ.get("NIFTY_DESK_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
