"""MikroTik RouterOS API helper."""
from __future__ import annotations
import routeros_api


class MikroTik:
    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    def _conn(self):
        pool = routeros_api.RouterOsApiPool(
            self.host, username=self.username, password=self.password,
            port=self.port, plaintext_login=True
        )
        return pool.get_api()

    # ── PPPoE ─────────────────────────────────────────────────────────────────

    def add_pppoe_secret(self, username: str, password: str, profile: str = "default") -> bool:
        try:
            api = self._conn()
            api.get_resource("/ppp/secret").add(
                name=username, password=password,
                service="pppoe", profile=profile
            )
            return True
        except Exception:
            return False

    def remove_pppoe_secret(self, username: str) -> bool:
        try:
            api = self._conn()
            res = api.get_resource("/ppp/secret")
            rows = res.get(name=username)
            for r in rows:
                res.remove(id=r["id"])
            return True
        except Exception:
            return False

    def enable_pppoe_secret(self, username: str) -> bool:
        try:
            api = self._conn()
            res = api.get_resource("/ppp/secret")
            rows = res.get(name=username)
            for r in rows:
                res.set(id=r["id"], disabled="no")
            return True
        except Exception:
            return False

    def disable_pppoe_secret(self, username: str) -> bool:
        try:
            api = self._conn()
            res = api.get_resource("/ppp/secret")
            rows = res.get(name=username)
            for r in rows:
                res.set(id=r["id"], disabled="yes")
            return True
        except Exception:
            return False

    def list_pppoe_active(self) -> list[dict]:
        try:
            api = self._conn()
            rows = api.get_resource("/ppp/active").get()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def list_pppoe_secrets(self) -> list[dict]:
        try:
            api = self._conn()
            rows = api.get_resource("/ppp/secret").get(service="pppoe")
            return [dict(r) for r in rows]
        except Exception:
            return []

    def list_pppoe_profiles(self) -> list[str]:
        try:
            api = self._conn()
            rows = api.get_resource("/ppp/profile").get()
            return [r["name"] for r in rows]
        except Exception:
            return ["default"]

    # ── Hotspot ───────────────────────────────────────────────────────────────

    def add_hotspot_user(self, username: str, password: str, profile: str = "default", comment: str = "") -> bool:
        try:
            api = self._conn()
            kwargs = dict(name=username, password=password, profile=profile)
            if comment:
                kwargs["comment"] = comment
            api.get_resource("/ip/hotspot/user").add(**kwargs)
            return True
        except Exception:
            return False

    def remove_hotspot_user(self, username: str) -> bool:
        try:
            api = self._conn()
            res = api.get_resource("/ip/hotspot/user")
            rows = res.get(name=username)
            for r in rows:
                res.remove(id=r["id"])
            return True
        except Exception:
            return False

    def list_hotspot_profiles(self) -> list[str]:
        try:
            api = self._conn()
            rows = api.get_resource("/ip/hotspot/user/profile").get()
            return [r["name"] for r in rows]
        except Exception:
            return ["default"]

    def list_hotspot_active(self) -> list[dict]:
        try:
            api = self._conn()
            rows = api.get_resource("/ip/hotspot/active").get()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ── System ────────────────────────────────────────────────────────────────

    def ping(self) -> bool:
        try:
            api = self._conn()
            api.get_resource("/system/identity").get()
            return True
        except Exception:
            return False

    def get_identity(self) -> str:
        try:
            api = self._conn()
            rows = api.get_resource("/system/identity").get()
            return rows[0]["name"] if rows else "Unknown"
        except Exception:
            return "Unknown"
