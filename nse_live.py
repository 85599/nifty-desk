"""Live NIFTY spot + option premiums from NSE's public option-chain endpoint.

No API key. NSE rejects bare requests, so a homepage hit boots the cookies first and every
call carries a browser-like header set. Called only when the user presses the button.
"""
from __future__ import annotations

import datetime as dt
import threading
import time

import requests

BASE = "https://www.nseindia.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/option-chain",
    "Origin": BASE,
    "Connection": "keep-alive",
}


class NseError(RuntimeError):
    pass


class NseClient:
    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._session: requests.Session | None = None
        self._booted_at = 0.0

    def _ensure_session(self) -> requests.Session:
        # NSE's cookies go stale within minutes; re-bootstrap past 10 minutes
        if self._session and time.time() - self._booted_at < 600:
            return self._session
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            session.get(f"{BASE}/option-chain", timeout=self.timeout)
        except requests.RequestException as exc:
            raise NseError(f"Could not reach nseindia.com ({exc.__class__.__name__}). "
                           "Check your internet connection.") from exc
        self._session = session
        self._booted_at = time.time()
        return session

    def _get(self, url: str, params: dict | None = None) -> dict:
        with self._lock:
            last_err = None
            for attempt in range(3):
                try:
                    session = self._ensure_session()
                    resp = session.get(url, params=params, timeout=self.timeout)
                    if resp.status_code == 200:
                        try:
                            data = resp.json()
                        except ValueError:
                            raise NseError(
                                "NSE returned a page instead of data (its bot filter tripped). "
                                "Wait a minute and press the button again.")
                        if isinstance(data, dict):
                            return data
                        last_err = NseError("Unexpected response from NSE.")
                    elif resp.status_code in (401, 403, 429):
                        self._session = None  # cookie dead, re-bootstrap
                        last_err = NseError(f"NSE blocked the request (HTTP {resp.status_code}).")
                    else:
                        last_err = NseError(f"NSE responded HTTP {resp.status_code}.")
                except NseError:
                    raise
                except requests.RequestException as exc:
                    last_err = NseError(f"NSE request failed ({exc.__class__.__name__}).")
                time.sleep(0.6 * (attempt + 1))
            raise last_err or NseError("NSE request failed.")

    def expiry_dates(self, symbol: str = "NIFTY") -> list[str]:
        data = self._get(f"{BASE}/api/option-chain-contract-info", {"symbol": symbol})
        dates = data.get("expiryDates") or []
        return [d for d in dates if isinstance(d, str)]

    def chain(self, symbol: str = "NIFTY", expiry: str | None = None) -> dict:
        """Live option chain for one expiry, keyed by strike."""
        dates = self.expiry_dates(symbol)
        if expiry:
            want = _normalise_date(expiry)
            chosen = next((d for d in dates if _normalise_date(d) == want), None)
            if chosen is None:
                raise NseError(f"NSE has no {symbol} expiry '{expiry}'. "
                               f"Available: {', '.join(dates[:4])}")
        else:
            if not dates:
                raise NseError("NSE returned no expiry list.")
            chosen = dates[0]

        data = self._get(f"{BASE}/api/option-chain-v3",
                         {"type": "Indices", "symbol": symbol, "expiry": chosen})
        records = data.get("records") or {}
        rows = records.get("data") or []
        spot = records.get("underlyingValue") or 0
        stamp = records.get("timestamp") or ""
        if not rows or not spot:
            raise NseError(f"NSE sent no chain data for {symbol} {chosen}. "
                           "Markets may be closed or the expiry is not weekly/index F&O.")

        strikes: dict[float, dict] = {}
        for row in rows:
            k = row.get("strikePrice")
            if k is None:
                continue
            entry = strikes.setdefault(float(k), {})
            for side in ("CE", "PE"):
                leg = row.get(side)
                if leg:
                    entry[side] = {
                        "last": _num(leg.get("lastPrice")),
                        "bid": _num(leg.get("buyPrice1")),
                        "ask": _num(leg.get("sellPrice1")),
                        "oi": _num(leg.get("openInterest")),
                        "chg_oi": _num(leg.get("changeinOpenInterest")),
                        "iv": _num(leg.get("impliedVolatility")),
                        "volume": _num(leg.get("totalTradedVolume")),
                    }
        return {
            "symbol": symbol,
            "expiry": chosen,
            "spot": float(spot),
            "timestamp": stamp,
            "strikes": strikes,
            "expiry_dates": dates,
            "as_of": dt.datetime.now().strftime("%d-%b-%Y %H:%M:%S"),
        }


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise_date(text: str) -> str:
    """Any spelling of an expiry -> 'DD-Mon-YYYY' so terminal and NSE strings compare equal."""
    from parse_grid import MONTHS, _parse_nse_date
    parsed = _parse_nse_date(text)
    if not parsed:
        return text.strip()
    d, m, y = parsed
    return f"{d:02d}-{MONTHS[m - 1]}-{y}"


def atm_strike(spot: float, strikes, step: int = 50) -> float:
    if not strikes:
        return round(spot / step) * step
    return min(strikes, key=lambda k: abs(k - spot))
