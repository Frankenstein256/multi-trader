"""engine.py - executes trades across all accounts with per-account risk."""
import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

import risk
from brokers import Broker, BrokerError, MetaApiBroker, PaperBroker
from risk import RiskError
from store import Store

log = logging.getLogger("engine")
TAG = "MC"          # comment tag on every trade we open
TIMEOUT = 25        # seconds per broker call


class Engine:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._brokers: Dict[str, tuple] = {}
        self._cache: Optional[tuple] = None

    # ---- brokers ------------------------------------------------------ #
    def broker(self, acct: Dict[str, Any]) -> Broker:
        key = (acct["metaapi_id"],)
        cached = self._brokers.get(acct["id"])
        if cached and cached[0] == key:
            return cached[1]
        if acct["metaapi_id"].strip().lower() == "paper":
            b: Broker = PaperBroker()
        else:
            token = os.environ.get("METAAPI_TOKEN", "")
            if not token:
                raise BrokerError("METAAPI_TOKEN is not set on the server")
            b = MetaApiBroker(token, acct["metaapi_id"].strip())
        self._brokers[acct["id"]] = (key, b)
        return b

    @staticmethod
    async def _t(coro):
        return await asyncio.wait_for(coro, TIMEOUT)

    @staticmethod
    def _sym(acct: Dict[str, Any], symbol: str) -> str:
        suffix = acct.get("suffix", "") or ""
        return symbol if (not suffix or symbol.endswith(suffix)) else symbol + suffix

    def targets(self, ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        accts = [a for a in self.store.accounts if a.get("enabled", True)]
        return [a for a in accts if a["id"] in ids] if ids else accts

    # ---- open --------------------------------------------------------- #
    async def _open_one(self, acct, symbol, side, sl, tp, risk_percent) -> Dict[str, Any]:
        t0 = time.perf_counter()
        out: Dict[str, Any] = {"account": acct["label"], "ok": False}
        try:
            b = self.broker(acct)
            sym = self._sym(acct, symbol)
            info = await self._t(b.info())
            spec = await self._t(b.spec(sym))
            hint = (sl + tp) / 2 if (sl and tp) else None
            px = await self._t(b.price(sym, hint))
            entry = px["ask"] if side == "BUY" else px["bid"]
            if sl and ((side == "BUY" and sl >= entry) or (side == "SELL" and sl <= entry)):
                raise RiskError("Stop-loss is on the wrong side of the price")
            if tp and ((side == "BUY" and tp <= entry) or (side == "SELL" and tp >= entry)):
                raise RiskError("Take-profit is on the wrong side of the price")
            conv = await self._t(b.conversion(spec["profit_currency"], info["currency"],
                                              acct.get("suffix", "") or ""))
            lots = risk.lots_for_risk(
                equity=info["equity"], risk_percent=risk_percent or acct["risk_percent"],
                entry=entry, sl=sl, spec=spec, conv=conv, max_lot=acct.get("max_lot") or None)
            res = await self._t(b.open(sym, side, lots, sl, tp, TAG))
            out.update(ok=True, symbol=sym, lots=lots, position_id=res.get("position_id"))
        except (BrokerError, RiskError) as exc:
            out["error"] = str(exc)
        except asyncio.TimeoutError:
            out["error"] = "Timed out talking to the broker"
        except Exception as exc:  # never let one account break the others
            log.exception("open failed")
            out["error"] = f"{type(exc).__name__}: {exc}"
        out["ms"] = round((time.perf_counter() - t0) * 1000)
        log.info("OPEN %s %s %s -> %s", acct["label"], side, symbol, out)
        return out

    async def open_trade(self, symbol, side, sl, tp, risk_percent=None, ids=None):
        accts = self.targets(ids)
        if not accts:
            return [{"account": "-", "ok": False, "error": "No enabled accounts"}]
        self._cache = None
        return list(await asyncio.gather(*(
            self._open_one(a, symbol, side, sl, tp, risk_percent) for a in accts)))

    # ---- close -------------------------------------------------------- #
    async def _close_one(self, acct, symbol: Optional[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"account": acct["label"], "ok": False}
        try:
            b = self.broker(acct)
            sym = self._sym(acct, symbol) if symbol else None
            positions = [p for p in await self._t(b.positions())
                         if p["comment"].startswith(TAG) and (sym is None or p["symbol"] == sym)]
            for p in positions:
                await self._t(b.close(p["id"]))
            out.update(ok=True, closed=len(positions))
        except (BrokerError, asyncio.TimeoutError) as exc:
            out["error"] = str(exc) or "Timed out"
        except Exception as exc:
            log.exception("close failed")
            out["error"] = f"{type(exc).__name__}: {exc}"
        log.info("CLOSE %s %s -> %s", acct["label"], symbol or "ALL", out)
        return out

    async def close_trades(self, symbol: Optional[str] = None, ids=None):
        self._cache = None
        return list(await asyncio.gather(*(self._close_one(a, symbol) for a in self.targets(ids))))

    # ---- dashboard snapshot ------------------------------------------ #
    async def _snap_one(self, acct) -> Dict[str, Any]:
        row = dict(acct)
        try:
            b = self.broker(acct)
            row["info"] = await self._t(b.info())
            row["positions"] = await self._t(b.positions())
        except Exception as exc:
            row["error"] = str(exc) or type(exc).__name__
        return row

    async def snapshot(self) -> List[Dict[str, Any]]:
        if self._cache and time.time() - self._cache[0] < 5:
            return self._cache[1]
        rows = list(await asyncio.gather(*(self._snap_one(a) for a in self.store.accounts)))
        self._cache = (time.time(), rows)
        return rows
