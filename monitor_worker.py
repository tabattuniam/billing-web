"""Background worker monitoring PPPoE multi-tenant.

Di-import oleh app.py dan dijalankan via APScheduler setiap 30 detik.
Untuk setiap tenant yang punya addon monitor_telegram aktif + config bot,
worker akan poll semua MikroTik server tenant tersebut dan kirim notifikasi
up/down ke grup Telegram tenant.

Fitur bot Telegram:
- Notifikasi OFFLINE/ONLINE otomatis (dengan nama pelanggan)
- Perintah /online  → daftar semua yang sedang online
- Perintah /offline → daftar semua yang sedang offline
- Perintah /cek <username> → cek status satu pelanggan
- Perintah /bantuan → daftar perintah
"""
from __future__ import annotations

import time
import logging
import asyncio
import requests

log = logging.getLogger(__name__)

# ── Telegram helper ───────────────────────────────────────────────────────────

def _tg_send(bot_token: str, chat_id: str, text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
        return r.json().get("ok", False)
    except Exception as e:
        log.warning("Telegram send gagal: %s", e)
        return False


def _tg_get_updates(bot_token: str, offset: int = 0) -> list[dict]:
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getUpdates",
            params={"offset": offset, "timeout": 0, "limit": 50},
            timeout=10,
        )
        d = r.json()
        if d.get("ok"):
            return d.get("result", [])
    except Exception as e:
        log.debug("getUpdates error: %s", e)
    return []


def tg_test(bot_token: str, chat_id: str) -> tuple[bool, str]:
    """Test koneksi bot Telegram. Return (ok, pesan_error)."""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getMe", timeout=8
        )
        d = r.json()
        if not d.get("ok"):
            return False, "Token bot tidak valid"
        bot_name = d["result"].get("first_name", "Bot")
        r2 = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": (
                    f"✅ <b>Test berhasil!</b>\n\n"
                    f"Bot <b>{bot_name}</b> siap mengirim notifikasi monitoring ke grup ini.\n\n"
                    f"📋 <b>Perintah yang tersedia:</b>\n"
                    f"/online — daftar pelanggan online\n"
                    f"/offline — daftar pelanggan offline\n"
                    f"/cek &lt;username&gt; — cek status satu pelanggan\n"
                    f"/bantuan — tampilkan semua perintah"
                ),
                "parse_mode": "HTML",
            },
            timeout=8,
        )
        d2 = r2.json()
        if d2.get("ok"):
            return True, ""
        err = d2.get("description", "Gagal kirim ke grup")
        return False, err
    except Exception as e:
        return False, str(e)


# ── Format pesan notifikasi ───────────────────────────────────────────────────

def _fmt_down(username: str, nama: str, server_nama: str, since_ts: int) -> str:
    menit = max(0, (int(time.time()) - since_ts) // 60)
    durasi = f"{menit} menit" if menit > 0 else "baru saja"
    nama_info = f" ({nama})" if nama and nama != username else ""
    return (
        f"🔴 <b>OFFLINE</b> — {username}{nama_info}\n"
        f"📡 Server: {server_nama}\n"
        f"⏱ Offline sejak: {durasi}"
    )


def _fmt_up(username: str, nama: str, server_nama: str) -> str:
    nama_info = f" ({nama})" if nama and nama != username else ""
    return (
        f"🟢 <b>ONLINE</b> — {username}{nama_info}\n"
        f"📡 Server: {server_nama}"
    )


# ── Format balasan perintah ───────────────────────────────────────────────────

def _fmt_list_online(rows: list[dict]) -> str:
    online = [r for r in rows if r["is_online"]]
    if not online:
        return "📋 Tidak ada pelanggan yang sedang online."
    lines = [f"🟢 <b>Online sekarang ({len(online)} pelanggan)</b>"]
    for r in online:
        nama = r.get("nama_pelanggan") or ""
        srv  = r.get("server_nama") or r["server_id"]
        info = f"  <code>{r['username']}</code>"
        if nama and nama != r["username"]:
            info += f" — {nama}"
        info += f" · {srv}"
        lines.append(info)
    return "\n".join(lines)


def _fmt_list_offline(rows: list[dict]) -> str:
    offline = [r for r in rows if not r["is_online"]]
    if not offline:
        return "📋 Semua pelanggan sedang online."
    lines = [f"🔴 <b>Offline sekarang ({len(offline)} pelanggan)</b>"]
    now = int(time.time())
    for r in offline:
        nama = r.get("nama_pelanggan") or ""
        srv  = r.get("server_nama") or r["server_id"]
        menit = max(0, (now - int(r["last_change_ts"])) // 60) if r.get("last_change_ts") else 0
        info = f"  <code>{r['username']}</code>"
        if nama and nama != r["username"]:
            info += f" — {nama}"
        info += f" · {srv}"
        if menit:
            info += f" ({menit} mnt)"
        lines.append(info)
    return "\n".join(lines)


def _fmt_cek(row: dict | None, username: str) -> str:
    if not row:
        return f"❓ Username <code>{username}</code> tidak ditemukan dalam daftar monitoring."
    nama = row.get("nama_pelanggan") or ""
    srv  = row.get("server_nama") or row["server_id"]
    status = "🟢 <b>ONLINE</b>" if row["is_online"] else "🔴 <b>OFFLINE</b>"
    now = int(time.time())
    menit = max(0, (now - int(row["last_change_ts"])) // 60) if row.get("last_change_ts") else 0
    durasi = f"{menit} menit lalu" if menit else "baru saja"
    lines = [
        f"{status} — <code>{row['username']}</code>",
    ]
    if nama and nama != row["username"]:
        lines.append(f"👤 {nama}")
    lines.append(f"📡 Server: {srv}")
    lines.append(f"🕐 Terakhir berubah: {durasi}")
    return "\n".join(lines)


_BANTUAN = (
    "🤖 <b>Perintah Bot Monitoring</b>\n\n"
    "/online — daftar semua pelanggan yang sedang online\n"
    "/offline — daftar semua pelanggan yang sedang offline\n"
    "/cek &lt;username&gt; — cek status satu pelanggan\n"
    "  Contoh: <code>/cek budi123</code>\n"
    "/bantuan — tampilkan pesan ini"
)


# ── Worker notifikasi (dipanggil tiap 30 detik) ───────────────────────────────

async def run_monitor_tick(db, get_mt_fn):
    """Satu siklus monitoring untuk semua tenant aktif."""
    try:
        tenants = db.list_active_monitor_tenants()
    except Exception:
        log.exception("Gagal ambil daftar tenant monitor")
        return

    for tenant in tenants:
        user_id       = tenant["user_id"]
        bot_token     = tenant["bot_token"]
        group_chat_id = tenant["group_chat_id"]
        grace         = int(tenant.get("grace_seconds") or 60)
        notify_down   = bool(tenant.get("notify_on_down", 1))
        notify_up     = bool(tenant.get("notify_on_up", 1))
        can_notify    = bool(bot_token and group_chat_id)

        try:
            servers = db.list_servers(user_id)
        except Exception:
            continue

        for srv in servers:
            if srv.get("status") != "aktif":
                continue
            server_id   = srv["id"]
            server_nama = srv["nama"]

            try:
                mt = get_mt_fn(server_id)
                if not mt:
                    continue
                secrets      = await asyncio.to_thread(mt.list_pppoe_secrets)
                actives_list = await asyncio.to_thread(mt.list_pppoe_active)
                active_names = {a.get("name", "") for a in actives_list}
            except Exception as e:
                log.debug("Monitor tick gagal server %s: %s", server_id, e)
                continue

            now = int(time.time())

            for secret in secrets:
                username = secret.get("name", "")
                if not username or secret.get("disabled") == "true":
                    continue

                is_online = username in active_names
                prev      = db.get_monitor_state(user_id, server_id, username)

                if prev is None:
                    db.upsert_monitor_state(user_id, server_id, username,
                                            int(is_online), now, int(is_online))
                    continue

                was_online  = bool(prev["is_online"])
                last_change = int(prev["last_change_ts"])
                notified    = prev["notified_state"]

                if is_online != was_online:
                    db.upsert_monitor_state(user_id, server_id, username,
                                            int(is_online), now, None)
                    db.log_monitor_event(user_id, server_id, username,
                                         "up" if is_online else "down", now)
                    last_change = now

                nama = db.get_pppoe_nama(user_id, username)

                # Notifikasi UP
                if is_online and notified != 1:
                    if can_notify and notify_up:
                        await asyncio.to_thread(_tg_send, bot_token, group_chat_id,
                                 _fmt_up(username, nama, server_nama))
                    db.mark_monitor_notified(user_id, server_id, username, 1)

                # Notifikasi DOWN (setelah grace period)
                elif not is_online and notified != 0:
                    if (now - last_change) >= grace:
                        if can_notify and notify_down:
                            await asyncio.to_thread(_tg_send, bot_token, group_chat_id,
                                     _fmt_down(username, nama, server_nama, last_change))
                        db.mark_monitor_notified(user_id, server_id, username, 0)

    log.debug("Monitor tick selesai: %d tenant", len(tenants))


# ── Worker perintah bot (dipanggil tiap 15 detik) ────────────────────────────

async def run_command_tick(db, get_mt_fn=None):
    """Poll Telegram getUpdates dan balas perintah /online /offline /cek /bantuan."""
    try:
        tenants = db.list_active_monitor_tenants()
    except Exception:
        return

    for tenant in tenants:
        user_id       = tenant["user_id"]
        bot_token     = tenant["bot_token"]
        group_chat_id = tenant["group_chat_id"]

        if not bot_token:
            continue

        cfg = db.get_monitor_config(user_id)
        offset = int((cfg or {}).get("tg_update_offset") or 0)

        updates = await asyncio.to_thread(_tg_get_updates, bot_token, offset)
        if not updates:
            continue

        new_offset = offset
        for upd in updates:
            new_offset = max(new_offset, upd["update_id"] + 1)
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue

            text      = (msg.get("text") or "").strip()
            chat_id   = str(msg.get("chat", {}).get("id", ""))
            chat_type = msg.get("chat", {}).get("type", "")

            # Terima dari: grup terdaftar ATAU private chat langsung ke bot
            is_registered_group = (group_chat_id and chat_id == group_chat_id)
            is_private = (chat_type == "private")
            if not is_registered_group and not is_private:
                continue

            # Tidak ada teks atau bukan perintah
            if not text.startswith("/"):
                continue

            parts   = text.split(None, 1)
            cmd     = parts[0].split("@")[0].lower()
            arg     = parts[1].strip() if len(parts) > 1 else ""

            reply_to = chat_id or group_chat_id
            if not reply_to:
                continue

            if cmd == "/bantuan" or cmd == "/start":
                await asyncio.to_thread(_tg_send, bot_token, reply_to, _BANTUAN)

            elif cmd == "/online":
                rows  = db.list_monitor_state_all(user_id)
                await asyncio.to_thread(_tg_send, bot_token, reply_to, _fmt_list_online(rows))

            elif cmd == "/offline":
                rows  = db.list_monitor_state_all(user_id)
                await asyncio.to_thread(_tg_send, bot_token, reply_to, _fmt_list_offline(rows))

            elif cmd == "/cek":
                if not arg:
                    await asyncio.to_thread(_tg_send, bot_token, reply_to,
                             "⚠️ Gunakan: <code>/cek &lt;username&gt;</code>\nContoh: <code>/cek budi123</code>")
                else:
                    row = db.get_monitor_state_by_username(user_id, arg)
                    if not row:
                        nama = db.get_pppoe_nama(user_id, arg)
                    else:
                        nama = db.get_pppoe_nama(user_id, arg)
                        row["nama_pelanggan"] = nama
                    await asyncio.to_thread(_tg_send, bot_token, reply_to, _fmt_cek(row, arg))

        if new_offset != offset:
            db.set_tg_update_offset(user_id, new_offset)

    log.debug("Command tick selesai")
