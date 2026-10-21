"""brokers.py - broker adapters: PaperBroker (free demo) and MetaApiBroker (real MT5)."""
import asyncio
import itertools
from typing import Any, Dict, List, Optional


class BrokerError(Exception):
    """Readable broker/connection failure."""


class Broker:
    """Interface every adapter implements."""
    is_paper = False

    async def info(self) -> Dict[str, Any]: ...                      # balance, equity, currency
    async def positions(self) -> List[Dict[str, Any]]: ...           # id,symbol,side,volume,open_price,sl,tp,profit,comment
    async def spec(self, symbol: str) -> Dict[str, Any]: ...         # contract_size, volume_step, min_volume, max_volume, profit_currency
    async def price(self, symbol: str, hint: Optional[float] = None) -> Dict[str, float]: ...  # bid, ask
    async def conversion(self, profit_ccy: str, account_ccy: str, suffix: str = "") -> float: ...
    async def open(self, symbol: str, side: str, volume: float, sl: Optional[float],
                   tp: Optional[float], comment: str) -> Dict[str, Any]: ...
    async def close(self, position_id: str, volume: Optional[float] = None) -> Dict[str, Any]: ...


# --------------------------------------------------------------------------- #
class PaperBroker(Broker):
    """Simulated account so the whole app can be tested with zero cost.
    Prices are simulated: the midpoint of SL and TP is used as 'market price'."""
    is_paper = True
    CONTRACTS = {"XAUUSD": 100, "XAGUSD": 5000, "US30": 1, "NAS100": 1, "SPX500": 1,
                 "BTCUSD": 1, "ETHUSD": 1}

    def __init__(self, balance: float = 10000.0) -> None:
        self.balance = balance
        self._pos: Dict[str, Dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._last: Dict[str, float] = {}

    async def info(self):
        return {"balance": self.balance, "equity": self.balance, "currency": "USD"}

    async def positions(self):
        return [dict(p) for p in self._pos.values()]

    async def spec(self, symbol):
        base = symbol.upper()
        contract = next((v for k, v in self.CONTRACTS.items() if base.startswith(k)), 100000)
        return {"contract_size": contract, "volume_step": 0.01, "min_volume": 0.01,
                "max_volume": 100.0, "profit_currency": "USD"}

    async def price(self, symbol, hint=None):
        if hint:
            self._last[symbol] = hint
        px = self._last.get(symbol)
        if not px:
            raise BrokerError("Paper mode needs both SL and TP (price is simulated from them)")
        return {"bid": px, "ask": px}

    async def conversion(self, profit_ccy, account_ccy, suffix=""):
        return 1.0

    async def open(self, symbol, side, volume, sl, tp, comment):
        px = self._last[symbol]
        pid = f"P{next(self._ids)}"
        self._pos[pid] = {"id": pid, "symbol": symbol, "side": side, "volume": volume,
                          "open_price": px, "sl": sl or 0, "tp": tp or 0, "profit": 0.0,
                          "comment": comment}
        return {"position_id": pid, "price": px}

    async def close(self, position_id, volume=None):
        pos = self._pos.get(position_id)
        if not pos:
            raise BrokerError(f"Position {position_id} not found")
        if volume and volume < pos["volume"]:
            pos["volume"] = round(pos["volume"] - volume, 8)
        else:
            del self._pos[position_id]
        return {"position_id": position_id}


# --------------------------------------------------------------------------- #
_api = None


def _metaapi(token: str):
    global _api
    if _api is None:
        from metaapi_cloud_sdk import MetaApi  # imported lazily: paper mode needs no SDK
        _api = MetaApi(token)
    return _api


class MetaApiBroker(Broker):
    """Real MT5 account hosted by MetaApi (cloud). Needs METAAPI_TOKEN + account id."""

    def __init__(self, token: str, account_id: str) -> None:
        self.token, self.account_id = token, account_id
        self._conn = None
        self._lock = asyncio.Lock()

    async def _c(self):
        if self._conn is not None:
            return self._conn
        async with self._lock:
            if self._conn is None:
                try:
                    account = await _metaapi(self.token).metatrader_account_api.get_account(self.account_id)
                    if account.state != "DEPLOYED":
                        await account.deploy()
                    await account.wait_connected()
                    conn = account.get_rpc_connection()
                    await conn.connect()
                    await conn.wait_synchronized()
                    self._conn = conn
                except Exception as exc:
                    raise BrokerError(f"MetaApi connect failed: {exc}")
        return self._conn

    async def _run(self, fn: str, *args):
        conn = await self._c()
        try:
            return await getattr(conn, fn)(*args)
        except Exception as exc:
            raise BrokerError(str(exc))

    async def info(self):
        i = await self._run("get_account_information")
        return {"balance": i.get("balance", 0), "equity": i.get("equity", 0),
                "currency": i.get("currency", "USD")}

    async def positions(self):
        out = []
        for p in await self._run("get_positions"):
            out.append({"id": p["id"], "symbol": p["symbol"],
                        "side": "BUY" if "BUY" in p["type"] else "SELL",
                        "volume": p["volume"], "open_price": p.get("openPrice", 0),
                        "sl": p.get("stopLoss", 0), "tp": p.get("takeProfit", 0),
                        "profit": p.get("profit", 0), "comment": p.get("comment", "")})
        return out

    async def spec(self, symbol):
        s = await self._run("get_symbol_specification", symbol)
        return {"contract_size": s.get("contractSize"), "volume_step": s.get("volumeStep", 0.01),
                "min_volume": s.get("minVolume", 0.01), "max_volume": s.get("maxVolume", 100),
                "profit_currency": s.get("profitCurrency") or s.get("quoteCurrency") or "USD"}

    async def price(self, symbol, hint=None):
        p = await self._run("get_symbol_price", symbol)
        return {"bid": p["bid"], "ask": p["ask"]}

    async def conversion(self, profit_ccy, account_ccy, suffix=""):
        if profit_ccy == account_ccy:
            return 1.0
        for sym, invert in ((f"{profit_ccy}{account_ccy}{suffix}", False),
                            (f"{profit_ccy}{account_ccy}", False),
                            (f"{account_ccy}{profit_ccy}{suffix}", True),
                            (f"{account_ccy}{profit_ccy}", True)):
            try:
                p = await self._run("get_symbol_price", sym)
                mid = (p["bid"] + p["ask"]) / 2
                return 1 / mid if invert else mid
            except BrokerError:
                continue
        raise BrokerError(f"No {profit_ccy}/{account_ccy} rate available to convert risk")

    async def open(self, symbol, side, volume, sl, tp, comment):
        fn = "create_market_buy_order" if side == "BUY" else "create_market_sell_order"
        res = await self._run(fn, symbol, volume, sl or None, tp or None, {"comment": comment})
        return {"position_id": res.get("positionId") or res.get("orderId"), "price": None}

    async def close(self, position_id, volume=None):
        if volume:
            return await self._run("close_position_partially", position_id, volume, {})
        return await self._run("close_position", position_id, {})
