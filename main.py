"""main.py - web app: dashboard API, one-click trading, signals and TradingView webhook."""
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator

from engine import Engine
from store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
if not ADMIN_PASSWORD or not WEBHOOK_SECRET:
    raise SystemExit("ADMIN_PASSWORD and WEBHOOK_SECRET must be set (see .env)")

store = Store()
engine = Engine(store)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)) -> None:
    ok = secrets.compare_digest(creds.username.encode(), ADMIN_USER.encode()) and \
        secrets.compare_digest(creds.password.encode(), ADMIN_PASSWORD.encode())
    if not ok:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


# ---------------------------------------------------------------- models --
class AccountIn(BaseModel):
    id: Optional[str] = None
    label: str = Field(min_length=1, max_length=40)
    metaapi_id: str = Field(min_length=1, max_length=80)   # MetaApi account id, or "paper"
    risk_percent: float = Field(1.0, gt=0, le=100)
    suffix: str = ""
    max_lot: Optional[float] = Field(None, gt=0)
    enabled: bool = True


class TradeIn(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    sl: float = Field(gt=0)
    tp: Optional[float] = Field(None, gt=0)
    risk_percent: Optional[float] = Field(None, gt=0, le=100)
    accounts: Optional[List[str]] = None

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        v = v.split(":")[-1].strip().upper()
        if not v:
            raise ValueError("symbol required")
        return v


class CloseIn(BaseModel):
    symbol: Optional[str] = None
    accounts: Optional[List[str]] = None


class ModeIn(BaseModel):
    mode: Literal["auto", "manual"]


# ---------------------------------------------------------------- pages ---
@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/", dependencies=[Depends(auth)])
async def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/api/state", dependencies=[Depends(auth)])
async def state():
    return {"mode": store.data["mode"], "accounts": await engine.snapshot(),
            "signals": store.data["signals"][:30],
            "metaapi_ready": bool(os.environ.get("METAAPI_TOKEN"))}


# ------------------------------------------------------------- accounts ---
@app.post("/api/accounts", dependencies=[Depends(auth)])
async def save_account(a: AccountIn):
    store.upsert_account(a.model_dump())
    engine._cache = None
    return {"ok": True}


@app.delete("/api/accounts/{acct_id}", dependencies=[Depends(auth)])
async def delete_account(acct_id: str):
    store.delete_account(acct_id)
    engine._cache = None
    return {"ok": True}


@app.post("/api/mode", dependencies=[Depends(auth)])
async def set_mode(m: ModeIn):
    store.data["mode"] = m.mode
    store.save()
    return {"ok": True, "mode": m.mode}


# -------------------------------------------------------------- trading ---
@app.post("/api/trade", dependencies=[Depends(auth)])
async def trade(t: TradeIn):
    results = await engine.open_trade(t.symbol, t.side, t.sl, t.tp, t.risk_percent, t.accounts)
    return {"results": results}


@app.post("/api/close", dependencies=[Depends(auth)])
async def close(c: CloseIn):
    sym = c.symbol.split(":")[-1].strip().upper() if c.symbol else None
    return {"results": await engine.close_trades(sym, c.accounts)}


# -------------------------------------------------------------- signals ---
async def _run_signal(sig: dict) -> list:
    if sig["action"] == "CLOSE":
        return await engine.close_trades(sig["symbol"])
    return await engine.open_trade(sig["symbol"], sig["action"], sig["sl"], sig["tp"],
                                   sig.get("risk_percent"))


@app.post("/api/signals/{sig_id}/take", dependencies=[Depends(auth)])
async def take_signal(sig_id: str):
    sig = store.get_signal(sig_id)
    if not sig or sig["status"] != "pending":
        raise HTTPException(404, "Signal not found or already handled")
    sig["status"] = "taken"
    sig["results"] = await _run_signal(sig)
    store.save()
    return {"results": sig["results"]}


@app.post("/api/signals/{sig_id}/dismiss", dependencies=[Depends(auth)])
async def dismiss_signal(sig_id: str):
    sig = store.get_signal(sig_id)
    if sig and sig["status"] == "pending":
        sig["status"] = "dismissed"
        store.save()
    return {"ok": True}


@app.post("/webhook")
async def webhook(request: Request):
    """TradingView / bot alerts. JSON: ACTION, SYMBOL, SL_PRICE, TP_PRICE, SECRET [,RISK_PERCENT]."""
    try:
        body = await request.json()
        data = {str(k).lower(): v for k, v in body.items()}
        action = str(data["action"]).upper()
        if action not in ("BUY", "SELL", "CLOSE"):
            raise ValueError("ACTION must be BUY, SELL or CLOSE")
        symbol = str(data["symbol"]).split(":")[-1].strip().upper()
        sl = float(data["sl_price"]) if data.get("sl_price") else None
        tp = float(data["tp_price"]) if data.get("tp_price") else None
        if action != "CLOSE" and not sl:
            raise ValueError("SL_PRICE is required")
        risk_pct = float(data["risk_percent"]) if data.get("risk_percent") else None
    except (ValueError, KeyError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": f"Bad request: {exc}"}, status_code=400)

    supplied = str(request.headers.get("x-webhook-secret") or data.get("secret") or "")
    if not hmac.compare_digest(supplied.encode(), WEBHOOK_SECRET.encode()):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    sig = store.add_signal({"time": int(time.time()), "action": action, "symbol": symbol,
                            "sl": sl, "tp": tp, "risk_percent": risk_pct,
                            "status": "pending", "results": []})
    if store.data["mode"] == "auto":
        sig["status"] = "auto"
        sig["results"] = await _run_signal(sig)
        store.save()
    return {"ok": True, "signal": sig["id"], "status": sig["status"], "results": sig["results"]}
