"""store.py - tiny JSON persistence (accounts, signals, mode)."""
import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))


class Store:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = DATA_DIR / "data.json"
        self.data: Dict[str, Any] = {"mode": "manual", "accounts": [], "signals": []}
        if self.path.is_file():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # accounts
    @property
    def accounts(self) -> List[Dict[str, Any]]:
        return self.data["accounts"]

    def upsert_account(self, acct: Dict[str, Any]) -> Dict[str, Any]:
        acct.setdefault("id", uuid.uuid4().hex[:8])
        for i, a in enumerate(self.accounts):
            if a["id"] == acct["id"]:
                self.accounts[i] = acct
                break
        else:
            self.accounts.append(acct)
        self.save()
        return acct

    def delete_account(self, acct_id: str) -> None:
        self.data["accounts"] = [a for a in self.accounts if a["id"] != acct_id]
        self.save()

    # signals
    def add_signal(self, sig: Dict[str, Any]) -> Dict[str, Any]:
        sig["id"] = uuid.uuid4().hex[:8]
        self.data["signals"].insert(0, sig)
        del self.data["signals"][100:]
        self.save()
        return sig

    def get_signal(self, sig_id: str):
        return next((s for s in self.data["signals"] if s["id"] == sig_id), None)
