"""BillingVPN — FastAPI billing web for PPPoE & Hotspot management."""
from __future__ import annotations
import time, yaml, requests, random, hashlib, json, uuid, sqlite3, re, asyncio
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeTimedSerializer, BadSignature

from storage import Storage
from mikrotik import MikroTik

# ── Config ────────────────────────────────────────────────────────────────────
_cfg_path = Path(__file__).parent / "configs" / "billing.yaml"
CFG = yaml.safe_load(_cfg_path.read_text())

APP_NAME    = CFG["app"]["name"]
APP_DOMAIN  = CFG["app"].get("domain", "billing.vpntunel.my.id")
MAIN_DOMAIN = CFG.get("vpntunel", {}).get("domain", APP_DOMAIN)
SECRET_KEY  = CFG["app"]["secret_key"]
PORT        = CFG["app"].get("port", 8094)
DB_PATH     = CFG["db_path"]
WA_URL      = CFG.get("wuzapi", {}).get("url", "")
WA_TOKEN    = CFG.get("wuzapi", {}).get("token", "")
WA_ADMIN_TOKEN   = CFG.get("wuzapi", {}).get("admin_token", "")
WA_USERS_DB      = CFG.get("wuzapi", {}).get("users_db", "")
PLATFORM_OWNER_WA = CFG.get("platform", {}).get("owner_wa", "")

INTERNAL_API_KEY = CFG.get("internal_api", {}).get("key", "")

MAYAR_KEY      = CFG.get("mayar", {}).get("api_key", "")
MAYAR_WEBHOOK  = CFG.get("mayar", {}).get("webhook_token", "")
MAYAR_BASE     = CFG.get("mayar", {}).get("base_url", "https://api.mayar.id")

# Estimasi fee Mayar untuk channel QRIS (paling murah). Dipakai di Mode A
# (pelanggan tanggung) untuk dihitung sebagai markup ke total bayar.
MAYAR_FEE_PERCENT = 0.7  # 0.7%

_mdt_cfg = CFG.get("midtrans", {})
MIDTRANS_SERVER_KEY = _mdt_cfg.get("server_key", "")
MIDTRANS_CLIENT_KEY = _mdt_cfg.get("client_key", "")
MIDTRANS_IS_PROD    = _mdt_cfg.get("is_production", True)
MIDTRANS_BASE       = "https://api.midtrans.com" if MIDTRANS_IS_PROD else "https://api.sandbox.midtrans.com"

db  = Storage(DB_PATH)

scheduler = AsyncIOScheduler(timezone="Asia/Jakarta")

def _restore_wa_webhooks():
    """Saat startup: set ulang webhook WuzAPI untuk semua tenant yang punya ai_chat aktif."""
    try:
        con = db._conn()
        rows = con.execute(
            "SELECT wg.wa_token, u.id as user_id FROM tenant_addons ta "
            "JOIN addons a ON a.id=ta.addon_id "
            "JOIN users u ON u.id=ta.user_id "
            "JOIN wa_gateway wg ON wg.user_id=u.id "
            "WHERE a.code='ai_chat' AND ta.status='active' AND u.status='aktif'"
        ).fetchall()
        con.close()
        for row in rows:
            token = row["wa_token"]
            webhook_url = f"https://{APP_DOMAIN}/wa/chatbot/{token}"
            _wa_set_webhook(token, webhook_url)
    except Exception:
        pass

@asynccontextmanager
async def lifespan(application: FastAPI):
    scheduler.start()
    _restore_wa_webhooks()
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)
tpl = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

signer = URLSafeTimedSerializer(SECRET_KEY)


def rp(n: int) -> str:
    return f"Rp {n:,}".replace(",", ".")


def ts_date(ts) -> str:
    from datetime import datetime
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%-d %b %Y %H:%M")
    except Exception:
        return str(ts)


def datetimeformat(ts, fmt="%-d %b %Y %H:%M") -> str:
    from datetime import datetime
    try:
        return datetime.fromtimestamp(int(ts)).strftime(fmt)
    except Exception:
        return "-"

tpl.env.filters["rp"] = rp
tpl.env.filters["ts_date"] = ts_date
tpl.env.filters["datetimeformat"] = datetimeformat

# ── Auth helpers ──────────────────────────────────────────────────────────────

def make_session(uid: str) -> str:
    return signer.dumps(uid)


def get_session(token: str) -> str | None:
    try:
        return signer.loads(token, max_age=86400 * 7)
    except BadSignature:
        return None


def current_user(request: Request) -> dict | None:
    token = request.cookies.get("session")
    if not token:
        return None
    uid = get_session(token)
    if not uid:
        return None
    return db.get_user(uid)


def current_user_agen(request: Request) -> dict | None:
    token = request.cookies.get("agen_session")
    if not token:
        return None
    uid = get_session(token)
    if not uid:
        return None
    return db.get_user(uid)


def current_user_teknisi(request: Request) -> dict | None:
    token = request.cookies.get("teknisi_session")
    if not token:
        return None
    uid = get_session(token)
    if not uid:
        return None
    return db.get_user(uid)


def require_login(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    if user.get("role") in ("agen", "sub_agen"):
        raise HTTPException(status_code=302, headers={"Location": "/panel"})
    # Tenant yang suspend karena tagihan SaaS hanya boleh akses halaman tagihan
    if user.get("status") == "suspend_saas":
        path = request.url.path
        if path not in ("/tagihan-saas",) and not path.startswith("/tagihan-saas/"):
            raise HTTPException(status_code=302, headers={"Location": "/tagihan-saas"})
    return user


def _isp_id(user: dict) -> str:
    """Return ISP owner user_id — untuk teknisi pakai parent_id."""
    if user["role"] == "teknisi":
        return user.get("parent_id") or user["id"]
    return user["id"]

def _log(request: Request, user: dict, aksi: str, detail: str = ""):
    try:
        ip = request.client.host if request.client else ""
        db.log_activity(user["id"], user.get("role", ""), aksi, detail, ip)
    except Exception:
        pass


def _ctx(request: Request, **kw) -> dict:
    """Build template context without 'request' (Starlette 1.x adds it automatically)."""
    user = kw.get("user") or {}
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    isp_id = user.get("parent_id") or user_id if isinstance(user, dict) else user_id
    monitor_aktif = db.is_addon_active(user_id, "monitor_telegram") if user_id else False
    monitor_online_aktif_ctx = False
    if isp_id:
        _con_mo2 = _addon_db()
        _mo2 = _con_mo2.execute(
            "SELECT ta.status FROM tenant_addons ta JOIN addons a ON a.id=ta.addon_id "
            "WHERE ta.user_id=? AND a.code='monitor_online' AND ta.status='active'", (isp_id,)
        ).fetchone()
        _con_mo2.close()
        monitor_online_aktif_ctx = bool(_mo2)
    return {"app_name": APP_NAME, "app_domain": APP_DOMAIN, "main_domain": MAIN_DOMAIN,
            "monitor_aktif_sidebar": monitor_aktif,
            "monitor_online_aktif_sidebar": monitor_online_aktif_ctx, **kw}

# ── WuzAPI & External WA ──────────────────────────────────────────────────────

def _normalize_wa(nomor: str) -> str:
    n = nomor.strip().replace("-", "").replace(" ", "")
    if n.startswith("0"):
        n = "62" + n[1:]
    return n


def _send_wa_fonnte(nomor: str, pesan: str, token: str) -> tuple[bool, str]:
    try:
        r = requests.post(
            "https://api.fonnte.com/send",
            headers={"Authorization": token},
            data={"target": _normalize_wa(nomor), "message": pesan, "countryCode": "62"},
            timeout=10,
        )
        d = r.json()
        if d.get("status"):
            return True, ""
        return False, d.get("reason") or d.get("message") or "Gagal kirim"
    except Exception as e:
        return False, str(e)


def _check_wablas_token(token: str) -> tuple[bool, str, dict]:
    """Validasi token Wablas via endpoint /api/send-message (GET probe). Return (ok, msg, detail)."""
    try:
        r = requests.get(
            "https://console.wablas.com/api/send-message",
            headers={"Authorization": token},
            timeout=10,
        )
        d = r.json()
        msg = (d.get("message") or "").lower()
        # Wablas returns this specific message when token is invalid/expired
        if "token invalid" in msg or "device expired" in msg or "expired" in msg:
            return False, d.get("message") or "Token tidak valid atau device expired", d
        # Any other response means token is accepted (field validation errors are fine)
        return True, f"Token valid · Wablas device aktif", d
    except Exception as e:
        return False, f"Gagal koneksi ke Wablas: {e}", {}


def _send_wa_wablas(nomor: str, pesan: str, token: str) -> tuple[bool, str]:
    try:
        r = requests.post(
            "https://console.wablas.com/api/send-message",
            headers={"Authorization": token},
            json={"phone": _normalize_wa(nomor), "message": pesan},
            timeout=10,
        )
        d = r.json()
        if d.get("status") == "success":
            return True, ""
        return False, d.get("message") or "Gagal kirim"
    except Exception as e:
        return False, str(e)


def send_wa(nomor: str, pesan: str, token: str = "",
            user_id: str = "", tipe: str = "") -> tuple[bool, str]:
    """Kirim WA. Return (ok, error_message). Log otomatis ke wa_log."""
    if not nomor:
        return False, "Nomor HP kosong"

    # Cek provider eksternal (Fonnte/Wablas) untuk ISP tenant
    if user_id:
        gw = db.get_wa_gateway(user_id)
        if gw:
            provider = gw.get("wa_provider") or "wuzapi"
            ext_tok = gw.get("wa_token_ext") or ""
            if provider in ("fonnte", "wablas") and ext_tok:
                if db.wa_is_nomor_invalid(nomor, user_id):
                    err = "Nomor tidak valid (ditandai otomatis setelah 3x gagal)"
                    db.add_wa_log(user_id, nomor, tipe, pesan, "skipped", err, ext_tok[:8] + "...")
                    return False, err
                fn = _send_wa_fonnte if provider == "fonnte" else _send_wa_wablas
                ok, err = fn(nomor, pesan, ext_tok)
                status = "sent" if ok else "failed"
                db.add_wa_log(user_id, nomor, tipe, pesan, status, err, f"{provider}:***")
                return ok, err

    # Fallback ke WuzAPI
    if not WA_URL:
        err = "WA_URL tidak dikonfigurasi"
        if user_id:
            db.add_wa_log(user_id, nomor, tipe, pesan, "failed", err, token)
        return False, err
    tok = token or WA_TOKEN
    if not tok:
        err = "Token WA belum dikonfigurasi"
        if user_id:
            db.add_wa_log(user_id, nomor, tipe, pesan, "failed", err, token)
        return False, err
    # Cek nomor yang sudah ditandai tidak valid (≥3x gagal error 463)
    if user_id and db.wa_is_nomor_invalid(nomor, user_id):
        err = "Nomor tidak valid (ditandai otomatis setelah 3x gagal)"
        db.add_wa_log(user_id, nomor, tipe, pesan, "skipped", err, tok)
        return False, err
    try:
        r = requests.post(
            f"{WA_URL}/chat/send/text",
            json={"phone": _normalize_wa(nomor), "body": pesan},
            headers={"Token": tok},
            timeout=8
        )
        data = r.json()
        if data.get("success") or data.get("code") == 200:
            if user_id:
                db.add_wa_log(user_id, nomor, tipe, pesan, "sent", "", tok)
            return True, ""
        err = data.get("message") or str(data)
        # Error 463 = nomor tidak terdaftar/tidak valid di WA
        if user_id and "463" in str(data):
            count = db.wa_catat_gagal_463(nomor, user_id)
            if count >= 3:
                err += f" [ditandai tidak valid setelah {count}x gagal]"
        if user_id:
            db.add_wa_log(user_id, nomor, tipe, pesan, "failed", err, tok)
        return False, err
    except Exception as e:
        err = str(e)
        if user_id:
            db.add_wa_log(user_id, nomor, tipe, pesan, "failed", err, tok)
        return False, err


def _isp_wa_token(user_id: str) -> str:
    """Ambil WA token ISP. Fallback ke token sistem jika belum setup/connected."""
    gw = db.get_wa_gateway(user_id)
    if gw and gw.get("wa_token"):
        s = _wa_session_status(gw["wa_token"])
        if _wa_is_logged_in(s):
            return gw["wa_token"]
    return WA_TOKEN


def _wa_create_user(user_id: str, nama: str) -> str:
    """Pastikan token terdaftar di WuzAPI DB. Return token."""
    token = f"billing_{user_id.lower()}"
    if not WA_USERS_DB or not Path(WA_USERS_DB).exists():
        return token
    try:
        con = sqlite3.connect(WA_USERS_DB)
        # Cek apakah token sudah ada
        exists = con.execute("SELECT id FROM users WHERE token=?", (token,)).fetchone()
        if not exists:
            uid = uuid.uuid4().hex
            con.execute(
                "INSERT INTO users (id,name,token,webhook,events,connected) VALUES (?,?,?,?,?,?)",
                (uid, nama[:50], token, "", "All", 0)
            )
            con.commit()
        con.close()
    except Exception:
        pass
    return token


def _wa_session_status(token: str) -> dict:
    try:
        r = requests.get(f"{WA_URL}/session/status", headers={"Token": token}, timeout=5)
        return r.json().get("data", {})
    except Exception:
        return {}


def _wa_is_logged_in(status: dict) -> bool:
    """Cek apakah WA benar-benar login (bukan sekadar session running)."""
    return bool(status.get("loggedIn") and status.get("jid"))


def _wa_get_qr(token: str) -> str:
    """Start session dan ambil QR code. Return QR base64 atau '' jika sudah login."""
    try:
        # Start/resume session
        requests.post(f"{WA_URL}/session/connect",
                      json={}, headers={"Token": token}, timeout=10)
        # QR ada di status response langsung
        status = _wa_session_status(token)
        if _wa_is_logged_in(status):
            return ""  # sudah login, tidak perlu QR
        # Ambil dari field qrcode di status, atau fallback ke endpoint /session/qr
        qr = status.get("qrcode", "")
        if not qr:
            r2 = requests.get(f"{WA_URL}/session/qr", headers={"Token": token}, timeout=5)
            qr = r2.json().get("data", {}).get("QRCode", "")
        return qr
    except Exception:
        return ""


def _wa_disconnect(token: str):
    try:
        requests.post(f"{WA_URL}/session/disconnect", json={},
                      headers={"Token": token}, timeout=5)
    except Exception:
        pass


def _wa_logout(token: str):
    """Logout dari WhatsApp — hapus linked device, session berikutnya perlu QR baru."""
    try:
        requests.post(f"{WA_URL}/session/logout", json={},
                      headers={"Token": token}, timeout=5)
    except Exception:
        pass


# ── WA Notification Templates ─────────────────────────────────────────────────

_DEFAULT_WA_TEMPLATES: dict[str, str] = {
    "penagihan": (
        "📋 *Tagihan Internet {bulan}*\n\n"
        "Halo *{nama}*,\n\n"
        "Tagihan bulan *{bulan}* sebesar *{nominal}* belum kami terima.\n\n"
        "Mohon segera lakukan pembayaran.\n\n"
        "Terima kasih 🙏\n"
        "_{isp}_"
    ),
    "tagihan_link": (
        "💳 *Tagihan Internet {bulan}*\n\n"
        "Halo *{nama}*,\n\n"
        "Tagihan bulan *{bulan}* sebesar:\n"
        "*{nominal}*\n\n"
        "Bayar sekarang via link berikut:\n"
        "{link}\n\n"
        "_Pembayaran akan dikonfirmasi otomatis._\n"
        "_{isp}_"
    ),
    "pembayaran": (
        "✅ *Pembayaran Diterima!*\n\n"
        "Halo *{nama}*,\n\n"
        "Pembayaran tagihan bulan *{bulan}* sebesar *{nominal}* telah kami terima.\n\n"
        "🧾 Lihat nota pembayaran:\n{link_nota}\n\n"
        "Terima kasih sudah membayar tepat waktu 🙏\n"
        "_{isp}_"
    ),
    "aktivasi": (
        "✅ *Selamat! Layanan Internet Anda Aktif*\n\n"
        "Halo *{nama}*,\n\n"
        "Akun PPPoE Anda telah berhasil didaftarkan.\n\n"
        "📦 Paket: *{paket}*\n"
        "👤 Username: `{username}`\n"
        "🔑 Password: `{password}`\n"
        "📅 Tagihan setiap tgl: *{tgl_bayar}*\n\n"
        "Hubungi kami jika ada kendala.\n"
        "_{isp}_"
    ),
    "reaktivasi": (
        "✅ *Layanan Internet Aktif Kembali*\n\n"
        "Halo *{nama}*,\n\n"
        "Layanan internet Anda telah *diaktifkan kembali*.\n\n"
        "Terima kasih sudah membayar 🙏\n"
        "_{isp}_"
    ),
    "suspend": (
        "⚠️ *Layanan Internet Dinonaktifkan*\n\n"
        "Halo *{nama}*,\n\n"
        "Layanan internet Anda telah *dinonaktifkan sementara*.\n\n"
        "Kemungkinan penyebab: tagihan belum terbayar.\n\n"
        "Hubungi kami untuk informasi lebih lanjut.\n"
        "_{isp}_"
    ),
    "voucher_online": (
        "✅ *Pembayaran Berhasil!*\n\n"
        "Terima kasih sudah berlangganan *{isp}*\n\n"
        "📦 Paket: {paket}\n"
        "⏱ Durasi: {durasi}\n\n"
        "🎟 *Kode Voucher Kamu:*\n\n"
        "  `{kode}`\n\n"
        "Masukkan kode ini di halaman login hotspot WiFi.\n"
        "_{isp}_"
    ),
    "reminder": (
        "{emoji} *Reminder Tagihan Internet*\n\n"
        "Halo *{nama}*,\n\n"
        "{keterangan}\n\n"
        "Jumlah: *{nominal}*\n\n"
        "Bayar sekarang:\n{link}\n\n"
        "_Abaikan jika sudah membayar._\n"
        "_{isp}_"
    ),
}


def _render_wa_template(user_id: str, jenis: str, **vars) -> str:
    """Ambil template tersimpan atau default, lalu substitusi variabel."""
    tmpl = db.get_wa_template(user_id, jenis)
    isi = (tmpl["isi"] if tmpl and tmpl.get("isi") else None) or _DEFAULT_WA_TEMPLATES.get(jenis, "")
    for k, v in vars.items():
        isi = isi.replace("{" + k + "}", str(v) if v is not None else "")
    return isi


# ── Auto Reminder Scheduler ──────────────────────────────────────────────────

def _run_auto_reminder():
    """Jalan tiap hari jam 08:00 WIB — kirim WA reminder + link bayar otomatis.

    Logika jatuh tempo:
    - tgl_bayar + grace_period (per-pelanggan jika diset, else ISP default) = jatuh tempo
    - Reminder dikirim H-3, H-1, H-0, H+1 dari jatuh tempo
    """
    from datetime import date, timedelta
    import calendar

    today = date.today()
    bulan = today.strftime("%Y-%m")
    hari  = today.day

    # Ambil semua ISP yang punya pelanggan PPPoE aktif
    con = db._conn()
    isps = con.execute(
        "SELECT DISTINCT user_id FROM pppoe_users WHERE status='aktif'"
    ).fetchall()
    con.close()

    for isp_row in isps:
        user_id = isp_row[0]
        tok = _isp_wa_token(user_id)
        isp = db.get_user(user_id)
        if not isp:
            continue
        if not isp.get("auto_reminder", 1):
            continue

        isp_grace = isp.get("grace_period") or 10
        isp_nama  = isp.get("nama") or isp.get("username") or ""

        # Generate tagihan untuk pelanggan yang tgl_bayar = hari ini (jika belum ada)
        db.generate_tagihan(user_id, bulan, tgl_bayar=hari)

        # Ambil grace_period per-pelanggan dalam satu query
        con2 = db._conn()
        grace_rows = con2.execute(
            "SELECT id, grace_period FROM pppoe_users WHERE user_id=?", (user_id,)
        ).fetchall()
        con2.close()
        grace_map = {r["id"]: r["grace_period"] for r in grace_rows}

        # Ambil tagihan unpaid/overdue
        tagihan_list = db.list_tagihan(user_id, bulan)
        for t in tagihan_list:
            if t["status"] == "paid":
                continue
            if not t.get("telepon"):
                continue

            tgl_bayar = t.get("tgl_bayar") or 1

            # Grace period: per-pelanggan jika diset, fallback ke ISP
            p_grace = grace_map.get(t["pppoe_id"])
            grace_period = p_grace if p_grace is not None else isp_grace

            # Hitung jatuh tempo = tgl_bayar + grace_period
            year, month = int(bulan[:4]), int(bulan[5:])
            max_day = calendar.monthrange(year, month)[1]
            tgl_bayar_actual = min(tgl_bayar, max_day)

            try:
                tgl_bayar_date   = date(year, month, tgl_bayar_actual)
                jatuh_tempo_date = tgl_bayar_date + timedelta(days=grace_period)
            except ValueError:
                continue

            days_until_due = (jatuh_tempo_date - today).days

            # Kirim reminder H-3, H-1, H-0, H+1
            if days_until_due not in [3, 1, 0, -1]:
                continue

            label          = _label_bulan(bulan)
            link           = f"https://{APP_DOMAIN}/bayar/tagihan/{t['id']}"
            nominal        = f"Rp {t['amount']:,}".replace(",", ".")
            jatuh_tempo_str = jatuh_tempo_date.strftime("%-d %b %Y")

            if days_until_due == 3:
                emoji      = "🔔"
                keterangan = f"Tagihan bulan *{label}* jatuh tempo *3 hari lagi* ({jatuh_tempo_str})."
            elif days_until_due == 1:
                emoji      = "⚠️"
                keterangan = f"Tagihan bulan *{label}* jatuh tempo *besok* ({jatuh_tempo_str})."
            elif days_until_due == 0:
                emoji      = "🚨"
                keterangan = f"Tagihan bulan *{label}* jatuh tempo *hari ini* ({jatuh_tempo_str})."
            else:
                emoji      = "❗"
                keterangan = f"Tagihan bulan *{label}* sudah *melewati jatuh tempo* (sejak {jatuh_tempo_str})."

            pesan = _render_wa_template(
                user_id, "reminder",
                nama=t["nama_pelanggan"], bulan=label, nominal=nominal,
                link=link, jatuh_tempo=jatuh_tempo_str,
                keterangan=keterangan, emoji=emoji, isp=isp_nama,
            )
            send_wa(t["telepon"], pesan, token=tok, user_id=user_id, tipe="reminder")


scheduler.add_job(_run_auto_reminder, CronTrigger(hour=8, minute=0, timezone="Asia/Jakarta"),
                  id="auto_reminder", replace_existing=True)


# ── Auto Generate Tagihan (harian, sesuai tgl_bayar pelanggan) ────────────────

def _run_auto_generate_tagihan():
    """Jalan tiap hari jam 07:00 WIB — generate tagihan untuk pelanggan yang tgl_bayar = hari ini."""
    from datetime import date
    today = date.today()
    bulan = today.strftime("%Y-%m")
    tgl   = today.day
    con = db._conn()
    isps = con.execute("SELECT DISTINCT user_id FROM pppoe_users WHERE status='aktif'").fetchall()
    con.close()
    for isp_row in isps:
        try:
            db.generate_tagihan(isp_row[0], bulan, tgl_bayar=tgl)
        except Exception:
            pass

scheduler.add_job(_run_auto_generate_tagihan,
                  CronTrigger(hour=7, minute=0, timezone="Asia/Jakarta"),
                  id="auto_generate_tagihan", replace_existing=True)


# ── Auto Suspend Overdue ──────────────────────────────────────────────────────

def _run_auto_suspend():
    """Jalan tiap hari jam 10:00 WIB — suspend pelanggan yang tagihan overdue."""
    import logging as _log
    from datetime import date
    today = date.today()
    bulan = today.strftime("%Y-%m")
    hari_ini = today.day
    _log.info(f"[auto_suspend] mulai — {today}")

    # Tandai tagihan overdue (tanpa update pppoe status, agar query berikutnya bisa menemukan aktif)
    con = db._conn()
    isps = con.execute("SELECT DISTINCT user_id FROM pppoe_users WHERE status='aktif'").fetchall()
    con.close()
    total_overdue = 0
    for isp_row in isps:
        n = db.tagihan_overdue(isp_row[0], bulan, hari_ini)
        if n:
            total_overdue += n
            _log.info(f"[auto_suspend] isp={isp_row[0]} overdue_baru={n}")

    # Cari tagihan overdue bulan ini yang pppoe_users-nya masih aktif
    # Filter bulan: hanya proses overdue dari bulan berjalan saja,
    # agar ISP yang re-aktifkan pelanggan tidak langsung di-suspend lagi karena tagihan lama.
    con = db._conn()
    rows = con.execute("""
        SELECT t.id, t.user_id, t.pppoe_id,
               p.username, p.server_id, p.telepon, p.nama_pelanggan
        FROM tagihan_pppoe t
        JOIN pppoe_users p ON p.id = t.pppoe_id
        WHERE t.status = 'overdue' AND p.status = 'aktif'
          AND (p.no_isolir IS NULL OR p.no_isolir = 0)
          AND t.bulan = ?
    """, (bulan,)).fetchall()
    con.close()

    _log.info(f"[auto_suspend] total_overdue_ditandai={total_overdue} akan_disuspend={len(rows)}")

    for r in rows:
        try:
            # Disable di MikroTik
            mt = get_mt(r["server_id"])
            mt_ok = False
            if mt:
                mt_ok = mt.disable_pppoe_secret(r["username"])
            else:
                _log.warning(f"[auto_suspend] MT tidak tersedia server_id={r['server_id']} username={r['username']}")

            # Update status DB (selalu, meski MT gagal — retry saat MT online)
            db.update_pppoe_status(r["pppoe_id"], "suspended")
            _log.info(f"[auto_suspend] suspended username={r['username']} mt_ok={mt_ok}")

            # WA notif
            if r["telepon"]:
                isp = db.get_user(r["user_id"])
                isp_nama = isp["nama"] if isp else ""
                tok = _isp_wa_token(r["user_id"])
                send_wa(
                    r["telepon"],
                    _render_wa_template(r["user_id"], "suspend",
                        nama=r["nama_pelanggan"], isp=isp_nama),
                    token=tok, user_id=r["user_id"], tipe="suspend"
                )
        except Exception as e:
            _log.error(f"[auto_suspend] error username={r.get('username','?')}: {e}")

scheduler.add_job(_run_auto_suspend, CronTrigger(hour=10, minute=0, timezone="Asia/Jakarta"),
                  id="auto_suspend", replace_existing=True)


# ── Auto Generate Tagihan SaaS (tanggal 1 tiap bulan) ────────────────────────

def _run_auto_saas_billing():
    """Tanggal 1 jam 06:00 WIB — generate tagihan SaaS untuk semua tenant aktif."""
    from datetime import date
    import uuid as _uuid
    bulan = date.today().strftime("%Y-%m")
    con = db._conn()
    tarif   = int(con.execute("SELECT value FROM platform_config WHERE key='saas_tarif_pppoe'").fetchone()[0] or 1000)
    minimum = int(con.execute("SELECT value FROM platform_config WHERE key='saas_minimum'").fetchone()[0] or 25000)
    ADDON_HOTSPOT_BULANAN = 20000
    tenants = con.execute("SELECT id, fitur_hotspot_bulanan FROM users WHERE role='admin' AND status='aktif'").fetchall()
    for t in tenants:
        uid, fitur_hb = t[0], (t[1] or 0)
        try:
            existing = con.execute("SELECT id FROM saas_tagihan WHERE user_id=? AND bulan=?", (uid, bulan)).fetchone()
            if existing:
                continue
            n_pppoe = con.execute(
                "SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND status='aktif'", (uid,)
            ).fetchone()[0]
            if n_pppoe == 0 and not fitur_hb:
                continue
            subtotal = n_pppoe * tarif
            addon_hb = ADDON_HOTSPOT_BULANAN if fitur_hb else 0
            total    = max(subtotal, minimum) + addon_hb
            tid = "SAAS-" + _uuid.uuid4().hex[:10].upper()
            con.execute(
                "INSERT INTO saas_tagihan (id,user_id,bulan,jumlah_pppoe,tarif_per_pppoe,subtotal,total,minimum,addon_hotspot_bulanan,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tid, uid, bulan, n_pppoe, tarif, subtotal, total, minimum, addon_hb, "unpaid", int(time.time()))
            )
            isp = con.execute("SELECT nama, wa_number FROM users WHERE id=?", (uid,)).fetchone()
            if isp and isp[1]:
                tok = db.get_platform_config("wa_token") or WA_TOKEN
                addon_info = f"\nAddon Hotspot Bulanan: *Rp {addon_hb:,}*" if addon_hb else ""
                send_wa(isp[1],
                    f"🧾 *Tagihan SaaS {bulan}*\n\n"
                    f"Halo *{isp[0]}*,\n"
                    f"Tagihan sewa platform bulan *{bulan}* telah diterbitkan.\n\n"
                    f"PPPoE Aktif: *{n_pppoe} pelanggan*\n"
                    f"Subtotal PPPoE: *Rp {max(subtotal,minimum):,}*"
                    f"{addon_info}\n"
                    f"Total: *Rp {total:,}*\n\n"
                    f"Silakan bayar via menu Tagihan SaaS sebelum tanggal 10.",
                    token=tok)
        except Exception:
            pass
    con.commit()
    con.close()

scheduler.add_job(_run_auto_saas_billing,
                  CronTrigger(day=1, hour=6, minute=0, timezone="Asia/Jakarta"),
                  id="auto_saas_billing", replace_existing=True)


# ── Auto Suspend Tenant SaaS Overdue (tanggal 10) ────────────────────────────

def _run_auto_saas_suspend():
    """Tanggal 10 jam 08:00 WIB — suspend tenant yang tagihan SaaS belum lunas."""
    from datetime import date
    bulan = date.today().strftime("%Y-%m")
    con = db._conn()
    # Cari tenant dengan tagihan unpaid/waiting_payment bulan ini
    rows = con.execute("""
        SELECT s.id AS tagihan_id, s.user_id, s.total, u.nama, u.wa_number
        FROM saas_tagihan s JOIN users u ON u.id=s.user_id
        WHERE s.bulan=? AND s.status IN ('unpaid','waiting_payment')
        AND u.status='aktif'
    """, (bulan,)).fetchall()
    for r in rows:
        try:
            con.execute("UPDATE users SET status='suspend_saas' WHERE id=?", (r["user_id"],))
            # WA notif ke ISP
            if r["wa_number"]:
                tok = db.get_platform_config("wa_token") or WA_TOKEN
                send_wa(r["wa_number"],
                    f"⚠️ *Akun Disuspend*\n\n"
                    f"Halo *{r['nama']}*,\n"
                    f"Akun Anda disuspend karena tagihan SaaS bulan *{bulan}* "
                    f"sebesar *Rp {r['total']:,}* belum dilunasi.\n\n"
                    f"Silakan lakukan pembayaran untuk mengaktifkan kembali akun.",
                    token=tok)
        except Exception:
            pass
    con.commit()
    con.close()

scheduler.add_job(_run_auto_saas_suspend,
                  CronTrigger(day=10, hour=8, minute=0, timezone="Asia/Jakarta"),
                  id="auto_saas_suspend", replace_existing=True)


# ── PPPoE Online Cache ────────────────────────────────────────────────────────

async def _refresh_pppoe_online():
    """Ambil daftar PPPoE aktif dari semua server MikroTik, simpan ke cache."""
    import sqlite3 as _sq
    con = _sq.connect(str(Path(__file__).parent / "data" / "billing.db"))
    con.row_factory = _sq.Row
    servers = con.execute("SELECT * FROM mikrotik_servers WHERE status='aktif'").fetchall()
    con.close()
    for s in servers:
        try:
            mt = MikroTik(s["vpn_ip"], s["api_port"], s["api_user"], s["api_password"])
            actives = mt.list_pppoe_active()
            usernames = [a.get("name", "") for a in actives if a.get("name")]
            db.update_online_cache(s["id"], usernames)
        except Exception:
            pass

scheduler.add_job(_refresh_pppoe_online, "interval", minutes=15,
                  id="pppoe_online_refresh", replace_existing=True)


# ── Retry MT push untuk voucher yang gagal saat MikroTik down ────────────────

async def _retry_voucher_mt_push():
    """Coba push ulang voucher yang belum masuk ke MikroTik (mt_pushed=0)."""
    import logging as _log
    pending = db.vouchers_pending_mt_push()
    if not pending:
        return
    _log.warning(f"[MT retry] {len(pending)} voucher pending MT push")
    for v in pending:
        try:
            server = db.get_server(v["server_id"])
            if not server:
                continue
            paket = db.get_paket_hotspot(v["paket_id"])
            mt = MikroTik(server["vpn_ip"], server["api_port"], server["api_user"], server["api_password"])
            profile      = (paket or {}).get("kecepatan") or "default"
            comment      = (paket or {}).get("nama", "")
            limit_uptime = (paket or {}).get("durasi") or ""
            ok = mt.add_hotspot_user(v["kode"], v["kode"], profile=profile,
                                     comment=comment, limit_uptime=limit_uptime)
            if ok:
                db.set_voucher_mt_pushed(v["kode"], pushed=True)
                _log.warning(f"[MT retry] OK: {v['kode']}")
            else:
                _log.warning(f"[MT retry] masih gagal: {v['kode']}")
        except Exception as e:
            _log.warning(f"[MT retry] error {v['kode']}: {e}")

scheduler.add_job(_retry_voucher_mt_push, "interval", minutes=5,
                  id="voucher_mt_retry", replace_existing=True)


# ── Sync status voucher dengan MikroTik ──────────────────────────────────────

async def _sync_voucher_status():
    """Sinkronisasi status voucher (terjual/dipakai) dengan kondisi nyata di MikroTik.

    Logika per voucher:
    - Ada di MT, uptime = 0s  → terjual   (belum dipakai pelanggan)
    - Ada di MT, uptime > 0s  → dipakai   (pelanggan sedang/sudah pakai)
    - Tidak ada di MT         → expired   (sudah habis, MT auto-hapus)
    """
    import logging as _log
    from collections import defaultdict

    vouchers = db.vouchers_active_for_sync()
    if not vouchers:
        return

    # Group per server agar hanya konek sekali per router
    by_server = defaultdict(list)
    for v in vouchers:
        by_server[v["server_id"]].append(v)

    for sid, vlist in by_server.items():
        s = vlist[0]
        try:
            mt = MikroTik(s["vpn_ip"], int(s["api_port"]), s["api_user"], s["api_password"])
            api = mt.api
            # Ambil semua hotspot user sekaligus (lebih efisien dari query satu-satu)
            all_hs = api.get_resource("/ip/hotspot/user").get()
            mt_users = {u.get("name", ""): u for u in all_hs}
        except Exception as e:
            _log.warning(f"[voucher sync] server {sid} tidak bisa diakses: {e}")
            continue

        for v in vlist:
            kode = v["kode"]
            try:
                if kode not in mt_users:
                    # Tidak ada di MT → sudah habis/expired
                    db.mark_voucher_expired(kode)
                    _log.warning(f"[voucher sync] {kode} tidak ada di MT → expired")
                else:
                    uptime = mt_users[kode].get("uptime", "0s")
                    if uptime and uptime != "0s":
                        if v["status"] == "terjual":
                            db.mark_voucher_dipakai(kode)
                            _log.warning(f"[voucher sync] {kode} uptime={uptime} → dipakai")
                    # uptime=0s dan ada di MT: status terjual, tidak perlu ubah
            except Exception as e:
                _log.warning(f"[voucher sync] error {kode}: {e}")

scheduler.add_job(_sync_voucher_status, "interval", minutes=10,
                  id="voucher_sync_status", replace_existing=True)


# ── MikroTik helper ───────────────────────────────────────────────────────────

def get_mt(server_id: str) -> MikroTik | None:
    s = db.get_server(server_id)
    if not s:
        return None
    # Support format "host:port" di field vpn_ip
    vpn_ip = s["vpn_ip"]
    api_port = s["api_port"]
    if ":" in vpn_ip:
        parts = vpn_ip.rsplit(":", 1)
        vpn_ip = parts[0]
        try:
            api_port = int(parts[1])
        except ValueError:
            pass
    return MikroTik(vpn_ip, api_port, s["api_user"], s["api_password"])

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = current_user(request)
    if user:
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return tpl.TemplateResponse(request, "login.html", _ctx(request))


@app.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    user = db.login(username, password)
    if not user:
        return tpl.TemplateResponse(request, "login.html", _ctx(request, error="Username atau password salah"))
    dest = _login_dest(user)
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie("session", make_session(user["id"]), httponly=True, max_age=86400 * 7)
    _log(request, user, "Login", f"Login sebagai {user['role']}")
    return resp


def _login_dest(user: dict) -> str:
    """Tentukan URL tujuan setelah login berdasarkan role."""
    if user["role"] in ("agen", "sub_agen"):
        return "/panel"
    if user["role"] == "teknisi":
        return "/pppoe/users"
    return "/dashboard"


# ── Login via WhatsApp PIN ────────────────────────────────────────────────────

@app.post("/login/wa/kirim", response_class=JSONResponse)
async def login_wa_kirim(request: Request, nomor_wa: str = Form(...)):
    nomor = nomor_wa.strip().replace("-", "").replace(" ", "")
    if nomor.startswith("0"):
        nomor = "62" + nomor[1:]
    user = db.get_user_by_wa(nomor)
    if not user:
        return JSONResponse({"ok": False, "msg": "Nomor WhatsApp tidak terdaftar."})
    otp = str(random.randint(100000, 999999))
    db.create_otp(user["id"], otp, ttl=300)
    send_wa(nomor,
        f"🔐 *Kode Login VPNTunel Billing*\n\n"
        f"Halo {user['nama']},\n\n"
        f"Kode PIN login kamu:\n\n"
        f"  *{otp}*\n\n"
        f"Berlaku 5 menit. Jangan berikan ke siapapun."
    )
    return JSONResponse({"ok": True, "msg": "PIN dikirim ke WhatsApp kamu."})


@app.post("/login/wa/verifikasi")
async def login_wa_verifikasi(request: Request, nomor_wa: str = Form(...), otp: str = Form(...)):
    nomor = nomor_wa.strip().replace("-", "").replace(" ", "")
    if nomor.startswith("0"):
        nomor = "62" + nomor[1:]
    user = db.get_user_by_wa(nomor)
    if not user:
        return tpl.TemplateResponse(request, "login.html", _ctx(request, error="Nomor tidak terdaftar.", tab="wa"))
    if not db.verify_otp(user["id"], otp.strip()):
        return tpl.TemplateResponse(request, "login.html", _ctx(request, error="PIN salah atau sudah kedaluwarsa.", tab="wa", nomor_wa=nomor_wa))
    dest = _login_dest(user)
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie("session", make_session(user["id"]), httponly=True, max_age=86400 * 7)
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("session")
    return resp


@app.get("/panel/logout")
async def panel_logout(request: Request):
    user = current_user_agen(request)
    isp = db.get_user(user["parent_id"]) if user and user.get("parent_id") else None
    slug = isp.get("slug") if isp else None
    dest = f"/beli/{slug}/login" if slug else "/login"
    resp = RedirectResponse(dest, status_code=302)
    resp.delete_cookie("agen_session")
    return resp

# ── Profil / Slug Toko ───────────────────────────────────────────────────────

import re as _re

def _to_slug(s: str) -> str:
    s = s.lower().strip()
    s = _re.sub(r"[^\w\s-]", "", s)
    s = _re.sub(r"[\s_]+", "-", s)
    s = _re.sub(r"-+", "-", s).strip("-")
    return s[:50]


@app.get("/profil", response_class=HTMLResponse)
async def profil_page(request: Request):
    user = require_login(request)
    # Re-read fee_mode dari DB (sessions cache mungkin stale)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT fee_mode, auto_reminder, grace_period, fitur_hotspot_bulanan FROM users WHERE id=?", (user["id"],)).fetchone()
    con.close()
    row_dict = dict(row) if row else {}
    fee_mode = row_dict.get("fee_mode") or "customer"
    auto_reminder = row_dict.get("auto_reminder")
    if auto_reminder is None:
        auto_reminder = 1
    grace_period = row_dict.get("grace_period")
    if grace_period is None:
        grace_period = 10
    fitur_hotspot_bulanan = row_dict.get("fitur_hotspot_bulanan") or 0
    # Read platform fee config
    try:
        pf_small  = int(db.get_platform_config("platform_fee_small") or 300)
        pf_large  = int(db.get_platform_config("platform_fee_large") or 700)
        pf_thresh = int(db.get_platform_config("platform_fee_threshold") or 50000)
    except Exception:
        pf_small, pf_large, pf_thresh = 300, 700, 50000
    return tpl.TemplateResponse(request, "profil.html", _ctx(
        request, user=user, active="profil",
        fee_mode=fee_mode, pf_small=pf_small, pf_large=pf_large, pf_thresh=pf_thresh,
        mayar_fee_percent=MAYAR_FEE_PERCENT, auto_reminder=auto_reminder,
        grace_period=grace_period, fitur_hotspot_bulanan=fitur_hotspot_bulanan,
    ))


@app.post("/profil/update", response_class=JSONResponse)
async def profil_update(request: Request):
    user = require_login(request)
    body = await request.json()
    nama      = (body.get("nama") or "").strip()
    nomor_wa  = (body.get("nomor_wa") or "").strip()
    password  = (body.get("password") or "").strip()
    password2 = (body.get("password2") or "").strip()

    if not nama:
        return JSONResponse({"ok": False, "detail": "Nama tidak boleh kosong"})

    pw_hash = None
    if password:
        if password != password2:
            return JSONResponse({"ok": False, "detail": "Konfirmasi password tidak cocok"})
        if len(password) < 6:
            return JSONResponse({"ok": False, "detail": "Password minimal 6 karakter"})
        pw_hash = hashlib.sha256(password.encode()).hexdigest()

    db.update_profil(user["id"], nama, nomor_wa, pw_hash)

    # Simpan rekening jika admin
    rek_bank = (body.get("rek_bank") or "").strip()
    rek_no   = (body.get("rek_no") or "").strip()
    rek_nama = (body.get("rek_nama") or "").strip()
    if user["role"] == "admin" and rek_bank and rek_no:
        db.save_rekening(user["id"], rek_bank, rek_no, rek_nama)

    # Update session
    sess = sessions.get(request.cookies.get("sid", ""))
    if sess:
        sess["nama"]     = nama
        sess["nomor_wa"] = nomor_wa

    return JSONResponse({"ok": True})


@app.post("/profil/reminder-toggle", response_class=JSONResponse)
async def toggle_auto_reminder(request: Request):
    user = require_login(request)
    if user["role"] != "admin":
        return JSONResponse({"ok": False, "msg": "Hanya admin ISP yang bisa ubah"})
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT auto_reminder FROM users WHERE id=?", (user["id"],)).fetchone()
    current = row[0] if row else 1
    if current is None:
        current = 1
    new_val = 0 if current else 1
    con.execute("UPDATE users SET auto_reminder=? WHERE id=?", (new_val, user["id"]))
    con.commit(); con.close()
    return JSONResponse({"ok": True, "auto_reminder": new_val})


@app.post("/profil/grace-period", response_class=JSONResponse)
async def update_grace_period(request: Request):
    user = require_login(request)
    if user["role"] != "admin":
        return JSONResponse({"ok": False, "msg": "Hanya admin ISP yang bisa ubah"})
    body = await request.json()
    try:
        gp = int(body.get("grace_period", 10))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "msg": "Nilai tidak valid"})
    gp = max(1, min(gp, 30))
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE users SET grace_period=? WHERE id=?", (gp, user["id"]))
    con.commit(); con.close()
    return JSONResponse({"ok": True, "grace_period": gp})


@app.post("/profil/fitur-hotspot-bulanan", response_class=JSONResponse)
async def toggle_fitur_hotspot_bulanan(request: Request):
    user = require_login(request)
    if user["role"] != "admin":
        return JSONResponse({"ok": False, "msg": "Hanya admin ISP yang bisa ubah"})
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT fitur_hotspot_bulanan FROM users WHERE id=?", (user["id"],)).fetchone()
    current = (row[0] or 0) if row else 0
    new_val = 0 if current else 1
    con.execute("UPDATE users SET fitur_hotspot_bulanan=? WHERE id=?", (new_val, user["id"]))
    con.commit(); con.close()
    return JSONResponse({"ok": True, "aktif": new_val})


@app.post("/profil/fee-mode", response_class=JSONResponse)
async def update_fee_mode(request: Request, fee_mode: str = Form(...)):
    user = require_login(request)
    if user["role"] != "admin":
        return JSONResponse({"ok": False, "msg": "Hanya admin ISP yang bisa ubah"})
    fee_mode = (fee_mode or "").strip().lower()
    if fee_mode not in ("customer", "tenant"):
        return JSONResponse({"ok": False, "msg": "Mode tidak valid (customer atau tenant)"})
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE users SET fee_mode=? WHERE id=?", (fee_mode, user["id"]))
    con.commit(); con.close()
    return JSONResponse({"ok": True, "fee_mode": fee_mode})


@app.post("/profil/slug")
async def update_slug(request: Request, slug: str = Form(...)):
    user = require_login(request)
    slug = _to_slug(slug)
    if not slug:
        return RedirectResponse("/dashboard?error=slug_kosong", status_code=302)
    if db.slug_exists(slug, exclude_uid=user["id"]):
        return RedirectResponse("/dashboard?error=slug_taken", status_code=302)
    db.update_slug(user["id"], slug)
    return RedirectResponse("/dashboard?ok=slug_updated", status_code=302)


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    from datetime import date
    user  = require_login(request)
    stats = db.stats(user["id"], user["role"])
    iid   = _isp_id(user)
    today = date.today()
    tahun = str(today.year)
    bulan_ini = today.strftime("%Y-%m")

    import calendar as _cal
    import time as _time

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Migration: tambah kolom baru jika belum ada
    for col_sql in [
        "ALTER TABLE pppoe_users ADD COLUMN isolated_at INTEGER DEFAULT NULL",
        "ALTER TABLE pppoe_users ADD COLUMN terpasang INTEGER DEFAULT 0",
    ]:
        try:
            con.execute(col_sql)
            con.commit()
        except Exception:
            pass
    # Backfill terpasang: pelanggan aktif atau sudah pernah di-push = sudah terpasang
    con.execute("UPDATE pppoe_users SET terpasang=1 WHERE (status='aktif' OR mt_pushed=1) AND terpasang=0")
    con.commit()

    transaksi_terbaru = [dict(r) for r in con.execute(
        "SELECT amount, keterangan, created_at FROM transaksi WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
        (iid,)
    ).fetchall()]
    tagihan_pending = con.execute(
        "SELECT COUNT(*) FROM tagihan_pppoe WHERE user_id=? AND status='unpaid'", (iid,)
    ).fetchone()[0]

    # PPPoE stats lengkap
    pppoe_total     = con.execute("SELECT COUNT(*) FROM pppoe_users WHERE user_id=?", (iid,)).fetchone()[0]
    pppoe_aktif     = con.execute("SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND status='aktif'", (iid,)).fetchone()[0]
    pppoe_suspended = con.execute("SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND status='suspended'", (iid,)).fetchone()[0]
    pppoe_nonaktif  = con.execute("SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND status='nonaktif'", (iid,)).fetchone()[0]

    # Pelanggan baru bulan ini + timestamps
    y, m = today.year, today.month
    ts_start = int(_time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1)))
    last_day = _cal.monthrange(y, m)[1]
    ts_end   = int(_time.mktime((y, m, last_day, 23, 59, 59, 0, 0, -1)))
    ts_today_start = int(_time.mktime((y, m, today.day, 0, 0, 0, 0, 0, -1)))
    ts_today_end   = int(_time.mktime((y, m, today.day, 23, 59, 59, 0, 0, -1)))

    pppoe_baru_bln = con.execute(
        "SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND created_at BETWEEN ? AND ?",
        (iid, ts_start, ts_end)
    ).fetchone()[0]

    # Card stats baru (sesuai referensi gambar)
    blm_terpasang = con.execute(
        "SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND terpasang=0", (iid,)
    ).fetchone()[0]
    blm_active = con.execute(
        "SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND terpasang=1 AND status='nonaktif'", (iid,)
    ).fetchone()[0]
    bayar_hari_ini = con.execute(
        "SELECT COUNT(*) FROM tagihan_pppoe WHERE user_id=? AND paid_at BETWEEN ? AND ?",
        (iid, ts_today_start, ts_today_end)
    ).fetchone()[0]
    sudah_bayar = con.execute(
        "SELECT COUNT(*) FROM tagihan_pppoe WHERE user_id=? AND bulan=? AND status IN ('paid','lunas')",
        (iid, bulan_ini)
    ).fetchone()[0]
    belum_ada_tagihan = con.execute(
        "SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND status='aktif' "
        "AND id NOT IN (SELECT pppoe_id FROM tagihan_pppoe WHERE user_id=? AND bulan=?)",
        (iid, iid, bulan_ini)
    ).fetchone()[0]
    belum_bayar = con.execute(
        "SELECT COUNT(*) FROM tagihan_pppoe WHERE user_id=? AND bulan=? AND status IN ('unpaid','overdue')",
        (iid, bulan_ini)
    ).fetchone()[0]
    isolir_hari_ini = con.execute(
        "SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND isolated_at BETWEEN ? AND ?",
        (iid, ts_today_start, ts_today_end)
    ).fetchone()[0]

    # Chart data: pendaftaran per hari bulan ini
    reg_rows = con.execute(
        "SELECT DATE(created_at,'unixepoch','localtime') as tgl, COUNT(*) as n "
        "FROM pppoe_users WHERE user_id=? AND created_at BETWEEN ? AND ? "
        "GROUP BY tgl ORDER BY tgl",
        (iid, ts_start, ts_end)
    ).fetchall()
    # Fill all days of month
    import datetime as _dt
    reg_map = {r["tgl"]: r["n"] for r in reg_rows}
    reg_labels = []
    reg_data   = []
    for d in range(1, last_day + 1):
        tgl = f"{y:04d}-{m:02d}-{d:02d}"
        reg_labels.append(str(d))
        reg_data.append(reg_map.get(tgl, 0))

    con.close()

    # Laporan data
    bulanan      = db.laporan_pendapatan(iid, tahun)
    pel_baru     = db.laporan_pelanggan_baru(iid, tahun)
    stats_bln    = db.stats_tagihan(iid, bulan_ini)
    total_tahun  = sum(b["total"] for b in bulanan)

    pppoe_stats = {
        "total": pppoe_total, "aktif": pppoe_aktif,
        "suspended": pppoe_suspended, "nonaktif": pppoe_nonaktif,
        "baru_bln": pppoe_baru_bln,
    }
    pelanggan_cards = {
        "total":             pppoe_total,
        "blm_terpasang":     blm_terpasang,
        "blm_active":        blm_active,
        "bayar_hari_ini":    bayar_hari_ini,
        "sudah_bayar":       sudah_bayar,
        "belum_ada_tagihan": belum_ada_tagihan,
        "belum_bayar":       belum_bayar,
        "suspended":         pppoe_suspended,
        "isolir_hari_ini":   isolir_hari_ini,
    }

    return tpl.TemplateResponse(request, "dashboard.html", _ctx(
        request, user=user, stats=stats,
        transaksi_terbaru=transaksi_terbaru,
        total_pppoe=pppoe_total,
        tagihan_pending=tagihan_pending,
        pppoe_stats=pppoe_stats,
        pelanggan_cards=pelanggan_cards,
        reg_labels=reg_labels,
        reg_data=reg_data,
        bulan_ini=bulan_ini,
        bulanan=bulanan, pel_baru=pel_baru,
        stats_bln=stats_bln, total_tahun=total_tahun,
        tahun=tahun,
    ))


@app.get("/bantuan", response_class=HTMLResponse)
async def bantuan_page(request: Request):
    user = require_login(request)
    return tpl.TemplateResponse(request, "bantuan.html", _ctx(request, user=user))


@app.get("/laporan", response_class=HTMLResponse)
async def laporan_page(request: Request):
    return RedirectResponse("/dashboard", status_code=302)

@app.get("/laporan-detail", response_class=HTMLResponse)
async def laporan_detail_page(request: Request, tahun: str = "", mode: str = "bulanan", bulan: str = "", metode: str = ""):
    user = require_login(request)
    from datetime import date
    today = date.today()
    if not tahun:
        tahun = str(today.year)
    if not bulan:
        bulan = today.strftime("%Y-%m")
    tahun_list = [str(today.year - i) for i in range(3)]
    bulan_list = []
    for y in range(today.year, today.year - 2, -1):
        for m in range(12, 0, -1):
            bulan_list.append(f"{y}-{str(m).zfill(2)}")
    iid = _isp_id(user)
    bulanan         = db.laporan_pendapatan(iid, tahun)
    pelanggan       = db.laporan_pelanggan_baru(iid, tahun)
    topup_manual    = db.laporan_topup_agen(iid, tahun)
    pendapatan_agen = db.laporan_pendapatan_agen(iid, tahun)
    harian          = db.laporan_pendapatan_harian(iid, bulan) if mode == "harian" else []
    riwayat         = db.laporan_riwayat_bayar(iid, bulan, metode) if mode == "riwayat" else []
    bulan_ini = today.strftime("%Y-%m")
    stats_bln = db.stats_tagihan(iid, bulan_ini)
    total_tahun        = sum(b["total"] for b in bulanan)
    total_topup_manual = sum(t["total"] for t in topup_manual)
    total_pend_agen    = sum(t["total"] for t in pendapatan_agen)
    total_semua        = total_tahun + total_topup_manual
    total_harian       = sum(h["total"] for h in harian)
    total_riwayat      = sum(r["amount"] for r in riwayat)
    return tpl.TemplateResponse(request, "laporan.html", _ctx(
        request, user=user, active="laporan",
        tahun=tahun, tahun_list=tahun_list,
        bulan=bulan, bulan_list=bulan_list,
        mode=mode, metode=metode,
        bulanan=bulanan, pelanggan=pelanggan,
        topup_manual=topup_manual, total_topup_manual=total_topup_manual,
        pendapatan_agen=pendapatan_agen, total_pend_agen=total_pend_agen,
        stats_bln=stats_bln, total_tahun=total_tahun, total_semua=total_semua,
        harian=harian, total_harian=total_harian,
        riwayat=riwayat, total_riwayat=total_riwayat,
    ))

# ── MikroTik Servers ──────────────────────────────────────────────────────────

@app.get("/servers", response_class=HTMLResponse)
async def servers_page(request: Request):
    user = require_login(request)
    servers = db.list_servers(user["id"])
    return tpl.TemplateResponse(request, "servers.html", _ctx(request, user=user, servers=servers))


@app.post("/servers/tambah")
async def server_tambah(
    request: Request,
    nama: str = Form(...), vpn_ip: str = Form(...),
    api_port: int = Form(8728), api_user: str = Form("admin"),
    api_password: str = Form(""), lokasi: str = Form(""),
):
    user = require_login(request)
    db.create_server(user["id"], nama, vpn_ip, api_port, api_user, api_password, lokasi)
    return RedirectResponse("/servers", status_code=302)


@app.post("/servers/edit/{sid}")
async def server_edit(
    request: Request, sid: str,
    nama: str = Form(...), vpn_ip: str = Form(...),
    api_port: int = Form(8728), api_user: str = Form("admin"),
    api_password: str = Form(""), lokasi: str = Form(""),
):
    user = require_login(request)
    s = db.get_server(sid)
    if s and (s["user_id"] == user["id"] or user["role"] == "admin"):
        db.update_server(sid, nama, vpn_ip, api_port, api_user, api_password, lokasi)
    return RedirectResponse("/servers", status_code=302)


@app.post("/servers/hapus/{sid}")
async def server_hapus(request: Request, sid: str):
    user = require_login(request)
    s = db.get_server(sid)
    if s and (s["user_id"] == user["id"] or user["role"] == "admin"):
        db.delete_server(sid)
    return RedirectResponse("/servers", status_code=302)


@app.get("/servers/ping/{sid}")
async def server_ping(request: Request, sid: str):
    require_login(request)
    mt = get_mt(sid)
    if mt and mt.ping():
        db.update_server_ping(sid)
        return JSONResponse({"ok": True, "identity": mt.get_identity()})
    return JSONResponse({"ok": False})

# ── PPPoE Paket ───────────────────────────────────────────────────────────────

@app.get("/pppoe/paket", response_class=HTMLResponse)
async def pppoe_paket(request: Request):
    user = require_login(request)
    pakets = db.list_paket_pppoe(user["id"])
    servers = db.list_servers(user["id"])
    return tpl.TemplateResponse(request, "pppoe_paket.html", _ctx(request, user=user, pakets=pakets, servers=servers))


@app.get("/pppoe/paket/profiles")
async def pppoe_paket_profiles(request: Request, server_id: str):
    user = require_login(request)
    s = db.get_server(server_id)
    if not s or s["user_id"] != user["id"]:
        return JSONResponse({"error": "Server tidak ditemukan"}, status_code=404)
    mt = get_mt(server_id)
    if not mt:
        return JSONResponse({"error": "Tidak dapat terhubung ke router"}, status_code=500)
    profiles = mt.list_pppoe_profiles_detail()
    return JSONResponse({"profiles": profiles})


@app.post("/pppoe/paket/import", response_class=JSONResponse)
async def pppoe_paket_import(request: Request):
    """Import beberapa profil PPPoE MikroTik sekaligus menjadi paket."""
    user = require_login(request)
    body  = await request.json()
    pakets = body.get("pakets", [])
    existing = {p["nama"].lower() for p in db.list_paket_pppoe(user["id"])}
    dibuat = 0
    for p in pakets:
        nama = (p.get("nama") or "").strip()
        if not nama or nama.lower() in existing:
            continue
        db.create_paket_pppoe(
            user["id"], nama,
            p.get("kecepatan") or "",
            int(p.get("harga") or 0)
        )
        existing.add(nama.lower())
        dibuat += 1
    return JSONResponse({"ok": True, "dibuat": dibuat})


@app.post("/pppoe/paket/tambah")
async def pppoe_paket_tambah(
    request: Request,
    nama: str = Form(...), kecepatan: str = Form(...), harga: int = Form(...)
):
    user = require_login(request)
    db.create_paket_pppoe(user["id"], nama, kecepatan, harga)
    return RedirectResponse("/pppoe/paket", status_code=302)


@app.post("/pppoe/paket/{pid}/edit", response_class=JSONResponse)
async def pppoe_paket_edit(request: Request, pid: int):
    user = require_login(request)
    body = await request.json()
    nama      = (body.get("nama") or "").strip()
    kecepatan = (body.get("kecepatan") or "").strip()
    harga     = int(body.get("harga") or 0)
    if not nama or not kecepatan or harga <= 0:
        return JSONResponse({"ok": False, "msg": "Nama, profile, dan harga wajib diisi"})
    p = db.get_paket_pppoe(pid)
    if not p or p["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "msg": "Paket tidak ditemukan"}, status_code=404)
    db.update_paket_pppoe(pid, user["id"], nama, kecepatan, harga)
    return JSONResponse({"ok": True})


@app.post("/pppoe/paket/{pid}/hapus", response_class=JSONResponse)
async def pppoe_paket_hapus(request: Request, pid: int):
    user = require_login(request)
    p = db.get_paket_pppoe(pid)
    if not p or p["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "msg": "Paket tidak ditemukan"}, status_code=404)
    db.delete_paket_pppoe(pid, user["id"])
    return JSONResponse({"ok": True})


# ── ODP ───────────────────────────────────────────────────────────────────────

@app.get("/odp", response_class=HTMLResponse)
async def odp_page(request: Request, server_id: str = ""):
    user = require_login(request)
    iid = _isp_id(user)
    servers  = db.list_servers(iid)
    odp_list = db.list_odp(iid, server_id if server_id else None)
    odc_list = db.list_odc(iid, server_id if server_id else None)
    odc_map  = {o["id"]: o for o in odc_list}
    for o in odp_list:
        if o.get("odc_id") and o.get("tap_tipe"):
            odc = odc_map.get(o["odc_id"])
            o["daya_odp_dbm"] = db.def_odp_power(odc, o["tap_tipe"]) if odc else None
        else:
            o["daya_odp_dbm"] = None
    return tpl.TemplateResponse(request, "odp.html", _ctx(
        request, user=user, odp_list=odp_list, odc_list=odc_list,
        servers=servers, sel_server=server_id))

@app.post("/odc/tambah")
async def odc_tambah(request: Request, server_id: str = Form(...), nama: str = Form(...),
                     lokasi: str = Form(""), tipe_splitter: str = Form("1:4"),
                     daya_masuk_dbm: str = Form("-3"),
                     lat: str = Form(""), lng: str = Form("")):
    user = require_login(request)
    iid = _isp_id(user)
    db.create_odc(iid, server_id, nama.strip(), lokasi.strip(), tipe_splitter,
                  float(daya_masuk_dbm or -3),
                  float(lat) if lat.strip() else None,
                  float(lng) if lng.strip() else None)
    return RedirectResponse("/odp", status_code=303)

@app.post("/odc/edit/{odc_id}")
async def odc_edit(request: Request, odc_id: int, server_id: str = Form(...),
                   nama: str = Form(...), lokasi: str = Form(""),
                   tipe_splitter: str = Form("1:4"), daya_masuk_dbm: str = Form("-3"),
                   lat: str = Form(""), lng: str = Form("")):
    user = require_login(request)
    db.update_odc(odc_id, server_id, nama.strip(), lokasi.strip(), tipe_splitter,
                  float(daya_masuk_dbm or -3),
                  float(lat) if lat.strip() else None,
                  float(lng) if lng.strip() else None)
    return RedirectResponse("/odp", status_code=303)

@app.post("/odc/hapus/{odc_id}")
async def odc_hapus(request: Request, odc_id: int):
    user = require_login(request)
    db.delete_odc(odc_id)
    return RedirectResponse("/odp", status_code=303)

@app.post("/odp/tambah")
async def odp_tambah(request: Request, server_id: str = Form(...), nama: str = Form(...),
                     lokasi: str = Form(""), kapasitas: int = Form(8),
                     lat: str = Form(""), lng: str = Form(""),
                     odc_id: str = Form(""), tap_tipe: str = Form("")):
    user = require_login(request)
    iid = _isp_id(user)
    _lat = float(lat) if lat.strip() else None
    _lng = float(lng) if lng.strip() else None
    _odc = int(odc_id) if odc_id.strip() else None
    db.create_odp(iid, server_id, nama.strip(), lokasi.strip(), kapasitas, _lat, _lng, _odc, tap_tipe)
    return RedirectResponse("/odp", status_code=303)

@app.post("/odp/edit/{odp_id}")
async def odp_edit(request: Request, odp_id: int, server_id: str = Form(...),
                   nama: str = Form(...), lokasi: str = Form(""), kapasitas: int = Form(8),
                   lat: str = Form(""), lng: str = Form(""),
                   odc_id: str = Form(""), tap_tipe: str = Form("")):
    user = require_login(request)
    _lat = float(lat) if lat.strip() else None
    _lng = float(lng) if lng.strip() else None
    _odc = int(odc_id) if odc_id.strip() else None
    db.update_odp(odp_id, server_id, nama.strip(), lokasi.strip(), kapasitas, _lat, _lng, _odc, tap_tipe)
    return RedirectResponse("/odp", status_code=303)

@app.get("/odp/{odp_id}/ports", response_class=JSONResponse)
async def odp_ports(request: Request, odp_id: int):
    user = require_login(request)
    iid = _isp_id(user)
    pelanggan = db.list_pppoe_by_odp(iid, odp_id)
    port_map = {p["odp_port"]: {"nama": p["nama_pelanggan"], "username": p["username"], "id": p["id"]} for p in pelanggan if p.get("odp_port")}
    return JSONResponse({"ok": True, "ports": port_map})

@app.post("/odp/hapus/{odp_id}")
async def odp_hapus(request: Request, odp_id: int):
    user = require_login(request)
    db.delete_odp(odp_id)
    return RedirectResponse("/odp", status_code=303)

@app.post("/odp/assign/{pppoe_id}")
async def odp_assign(request: Request, pppoe_id: int, odp_id: str = Form(""), odp_port: str = Form("")):
    user = require_login(request)
    _odp_id = int(odp_id) if odp_id else None
    _odp_port = int(odp_port) if odp_port else None
    db.assign_odp(pppoe_id, _odp_id, _odp_port)
    return RedirectResponse("/pppoe/users", status_code=303)

@app.get("/kalkulator-odp", response_class=HTMLResponse)
async def kalkulator_odp(request: Request):
    user = require_login(request)
    return tpl.TemplateResponse(request, "kalkulator_odp.html", _ctx(request, user=user))

# ── PPPoE Users ───────────────────────────────────────────────────────────────

@app.get("/pppoe/users", response_class=HTMLResponse)
async def pppoe_users(request: Request, server_id: str = "", status: str = "", q: str = "",
                       odp_id: str = "", page: int = 1, per_page: int = 50,
                       terpasang: str = "", tagihan: str = ""):
    user = require_login(request)
    iid = _isp_id(user)
    servers = db.list_servers(iid)
    odp_list = db.list_odp(iid)
    odp_filter_list = db.list_odp(iid, server_id if server_id else None)
    _odp_id = int(odp_id) if odp_id else None
    _sid = server_id if server_id else None
    _status = status if status and status != "semua" else None
    per_page = max(10, min(per_page, 200))
    page = max(1, page)

    # Resolve terpasang filter
    _terpasang = None
    if terpasang == "0":
        _terpasang = 0
    elif terpasang == "1":
        _terpasang = 1

    # Resolve tagihan-based filter → list of pppoe_ids
    import datetime as _dt
    bulan_ini = _dt.date.today().strftime("%Y-%m")
    _pppoe_ids = None
    _tagihan_label = ""
    if tagihan:
        con_t = sqlite3.connect(DB_PATH)
        con_t.row_factory = sqlite3.Row
        _today = _dt.date.today()
        _ts_start = int(_dt.datetime(_today.year, _today.month, _today.day, 0, 0, 0).timestamp())
        _ts_end   = int(_dt.datetime(_today.year, _today.month, _today.day, 23, 59, 59).timestamp())
        if tagihan == "bayar_hari_ini":
            rows = con_t.execute(
                "SELECT DISTINCT pppoe_id FROM tagihan_pppoe WHERE user_id=? AND paid_at BETWEEN ? AND ?",
                (iid, _ts_start, _ts_end)
            ).fetchall()
            _pppoe_ids = [r["pppoe_id"] for r in rows]
            _tagihan_label = "Bayar Hari Ini"
        elif tagihan == "sudah_bayar":
            rows = con_t.execute(
                "SELECT DISTINCT pppoe_id FROM tagihan_pppoe WHERE user_id=? AND bulan=? AND status IN ('paid','lunas')",
                (iid, bulan_ini)
            ).fetchall()
            _pppoe_ids = [r["pppoe_id"] for r in rows]
            _tagihan_label = "Sudah Bayar"
        elif tagihan == "belum_bayar":
            rows = con_t.execute(
                "SELECT DISTINCT pppoe_id FROM tagihan_pppoe WHERE user_id=? AND bulan=? AND status IN ('unpaid','overdue')",
                (iid, bulan_ini)
            ).fetchall()
            _pppoe_ids = [r["pppoe_id"] for r in rows]
            _tagihan_label = "Belum Bayar"
        elif tagihan == "belum_ada_tagihan":
            all_ids = [r["id"] for r in con_t.execute(
                "SELECT id FROM pppoe_users WHERE user_id=? AND status='aktif'", (iid,)
            ).fetchall()]
            billed = {r["pppoe_id"] for r in con_t.execute(
                "SELECT DISTINCT pppoe_id FROM tagihan_pppoe WHERE user_id=? AND bulan=?", (iid, bulan_ini)
            ).fetchall()}
            _pppoe_ids = [i for i in all_ids if i not in billed]
            _tagihan_label = "Belum Ada Tagihan"
        elif tagihan == "isolir_hari_ini":
            rows = con_t.execute(
                "SELECT id FROM pppoe_users WHERE user_id=? AND isolated_at BETWEEN ? AND ?",
                (iid, _ts_start, _ts_end)
            ).fetchall()
            _pppoe_ids = [r["id"] for r in rows]
            _tagihan_label = "Isolir Hari Ini"
        con_t.close()

    # Count per-tab stats from DB (lightweight, no row data)
    count_all   = db.count_pppoe_users(iid, _sid, _odp_id, q=q)
    count_aktif = db.count_pppoe_users(iid, _sid, _odp_id, status="aktif", q=q)
    count_sus   = db.count_pppoe_users(iid, _sid, _odp_id, status="suspended", q=q)
    count_non   = db.count_pppoe_users(iid, _sid, _odp_id, status="nonaktif", q=q)

    # Online filter needs client-side data; fetch minimal set for online tab count
    online_set = db.get_all_online_usernames()

    # Fetch only current page
    total = db.count_pppoe_users(iid, _sid, _odp_id, status=_status, q=q,
                                  terpasang=_terpasang, pppoe_ids=_pppoe_ids)
    # For online tab, online filter is post-processed (no DB column) — fetch all for that tab
    if status == "online":
        all_for_online = db.list_pppoe_users(iid, _sid, _odp_id, status="aktif", q=q,
                                              terpasang=_terpasang, pppoe_ids=_pppoe_ids)
        all_for_online = [u for u in all_for_online if u["username"] in online_set]
        total = len(all_for_online)
        offset = (page - 1) * per_page
        users = all_for_online[offset: offset + per_page]
        count_online = total
    else:
        count_online = len([u for u in db.list_pppoe_users(iid, _sid, _odp_id, status="aktif") if u["username"] in online_set])
        offset = (page - 1) * per_page
        users = db.list_pppoe_users(iid, _sid, _odp_id, status=_status, q=q,
                                     terpasang=_terpasang, pppoe_ids=_pppoe_ids,
                                     limit=per_page, offset=offset)

    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages

    pakets = db.list_paket_pppoe(iid)
    tagihan_bulan = db.list_tagihan(iid, bulan=bulan_ini)
    tagihan_map   = {t["pppoe_id"]: t["status"] for t in tagihan_bulan}
    tagihan_amount_map = {t["pppoe_id"]: t["amount"] for t in tagihan_bulan}

    def _first_tagihan_date(tgl_mulai_ts, tgl_bayar_day):
        if not tgl_mulai_ts:
            return None
        import datetime as _dtt
        try:
            start = _dtt.date.fromtimestamp(int(tgl_mulai_ts))
            tgl   = min(max(1, int(tgl_bayar_day or 1)), 28)
            # Coba bulan yang sama
            same_m = _dtt.date(start.year, start.month, tgl)
            if (same_m - start).days > 0:
                return same_m
            # Bulan berikutnya
            y, m = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
            return _dtt.date(y, m, tgl)
        except Exception:
            return None

    for u in users:
        u["first_tagihan_date"] = _first_tagihan_date(u.get("tgl_mulai"), u.get("tgl_bayar", 1))
    overdue_ids = {t["pppoe_id"] for t in tagihan_bulan if t["status"] == "overdue"}
    tagihan_total_amount = sum(t["amount"] for t in tagihan_bulan if t.get("amount"))
    tagihan_lunas_amount = sum(t["amount"] for t in tagihan_bulan if t["status"] in ("paid", "lunas") and t.get("amount"))
    cache_ages = []
    for s in servers:
        age = db.get_online_cache_age(s["id"])
        if age is not None:
            cache_ages.append(age)
    cache_age = min(cache_ages) if cache_ages else None
    push_msg = request.query_params.get("push")
    now_date = _dt.date.today().strftime("%Y-%m-%d")
    con2 = sqlite3.connect(DB_PATH)
    con2.row_factory = sqlite3.Row
    isp_row = con2.execute("SELECT auto_reminder FROM users WHERE id=?", (iid,)).fetchone()
    con2.close()
    isp_auto_reminder = (dict(isp_row).get("auto_reminder") if isp_row else 1)
    if isp_auto_reminder is None:
        isp_auto_reminder = 1

    tab_counts = {"semua": count_all, "aktif": count_aktif, "online": count_online,
                  "suspended": count_sus, "nonaktif": count_non}

    def page_url(p, s=status, pp=per_page):
        params = {"page": p, "per_page": pp}
        if server_id: params["server_id"] = server_id
        if s: params["status"] = s
        if q: params["q"] = q
        if odp_id: params["odp_id"] = odp_id
        if terpasang: params["terpasang"] = terpasang
        if tagihan: params["tagihan"] = tagihan
        return "/pppoe/users?" + "&".join(f"{k}={v}" for k, v in params.items())

    # Active filter label for badge
    aktif_filter = ""
    if _terpasang == 0:
        aktif_filter = "Belum Terpasang"
    elif _terpasang == 1:
        aktif_filter = "Sudah Terpasang"
    elif _tagihan_label:
        aktif_filter = _tagihan_label

    return tpl.TemplateResponse(request, "pppoe_users.html", _ctx(
        request, user=user, users=users, servers=servers, pakets=pakets,
        sel_server=server_id, sel_status=status, q=q, sel_odp=odp_id,
        odp_list=odp_list,
        online_set=online_set, overdue_ids=overdue_ids,
        tagihan_map=tagihan_map, tagihan_amount_map=tagihan_amount_map, bulan_ini=bulan_ini,
        odp_filter_list=odp_filter_list,
        cache_age=cache_age, push_msg=push_msg,
        now_date=now_date, isp_auto_reminder=isp_auto_reminder,
        tagihan_total_amount=tagihan_total_amount,
        tagihan_lunas_amount=tagihan_lunas_amount,
        tab_counts=tab_counts,
        page=page, per_page=per_page, total=total, total_pages=total_pages,
        page_url=page_url,
        aktif_filter=aktif_filter, sel_terpasang=terpasang, sel_tagihan=tagihan,
    ))


@app.post("/pppoe/users/tambah")
async def pppoe_user_tambah(
    request: Request,
    server_id: str = Form(...), nama_pelanggan: str = Form(...),
    username: str = Form(""), password: str = Form(""),
    paket_id: int = Form(...), telepon: str = Form(""),
    alamat: str = Form(""), tgl_bayar: int = Form(1),
    tgl_mulai: str = Form(""),
    odp_id: str = Form(""), odp_port: str = Form(""),
    terpasang: int = Form(0),
    grace_period_custom: str = Form(""),
    no_isolir: int = Form(0),
    sudah_di_mt: int = Form(0),
):
    user = require_login(request)
    iid = _isp_id(user)
    isp = db.get_user(iid)
    paket = db.get_paket_pppoe(paket_id)
    import datetime as _dt
    tgl_mulai_ts = None
    if tgl_mulai.strip():
        try:
            tgl_mulai_ts = int(_dt.datetime.strptime(tgl_mulai.strip(), "%Y-%m-%d").timestamp())
        except ValueError:
            pass
    gp_custom = int(grace_period_custom) if grace_period_custom.strip().isdigit() else None
    pid = db.create_pppoe_user(iid, server_id, nama_pelanggan, username, password, paket_id, telepon, alamat, tgl_bayar, mt_pushed=0, tgl_mulai=tgl_mulai_ts, terpasang=terpasang, grace_period=gp_custom, no_isolir=no_isolir)
    if odp_id.strip():
        db.assign_odp(pid, int(odp_id), int(odp_port) if odp_port.strip() else None)
    pushed = False
    if not username.strip():
        # TANPA SCREETS: tidak perlu push ke MikroTik
        pass
    elif sudah_di_mt:
        # AMBIL DARI ROUTER: sudah ada di MikroTik, tandai langsung
        db.set_mt_pushed(pid, 1)
        db.set_terpasang(pid, 1)
        pushed = True
    else:
        # BUAT SCREETS BARU: push ke MikroTik
        mt = get_mt(server_id)
        if mt:
            profile = paket["kecepatan"] if paket else "default"
            pushed = mt.add_pppoe_secret(username, password, profile=profile)
        if pushed:
            db.set_mt_pushed(pid, 1)
            db.set_terpasang(pid, 1)
    db.add_transaksi(iid, str(pid), "pppoe", paket["harga"] if paket else 0, f"Tambah PPPoE {username}")
    _log(request, user, "Tambah Pelanggan PPPoE", f"{nama_pelanggan} ({username})")
    # Notif WA aktivasi ke pelanggan
    if telepon:
        isp_nama = isp["nama"] if isp else "ISP"
        paket_info = f"{paket['nama']} ({paket['kecepatan']})" if paket else "-"
        kecepatan = paket["kecepatan"] if paket else ""
        send_wa(telepon,
            _render_wa_template(iid, "aktivasi",
                nama=nama_pelanggan, paket=paket_info, kecepatan=kecepatan,
                username=username, password=password,
                tgl_bayar=tgl_bayar, isp=isp_nama),
            token=_isp_wa_token(iid)
        )
    return RedirectResponse("/pppoe/users", status_code=302)


@app.post("/pppoe/users/hapus/{pid}", response_class=JSONResponse)
async def pppoe_user_hapus(request: Request, pid: int):
    user = require_login(request)
    iid = _isp_id(user)
    pu = db.get_pppoe_user(pid)
    if not pu or pu["user_id"] != iid:
        return JSONResponse({"ok": False, "msg": "Pelanggan tidak ditemukan"}, status_code=404)

    mt_ok = False
    mt_err = ""
    mt = get_mt(pu["server_id"])
    if mt:
        mt_ok = mt.remove_pppoe_secret(pu["username"])
        if not mt_ok:
            mt_err = "Gagal hapus dari MikroTik — secret tidak ditemukan atau koneksi gagal"
    else:
        mt_err = "Tidak dapat terhubung ke router"

    db.delete_pppoe_user(pid)
    _log(request, user, "Hapus Pelanggan PPPoE", f"{pu['nama_pelanggan']} ({pu['username']}) — MT: {'ok' if mt_ok else mt_err}")

    return JSONResponse({
        "ok": True,
        "mt_ok": mt_ok,
        "mt_err": mt_err,
        "nama": pu["nama_pelanggan"],
    })


@app.post("/pppoe/users/edit/{pid}")
async def pppoe_user_edit(
    request: Request, pid: int,
    nama_pelanggan: str = Form(...), telepon: str = Form(""),
    alamat: str = Form(""), tgl_bayar: int = Form(1),
    username: str = Form(""), password: str = Form(""),
    tgl_mulai: str = Form(""),
    ppp_mode: str = Form("baru"),
):
    user = require_login(request)
    iid = _isp_id(user)
    pu = db.get_pppoe_user(pid)
    if pu and pu["user_id"] == iid:
        import datetime as _dt
        tgl_mulai_ts = None
        if tgl_mulai.strip():
            try:
                tgl_mulai_ts = int(_dt.datetime.strptime(tgl_mulai.strip(), "%Y-%m-%d").timestamp())
            except ValueError:
                pass
        if ppp_mode == "none":
            new_user = ""
            new_pass = ""
        else:
            new_user = username.strip() or pu["username"]
            new_pass = password.strip() or pu["password"]
        old_tgl_bayar = pu["tgl_bayar"]
        db.update_pppoe_user(pid, nama_pelanggan, telepon, alamat, tgl_bayar, new_user, new_pass, tgl_mulai=tgl_mulai_ts)
        # Jika tgl_bayar berubah → hapus tagihan pending bulan ini & generate ulang
        if tgl_bayar != old_tgl_bayar:
            import datetime as _dt2
            bulan_ini = _dt2.date.today().strftime("%Y-%m")
            db.regenerate_tagihan_user(pid, bulan_ini)
        # Update credentials di MikroTik jika ada perubahan (tidak hapus saat mode none)
        mt = get_mt(pu["server_id"])
        if mt and ppp_mode != "none" and (new_user != pu["username"] or new_pass != pu["password"]):
            try:
                secrets = mt.api.get_resource("/ppp/secret")
                rows = secrets.get(name=pu["username"])
                if rows:
                    update_data = {"id": rows[0]["id"], "password": new_pass}
                    if new_user != pu["username"]:
                        update_data["name"] = new_user
                    secrets.set(**update_data)
            except Exception:
                pass
    return RedirectResponse("/pppoe/users", status_code=302)


@app.post("/pppoe/users/status/{pid}")
async def pppoe_user_status(request: Request, pid: int, status: str = Form(...)):
    user = require_login(request)
    iid = _isp_id(user)
    isp = db.get_user(iid)
    pu = db.get_pppoe_user(pid)
    if pu and pu["user_id"] == iid:
        mt = get_mt(pu["server_id"])
        if mt:
            if status == "nonaktif":
                mt.disable_pppoe_secret(pu["username"])
            else:
                mt.enable_pppoe_secret(pu["username"])
        db.update_pppoe_status(pid, status)
        # Notif WA suspend / reaktivasi
        if pu.get("telepon"):
            isp_nama = isp["nama"] if isp else "ISP"
            jenis_notif = "suspend" if status == "nonaktif" else "reaktivasi"
            send_wa(pu["telepon"],
                _render_wa_template(iid, jenis_notif,
                    nama=pu["nama_pelanggan"], isp=isp_nama),
                token=_isp_wa_token(iid)
            )
    return RedirectResponse("/pppoe/users", status_code=302)


@app.post("/pppoe/users/terpasang/{pid}", response_class=JSONResponse)
async def pppoe_set_terpasang(request: Request, pid: int, terpasang: int = Form(1)):
    """Update status pemasangan fisik pelanggan (dipakai teknisi)."""
    user = require_login(request)
    iid = _isp_id(user)
    pu = db.get_pppoe_user(pid)
    if not pu or pu["user_id"] != iid:
        return JSONResponse({"ok": False, "msg": "Tidak ditemukan"}, status_code=404)
    db.set_terpasang(pid, terpasang)
    label = "Sudah Terpasang" if terpasang else "Belum Terpasang"
    _log(request, user, "Update Pemasangan", f"{pu['nama_pelanggan']} → {label}")
    return JSONResponse({"ok": True, "terpasang": terpasang, "label": label})


# ── PPPoE Push & Online Refresh ──────────────────────────────────────────────

@app.post("/pppoe/users/push/{pid}")
async def pppoe_push_mt(request: Request, pid: int):
    """Push satu pelanggan ke MikroTik (untuk yang belum tersinkron)."""
    user = require_login(request)
    iid = _isp_id(user)
    pu = db.get_pppoe_user(pid)
    if not pu or pu["user_id"] != iid:
        return RedirectResponse("/pppoe/users", status_code=302)
    mt = get_mt(pu["server_id"])
    ok = False
    if mt:
        paket = db.get_paket_pppoe(pu["paket_id"]) if pu.get("paket_id") else None
        profile = paket["kecepatan"] if paket else "default"
        try:
            secrets = mt.api.get_resource("/ppp/secret")
            existing = secrets.get(name=pu["username"])
            if not existing:
                secrets.add(name=pu["username"], password=pu["password"],
                            service="pppoe", profile=profile,
                            comment=f"{pu['nama_pelanggan']} | {pu.get('telepon','')}")
            ok = True
        except Exception:
            pass
    if ok:
        db.set_mt_pushed(pid, 1)
        db.set_terpasang(pid, 1)
        # Notif WA aktivasi saat push berhasil
        if pu.get("telepon"):
            isp = db.get_user(iid)
            isp_nama = isp["nama"] if isp else "ISP"
            paket_info = f"{paket['nama']} ({paket['kecepatan']})" if paket else "-"
            kecepatan = paket["kecepatan"] if paket else ""
            send_wa(pu["telepon"],
                _render_wa_template(iid, "aktivasi",
                    nama=pu["nama_pelanggan"], paket=paket_info, kecepatan=kecepatan,
                    username=pu["username"], password=pu["password"],
                    tgl_bayar=pu.get("tgl_bayar", 1), isp=isp_nama),
                token=_isp_wa_token(iid)
            )
    return RedirectResponse(f"/pppoe/users?push={'ok' if ok else 'fail'}", status_code=302)


@app.post("/pppoe/users/refresh-online", response_class=JSONResponse)
async def pppoe_refresh_online(request: Request):
    """Trigger refresh cache online secara manual."""
    user = require_login(request)
    await _refresh_pppoe_online()
    return JSONResponse({"ok": True})


# ── PPPoE Monitor Realtime ────────────────────────────────────────────────────

@app.get("/pppoe/monitor", response_class=HTMLResponse)
async def pppoe_monitor_page(request: Request):
    user = require_login(request)
    _iid = _isp_id(user)
    _con_chk = _addon_db()
    _mo_chk = _con_chk.execute(
        "SELECT ta.status FROM tenant_addons ta JOIN addons a ON a.id=ta.addon_id "
        "WHERE ta.user_id=? AND a.code='monitor_online' AND ta.status='active'", (_iid,)
    ).fetchone()
    _con_chk.close()
    if not _mo_chk:
        return RedirectResponse("/addons?err=monitor_online_belum_aktif", status_code=302)
    servers = db.list_servers(_isp_id(user))
    return tpl.TemplateResponse(request, "pppoe_monitor.html", _ctx(
        request, user=user, active="pppoe_monitor", servers=servers
    ))


@app.get("/pppoe/monitor/json", response_class=JSONResponse)
async def pppoe_monitor_json(request: Request):
    """Ambil data sesi aktif realtime dari semua MikroTik server."""
    user = require_login(request)
    iid = _isp_id(user)
    servers = db.list_servers(iid)
    # Ambil semua pelanggan untuk match username → nama
    all_users = db.list_pppoe_users(iid)
    user_map = {u["username"]: u for u in all_users}

    result = []
    total_online = 0
    for s in servers:
        mt = get_mt(s["id"])
        if not mt:
            result.append({"server_id": s["id"], "server_nama": s["nama"], "error": "Tidak terhubung", "sessions": []})
            continue
        actives = mt.list_pppoe_active()
        sessions = []
        for a in actives:
            name = a.get("name", "")
            pu = user_map.get(name)
            sessions.append({
                "session_id": a.get(".id", ""),
                "username":   name,
                "address":    a.get("address", "-"),
                "caller_id":  a.get("caller-id", "-"),
                "uptime":     a.get("uptime", "-"),
                "service":    a.get("service", "pppoe"),
                "nama_pelanggan": pu["nama_pelanggan"] if pu else "-",
                "telepon":    pu["telepon"] if pu else "",
                "paket_nama": pu.get("paket_nama", "-") if pu else "-",
            })
        total_online += len(sessions)
        result.append({
            "server_id":   s["id"],
            "server_nama": s["nama"],
            "sessions":    sessions,
            "error":       None,
        })

    return JSONResponse({
        "ok": True,
        "total_online": total_online,
        "servers": result,
        "fetched_at": int(time.time()),
    })


@app.post("/pppoe/monitor/kick", response_class=JSONResponse)
async def pppoe_monitor_kick(request: Request):
    """Disconnect / kick satu sesi PPPoE aktif."""
    user = require_login(request)
    iid = _isp_id(user)
    body = await request.json()
    server_id  = (body.get("server_id") or "").strip()
    session_id = (body.get("session_id") or "").strip()
    username   = (body.get("username") or "").strip()
    if not server_id or not session_id:
        return JSONResponse({"ok": False, "msg": "Parameter tidak lengkap"})
    s = db.get_server(server_id)
    if not s or s["user_id"] != iid:
        return JSONResponse({"ok": False, "msg": "Server tidak ditemukan"}, status_code=403)
    mt = get_mt(server_id)
    if not mt:
        return JSONResponse({"ok": False, "msg": "Tidak dapat terhubung ke router"})
    ok = mt.kick_pppoe_session(session_id)
    if ok:
        _log(request, user, "Kick PPPoE Session", f"{username} @ {s['nama']}")
    return JSONResponse({"ok": ok, "msg": "" if ok else "Gagal disconnect sesi"})


# ── PPPoE Monitoring & Laporan ────────────────────────────────────────────────


# ── PPPoE Import dari MikroTik ───────────────────────────────────────────────

@app.get("/pppoe/users/import-preview")
async def pppoe_import_preview(request: Request, server_id: str):
    user = require_login(request)
    s = db.get_server(server_id)
    if not s or s["user_id"] != user["id"]:
        return JSONResponse({"error": "Server tidak ditemukan"}, status_code=404)
    mt = get_mt(server_id)
    if not mt:
        return JSONResponse({"error": "Tidak dapat terhubung ke server"}, status_code=500)
    secrets = mt.list_pppoe_secrets()
    existing = {u["username"] for u in db.list_pppoe_users(user["id"], server_id)}
    new_secrets = [
        {
            "username": s["name"],
            "password": s.get("password", ""),
            "profile":  s.get("profile", "default"),
            "comment":  s.get("comment", ""),
            "disabled": s.get("disabled", "false"),
        }
        for s in secrets if s["name"] not in existing
    ]
    return JSONResponse({"secrets": new_secrets})


@app.post("/pppoe/users/import")
async def pppoe_import(request: Request):
    user = require_login(request)
    body = await request.json()
    server_id = body.get("server_id", "")
    items     = body.get("items", [])
    s = db.get_server(server_id)
    if not s or s["user_id"] != user["id"]:
        return JSONResponse({"error": "Server tidak ditemukan"}, status_code=404)
    imported = 0
    for item in items:
        username      = item.get("username", "").strip()
        password      = item.get("password", "").strip()
        nama_pelanggan= item.get("nama", username).strip() or username
        telepon       = item.get("telepon", "").strip()
        paket_id      = item.get("paket_id")
        tgl_bayar     = int(item.get("tgl_bayar", 1))
        if not username:
            continue
        paket = db.get_paket_pppoe(int(paket_id)) if paket_id else None
        db.create_pppoe_user(
            user["id"], server_id, nama_pelanggan, username, password,
            paket["id"] if paket else None, telepon, "", tgl_bayar,
            mt_pushed=1, terpasang=1  # diimport dari MikroTik = sudah terpasang
        )
        imported += 1
    return JSONResponse({"imported": imported})


@app.post("/pppoe/users/sync-mt-pushed", response_class=JSONResponse)
async def pppoe_sync_mt_pushed(request: Request):
    """Cek PPPoE secrets di semua MikroTik server, update mt_pushed di DB sesuai keberadaannya."""
    user = require_login(request)
    iid  = _isp_id(user)
    servers = db.list_servers(iid)
    all_users = db.list_pppoe_users(iid)

    updated_ada = 0
    updated_tidak = 0
    errors = []

    for s in servers:
        mt = get_mt(s["id"])
        if not mt:
            errors.append(f"{s['nama']}: tidak terhubung")
            continue
        secrets = mt.list_pppoe_secrets()
        mt_usernames = {sec.get("name", "").lower() for sec in secrets}

        server_users = [u for u in all_users if u["server_id"] == s["id"]]
        con = sqlite3.connect(DB_PATH)
        for u in server_users:
            exists = u["username"].lower() in mt_usernames
            new_val = 1 if exists else 0
            if u.get("terpasang") != new_val or u.get("mt_pushed") != new_val:
                con.execute("UPDATE pppoe_users SET mt_pushed=?, terpasang=? WHERE id=?",
                            (new_val, new_val, u["id"]))
                if exists:
                    updated_ada += 1
                else:
                    updated_tidak += 1
        con.commit()
        con.close()

    return JSONResponse({
        "ok": True,
        "updated_ada": updated_ada,
        "updated_tidak": updated_tidak,
        "errors": errors,
        "msg": f"Sync selesai. {updated_ada} ditandai terpasang, {updated_tidak} belum terpasang."
        + (f" Error: {', '.join(errors)}" if errors else ""),
    })


# ── WA Gateway ───────────────────────────────────────────────────────────────

@app.get("/wa-gateway", response_class=HTMLResponse)
async def wa_gateway_page(request: Request):
    user = require_login(request)
    gw = db.get_wa_gateway(user["id"])
    status_data = {}
    saved = db.list_wa_templates(user["id"])
    templates = []
    for jenis, label, desc, vars_used in _WA_TEMPLATE_META:
        s = saved.get(jenis, {})
        default_isi = _DEFAULT_WA_TEMPLATES.get(jenis, "")
        templates.append({
            "jenis": jenis, "label": label, "desc": desc, "vars_used": vars_used,
            "isi": s.get("isi") if s else default_isi,
            "default_isi": default_isi,
            "is_custom": bool(s),
            "updated_at": s.get("updated_at") if s else None,
        })
    return tpl.TemplateResponse(request, "wa_gateway.html", _ctx(
        request, user=user, gw=gw, status_data=status_data,
        templates=templates, active="wa_gateway"
    ))


@app.post("/wa-gateway/setup", response_class=JSONResponse)
async def wa_gateway_setup(request: Request):
    user = require_login(request)
    # Selalu pastikan token terdaftar di WuzAPI (idempotent)
    token = _wa_create_user(user["id"], user["nama"])
    db.upsert_wa_gateway(user["id"], token)
    # Trigger connect + ambil QR
    qr = _wa_get_qr(token)
    status = _wa_session_status(token)
    if _wa_is_logged_in(status):
        nomor = status.get("jid", "").split(":")[0]
        db.update_wa_gateway_status(user["id"], "connected", nomor)
        return JSONResponse({"ok": True, "connected": True, "nomor": nomor})
    return JSONResponse({"ok": True, "connected": False, "qr": qr})


@app.get("/wa-gateway/status", response_class=JSONResponse)
async def wa_gateway_status(request: Request):
    user = require_login(request)
    gw = db.get_wa_gateway(user["id"])
    if not gw or not gw.get("wa_token"):
        return JSONResponse({"connected": False})
    s = _wa_session_status(gw["wa_token"])
    connected = _wa_is_logged_in(s)
    nomor = s.get("jid", "").split(":")[0] if connected else ""
    if connected:
        db.update_wa_gateway_status(user["id"], "connected", nomor)
    return JSONResponse({"connected": connected, "nomor": nomor,
                         "name": s.get("name", "")})


@app.post("/wa-gateway/disconnect", response_class=JSONResponse)
async def wa_gateway_disconnect(request: Request):
    user = require_login(request)
    gw = db.get_wa_gateway(user["id"])
    if gw and gw.get("wa_token"):
        _wa_disconnect(gw["wa_token"])
        db.update_wa_gateway_status(user["id"], "disconnected", "")
    return JSONResponse({"ok": True})


@app.post("/wa-gateway/logout", response_class=JSONResponse)
async def wa_gateway_logout(request: Request):
    """Logout dari WhatsApp dan hapus linked device — diperlukan untuk ganti nomor."""
    user = require_login(request)
    gw = db.get_wa_gateway(user["id"])
    if gw and gw.get("wa_token"):
        _wa_logout(gw["wa_token"])
        db.update_wa_gateway_status(user["id"], "disconnected", "")
    return JSONResponse({"ok": True})


@app.post("/wa-gateway/provider", response_class=JSONResponse)
async def wa_gateway_set_provider(request: Request):
    """Simpan pilihan provider (wuzapi/fonnte/wablas) + token eksternal."""
    user = require_login(request)
    body = await request.json()
    provider = body.get("provider", "wuzapi")
    token_ext = body.get("token_ext", "").strip()
    if provider not in ("wuzapi", "fonnte", "wablas"):
        return JSONResponse({"ok": False, "msg": "Provider tidak valid"})
    db.update_wa_provider(user["id"], provider, token_ext)
    return JSONResponse({"ok": True, "msg": f"Provider disimpan: {provider}"})


@app.post("/wa-gateway/check-token", response_class=JSONResponse)
async def wa_check_token(request: Request):
    """Validasi token Wablas/Fonnte tanpa kirim pesan."""
    user = require_login(request)
    body = await request.json()
    provider = body.get("provider", "")
    token    = body.get("token", "").strip()
    if not token:
        return JSONResponse({"ok": False, "msg": "Token kosong"})
    if provider == "wablas":
        ok, msg, detail = await asyncio.to_thread(_check_wablas_token, token)
        return JSONResponse({"ok": ok, "msg": msg, "detail": detail})
    elif provider == "fonnte":
        try:
            import requests as _req
            r = _req.post("https://api.fonnte.com/device",
                          headers={"Authorization": token}, timeout=10)
            d = r.json()
            if d.get("status"):
                nomor  = d.get("device", "—")
                nama   = d.get("name", "—")
                status = d.get("device_status", "—")
                exp    = d.get("expired", "—")
                return JSONResponse({"ok": True,
                    "msg": f"Token valid · {nomor} ({nama}) · Status: {status} · Expired: {exp}",
                    "detail": d})
            return JSONResponse({"ok": False, "msg": d.get("reason") or d.get("message") or "Token tidak valid", "detail": d})
        except Exception as e:
            return JSONResponse({"ok": False, "msg": str(e), "detail": {}})
    return JSONResponse({"ok": False, "msg": f"Provider '{provider}' tidak mendukung pengecekan token"})


@app.post("/wa-gateway/test-notif", response_class=JSONResponse)
async def wa_test_notif(request: Request):
    """Kirim WA test notifikasi ke nomor tertentu."""
    user = require_login(request)
    body = await request.json()
    nomor = body.get("telepon", "")
    jenis = body.get("type", "aktivasi")
    tok = _isp_wa_token(user["id"])
    isp_nama = user["nama"]
    if not nomor:
        return JSONResponse({"ok": False, "detail": "Nomor HP wajib diisi"})
    contoh = {
        "aktivasi": (
            f"✅ *Selamat! Layanan Internet Anda Aktif*\n\n"
            f"Halo *Pelanggan Contoh*,\n\n"
            f"Akun PPPoE Anda telah berhasil didaftarkan.\n\n"
            f"📦 Paket: *10 Mbps*\n"
            f"👤 Username: `pelanggan01`\n"
            f"🔑 Password: `pass123`\n"
            f"📅 Tagihan setiap tgl: *5*\n\n"
            f"Hubungi kami jika ada kendala.\n"
            f"_{isp_nama}_"
        ),
        "suspend": (
            f"⚠️ *Layanan Internet Dinonaktifkan*\n\n"
            f"Halo *Pelanggan Contoh*,\n\n"
            f"Layanan internet Anda telah *dinonaktifkan sementara*.\n\n"
            f"Kemungkinan penyebab: tagihan belum terbayar.\n\n"
            f"Hubungi kami untuk informasi lebih lanjut.\n"
            f"_{isp_nama}_"
        ),
        "reaktivasi": (
            f"✅ *Layanan Internet Aktif Kembali*\n\n"
            f"Halo *Pelanggan Contoh*,\n\n"
            f"Layanan internet Anda telah *diaktifkan kembali*.\n\n"
            f"Terima kasih telah melakukan pembayaran.\n"
            f"_{isp_nama}_"
        ),
        "tagihan": (
            f"🔔 *Reminder Tagihan Internet*\n\n"
            f"Halo *Pelanggan Contoh*,\n\n"
            f"Tagihan bulan ini jatuh tempo *3 hari lagi* (tgl 5).\n\n"
            f"Jumlah: *Rp 150.000*\n\n"
            f"Bayar sekarang:\nhttps://billing.vpntunel.my.id/bayar/tagihan/xxx\n\n"
            f"_Abaikan jika sudah membayar._"
        ),
    }
    pesan = contoh.get(jenis, contoh["aktivasi"])
    ok, err = send_wa(nomor, pesan, user_id=user["id"], tipe="test")
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "detail": err or "Gagal kirim WA"})


# ── WA Notification Template Routes ──────────────────────────────────────────

_WA_TEMPLATE_META = [
    ("penagihan",    "Reminder Tagihan",        "Kirim pengingat tagihan manual ke pelanggan",
     ["{nama}", "{nominal}", "{bulan}", "{paket}", "{tgl_bayar}", "{isp}"]),
    ("tagihan_link", "Tagihan + Link Bayar",    "Kirim tagihan beserta link pembayaran Midtrans",
     ["{nama}", "{nominal}", "{bulan}", "{link}", "{paket}", "{isp}"]),
    ("pembayaran",   "Konfirmasi Pembayaran",   "Notifikasi otomatis saat tagihan lunas",
     ["{nama}", "{nominal}", "{bulan}", "{isp}"]),
    ("aktivasi",     "Aktivasi Layanan",        "Dikirim saat pelanggan baru didaftarkan",
     ["{nama}", "{paket}", "{kecepatan}", "{username}", "{password}", "{tgl_bayar}", "{isp}"]),
    ("reaktivasi",   "Reaktivasi Layanan",      "Dikirim saat layanan diaktifkan kembali",
     ["{nama}", "{isp}"]),
    ("suspend",      "Suspend Layanan",         "Dikirim saat layanan dinonaktifkan",
     ["{nama}", "{isp}"]),
    ("voucher_online", "Voucher Online Berhasil", "Dikirim otomatis saat pelanggan beli voucher hotspot online",
     ["{paket}", "{durasi}", "{kode}", "{isp}"]),
    ("reminder",      "Auto Reminder Tagihan",   "Dikirim otomatis H-3, H-1, H-0, H+1 sebelum/sesudah jatuh tempo",
     ["{nama}", "{bulan}", "{nominal}", "{link}", "{jatuh_tempo}", "{keterangan}", "{emoji}", "{isp}"]),
]


@app.get("/wa-templates", response_class=HTMLResponse)
async def wa_templates_page(request: Request):
    return RedirectResponse("/wa-gateway#templates", status_code=302)


@app.post("/wa-templates/save", response_class=JSONResponse)
async def wa_templates_save(request: Request):
    user = require_login(request)
    body = await request.json()
    jenis = body.get("jenis", "")
    isi   = body.get("isi", "").strip()
    valid_jenis = {m[0] for m in _WA_TEMPLATE_META}
    if jenis not in valid_jenis:
        return JSONResponse({"ok": False, "msg": "Jenis tidak valid"})
    if not isi:
        return JSONResponse({"ok": False, "msg": "Template tidak boleh kosong"})
    db.save_wa_template(user["id"], jenis, isi)
    return JSONResponse({"ok": True})


@app.post("/wa-templates/reset", response_class=JSONResponse)
async def wa_templates_reset(request: Request):
    user = require_login(request)
    body = await request.json()
    jenis = body.get("jenis", "")
    valid_jenis = {m[0] for m in _WA_TEMPLATE_META}
    if jenis not in valid_jenis:
        return JSONResponse({"ok": False, "msg": "Jenis tidak valid"})
    db.delete_wa_template(user["id"], jenis)
    return JSONResponse({"ok": True, "default_isi": _DEFAULT_WA_TEMPLATES.get(jenis, "")})


# ── Tagihan PPPoE ────────────────────────────────────────────────────────────

def _reaktivasi_pppoe(pppoe_id: int, user_id: str):
    """Enable PPPoE di MikroTik + update status DB + kirim WA reaktivasi."""
    pu = db.get_pppoe_user(pppoe_id)
    if not pu or pu["status"] == "aktif":
        return
    # Enable di MikroTik
    mt = get_mt(pu["server_id"])
    if mt:
        mt.enable_pppoe_secret(pu["username"])
    # Update DB
    db.update_pppoe_status(pppoe_id, "aktif")
    # WA notif
    if pu.get("telepon"):
        isp = db.get_user(user_id)
        isp_nama = isp["nama"] if isp else ""
        tok = _isp_wa_token(user_id)
        send_wa(
            pu["telepon"],
            _render_wa_template(user_id, "reaktivasi",
                nama=pu["nama_pelanggan"], isp=isp_nama),
            token=tok
        )


def _bulan_sekarang() -> str:
    from datetime import date
    return date.today().strftime("%Y-%m")

def _bulan_list() -> list[str]:
    """12 bulan terakhir untuk dropdown filter."""
    from datetime import date, timedelta
    months = []
    d = date.today()
    for _ in range(12):
        months.append(d.strftime("%Y-%m"))
        d = (d.replace(day=1) - timedelta(days=1))
    return months

def _label_bulan(b: str) -> str:
    from datetime import datetime
    try:
        return datetime.strptime(b, "%Y-%m").strftime("%B %Y")
    except Exception:
        return b

tpl.env.filters["label_bulan"] = _label_bulan


@app.get("/pppoe/tagihan", response_class=HTMLResponse)
async def tagihan_page(request: Request, bulan: str = "", status: str = "paid", q: str = "", metode: str = ""):
    user = require_login(request)
    if not bulan:
        bulan = _bulan_sekarang()
    tagihan = db.list_tagihan(user["id"], bulan, status if status else None)
    if q:
        q_lower = q.lower()
        tagihan = [t for t in tagihan if
                   q_lower in (t.get("nama_pelanggan") or "").lower() or
                   q_lower in (t.get("pppoe_username") or "").lower()]
    if metode:
        tagihan = [t for t in tagihan if (t.get("metode_bayar") or "") == metode]
    stats   = db.stats_tagihan(user["id"], bulan)
    bulans  = _bulan_list()
    # Kumpulkan metode yang ada untuk opsi filter
    all_metode = sorted({t["metode_bayar"] for t in db.list_tagihan(user["id"], bulan) if t.get("metode_bayar")})
    return tpl.TemplateResponse(request, "pppoe_tagihan.html", _ctx(
        request, user=user, tagihan=tagihan, stats=stats,
        bulan=bulan, bulans=bulans, sel_status=status, q=q,
        sel_metode=metode, all_metode=all_metode, active="pppoe_tagihan"
    ))


@app.post("/pppoe/tagihan/generate", response_class=JSONResponse)
async def tagihan_generate(request: Request, bulan: str = Form("")):
    user = require_login(request)
    if not bulan:
        bulan = _bulan_sekarang()
    n = db.generate_tagihan(user["id"], bulan)
    return JSONResponse({"ok": True, "dibuat": n, "bulan": bulan})


@app.get("/pppoe/tagihan/belum-bayar", response_class=JSONResponse)
async def tagihan_belum_bayar(request: Request, bulan: str = ""):
    user = require_login(request)
    if not bulan:
        bulan = _bulan_sekarang()
    tagihan = db.list_tagihan(user["id"], bulan)
    belum = [
        {
            "id": t["id"],
            "nama": t["nama_pelanggan"],
            "username": t["pppoe_username"],
            "paket": t["paket_nama"] or "",
            "amount": t["amount"],
            "status": t["status"],
        }
        for t in tagihan if t["status"] in ("unpaid", "overdue")
    ]
    return JSONResponse({"ok": True, "data": belum, "bulan": bulan})


@app.post("/pppoe/users/{pid}/generate-tagihan", response_class=JSONResponse)
async def generate_tagihan_satu(request: Request, pid: int):
    user = require_login(request)
    iid  = _isp_id(user)
    con  = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    p = con.execute(
        "SELECT p.*, pk.harga FROM pppoe_users p "
        "LEFT JOIN paket_pppoe pk ON pk.id=p.paket_id "
        "WHERE p.id=? AND p.user_id=?", (pid, iid)
    ).fetchone()
    if not p:
        con.close()
        return JSONResponse({"ok": False, "msg": "Pelanggan tidak ditemukan"})
    bulan  = _bulan_sekarang()
    harga  = p["harga"] or 0
    now    = int(time.time())
    # Prorate jika tagihan pertama
    prev = con.execute(
        "SELECT COUNT(*) FROM tagihan_pppoe WHERE pppoe_id=? AND bulan<?", (pid, bulan)
    ).fetchone()[0]
    if prev == 0 and p["tgl_mulai"]:
        amount = db._hitung_prorate(harga, p["tgl_mulai"], bulan, p["tgl_bayar"] or 1)
    else:
        amount = harga
    try:
        con.execute(
            "INSERT OR IGNORE INTO tagihan_pppoe (user_id,pppoe_id,bulan,amount,created_at) VALUES (?,?,?,?,?)",
            (iid, pid, bulan, amount, now)
        )
        changes = con.execute("SELECT changes()").fetchone()[0]
        con.commit()
        con.close()
        prorate_info = f" (prorate {amount:,} dari {harga:,})" if amount != harga else ""
        if changes:
            return JSONResponse({"ok": True, "msg": f"Tagihan {bulan} dibuat — Rp {amount:,}{prorate_info}"})
        else:
            return JSONResponse({"ok": False, "msg": f"Tagihan {bulan} sudah ada"})
    except Exception as e:
        con.close()
        return JSONResponse({"ok": False, "msg": str(e)})


@app.post("/pppoe/tagihan/{tid}/lunas", response_class=JSONResponse)
async def tagihan_lunas(request: Request, tid: int):
    user = require_login(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    metode     = (body.get("metode") or "Manual").strip()
    keterangan = (body.get("keterangan") or "").strip()
    t = db.get_tagihan(tid)
    ok = db.bayar_tagihan(tid, user["id"], metode=metode, keterangan=keterangan)
    if ok and t:
        _log(request, user, "Bayar Tagihan", f"{t['nama_pelanggan']} — {_label_bulan(t['bulan'])} — Rp {t['amount']:,} ({metode})")
        _reaktivasi_pppoe(t["pppoe_id"], t["user_id"])
        if t.get("telepon"):
            label      = _label_bulan(t["bulan"])
            tok        = _isp_wa_token(t["user_id"])
            nominal    = f"Rp {t['amount']:,}".replace(",", ".")
            link_nota  = f"https://{APP_DOMAIN}/nota/{tid}"
            send_wa(
                t["telepon"],
                _render_wa_template(t["user_id"], "pembayaran",
                    nama=t["nama_pelanggan"], nominal=nominal,
                    bulan=label, isp=user["nama"], link_nota=link_nota),
                token=tok, user_id=t["user_id"], tipe="pembayaran"
            )
    return JSONResponse({"ok": ok})

@app.post("/pppoe/tagihan/{tid}/edit", response_class=JSONResponse)
async def tagihan_edit(request: Request, tid: int):
    user = require_login(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    amount = body.get("amount")
    status = (body.get("status") or "").strip()
    if amount is None or int(amount) < 0:
        return JSONResponse({"ok": False, "msg": "Nominal tidak valid"})
    if status not in ("unpaid", "paid", "overdue"):
        return JSONResponse({"ok": False, "msg": "Status tidak valid"})
    t = db.get_tagihan(tid)
    if not t or t["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "msg": "Tagihan tidak ditemukan"})
    con = db._conn()
    paid_at = int(__import__("time").time()) if status == "paid" else None
    con.execute("UPDATE tagihan_pppoe SET amount=?, status=?, paid_at=? WHERE id=?",
                (int(amount), status, paid_at, tid))
    con.commit()
    con.close()
    _log(request, user, "Edit Tagihan", f"{t['nama_pelanggan']} — {_label_bulan(t['bulan'])} — Rp {amount:,} ({status})")
    return JSONResponse({"ok": True})

@app.post("/pppoe/tagihan/{tid}/hapus", response_class=JSONResponse)
async def tagihan_hapus(request: Request, tid: int):
    """Hapus tagihan."""
    user = require_login(request)
    t = db.get_tagihan(tid)
    if not t or t["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "msg": "Tagihan tidak ditemukan"})
    
    con = db._conn()
    con.execute("DELETE FROM tagihan_pppoe WHERE id=?", (tid,))
    con.commit()
    con.close()
    
    _log(request, user, "Hapus Tagihan", f"{t['nama_pelanggan']} — {_label_bulan(t['bulan'])} — Rp {t['amount']:,}")
    return JSONResponse({"ok": True})


@app.post("/pppoe/tagihan/{tid}/kirim-link", response_class=JSONResponse)
async def tagihan_kirim_link(request: Request, tid: int):
    """Kirim link tagihan via WA (QRIS/transfer manual)."""
    user = require_login(request)
    t = db.get_tagihan(tid)
    if not t or t["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "msg": "Tagihan tidak ditemukan"})
    if t["status"] == "paid":
        return JSONResponse({"ok": False, "msg": "Tagihan sudah lunas"})
    if not t.get("telepon"):
        return JSONResponse({"ok": False, "msg": "Nomor WA pelanggan belum diisi"})

    label = _label_bulan(t["bulan"])
    link = f"https://{APP_DOMAIN}/bayar/tagihan/{tid}"
    nominal = f"Rp {t['amount']:,}".replace(",", ".")
    send_wa(
        t["telepon"],
        _render_wa_template(user["id"], "tagihan_link",
            nama=t["nama_pelanggan"], nominal=nominal,
            bulan=label, link=link,
            paket=t.get("paket_nama") or "", isp=user["nama"]),
        token=_isp_wa_token(user["id"])
    )
    return JSONResponse({"ok": True, "link": link})


@app.post("/pppoe/users/kirim-reminder/{pid}", response_class=JSONResponse)
async def kirim_reminder_pelanggan(request: Request, pid: int):
    user = require_login(request)
    iid = _isp_id(user)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    pu = con.execute("SELECT * FROM pppoe_users WHERE id=? AND user_id=?", (pid, iid)).fetchone()
    con.close()
    if not pu:
        return JSONResponse({"ok": False, "msg": "Pelanggan tidak ditemukan"})
    pu = dict(pu)
    if not pu.get("telepon"):
        return JSONResponse({"ok": False, "msg": "Pelanggan tidak punya nomor WA"})
    from datetime import date
    bulan = date.today().strftime("%Y-%m")
    label = _label_bulan(bulan)
    tagihan_list = db.list_tagihan(iid, bulan)
    tagihan = next((t for t in tagihan_list if t["pppoe_id"] == pid and t["status"] != "paid"), None)
    if tagihan:
        nominal = f"Rp {tagihan['amount']:,}".replace(",", ".")
        link = f"https://{APP_DOMAIN}/bayar/tagihan/{tagihan['id']}"
        pesan = (
            f"🔔 *Reminder Tagihan Internet*\n\n"
            f"Halo *{pu['nama_pelanggan']}*,\n\n"
            f"Tagihan bulan *{label}* senilai *{nominal}* belum terbayar.\n\n"
            f"Bayar sekarang:\n{link}\n\n"
            f"_Abaikan jika sudah membayar._"
        )
    else:
        pesan = _render_wa_template(iid, "penagihan",
            nama=pu["nama_pelanggan"], nominal="", bulan=label,
            paket="", tgl_bayar=pu.get("tgl_bayar") or 1, isp=user["nama"])
    ok, msg = send_wa(pu["telepon"], pesan, token=_isp_wa_token(iid),
                      user_id=iid, tipe="link_bayar")
    return JSONResponse({"ok": ok, "msg": msg})


@app.get("/pppoe/tagihan/export")
async def tagihan_export(request: Request, bulan: str = ""):
    """Export tagihan bulan tertentu sebagai CSV."""
    import csv, io
    from fastapi.responses import StreamingResponse
    user = require_login(request)
    if not bulan:
        bulan = _bulan_sekarang()
    tagihan_list = db.list_tagihan(user["id"], bulan)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["No", "Nama Pelanggan", "Username", "Paket", "Server",
                     "Tgl Bayar", "Nominal", "Status", "Metode", "Tgl Lunas", "Telepon"])
    for i, t in enumerate(tagihan_list, 1):
        paid_at = ""
        if t.get("paid_at"):
            from datetime import datetime
            paid_at = datetime.fromtimestamp(t["paid_at"]).strftime("%d/%m/%Y %H:%M")
        writer.writerow([
            i,
            t.get("nama_pelanggan", ""),
            t.get("pppoe_username", ""),
            t.get("paket_nama", ""),
            t.get("server_nama", ""),
            t.get("tgl_bayar", ""),
            t.get("amount", 0),
            t.get("status", ""),
            t.get("metode_bayar", ""),
            paid_at,
            t.get("telepon", ""),
        ])
    output.seek(0)
    filename = f"tagihan_{bulan}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.post("/pppoe/tagihan/reminder", response_class=JSONResponse)
async def tagihan_reminder(request: Request, bulan: str = Form("")):
    user = require_login(request)
    if not bulan:
        bulan = _bulan_sekarang()
    tagihan = db.list_tagihan(user["id"], bulan, "unpaid")
    tagihan += db.list_tagihan(user["id"], bulan, "overdue")
    terkirim = 0
    for t in tagihan:
        if not t.get("telepon"):
            continue
        label = _label_bulan(bulan)
        nominal = f"Rp {t['amount']:,}".replace(",", ".")
        pesan = _render_wa_template(user["id"], "penagihan",
            nama=t["nama_pelanggan"], nominal=nominal, bulan=label,
            paket=t.get("paket_nama") or "", tgl_bayar=t.get("tgl_bayar") or 1,
            isp=user["nama"])
        send_wa(t["telepon"], pesan, token=_isp_wa_token(user["id"]),
                user_id=user["id"], tipe="reminder_manual")
        terkirim += 1
    return JSONResponse({"ok": True, "terkirim": terkirim})


# ── WA Log ────────────────────────────────────────────────────────────────────

@app.get("/wa-log", response_class=HTMLResponse)
async def wa_log_page(request: Request, tipe: str = "", status: str = "", q: str = ""):
    user = require_login(request)
    iid  = _isp_id(user)
    logs = db.list_wa_log(iid, tipe=tipe, status=status, q=q)
    return tpl.TemplateResponse(request, "wa_log.html", _ctx(
        request, user=user, logs=logs,
        sel_tipe=tipe, sel_status=status, q=q,
        active="wa_log"
    ))


@app.get("/pppoe/wa-log", response_class=HTMLResponse)
async def wa_log_redirect(request: Request):
    return RedirectResponse("/wa-log", status_code=301)


@app.post("/wa-log/{lid}/kirim-ulang", response_class=JSONResponse)
async def wa_log_kirim_ulang(request: Request, lid: int):
    user = require_login(request)
    iid  = _isp_id(user)
    log  = db.get_wa_log(lid)
    if not log or log["user_id"] != iid:
        return JSONResponse({"ok": False, "msg": "Log tidak ditemukan"})
    tok = log.get("token") or _isp_wa_token(iid)
    ok, err = send_wa(log["nomor"], log["pesan"], token=tok,
                      user_id=iid, tipe=log["tipe"])
    return JSONResponse({"ok": ok, "msg": err or "Pesan berhasil dikirim ulang"})


@app.post("/pppoe/wa-log/{lid}/kirim-ulang", response_class=JSONResponse)
async def wa_log_kirim_ulang_old(request: Request, lid: int):
    return RedirectResponse(f"/wa-log/{lid}/kirim-ulang", status_code=307)


@app.post("/wa-log/{lid}/hapus", response_class=JSONResponse)
async def wa_log_hapus(request: Request, lid: int):
    user = require_login(request)
    iid  = _isp_id(user)
    ok   = db.delete_wa_log(lid, iid)
    return JSONResponse({"ok": ok, "msg": "" if ok else "Log tidak ditemukan"})


@app.post("/wa/kirim-manual", response_class=JSONResponse)
async def wa_kirim_manual(request: Request):
    """Kirim WA manual ke nomor tertentu (dari UI admin)."""
    user = require_login(request)
    iid  = _isp_id(user)
    body = await request.json()
    nomor = str(body.get("nomor", "")).strip()
    pesan = str(body.get("pesan", "")).strip()
    if not nomor:
        return JSONResponse({"ok": False, "msg": "Nomor HP kosong"})
    if not pesan:
        return JSONResponse({"ok": False, "msg": "Pesan kosong"})
    ok, err = send_wa(nomor, pesan, user_id=iid, tipe="manual")
    return JSONResponse({"ok": ok, "msg": err or "Pesan berhasil dikirim"})


@app.post("/api/wa/send", response_class=JSONResponse)
async def api_wa_send(request: Request):
    """Internal API — kirim WA tanpa session (pakai API key). Untuk integrasi antar service."""
    body = await request.json()
    if body.get("api_key") != INTERNAL_API_KEY or not INTERNAL_API_KEY:
        return JSONResponse({"ok": False, "msg": "Unauthorized"}, status_code=401)
    nomor = str(body.get("nomor", "")).strip()
    pesan = str(body.get("pesan", "")).strip()
    user_id = str(body.get("user_id", "")).strip()
    tipe  = str(body.get("tipe", "info")).strip()
    if not nomor:
        return JSONResponse({"ok": False, "msg": "Nomor kosong"})
    if not pesan:
        return JSONResponse({"ok": False, "msg": "Pesan kosong"})
    ok, err = send_wa(nomor, pesan, user_id=user_id, tipe=tipe)
    return JSONResponse({"ok": ok, "msg": err or "Terkirim"})


# ── Hotspot Paket ─────────────────────────────────────────────────────────────

@app.get("/hotspot/paket", response_class=HTMLResponse)
async def hotspot_paket(request: Request):
    user = require_login(request)
    pakets  = db.list_paket_hotspot(user["id"])
    servers = db.list_servers(user["id"])
    return tpl.TemplateResponse(request, "hotspot_paket.html", _ctx(request, user=user, pakets=pakets, servers=servers))


@app.get("/hotspot/paket/profiles")
async def hotspot_paket_profiles(request: Request, server_id: str):
    user = require_login(request)
    s = db.get_server(server_id)
    if not s or s["user_id"] != user["id"]:
        return JSONResponse({"error": "Server tidak ditemukan"}, status_code=404)
    mt = get_mt(server_id)
    if not mt:
        return JSONResponse({"error": "Tidak dapat terhubung ke router"}, status_code=500)
    profiles = mt.list_hotspot_profiles_detail()
    return JSONResponse({"profiles": profiles})


@app.post("/hotspot/paket/import", response_class=JSONResponse)
async def hotspot_paket_import(request: Request):
    """Import beberapa profile MikroTik sekaligus menjadi paket hotspot."""
    user = require_login(request)
    body = await request.json()
    pakets = body.get("pakets", [])  # [{nama, durasi, kecepatan, harga}]
    # Nama paket yang sudah ada — skip duplikat
    existing = {p["nama"].lower() for p in db.list_paket_hotspot(user["id"])}
    dibuat = 0
    for p in pakets:
        nama = (p.get("nama") or "").strip()
        if not nama or nama.lower() in existing:
            continue
        db.create_paket_hotspot(
            user["id"], nama,
            p.get("durasi") or "1d",
            p.get("kecepatan") or "",
            int(p.get("harga") or 0),
            p.get("server_id") or ""
        )
        existing.add(nama.lower())
        dibuat += 1
    return JSONResponse({"ok": True, "dibuat": dibuat})


@app.post("/hotspot/paket/tambah")
async def hotspot_paket_tambah(
    request: Request,
    nama: str = Form(...), durasi: str = Form(...),
    kecepatan: str = Form(""), rate_limit: str = Form(""), harga: int = Form(...),
    server_id: str = Form("")
):
    user = require_login(request)
    profile = kecepatan or rate_limit
    db.create_paket_hotspot(user["id"], nama, durasi, profile, harga, server_id)
    return RedirectResponse("/hotspot/paket", status_code=302)


@app.post("/hotspot/paket/edit/{pid}")
async def hotspot_paket_edit(
    request: Request, pid: int,
    nama: str = Form(...), durasi: str = Form(...),
    kecepatan: str = Form(""), rate_limit: str = Form(""), harga: int = Form(...),
    server_id: str = Form("")
):
    user = require_login(request)
    profile = kecepatan or rate_limit
    db.update_paket_hotspot(pid, user["id"], nama, durasi, profile, harga, server_id)
    return RedirectResponse("/hotspot/paket", status_code=302)


@app.post("/hotspot/paket/hapus/{pid}")
async def hotspot_paket_hapus(request: Request, pid: int):
    user = require_login(request)
    con = db._conn()
    row = con.execute("SELECT user_id FROM paket_hotspot WHERE id=?", (pid,)).fetchone()
    if row and row[0] == user["id"]:
        con.execute("UPDATE paket_hotspot SET status='nonaktif' WHERE id=?", (pid,))
        con.commit()
    con.close()
    return RedirectResponse("/hotspot/paket", status_code=302)


# ── Voucher Hotspot ───────────────────────────────────────────────────────────

@app.get("/hotspot/voucher", response_class=HTMLResponse)
async def hotspot_voucher(request: Request, server_id: str = "", status: str = "", paket_id: str = "", comment: str = "", tab: str = "stok"):
    user = require_login(request)
    servers  = db.list_servers(user["id"])
    pakets   = db.list_paket_hotspot(user["id"])
    comments = db.list_voucher_comments(user["id"])
    comments_detail = db.list_voucher_comments_with_agen(user["id"])
    vouchers = db.list_vouchers(user["id"], server_id or None, status or None, paket_id or None, comment or None)
    transaksi = db.list_voucher_transaksi(user["id"])
    import sqlite3 as _sq
    _con = _sq.connect(DB_PATH)
    _con.row_factory = _sq.Row
    _uid = user["id"]
    _sid_filter = f" AND server_id='{server_id}'" if server_id else ""
    stats_v = {
        "tersedia": _con.execute(f"SELECT COUNT(*) FROM voucher_hotspot WHERE user_id=? AND status='tersedia'{_sid_filter}", (_uid,)).fetchone()[0],
        "terjual":  _con.execute(f"SELECT COUNT(*) FROM voucher_hotspot WHERE user_id=? AND status='terjual'{_sid_filter}",  (_uid,)).fetchone()[0],
        "dipakai":  _con.execute(f"SELECT COUNT(*) FROM voucher_hotspot WHERE user_id=? AND status='dipakai'{_sid_filter}",  (_uid,)).fetchone()[0],
        "expired":  _con.execute(f"SELECT COUNT(*) FROM voucher_hotspot WHERE user_id=? AND status='expired'{_sid_filter}",  (_uid,)).fetchone()[0],
    }
    _con.close()
    return tpl.TemplateResponse(request, "voucher.html", _ctx(
        request, user=user, vouchers=vouchers, servers=servers, pakets=pakets,
        comments=comments, comments_detail=comments_detail,
        sel_server=server_id, sel_status=status, sel_paket=paket_id, sel_comment=comment,
        stats_v=stats_v, transaksi=transaksi, active_tab=tab
    ))


@app.post("/hotspot/voucher/generate")
async def voucher_generate(
    request: Request,
    server_id: str = Form(...), paket_id: int = Form(...),
    jumlah: int = Form(...), push_mikrotik: str = Form(""),
    comment: str = Form("")
):
    user = require_login(request)
    jumlah = min(jumlah, 500)
    kodes = db.create_vouchers(user["id"], server_id, paket_id, jumlah, comment.strip())

    push_ok = 0
    push_fail = 0
    push_attempted = False

    if push_mikrotik:
        push_attempted = True
        paket = db.get_paket_hotspot(paket_id)
        mt = get_mt(server_id)
        if mt and paket:
            profile      = paket.get("kecepatan") or "default"
            mt_comment   = comment.strip() or paket.get("nama", "")
            limit_uptime = paket.get("durasi") or ""
            for kode in kodes:
                ok = mt.add_hotspot_user(kode, kode, profile=profile,
                                         comment=mt_comment, limit_uptime=limit_uptime)
                if ok:
                    push_ok += 1
                    db.set_voucher_mt_pushed(kode, True)
                else:
                    push_fail += 1
        else:
            push_fail = len(kodes)

    if push_attempted:
        if push_fail == 0:
            status = f"generate_ok&pushed={push_ok}"
        else:
            status = f"generate_push_error&pushed={push_ok}&failed={push_fail}"
    else:
        status = f"generate_ok&pushed=0"

    return RedirectResponse(f"/hotspot/voucher?ok={status}&jumlah={len(kodes)}", status_code=302)


@app.get("/hotspot/voucher/mt-users", response_class=JSONResponse)
async def mt_users_list(request: Request, server_id: str = "", profile: str = "",
                        username: str = "", comment: str = ""):
    user = require_login(request)
    if not server_id:
        return JSONResponse({"ok": False, "msg": "Pilih router dulu", "data": [], "total": 0})
    mt = get_mt(server_id)
    if not mt:
        return JSONResponse({"ok": False, "msg": "Server tidak bisa dihubungi", "data": [], "total": 0})
    try:
        rows = mt.list_hotspot_users(profile or "", username.strip(), comment.strip())
        return JSONResponse({"ok": True, "total": len(rows), "data": rows})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e), "data": [], "total": 0})


@app.post("/hotspot/voucher/mt-delete", response_class=JSONResponse)
async def mt_user_delete(request: Request, server_id: str = Form(...), mt_id: str = Form(...), kode: str = Form("")):
    user = require_login(request)
    mt = get_mt(server_id)
    if not mt:
        return JSONResponse({"ok": False, "msg": "Server tidak bisa dihubungi"})
    ok = mt.remove_hotspot_user_by_id(mt_id)
    if ok and kode:
        con = sqlite3.connect(DB_PATH)
        con.execute("UPDATE voucher_hotspot SET status='expired' WHERE user_id=? AND kode=?", (user["id"], kode))
        con.commit(); con.close()
    return JSONResponse({"ok": ok, "msg": "Berhasil dihapus dari MikroTik" if ok else "Gagal hapus"})


@app.post("/hotspot/voucher/mt-edit", response_class=JSONResponse)
async def mt_user_edit(request: Request, server_id: str = Form(...), mt_id: str = Form(...),
                       profile: str = Form(""), comment: str = Form(""),
                       password: str = Form(""), limit_uptime: str = Form("")):
    user = require_login(request)
    mt = get_mt(server_id)
    if not mt:
        return JSONResponse({"ok": False, "msg": "Server tidak bisa dihubungi"})
    ok = mt.edit_hotspot_user(mt_id, profile, comment, password, limit_uptime)
    return JSONResponse({"ok": ok, "msg": "Berhasil diupdate" if ok else "Gagal update"})


@app.post("/hotspot/voucher/tambah", response_class=JSONResponse)
async def voucher_tambah(
    request: Request,
    server_id: str = Form(...), paket_id: int = Form(...),
    kode: str = Form(""), push_mikrotik: str = Form(""),
    comment: str = Form("")
):
    user = require_login(request)
    import random, string as _str
    kode = kode.strip().upper()
    if not kode:
        kode = "".join(random.choices(_str.ascii_uppercase + _str.digits, k=8))

    ok = db.create_voucher_single(user["id"], server_id, paket_id, kode, comment.strip())
    if not ok:
        return JSONResponse({"ok": False, "msg": f"Kode '{kode}' sudah digunakan."}, status_code=400)

    push_ok = False
    if push_mikrotik:
        paket = db.get_paket_hotspot(paket_id)
        mt = get_mt(server_id)
        if mt and paket:
            profile      = paket.get("kecepatan") or "default"
            mt_comment   = comment.strip() or paket.get("nama", "")
            limit_uptime = paket.get("durasi") or ""
            push_ok = mt.add_hotspot_user(kode, kode, profile=profile,
                                           comment=mt_comment, limit_uptime=limit_uptime)
            if push_ok:
                db.set_voucher_mt_pushed(kode, True)

    return JSONResponse({"ok": True, "kode": kode, "pushed": push_ok})


@app.post("/hotspot/voucher/hapus")
async def voucher_hapus(request: Request, server_id: str = Form(...), status: str = Form("tersedia")):
    user = require_login(request)
    db.delete_vouchers(user["id"], server_id, status)
    return RedirectResponse("/hotspot/voucher", status_code=302)


@app.post("/hotspot/voucher/hapus-comment")
async def voucher_hapus_comment(request: Request, comment: str = Form(...), status: str = Form("tersedia")):
    user = require_login(request)
    db.delete_vouchers_by_comment(user["id"], comment, status)
    return RedirectResponse(f"/hotspot/voucher", status_code=302)


@app.post("/hotspot/voucher/hapus-kode", response_class=JSONResponse)
async def voucher_hapus_kode(request: Request, kode: str = Form(...), server_id: str = Form("")):
    """Hapus satu voucher dari DB dan MikroTik (JSON response untuk fetch)."""
    user = require_login(request)
    deleted = db.delete_voucher_kode(user["id"], kode)
    if not deleted:
        return JSONResponse({"ok": False, "msg": "Voucher tidak ditemukan atau sudah dipakai"})
    if server_id:
        server = db.get_server(server_id)
        if server and server["user_id"] == user["id"]:
            try:
                mt = MikroTik(server["vpn_ip"], server["api_port"],
                              server["api_user"], server["api_password"])
                mt.remove_hotspot_user(kode)
            except Exception:
                pass
    return JSONResponse({"ok": True})


@app.post("/hotspot/voucher/hapus-kode-form")
async def voucher_hapus_kode_form(request: Request, kode: str = Form(...), server_id: str = Form("")):
    """Hapus satu voucher dari DB dan MikroTik (form POST, redirect kembali)."""
    user = require_login(request)
    db.delete_voucher_kode(user["id"], kode)
    if server_id:
        server = db.get_server(server_id)
        if server and server["user_id"] == user["id"]:
            try:
                mt = MikroTik(server["vpn_ip"], server["api_port"],
                              server["api_user"], server["api_password"])
                mt.remove_hotspot_user(kode)
            except Exception:
                pass
    # Redirect kembali ke halaman sebelumnya dengan filter yang sama
    referer = request.headers.get("referer", "/hotspot/voucher")
    return RedirectResponse(referer, status_code=302)


@app.post("/hotspot/voucher/push", response_class=JSONResponse)
async def voucher_push(request: Request, server_id: str = Form(...), comment: str = Form("")):
    """Push ulang voucher tersedia dari DB ke MikroTik."""
    user = require_login(request)
    server = db.get_server(server_id)
    if not server or server["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "msg": "Server tidak ditemukan"})
    vouchers = db.list_vouchers(user["id"], server_id, "tersedia", None, comment or None)
    if not vouchers:
        return JSONResponse({"ok": False, "msg": "Tidak ada voucher tersedia untuk di-push"})
    try:
        mt = MikroTik(server["vpn_ip"], server["api_port"],
                      server["api_user"], server["api_password"])
    except Exception as e:
        return JSONResponse({"ok": False, "msg": f"Gagal koneksi ke MikroTik: {e}"})
    berhasil, gagal = 0, 0
    for v in vouchers:
        paket = db.get_paket_hotspot(v["paket_id"]) if v.get("paket_id") else None
        profile = paket.get("kecepatan") or "default" if paket else "default"
        limit_uptime = paket.get("durasi") or "" if paket else ""
        mt_comment = v.get("comment") or (paket.get("nama", "") if paket else "")
        try:
            ok = mt.add_hotspot_user(v["kode"], v["kode"], profile=profile,
                                    comment=mt_comment, limit_uptime=limit_uptime)
            if ok:
                db.set_voucher_mt_pushed(v["kode"], True)
                berhasil += 1
            else:
                gagal += 1
        except Exception:
            gagal += 1
    return JSONResponse({"ok": True, "berhasil": berhasil, "gagal": gagal,
                         "msg": f"{berhasil} voucher di-push, {gagal} gagal/sudah ada"})


@app.get("/hotspot/voucher/print", response_class=HTMLResponse)
async def voucher_print(request: Request, server_id: str = "", paket_id: str = "", comment: str = ""):
    user = require_login(request)
    comment_filter = "" if comment == "__none__" else (comment or None)
    vouchers = db.list_vouchers(
        user["id"],
        server_id or None,
        "tersedia",
        paket_id or None,
        comment_filter
    ) if (comment != "") else []
    pakets   = db.list_paket_hotspot(user["id"])
    comments = db.list_voucher_comments(user["id"])
    return tpl.TemplateResponse(request, "voucher_print.html", _ctx(
        request, user=user, vouchers=vouchers, pakets=pakets,
        comments=comments, sel_comment=comment, print_base_url="/hotspot/voucher/print"
    ))

# ── Agen Management ───────────────────────────────────────────────────────────

@app.get("/tim", response_class=HTMLResponse)
async def tim_page(request: Request):
    user = require_login(request)
    if user["role"] not in ("admin", "agen"):
        return RedirectResponse("/dashboard", status_code=302)
    if user["role"] == "admin":
        agenlist = db.list_users(role="agen", parent_id=user["id"])
        teknisi_list = db.list_teknisi(user["id"])
    else:
        agenlist = db.list_users(role="sub_agen", parent_id=user["id"])
        teknisi_list = []
    stats = {a["id"]: db.stats_agen(a["id"]) for a in agenlist}
    isp_slug = user.get("slug") or user.get("username", "")
    hotspot_pakets = db.list_paket_hotspot(user["id"])
    return tpl.TemplateResponse(request, "tim.html", _ctx(
        request, user=user, agenlist=agenlist, teknisi_list=teknisi_list,
        stats=stats, isp_slug=isp_slug, app_domain=APP_DOMAIN,
        hotspot_pakets=hotspot_pakets,
        ok=request.query_params.get("ok"),
        error=request.query_params.get("error"),
    ))


@app.post("/tim/agen-paket", response_class=JSONResponse)
async def tim_agen_paket(request: Request):
    """Simpan paket mana yang boleh diakses agen untuk generate voucher."""
    user = require_login(request)
    if user["role"] not in ("admin", "agen"):
        return JSONResponse({"ok": False, "msg": "Tidak diizinkan"})
    body = await request.json()
    enabled_ids = [int(i) for i in body.get("enabled_ids", []) if str(i).isdigit()]
    db.set_agen_paket_akses(user["id"], enabled_ids)
    return JSONResponse({"ok": True, "msg": f"{len(enabled_ids)} paket diaktifkan untuk agen"})


@app.get("/agen", response_class=HTMLResponse)
async def agen_page(request: Request):
    return RedirectResponse("/tim", status_code=302)

@app.get("/teknisi-admin", response_class=HTMLResponse)
async def teknisi_admin_redirect(request: Request):
    return RedirectResponse("/tim?tab=teknisi", status_code=302)


@app.post("/agen/tambah")
async def agen_tambah(
    request: Request,
    nama: str = Form(...), username: str = Form(...),
    password: str = Form(...), nomor_wa: str = Form(""),
    role: str = Form("agen")
):
    user = require_login(request)
    if db.username_exists(username):
        return RedirectResponse("/agen?error=username_exists", status_code=302)
    if user["role"] == "admin" and role not in ("agen", "sub_agen", "teknisi"):
        role = "agen"
    if user["role"] == "agen":
        role = "sub_agen"
    parent_id = user["id"]  # selalu set parent ke user yang membuat
    db.create_user(nama, username, password, role, parent_id, nomor_wa)
    _log(request, user, f"Tambah {role.title()}", f"{nama} ({username})")
    return RedirectResponse("/agen", status_code=302)


@app.post("/agen/status/{uid}")
async def agen_status(request: Request, uid: str, status: str = Form(...)):
    require_login(request)
    db.update_user_status(uid, status)
    return RedirectResponse("/agen", status_code=302)


@app.post("/agen/edit/{uid}")
async def agen_edit(request: Request, uid: str, nama: str = Form(...), nomor_wa: str = Form(""),
                    komisi_persen: int = Form(0)):
    user = require_login(request)
    target = db.get_user(uid)
    if target and (user["role"] == "admin" or target.get("parent_id") == user["id"]):
        db.update_user(uid, nama, nomor_wa)
        db.set_komisi(uid, komisi_persen)
    return RedirectResponse("/agen", status_code=302)


@app.post("/agen/reset-password/{uid}")
async def agen_reset_password(request: Request, uid: str, password: str = Form(...)):
    user = require_login(request)
    target = db.get_user(uid)
    if target and (user["role"] == "admin" or target.get("parent_id") == user["id"]):
        db.reset_password(uid, password)
    return RedirectResponse("/agen", status_code=302)


@app.post("/agen/hapus/{uid}")
async def agen_hapus(request: Request, uid: str):
    user = require_login(request)
    target = db.get_user(uid)
    if target and (user["role"] == "admin" or target.get("parent_id") == user["id"]):
        db.delete_user(uid)
        _log(request, user, f"Hapus {target.get('role','').title()}", f"{target['nama']} ({target['username']})")
    return RedirectResponse("/agen", status_code=302)


# ── Manajemen Teknisi (admin billing) ────────────────────────────────────────



@app.post("/teknisi-admin/tambah")
async def teknisi_tambah(
    request: Request,
    nama: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    nomor_wa: str = Form(""),
):
    user = require_login(request)
    if user["role"] != "admin":
        return RedirectResponse("/dashboard", status_code=302)
    if db.username_exists(username):
        return RedirectResponse("/teknisi-admin?error=username_exists", status_code=302)
    db.create_user(nama, username, password, "teknisi", user["id"], nomor_wa)
    return RedirectResponse("/teknisi-admin?ok=created", status_code=302)


@app.post("/teknisi-admin/edit/{uid}")
async def teknisi_edit(request: Request, uid: str, nama: str = Form(...), nomor_wa: str = Form("")):
    user = require_login(request)
    if user["role"] != "admin":
        return RedirectResponse("/dashboard", status_code=302)
    db.update_user(uid, nama, nomor_wa)
    return RedirectResponse("/teknisi-admin", status_code=302)


@app.post("/teknisi-admin/reset-password/{uid}")
async def teknisi_reset_pw(request: Request, uid: str, password: str = Form(...)):
    user = require_login(request)
    if user["role"] != "admin":
        return RedirectResponse("/dashboard", status_code=302)
    db.reset_password(uid, password)
    return RedirectResponse("/teknisi-admin", status_code=302)


@app.post("/teknisi-admin/status/{uid}")
async def teknisi_toggle_status(request: Request, uid: str, status: str = Form(...)):
    user = require_login(request)
    if user["role"] != "admin":
        return RedirectResponse("/dashboard", status_code=302)
    db.update_user_status(uid, status)
    return RedirectResponse("/teknisi-admin", status_code=302)


@app.post("/teknisi-admin/hapus/{uid}")
async def teknisi_hapus(request: Request, uid: str):
    user = require_login(request)
    if user["role"] != "admin":
        return RedirectResponse("/dashboard", status_code=302)
    db.delete_user(uid)
    return RedirectResponse("/teknisi-admin", status_code=302)


# ── Saldo ─────────────────────────────────────────────────────────────────────

@app.get("/saldo", response_class=HTMLResponse)
async def saldo_page(request: Request, bulan: str = "", tab: str = "mutasi", tipe: str = ""):
    from datetime import date, datetime
    user = require_login(request)
    iid  = _isp_id(user)
    if not bulan:
        bulan = date.today().strftime("%Y-%m")

    def _in_bulan(ts, bln):
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m") == bln
        except Exception:
            return False

    # ── Tab 1: Mutasi Saldo ──────────────────────────────────────────────────
    logs_all = db.list_saldo_log(user["id"])
    logs = [l for l in logs_all if _in_bulan(l["created_at"], bulan)]
    total_kredit = sum(l["jumlah"] for l in logs if l["tipe"] == "kredit")
    total_debit  = sum(l["jumlah"] for l in logs if l["tipe"] == "debit")
    kredit_mayar = sum(l["jumlah"] for l in logs if l["tipe"] == "kredit" and l.get("sumber") == "mayar")
    kredit_qris  = sum(l["jumlah"] for l in logs if l["tipe"] == "kredit" and l.get("sumber") == "qris_statis")

    # ── Tab 2: Riwayat Transaksi ─────────────────────────────────────────────
    con = db._conn()
    orders = con.execute(
        """SELECT o.id, o.amount, o.status, o.nomor_hp, o.created_at,
                  p.nama as paket_nama, 'hotspot_order' as tipe,
                  tx.amount as nett_amount
           FROM hotspot_orders o
           LEFT JOIN paket_hotspot p ON p.id = o.paket_id
           LEFT JOIN transaksi tx ON tx.ref_id = o.id AND tx.ref_type = 'voucher'
           WHERE o.user_id=? ORDER BY o.created_at DESC LIMIT 300""",
        (iid,)
    ).fetchall()
    tagihan = con.execute(
        """SELECT t.id, t.amount, t.status, t.paid_at, t.created_at,
                  u.nama_pelanggan as pelanggan, t.bulan, 'tagihan_pppoe' as tipe
           FROM tagihan_pppoe t
           LEFT JOIN pppoe_users u ON u.id = t.pppoe_id
           WHERE t.user_id=? ORDER BY t.created_at DESC LIMIT 300""",
        (iid,)
    ).fetchall()
    txs_raw = con.execute(
        "SELECT *, 'internal' as tipe FROM transaksi WHERE user_id=? ORDER BY created_at DESC LIMIT 300",
        (iid,)
    ).fetchall()
    tarik_pending = con.execute(
        "SELECT COUNT(*) FROM tarik_saldo WHERE user_id=? AND status='pending'", (user["id"],)
    ).fetchone()[0]
    con.close()

    all_txs = []
    for o in orders:
        nett = o["nett_amount"]
        fee  = (o["amount"] - nett) if nett else None
        ket  = f"Voucher {o['paket_nama'] or ''} · {o['nomor_hp'] or ''}"
        if fee:
            ket += f" (nett Rp {nett:,} · fee Rp {fee:,})".replace(",", ".")
        all_txs.append({"id": o["id"], "tipe": "hotspot_order",
            "keterangan": ket,
            "amount": o["amount"], "status": o["status"], "created_at": o["created_at"]})
    for t in tagihan:
        all_txs.append({"id": str(t["id"]), "tipe": "tagihan_pppoe",
            "keterangan": f"Tagihan {t['bulan']} · {t['pelanggan'] or ''}",
            "amount": t["amount"], "status": t["status"], "created_at": t["created_at"]})
    for tx in txs_raw:
        # Skip transaksi voucher — sudah ditampilkan via hotspot_orders (hindari duplikasi)
        if tx["ref_type"] == "voucher":
            continue
        all_txs.append({"id": tx["id"], "tipe": tx["ref_type"] if tx["ref_type"] else "internal",
            "keterangan": tx["keterangan"],
            "amount": tx["amount"], "status": tx["status"], "created_at": tx["created_at"]})

    if tipe:
        all_txs = [t for t in all_txs if t["tipe"] == tipe]
    txs = [t for t in all_txs if _in_bulan(t["created_at"], bulan)]
    txs.sort(key=lambda x: x["created_at"] or 0, reverse=True)

    tx_stats = {
        "total": len(txs),
        "omzet": sum(t["amount"] for t in txs if t["status"] in ("paid", "lunas")),
        "paid":  sum(1 for t in txs if t["status"] in ("paid", "lunas")),
        "pending": sum(1 for t in txs if t["status"] == "pending"),
    }

    saved_rek = {"bank": user.get("rek_bank") or "", "no": user.get("rek_no") or "", "nama": user.get("rek_nama") or ""}
    pending_topup_count = db.count_topup_manual_pending(user["id"]) if user.get("role") == "admin" else 0

    # Tagihan SaaS yang belum lunas
    con2 = db._conn()
    saas_unpaid = con2.execute(
        "SELECT * FROM saas_tagihan WHERE user_id=? AND status IN ('unpaid','waiting_payment') ORDER BY bulan DESC LIMIT 1",
        (user["id"],)
    ).fetchone()
    saas_unpaid = dict(saas_unpaid) if saas_unpaid else None
    con2.close()
    platform_qris = db.get_platform_config("qris_image")

    return tpl.TemplateResponse(request, "saldo.html", _ctx(
        request, user=user, logs=logs, txs=txs, saved_rek=saved_rek,
        sel_bulan=bulan, sel_tab=tab, sel_tipe=tipe,
        stats={"kredit": total_kredit, "debit": total_debit,
               "kredit_mayar": kredit_mayar, "kredit_qris": kredit_qris,
               "total_log": len(logs), "tarik_pending": tarik_pending},
        tx_stats=tx_stats,
        pending_topup_count=pending_topup_count,
        saas_unpaid=saas_unpaid,
        platform_qris=platform_qris))




@app.post("/saldo/tarik", response_class=JSONResponse)
async def saldo_tarik_request(request: Request):
    """Admin ISP ajukan permintaan tarik saldo."""
    user = require_login(request)
    if user["role"] != "admin":
        return JSONResponse({"ok": False, "detail": "Hanya admin yang bisa tarik saldo"}, status_code=403)
    body = await request.json()
    nominal       = int(body.get("nominal") or 0)
    bank_name     = (body.get("bank_name") or "").strip()
    rekening_no   = (body.get("rekening_no") or "").strip()
    rekening_name = (body.get("rekening_name") or "").strip()
    catatan       = (body.get("catatan") or "").strip()

    TARIK_MIN    = 1_000_000
    TARIK_FEE    = 5_000
    total_potong = nominal + TARIK_FEE

    if nominal < TARIK_MIN:
        return JSONResponse({"ok": False, "detail": f"Minimal tarik saldo Rp {TARIK_MIN:,}"})
    if total_potong > user["saldo"]:
        return JSONResponse({"ok": False, "detail": (
            f"Saldo tidak cukup. Dibutuhkan Rp {total_potong:,} "
            f"(nominal Rp {nominal:,} + biaya admin Rp {TARIK_FEE:,}), "
            f"saldo Anda Rp {user['saldo']:,}. Turunkan nominal tarik."
        )})
    if not bank_name or not rekening_no:
        return JSONResponse({"ok": False, "detail": "Nama bank dan nomor rekening wajib diisi"})

    db.save_rekening(user["id"], bank_name, rekening_no, rekening_name)
    rid = db.buat_tarik_saldo(user["id"], user["id"], nominal, bank_name, rekening_no, rekening_name, catatan)

    pesan_admin = (
        f"💸 *Permintaan Tarik Saldo*\n\n"
        f"Nominal: *Rp {nominal:,}*\n"
        f"Biaya admin: Rp {TARIK_FEE:,}\n"
        f"Total dipotong: *Rp {total_potong:,}*\n"
        f"Rekening: *{bank_name}*\n"
        f"No. Rek: `{rekening_no}`\n"
        f"Atas Nama: {rekening_name}\n"
        f"{('Catatan: ' + catatan + chr(10)) if catatan else ''}\n"
        f"Request masuk dan menunggu konfirmasi platform."
    )
    # WA notif ke admin ISP sendiri
    if user.get("nomor_wa"):
        send_wa(user["nomor_wa"], pesan_admin, token=_isp_wa_token(user["id"]))

    # WA notif ke platform SA menggunakan platform token
    platform_token = db.get_platform_config("wa_token") or WA_TOKEN
    sa_wa = db.get_platform_config("wa_number") or PLATFORM_OWNER_WA
    if sa_wa:
        pesan_sa = (
            f"🔔 *[Platform] Permintaan Tarik Saldo ISP*\n\n"
            f"Tenant: *{user['nama']}*\n"
            f"Nominal: *Rp {nominal:,}*\n"
            f"Rekening: {bank_name} — `{rekening_no}`\n"
            f"Atas Nama: {rekening_name}\n"
            f"{('Catatan: ' + catatan + chr(10)) if catatan else ''}"
            f"\nBuka admin.vpntunel.my.id → Tarik Saldo untuk menyetujui."
        )
        send_wa(sa_wa, pesan_sa, token=platform_token)

    return JSONResponse({"ok": True, "id": rid})


@app.get("/saldo/tarik-requests", response_class=HTMLResponse)
async def tarik_requests_page(request: Request):
    """Admin ISP lihat riwayat tarik saldo sendiri."""
    user = require_login(request)
    requests_list = db.list_tarik_saldo(user["id"], as_agen=False)
    pending_count = sum(1 for r in requests_list if r["status"] == "pending")
    return tpl.TemplateResponse(request, "tarik_requests.html", _ctx(
        request, user=user, active="saldo",
        requests_list=requests_list, pending_count=pending_count
    ))


@app.delete("/saldo/tarik/{rid}", response_class=JSONResponse)
async def hapus_tarik_request(request: Request, rid: int):
    """Admin ISP hapus request tarik saldo milik sendiri yang masih pending."""
    user = require_login(request)
    ok = db.hapus_tarik_saldo(rid, user["id"])
    if not ok:
        return JSONResponse({"ok": False, "detail": "Request tidak ditemukan atau sudah diproses"}, status_code=404)
    return JSONResponse({"ok": True})


@app.post("/saldo/topup/{uid}")
async def saldo_topup(request: Request, uid: str, jumlah: int = Form(...), keterangan: str = Form("")):
    user = require_login(request)
    if user["role"] != "admin":
        return RedirectResponse("/saldo", status_code=302)
    db.topup_saldo(uid, jumlah, keterangan, sumber="qris_statis")
    return RedirectResponse("/agen", status_code=302)


# ── QRIS Upload & Topup Manual ────────────────────────────────────────────────

@app.post("/pengaturan/qris-upload")
async def qris_upload(request: Request, qris_file: UploadFile = File(...)):
    user = require_login(request)
    import base64, imghdr
    data = await qris_file.read()
    if len(data) > 500_000:
        return RedirectResponse("/pengaturan?error=File+terlalu+besar+(maks+500KB)", status_code=302)
    b64 = base64.b64encode(data).decode()
    ext = qris_file.filename.rsplit(".", 1)[-1].lower() if "." in qris_file.filename else "png"
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
    db.update_user_field(user["id"], "qris_image", f"data:{mime};base64,{b64}")
    return RedirectResponse("/profil?ok=qris_saved", status_code=302)


@app.get("/pengaturan/qris-image")
async def qris_image(request: Request):
    """Return QRIS image milik ISP (untuk ditampilkan ke agen)."""
    from fastapi.responses import Response
    user = _require_agen(request)
    if not user:
        raise HTTPException(404)
    isp = db.get_user(user["parent_id"]) if user.get("parent_id") else None
    if not isp or not isp.get("qris_image"):
        raise HTTPException(404)
    img_data = isp["qris_image"]
    if img_data.startswith("data:"):
        mime, b64 = img_data.split(";base64,", 1)
        mime = mime.replace("data:", "")
        import base64
        return Response(content=base64.b64decode(b64), media_type=mime)
    raise HTTPException(404)


@app.post("/panel/topup/manual", response_class=JSONResponse)
async def panel_topup_manual(request: Request, amount: int = Form(...), catatan: str = Form("")):
    """Agen ajukan topup manual via QRIS platform."""
    user = _require_agen(request)
    if not user:
        return JSONResponse({"ok": False, "msg": "Tidak terotorisasi"})
    if amount < 5000:
        return JSONResponse({"ok": False, "msg": "Minimal topup Rp 5.000"})
    isp = db.get_user(user["parent_id"]) if user.get("parent_id") else None
    if not isp:
        return JSONResponse({"ok": False, "msg": "ISP tidak ditemukan"})
    oid = db.create_topup_manual(user["id"], isp["id"], amount, catatan.strip())
    # Notif WA ke platform SA (bukan ISP lagi)
    sa_wa = db.get_platform_config("wa_number") or PLATFORM_OWNER_WA
    platform_token = db.get_platform_config("wa_token") or WA_TOKEN
    isp_nama = isp.get("nama") or isp.get("username", "-")
    wa_msg = (f"💳 *[Platform] Permintaan Topup*\n\n"
              f"Agen: *{user['nama']}*\n"
              f"ISP: *{isp_nama}*\n"
              f"Nominal: *Rp {amount:,}*\n"
              f"Catatan: {catatan or '-'}\n\n"
              f"Cek panel SA untuk konfirmasi: /sa?tab=topup")
    if sa_wa:
        send_wa(sa_wa, wa_msg, token=platform_token)
    return JSONResponse({"ok": True, "order_id": oid, "msg": "Permintaan terkirim, tunggu konfirmasi platform"})


@app.get("/saldo/topup-manual", response_class=HTMLResponse)
async def saldo_topup_manual_page(request: Request, status: str = "pending"):
    user = require_login(request)
    orders = db.list_topup_manual_isp(user["id"], status)
    pending_count = db.count_topup_manual_pending(user["id"])
    return tpl.TemplateResponse(request, "topup_manual.html", _ctx(
        request, user=user, orders=orders, sel_status=status, pending_count=pending_count
    ))


@app.post("/panel/topup/midtrans", response_class=JSONResponse)
async def panel_topup_midtrans(request: Request, amount: int = Form(...)):
    """Buat transaksi QRIS Midtrans untuk topup saldo agen."""
    user = _require_agen(request)
    if not user:
        return JSONResponse({"ok": False, "msg": "Tidak terotorisasi"})
    if amount < 5000:
        return JSONResponse({"ok": False, "msg": "Minimal topup Rp 5.000"})
    if not MIDTRANS_SERVER_KEY:
        return JSONResponse({"ok": False, "msg": "Midtrans belum dikonfigurasi"})
    isp = db.get_user(user["parent_id"]) if user.get("parent_id") else None
    if not isp:
        return JSONResponse({"ok": False, "msg": "ISP tidak ditemukan"})
    order_id = f"topup-agen-{user['id']}-{int(time.time())}"
    payload = {
        "transaction_details": {"order_id": order_id, "gross_amount": amount},
        "payment_type": "qris",
        "qris": {"acquirer": "gopay"},
        "customer_details": {"first_name": user.get("nama") or user["username"], "email": user.get("email") or "noreply@vpntunel.my.id"},
        "custom_field1": str(user["id"]),
        "custom_field2": str(isp["id"]),
    }
    import base64
    auth = base64.b64encode(f"{MIDTRANS_SERVER_KEY}:".encode()).decode()
    try:
        resp = requests.post(
            f"{MIDTRANS_BASE}/v2/charge",
            json=payload,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            timeout=15,
        )
        data = resp.json()
    except Exception as e:
        return JSONResponse({"ok": False, "msg": f"Gagal hubungi Midtrans: {e}"})
    if data.get("status_code") not in ("201", "200"):
        return JSONResponse({"ok": False, "msg": data.get("status_message", "Gagal buat transaksi")})
    qr_url = None
    for action in data.get("actions", []):
        if action.get("name") == "generate-qr-code":
            qr_url = action["url"]
            break
    db.create_topup_manual(user["id"], isp["id"], amount, f"QRIS Midtrans #{order_id}", tipe="midtrans", ref=order_id)
    return JSONResponse({"ok": True, "order_id": order_id, "qr_url": qr_url, "amount": amount})


@app.post("/webhook/midtrans", response_class=JSONResponse)
async def webhook_midtrans(request: Request):
    """Terima notifikasi pembayaran dari Midtrans dan kredit saldo agen otomatis."""
    import base64, hashlib
    body = await request.json()
    order_id    = body.get("order_id", "")
    status_code = body.get("status_code", "")
    gross_amount_raw = body.get("gross_amount", "0")  # pakai string asli untuk signature
    gross_amount = int(float(gross_amount_raw))
    transaction_status = body.get("transaction_status", "")
    fraud_status = body.get("fraud_status", "")
    sig_key = body.get("signature_key", "")

    expected_sig = hashlib.sha512(
        f"{order_id}{status_code}{gross_amount_raw}{MIDTRANS_SERVER_KEY}".encode()
    ).hexdigest()
    if sig_key != expected_sig:
        return JSONResponse({"ok": False, "msg": "Invalid signature"}, status_code=403)

    if transaction_status not in ("settlement", "capture") or fraud_status not in ("accept", ""):
        return JSONResponse({"ok": True, "msg": "Ignored"})

    con = db._conn()
    order = con.execute(
        "SELECT * FROM topup_orders WHERE ref=? AND tipe='midtrans' AND status='pending'", (order_id,)
    ).fetchone()
    if not order:
        con.close()
        return JSONResponse({"ok": True, "msg": "Order tidak ditemukan atau sudah diproses"})
    order = dict(order)
    now = int(time.time())
    amount = order["amount"]
    fee = 10_000 if amount > 800_000 else 5_000
    credited = amount - fee

    con.execute("UPDATE topup_orders SET status='paid', paid_at=? WHERE id=?", (now, order["id"]))
    con.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (credited, order["user_id"]))
    ket = f"Topup QRIS Midtrans #{order_id} (dipotong biaya admin Rp {fee:,})"
    con.execute(
        "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,sumber,created_at) VALUES (?,?,?,?,?,?)",
        (order["user_id"], credited, "kredit", ket, "midtrans", now),
    )
    if order.get("isp_id"):
        con.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (credited, order["isp_id"]))
        con.execute(
            "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,sumber,created_at) VALUES (?,?,?,?,?,?)",
            (order["isp_id"], credited, "kredit", f"Topup agen otomatis #{order_id}", "midtrans", now),
        )
    agen = con.execute("SELECT nomor_wa FROM users WHERE id=?", (order["user_id"],)).fetchone()
    con.commit()
    con.close()
    if agen and agen["nomor_wa"]:
        send_wa(agen["nomor_wa"],
            f"✅ Topup Rp {amount:,} berhasil! Saldo masuk Rp {credited:,} (dipotong biaya admin Rp {fee:,}).",
            token=WA_TOKEN, tipe="topup")
    return JSONResponse({"ok": True})


@app.post("/saldo/topup-manual/{oid}/approve", response_class=JSONResponse)
async def topup_manual_approve(request: Request, oid: str):
    # Approval sekarang dilakukan oleh SA platform, bukan ISP
    return JSONResponse({"ok": False, "msg": "Persetujuan topup dikelola oleh platform vpntunel."})


@app.post("/saldo/topup-manual/{oid}/reject", response_class=JSONResponse)
async def topup_manual_reject(request: Request, oid: str):
    return JSONResponse({"ok": False, "msg": "Persetujuan topup dikelola oleh platform vpntunel."})

# ── Hotspot Bulanan ───────────────────────────────────────────────────────────

def _require_fitur_hb(user: dict):
    """Raise 403 jika fitur Hotspot Bulanan tidak diaktifkan tenant."""
    if not (user.get("fitur_hotspot_bulanan") or 0):
        raise HTTPException(status_code=403, detail="Fitur Hotspot Bulanan belum diaktifkan. Aktifkan di halaman Profil.")


@app.get("/hotspot/bulanan", response_class=HTMLResponse)
async def hotspot_bulanan(request: Request, bulan: str = "", status: str = ""):
    user = require_login(request)
    _require_fitur_hb(user)
    from datetime import date
    if not bulan:
        bulan = date.today().strftime("%Y-%m")
    iid = _isp_id(user)
    pelanggan = db.list_hotspot_pelanggan(iid)
    tagihan   = db.list_hotspot_tagihan(iid, bulan, status)
    servers   = db.list_servers(iid)
    stats     = db.stats_hotspot_tagihan(iid, bulan)
    profiles: list[str] = []
    for s in servers:
        mt = get_mt(s["id"])
        if mt:
            profiles = mt.list_hotspot_profiles()
            break
    return tpl.TemplateResponse(request, "hotspot_bulanan.html", _ctx(
        request, user=user, active="hotspot_bulanan",
        pelanggan=pelanggan, tagihan=tagihan, servers=servers,
        profiles=profiles, stats=stats,
        sel_bulan=bulan, sel_status=status,
    ))


@app.post("/hotspot/bulanan/tambah", response_class=JSONResponse)
async def hotspot_bulanan_tambah(
    request: Request,
    server_id: str = Form(...), nama: str = Form(...),
    nomor_wa: str = Form(""), username: str = Form(...),
    password: str = Form(...), profile: str = Form("default"),
    harga: int = Form(...), jatuh_tempo: int = Form(1),
    catatan: str = Form(""),
):
    user = require_login(request)
    _require_fitur_hb(user)
    iid = _isp_id(user)
    # Buat akun di MikroTik
    mt = get_mt(server_id)
    if not mt:
        return JSONResponse({"ok": False, "msg": "Server tidak dapat dihubungi"})
    ok = mt.add_hotspot_user(username, password, profile)
    if not ok:
        return JSONResponse({"ok": False, "msg": "Gagal membuat akun di MikroTik (username mungkin sudah ada)"})
    pid = db.add_hotspot_pelanggan(iid, server_id, nama.strip(), nomor_wa.strip(),
                                    username.strip(), password, profile, harga, jatuh_tempo, catatan.strip())
    return JSONResponse({"ok": True, "id": pid})


@app.post("/hotspot/bulanan/{pid}/edit", response_class=JSONResponse)
async def hotspot_bulanan_edit(
    request: Request, pid: str,
    nama: str = Form(""), nomor_wa: str = Form(""),
    password: str = Form(""), profile: str = Form(""),
    harga: int = Form(0), jatuh_tempo: int = Form(0),
    catatan: str = Form(""),
):
    user = require_login(request)
    iid = _isp_id(user)
    p = db.get_hotspot_pelanggan(pid, iid)
    if not p:
        return JSONResponse({"ok": False, "msg": "Pelanggan tidak ditemukan"})
    updates: dict = {}
    if nama:       updates["nama"] = nama.strip()
    if nomor_wa:   updates["nomor_wa"] = nomor_wa.strip()
    if password:
        updates["password"] = password
        mt = get_mt(p["server_id"])
        if mt:
            mt.add_hotspot_user(p["username"], password, p.get("profile", "default"))  # update via remove+add
            # MikroTik RouterOS supports set password directly via enable
            try:
                api = mt._conn()
                res = api.get_resource("/ip/hotspot/user")
                rows = res.get(name=p["username"])
                if rows:
                    res.set(id=rows[0]["id"], password=password)
            except Exception:
                pass
    if profile:    updates["profile"] = profile
    if harga:      updates["harga"] = harga
    if jatuh_tempo: updates["jatuh_tempo"] = jatuh_tempo
    if catatan is not None: updates["catatan"] = catatan
    db.update_hotspot_pelanggan(pid, iid, **updates)
    return JSONResponse({"ok": True})


@app.post("/hotspot/bulanan/{pid}/hapus", response_class=JSONResponse)
async def hotspot_bulanan_hapus(request: Request, pid: str):
    user = require_login(request)
    iid = _isp_id(user)
    p = db.get_hotspot_pelanggan(pid, iid)
    if not p:
        return JSONResponse({"ok": False, "msg": "Tidak ditemukan"})
    mt = get_mt(p["server_id"])
    if mt:
        mt.remove_hotspot_user(p["username"])
    db.delete_hotspot_pelanggan(pid, iid)
    return JSONResponse({"ok": True})


@app.post("/hotspot/bulanan/{pid}/disable", response_class=JSONResponse)
async def hotspot_bulanan_disable(request: Request, pid: str):
    user = require_login(request)
    iid = _isp_id(user)
    p = db.get_hotspot_pelanggan(pid, iid)
    if not p:
        return JSONResponse({"ok": False, "msg": "Tidak ditemukan"})
    mt = get_mt(p["server_id"])
    if mt:
        mt.disable_hotspot_user(p["username"])
    db.update_hotspot_pelanggan(pid, iid, status="nonaktif")
    return JSONResponse({"ok": True})


@app.post("/hotspot/bulanan/{pid}/enable", response_class=JSONResponse)
async def hotspot_bulanan_enable(request: Request, pid: str):
    user = require_login(request)
    iid = _isp_id(user)
    p = db.get_hotspot_pelanggan(pid, iid)
    if not p:
        return JSONResponse({"ok": False, "msg": "Tidak ditemukan"})
    mt = get_mt(p["server_id"])
    if mt:
        mt.enable_hotspot_user(p["username"])
    db.update_hotspot_pelanggan(pid, iid, status="aktif")
    return JSONResponse({"ok": True})


@app.post("/hotspot/bulanan/tagihan/{tid}/bayar", response_class=JSONResponse)
async def hotspot_tagihan_bayar(request: Request, tid: str):
    user = require_login(request)
    iid = _isp_id(user)
    tagihan = db.bayar_hotspot_tagihan(tid, iid)
    if not tagihan:
        return JSONResponse({"ok": False, "msg": "Tagihan tidak ditemukan"})
    p = db.get_hotspot_pelanggan(tagihan["pelanggan_id"])
    if p and p["status"] == "nonaktif":
        mt = get_mt(p["server_id"])
        if mt:
            mt.enable_hotspot_user(p["username"])
        db.update_hotspot_pelanggan(p["id"], iid, status="aktif")
    if p and p.get("nomor_wa"):
        msg = (f"✅ Pembayaran diterima!\n\n"
               f"Nama: *{p['nama']}*\n"
               f"Bulan: {tagihan['bulan']}\n"
               f"Jumlah: *Rp {tagihan['amount']:,}*\n\n"
               f"Terima kasih 🙏")
        send_wa(p["nomor_wa"], msg, token=_isp_wa_token(iid), user_id=iid)
    return JSONResponse({"ok": True})


@app.post("/hotspot/bulanan/tagihan/generate", response_class=JSONResponse)
async def hotspot_tagihan_generate(request: Request, bulan: str = Form("")):
    """Generate tagihan bulan ini untuk semua pelanggan aktif yang belum ada tagihannya."""
    user = require_login(request)
    iid = _isp_id(user)
    from datetime import date
    if not bulan:
        bulan = date.today().strftime("%Y-%m")
    pelanggan = db.list_hotspot_pelanggan(iid, status="aktif")
    n = 0
    for p in pelanggan:
        t = db.get_or_create_hotspot_tagihan(p["id"], iid, bulan, p["harga"])
        if t:
            n += 1
    return JSONResponse({"ok": True, "generated": n, "bulan": bulan})


# ── Transaksi ─────────────────────────────────────────────────────────────────

@app.get("/transaksi", response_class=HTMLResponse)
async def transaksi_page(request: Request, tipe: str = "", bulan: str = ""):
    from datetime import date
    user = require_login(request)
    iid  = _isp_id(user)
    if not bulan:
        bulan = date.today().strftime("%Y-%m")
    con = db._conn()

    # Hotspot orders (toko online) - semua status
    orders = con.execute(
        """SELECT o.id, o.amount, o.status, o.nomor_hp, o.created_at, o.paid_at,
                  o.voucher_id, p.nama as paket_nama, 'hotspot_order' as tipe
           FROM hotspot_orders o
           LEFT JOIN paket_hotspot p ON p.id = o.paket_id
           WHERE o.user_id=? ORDER BY o.created_at DESC LIMIT 200""",
        (iid,)
    ).fetchall()

    # Tagihan PPPoE (yang sudah dibayar)
    tagihan = con.execute(
        """SELECT t.id, t.amount, t.status, t.paid_at, t.created_at,
                  u.nama_pelanggan as pelanggan, t.bulan, 'tagihan_pppoe' as tipe
           FROM tagihan_pppoe t
           LEFT JOIN pppoe_users u ON u.id = t.pppoe_id
           WHERE t.user_id=? ORDER BY t.created_at DESC LIMIT 200""",
        (iid,)
    ).fetchall()

    # Transaksi internal (PPPoE tambah, dll)
    txs_raw = con.execute(
        "SELECT *, 'internal' as tipe FROM transaksi WHERE user_id=? ORDER BY created_at DESC LIMIT 200",
        (iid,)
    ).fetchall()
    con.close()

    # Gabung dan format semua transaksi
    all_txs = []
    for o in orders:
        all_txs.append({
            "id": o["id"], "tipe": "hotspot_order",
            "label": "Voucher Online",
            "keterangan": f"Beli voucher {o['paket_nama'] or ''} · {o['nomor_hp'] or ''}",
            "amount": o["amount"],
            "status": o["status"],
            "created_at": o["created_at"],
            "paid_at": o["paid_at"],
            "nomor_hp": o["nomor_hp"] or "",
            "voucher_id": o["voucher_id"],
        })
    for t in tagihan:
        all_txs.append({
            "id": str(t["id"]), "tipe": "tagihan_pppoe",
            "label": "Tagihan PPPoE",
            "keterangan": f"Tagihan {t['bulan']} · {t['pelanggan'] or ''}",
            "amount": t["amount"],
            "status": t["status"],
            "created_at": t["created_at"],
            "paid_at": t["paid_at"],
        })
    for tx in txs_raw:
        all_txs.append({
            "id": tx["id"], "tipe": tx["ref_type"],
            "label": "PPPoE" if tx["ref_type"] == "pppoe" else tx["ref_type"].title(),
            "keterangan": tx["keterangan"],
            "amount": tx["amount"],
            "status": tx["status"],
            "created_at": tx["created_at"],
            "paid_at": None,
        })

    # Filter tipe
    if tipe:
        all_txs = [t for t in all_txs if t["tipe"] == tipe]

    # Filter bulan
    import time as _time
    def _in_bulan(ts, bln):
        if not ts:
            return False
        from datetime import datetime
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m") == bln
        except Exception:
            return False
    all_txs_bulan = [t for t in all_txs if _in_bulan(t["created_at"], bulan)]

    # Sort by created_at desc
    all_txs_bulan.sort(key=lambda x: x["created_at"] or 0, reverse=True)

    # Stats bulan ini
    total_omzet = sum(t["amount"] for t in all_txs_bulan if t["status"] in ("paid", "lunas"))
    total_all   = len(all_txs_bulan)
    total_paid  = sum(1 for t in all_txs_bulan if t["status"] in ("paid", "lunas"))
    total_pending = sum(1 for t in all_txs_bulan if t["status"] == "pending")

    return tpl.TemplateResponse(request, "transaksi.html", _ctx(
        request, user=user,
        txs=all_txs_bulan,
        sel_tipe=tipe, sel_bulan=bulan,
        stats={"omzet": total_omzet, "total": total_all,
               "paid": total_paid, "pending": total_pending},
    ))


@app.post("/hotspot/order/{order_id}/kirim-wa", response_class=JSONResponse)
async def hotspot_order_kirim_wa(request: Request, order_id: str):
    user = require_login(request)
    iid  = _isp_id(user)
    con  = db._conn()
    row  = con.execute(
        """SELECT o.*, v.kode, p.nama as paket_nama, p.durasi
           FROM hotspot_orders o
           LEFT JOIN voucher_hotspot v ON v.id = o.voucher_id
           LEFT JOIN paket_hotspot p ON p.id = o.paket_id
           WHERE o.id=? AND o.user_id=?""",
        (order_id, iid)
    ).fetchone()
    con.close()
    if not row:
        return JSONResponse({"ok": False, "msg": "Order tidak ditemukan"})
    if row["status"] != "paid":
        return JSONResponse({"ok": False, "msg": "Order belum dibayar"})
    if not row["kode"]:
        return JSONResponse({"ok": False, "msg": "Voucher belum di-assign ke order ini"})
    if not row["nomor_hp"]:
        return JSONResponse({"ok": False, "msg": "Nomor HP pembeli tidak ada"})
    isp = db.get_user(iid)
    tok = _isp_wa_token(iid)
    pesan = _render_wa_template(
        iid, "voucher_online",
        isp=isp["nama"] if isp else "",
        paket=row["paket_nama"] or "",
        durasi=row["durasi"] or "",
        kode=row["kode"],
    )
    ok = send_wa(row["nomor_hp"], pesan, token=tok, user_id=iid, tipe="voucher_online")
    if ok:
        return JSONResponse({"ok": True, "msg": f"Voucher {row['kode']} dikirim ke {row['nomor_hp']}"})
    return JSONResponse({"ok": False, "msg": "Gagal kirim WA, cek konfigurasi gateway"})


# ── Bayar Tagihan PPPoE (Publik) ─────────────────────────────────────────────

@app.get("/bayar/tagihan/{tid}", response_class=HTMLResponse)
async def bayar_tagihan_page(request: Request, tid: int):
    t = db.get_tagihan(tid)
    if not t:
        return HTMLResponse("<h2>Tagihan tidak ditemukan</h2>", status_code=404)
    con_fm = sqlite3.connect(DB_PATH)
    con_fm.row_factory = sqlite3.Row
    fm_row = con_fm.execute("SELECT fee_mode FROM users WHERE id=?", (t["user_id"],)).fetchone()
    con_fm.close()
    fee_mode  = (dict(fm_row).get("fee_mode") if fm_row else None) or "customer"
    mayar_fee = _mayar_fee_estimate(t["amount"]) if fee_mode == "customer" else 0
    return tpl.TemplateResponse(request, "bayar_tagihan.html", _ctx(
        request, t=t,
        mayar_enabled=bool(MAYAR_KEY),
        mayar_fee=mayar_fee,
        fee_mode=fee_mode,
    ))


@app.get("/bayar/tagihan/{tid}/bayar-online", response_class=RedirectResponse)
async def bayar_tagihan_online(request: Request, tid: int):
    """Buat Mayar payment link untuk tagihan PPPoE dan redirect pelanggan."""
    import logging as _logging
    t = db.get_tagihan(tid)
    if not t:
        return HTMLResponse("<h2>Tagihan tidak ditemukan</h2>", status_code=404)
    if t["status"] == "paid":
        return RedirectResponse(f"/bayar/tagihan/{tid}", status_code=302)

    # Sudah ada payment link aktif — redirect langsung
    if t.get("snap_token") and t["snap_token"].startswith("mayar:"):
        parts = t["snap_token"].split(":")
        # cek status di Mayar, kalau masih unpaid re-use link
        if len(parts) >= 2:
            status = _mayar_get_payment_status(parts[1])
            if status and status not in ("paid", "expired", "cancelled"):
                # Ambil link lama - perlu buat ulang karena tidak menyimpan link
                pass  # buat link baru di bawah

    # Baca fee_mode ISP: 'customer' → pelanggan tanggung, 'tenant' → ISP tanggung
    con_fm = sqlite3.connect(DB_PATH)
    con_fm.row_factory = sqlite3.Row
    fm_row = con_fm.execute("SELECT fee_mode FROM users WHERE id=?", (t["user_id"],)).fetchone()
    con_fm.close()
    fee_mode = (dict(fm_row).get("fee_mode") if fm_row else None) or "customer"

    tagihan_amount = t["amount"]
    mayar_fee = _mayar_fee_estimate(tagihan_amount)
    bayar_amount = tagihan_amount + mayar_fee if fee_mode == "customer" else tagihan_amount

    redirect_url = f"https://{APP_DOMAIN}/bayar/tagihan/{tid}/sukses"
    nama = t.get("nama_pelanggan") or "Pelanggan"
    nomor = re.sub(r"[^0-9]", "", (t.get("telepon") or ""))
    if nomor.startswith("0"):
        nomor = "62" + nomor[1:]
    elif nomor and not nomor.startswith("62"):
        nomor = "62" + nomor
    nomor = nomor[:15]  # Mayar max 15 karakter
    desc = f"Tagihan Internet {t.get('bulan','')} - {nama} ({t.get('isp_nama','')})"
    order_id = f"TGHN-{tid}-{int(time.time())}"

    result = _mayar_create_payment(order_id, bayar_amount, nama, nomor, desc, redirect_url)
    if not result or not result.get("link"):
        _logging.warning(f"[Mayar] Gagal buat payment tagihan {tid}")
        return RedirectResponse(f"/bayar/tagihan/{tid}?err=payment_failed", status_code=302)

    snap = f"mayar:{result['id']}:{result.get('transaction_id','')}"
    db.set_tagihan_snap_token(tid, snap)
    _logging.warning(f"[Mayar] tagihan {tid} payment created: {result['link']}")
    return RedirectResponse(result["link"], status_code=302)


@app.get("/bayar/tagihan/{tid}/sukses", response_class=HTMLResponse)
async def bayar_tagihan_sukses(request: Request, tid: int):
    t = db.get_tagihan(tid)
    if not t:
        return HTMLResponse("<h2>Tagihan tidak ditemukan</h2>", status_code=404)
    return tpl.TemplateResponse(request, "bayar_tagihan.html", _ctx(
        request, t=t, sukses=(t["status"] == "paid")
    ))


@app.get("/nota/{tid}", response_class=HTMLResponse)
async def nota_tagihan(request: Request, tid: int):
    """Nota pembayaran publik — tidak perlu login."""
    t = db.get_tagihan(tid)
    if not t:
        return HTMLResponse("<h2>Nota tidak ditemukan</h2>", status_code=404)
    if t["status"] not in ("paid", "lunas"):
        return HTMLResponse("<h2>Tagihan belum lunas</h2>", status_code=403)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    isp_row = con.execute("SELECT nama, nomor_wa FROM users WHERE id=?", (t["user_id"],)).fetchone()
    con.close()
    isp = dict(isp_row) if isp_row else {}
    return tpl.TemplateResponse(request, "nota.html", _ctx(request, t=t, isp=isp))


@app.post("/pppoe/tagihan/{tid}/kirim-nota", response_class=JSONResponse)
async def tagihan_kirim_nota(request: Request, tid: int):
    """Kirim link nota pembayaran ke WA pelanggan."""
    user = require_login(request)
    t = db.get_tagihan(tid)
    if not t or t["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "msg": "Tagihan tidak ditemukan"})
    if t["status"] not in ("paid", "lunas"):
        return JSONResponse({"ok": False, "msg": "Tagihan belum lunas"})
    if not t.get("telepon"):
        return JSONResponse({"ok": False, "msg": "Nomor WA pelanggan belum diisi"})
    label   = _label_bulan(t["bulan"])
    nominal = f"Rp {t['amount']:,}".replace(",", ".")
    link    = f"https://{APP_DOMAIN}/nota/{tid}"
    pesan   = (
        f"*Nota Pembayaran Internet*\n\n"
        f"Pelanggan: *{t['nama_pelanggan']}*\n"
        f"Paket: {t.get('paket_nama') or '-'}\n"
        f"Bulan: {label}\n"
        f"Nominal: *{nominal}*\n"
        f"Status: ✅ *LUNAS*\n\n"
        f"Lihat nota:\n{link}\n\n"
        f"Terima kasih telah membayar tepat waktu 🙏"
    )
    tok = _isp_wa_token(t["user_id"])
    ok  = send_wa(t["telepon"], pesan, token=tok, user_id=t["user_id"], tipe="pembayaran")
    return JSONResponse({"ok": bool(ok), "link": link})


# ── Toko Hotspot Online (Publik) ─────────────────────────────────────────────


# ── Mayar.id helpers ──────────────────────────────────────────────────────────

def _mayar_create_payment(order_id: str, amount: int, nama: str, nomor_hp: str,
                          description: str, redirect_url: str) -> dict | None:
    """Buat payment di Mayar.id. Return dict {link, id, transaction_id} atau None."""
    if not MAYAR_KEY:
        return None
    payload = {
        "name": (nama or "Pelanggan")[:60],
        "email": "noreply@vpntunel.my.id",
        "mobile": (nomor_hp or "6285367281448")[:15],
        "amount": int(amount),
        "description": (description or "Voucher Hotspot") + f" #{order_id}",
        "redirectUrl": redirect_url,
    }
    import logging as _logging
    try:
        r = requests.post(
            f"{MAYAR_BASE}/hl/v1/payment/create",
            json=payload,
            headers={"Authorization": f"Bearer {MAYAR_KEY}", "Content-Type": "application/json"},
            timeout=12,
        )
        _logging.warning(f"[Mayar] create order={order_id} http={r.status_code} body={r.text[:300]}")
        data = r.json()
        if data.get("statusCode") == 200 and data.get("data"):
            d = data["data"]
            return {
                "link": d.get("link"),
                "id": d.get("id") or d.get("paymentLinkId"),
                "transaction_id": d.get("transaction_id") or d.get("transactionId"),
            }
        return None
    except Exception as e:
        _logging.warning(f"[Mayar] create error: {e}")
        return None


def _mayar_get_payment_status(payment_id: str) -> str | None:
    """Ambil status payment dari Mayar (unpaid|paid|expired|...). Return None jika gagal."""
    if not MAYAR_KEY or not payment_id:
        return None
    try:
        r = requests.get(
            f"{MAYAR_BASE}/hl/v1/payment",
            headers={"Authorization": f"Bearer {MAYAR_KEY}"},
            timeout=10,
        )
        data = r.json()
        for item in data.get("data", []):
            if item.get("id") == payment_id:
                return item.get("status")
        return None
    except Exception:
        return None


def _mayar_verify_payment(pay_link_id: str, tx_id: str) -> bool:
    """Verifikasi ulang ke Mayar API bahwa payment benar-benar SUCCESS.
    Return True hanya jika status terkonfirmasi success di sisi Mayar."""
    import logging as _log
    if not MAYAR_KEY:
        return True  # Jika tidak ada key, skip verifikasi (fallback ke webhook saja)
    try:
        # Coba cek via paymentLinkId
        if pay_link_id:
            r = requests.get(
                f"{MAYAR_BASE}/hl/v1/payment/{pay_link_id}",
                headers={"Authorization": f"Bearer {MAYAR_KEY}"},
                timeout=10,
            )
            if r.status_code == 200:
                d = r.json().get("data") or {}
                status = (d.get("status") or "").lower()
                _log.warning(f"[Mayar verify] link={pay_link_id} tx={tx_id} status={status}")
                return status in ("success", "paid", "closed")
        # Fallback: cari di list semua payment
        r = requests.get(
            f"{MAYAR_BASE}/hl/v1/payment",
            headers={"Authorization": f"Bearer {MAYAR_KEY}"},
            timeout=10,
        )
        if r.status_code == 200:
            for item in (r.json().get("data") or []):
                if item.get("id") == pay_link_id or item.get("transactionId") == tx_id:
                    status = (item.get("status") or "").lower()
                    _log.warning(f"[Mayar verify-list] link={pay_link_id} tx={tx_id} status={status}")
                    return status in ("success", "paid", "closed")
        _log.warning(f"[Mayar verify] tidak ditemukan di API, link={pay_link_id} tx={tx_id}")
        return False
    except Exception as e:
        _log.warning(f"[Mayar verify] error: {e} — lanjut tanpa verifikasi")
        return True  # Jika API error, jangan blokir webhook yang valid


def _mayar_find_payment_by_link(link_short: str) -> dict | None:
    """Cari payment di Mayar berdasarkan link short code (mis. cw25rjvzwd)."""
    if not MAYAR_KEY or not link_short:
        return None
    try:
        r = requests.get(
            f"{MAYAR_BASE}/hl/v1/payment",
            headers={"Authorization": f"Bearer {MAYAR_KEY}"},
            timeout=10,
        )
        data = r.json()
        for item in data.get("data", []):
            if item.get("link") == link_short:
                return item
        return None
    except Exception:
        return None


# ── Fee Mayar + Platform Commission helpers ─────────────────────────────────

def _platform_fee_amount(harga: int) -> int:
    """Komisi platform berdasarkan harga voucher.
    < threshold (default 50k) → fee_small (300)
    ≥ threshold              → fee_large (700)
    """
    try:
        small     = int(db.get_platform_config("platform_fee_small") or 300)
        large     = int(db.get_platform_config("platform_fee_large") or 700)
        threshold = int(db.get_platform_config("platform_fee_threshold") or 50000)
    except Exception:
        small, large, threshold = 300, 700, 50000
    return small if harga < threshold else large


def _mayar_fee_estimate(amount: int) -> int:
    """Estimasi fee Mayar untuk amount — baca persentase dari platform_config."""
    try:
        pct_str = db.get_platform_config("mayar_fee_percent")
        pct = float(pct_str) if pct_str else MAYAR_FEE_PERCENT
    except Exception:
        pct = MAYAR_FEE_PERCENT
    return int(round(amount * pct / 100))


def _calc_order_amounts(harga: int, fee_mode: str) -> dict:
    """Hitung jumlah bayar pelanggan + saldo masuk tenant + platform fee.

    Mode 'customer' (default): pelanggan tanggung fee Mayar.
        pelanggan_bayar = harga + mayar_fee_estimate
        saldo_tenant    = harga - platform_fee
    Mode 'tenant'  : tenant tanggung fee Mayar.
        pelanggan_bayar = harga
        saldo_tenant    = harga - mayar_fee_estimate - platform_fee
    """
    pf = _platform_fee_amount(harga)
    mf = _mayar_fee_estimate(harga)
    if fee_mode == "tenant":
        pelanggan_bayar = harga
        saldo_tenant    = max(0, harga - mf - pf)
    else:  # customer (default)
        pelanggan_bayar = harga + mf
        saldo_tenant    = max(0, harga - pf)
    return {
        "harga": harga,
        "fee_mayar_est": mf,
        "platform_fee": pf,
        "pelanggan_bayar": pelanggan_bayar,
        "saldo_tenant": saldo_tenant,
        "fee_mode": fee_mode,
    }


@app.get("/beli/{slug}/login", response_class=HTMLResponse)
async def store_login_page(request: Request, slug: str):
    isp = db.get_isp_by_slug(slug)
    if not isp:
        return HTMLResponse("<h2>Toko tidak ditemukan</h2>", status_code=404)
    # Selalu tampilkan form login — tidak auto-redirect meski sudah ada session
    return tpl.TemplateResponse(request, "store_login.html", _ctx(
        request, isp=isp, slug=slug, error=request.query_params.get("error")
    ))


@app.post("/beli/{slug}/login")
async def store_login_post(
    request: Request, slug: str,
    username: str = Form(...),
    password: str = Form(...),
):
    isp = db.get_isp_by_slug(slug)
    if not isp:
        return HTMLResponse("<h2>Toko tidak ditemukan</h2>", status_code=404)
    user = db.login(username, password)
    if user and user["role"] in ("agen", "sub_agen"):
        resp = RedirectResponse(_login_dest(user), status_code=303)
        resp.set_cookie("agen_session", make_session(user["id"]), httponly=True, max_age=86400 * 7)
        return resp
    return tpl.TemplateResponse(request, "store_login.html", _ctx(
        request, isp=isp, slug=slug, error="Username atau password salah"
    ))


# ── Panel Agen (halaman mandiri) ──────────────────────────────────────────────

def _require_agen(request: Request):
    """Pastikan user login dan role agen/sub_agen. Cek kedua cookie."""
    user = current_user_agen(request) or current_user(request)
    if not user or user["role"] not in ("agen", "sub_agen"):
        return None
    return user


@app.get("/beli/panel", response_class=HTMLResponse)
async def beli_panel_alias(request: Request):
    return await panel_agen(request)


@app.get("/panel", response_class=HTMLResponse)
async def panel_agen(request: Request):
    user = _require_agen(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    # Ambil data ISP parent
    isp = db.get_user(user["parent_id"]) if user.get("parent_id") else None
    pakets = db.list_paket_hotspot_agen(isp["id"]) if isp else []
    servers = db.list_servers(isp["id"]) if isp else []
    comments = db.list_voucher_comments(isp["id"], agen_id=user["id"]) if isp else []
    comment_counts = db.count_vouchers_by_comment(isp["id"], agen_id=user["id"]) if isp else {}
    comments_detail = db.list_voucher_comments_detail(isp["id"], agen_id=user["id"]) if isp else []
    saldo_log = db.list_saldo_log(user["id"])
    topup_orders = db.list_topup_orders(user["id"])
    user = db.get_user(user["id"])  # refresh saldo terkini
    last_kodes = request.query_params.getlist("kode")
    last_paket = None
    if last_kodes:
        try:
            pid = int(request.query_params.get("paket_id", 0))
            last_paket = db.get_paket_hotspot(pid) if pid else None
        except Exception:
            pass
    platform_qris = db.get_platform_config("qris_image")
    return tpl.TemplateResponse(request, "panel_agen.html", _ctx(
        request, user=user, isp=isp, pakets=pakets, servers=servers,
        comments=comments, comment_counts=comment_counts, comments_detail=comments_detail,
        saldo_log=saldo_log, topup_orders=topup_orders,
        ok_msg=request.query_params.get("ok"),
        err_msg=request.query_params.get("error"),
        last_kodes=last_kodes, last_paket=last_paket,
        platform_qris=platform_qris
    ))


@app.post("/panel/topup", response_class=JSONResponse)
async def panel_topup(request: Request, amount: int = Form(...)):
    """Buat order topup saldo via QRIS/transfer manual."""
    user = _require_agen(request)
    if not user:
        return JSONResponse({"ok": False, "msg": "Tidak terotorisasi"})
    if amount < 10000:
        return JSONResponse({"ok": False, "msg": "Minimal topup Rp 10.000"})
    oid = db.create_topup_order(user["id"], amount)
    return JSONResponse({"ok": True, "order_id": oid})


@app.get("/panel/topup/cek/{oid}", response_class=JSONResponse)
async def panel_topup_cek(request: Request, oid: str):
    """Cek status topup (polling dari frontend)."""
    user = _require_agen(request)
    if not user:
        return JSONResponse({"ok": False})
    order = db.get_topup_order(oid)
    if not order or order["user_id"] != user["id"]:
        return JSONResponse({"ok": False})
    return JSONResponse({"ok": True, "status": order["status"]})


@app.post("/panel/generate", response_class=HTMLResponse)
async def panel_generate(
    request: Request,
    paket_id: int = Form(...),
    server_id: str = Form(...),
    jumlah: int = Form(...),
    comment: str = Form(""),
):
    """Generate voucher ke MikroTik menggunakan saldo agen."""
    user = _require_agen(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    isp = db.get_user(user["parent_id"]) if user.get("parent_id") else None
    if not isp:
        return RedirectResponse("/panel?error=ISP+tidak+ditemukan", status_code=302)

    jumlah = max(1, min(jumlah, 100))
    comment = comment.strip()
    if not comment:
        from datetime import datetime as _dt
        comment = _dt.now().strftime("Batch %d-%b-%Y %H:%M")
    result = db.generate_voucher_agen(user["id"], isp["id"], paket_id, jumlah, comment, server_id)
    if not result["ok"]:
        return RedirectResponse(f"/panel?error={result['msg']}", status_code=302)

    kodes = result["kodes"]
    # Push ke MikroTik — gunakan server_id yang sudah di-resolve storage (termasuk fallback)
    resolved_server_id = result.get("server_id") or server_id
    server = db.get_server(resolved_server_id) if resolved_server_id else None
    push_errors = []
    if server and server["user_id"] == isp["id"]:
        paket = db.get_paket_hotspot(paket_id)
        try:
            mt = MikroTik(server["vpn_ip"], server["api_port"],
                          server["api_user"], server["api_password"])
            profile      = (paket.get("kecepatan") or "default") if paket else "default"
            mt_comment   = comment or (paket.get("nama", "") if paket else "")
            limit_uptime = (paket.get("durasi") or "") if paket else ""
            for kode in kodes:
                ok = mt.add_hotspot_user(
                    kode, kode,
                    profile=profile,
                    comment=mt_comment,
                    limit_uptime=limit_uptime
                )
                if ok:
                    db.set_voucher_mt_pushed(kode, True)
                else:
                    push_errors.append(kode)
        except Exception as e:
            push_errors = kodes  # semua gagal, catat di log
            _log(request, user, "Generate Voucher Push Error", str(e))

    paket_nama = db.get_paket_hotspot(paket_id)
    push_info = f" | push_error={len(push_errors)}" if push_errors else ""
    _log(request, user, "Generate Voucher",
         f"{jumlah} voucher paket {paket_nama['nama'] if paket_nama else paket_id}"
         f"{' [' + comment + ']' if comment else ''}{push_info}")
    params = "&".join(f"kode={k}" for k in kodes)
    status = "generate_push_error" if push_errors else "generate"
    return RedirectResponse(f"/panel?ok={status}&paket_id={paket_id}&{params}", status_code=303)


@app.get("/panel/voucher/print", response_class=HTMLResponse)
async def panel_voucher_print(request: Request, comment: str = ""):
    user = _require_agen(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    isp = db.get_user(user["parent_id"]) if user.get("parent_id") else None
    if not isp:
        return RedirectResponse("/panel", status_code=302)
    comments = db.list_voucher_comments(isp["id"], agen_id=user["id"])
    comment_filter = "" if comment == "__none__" else (comment or None)
    vouchers = db.list_vouchers(isp["id"], None, "tersedia", None, comment_filter) if comment else []
    app_name = isp.get("nama") or "Voucher"
    return tpl.TemplateResponse(request, "voucher_print.html", {
        "request": request,
        "app_name": app_name,
        "vouchers": vouchers,
        "comments": comments,
        "sel_comment": comment,
        "print_base_url": "/panel/voucher/print",
    })


@app.get("/panel/logout")
async def panel_logout(request: Request):
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("agen_session")
    return resp


@app.get("/beli/{slug}/agen", response_class=HTMLResponse)
async def store_agen_page(request: Request, slug: str):
    """Halaman beli voucher untuk agen — menggunakan saldo."""
    isp = db.get_isp_by_slug(slug)
    if not isp:
        return HTMLResponse("<h2>Toko tidak ditemukan</h2>", status_code=404)
    user = current_user(request)
    if not user or user["role"] not in ("agen", "sub_agen"):
        return RedirectResponse(f"/beli/{slug}/login", status_code=302)
    pakets = db.list_paket_hotspot_publik(isp["id"])
    riwayat = db.list_saldo_log(user["id"])
    ok_msg = request.query_params.get("ok")
    err_msg = request.query_params.get("error")
    last_kode = request.query_params.get("kode")
    return tpl.TemplateResponse(request, "store_agen.html", _ctx(
        request, isp=isp, slug=slug, user=user,
        pakets=pakets, riwayat=riwayat,
        ok_msg=ok_msg, err_msg=err_msg, last_kode=last_kode
    ))


@app.post("/beli/{slug}/agen/beli", response_class=HTMLResponse)
async def store_agen_beli(request: Request, slug: str,
                          paket_id: int = Form(...), comment: str = Form("")):
    """Proses pembelian voucher menggunakan saldo."""
    isp = db.get_isp_by_slug(slug)
    if not isp:
        return HTMLResponse("<h2>Toko tidak ditemukan</h2>", status_code=404)
    user = current_user(request)
    if not user or user["role"] not in ("agen", "sub_agen"):
        return RedirectResponse(f"/beli/{slug}/login", status_code=302)
    comment = comment.strip()
    if not comment:
        return RedirectResponse(f"/beli/{slug}/agen?error=Comment+wajib+diisi", status_code=303)
    result = db.beli_voucher_saldo(user["id"], isp["id"], paket_id, comment=comment)
    if result["ok"]:
        # Update comment di MikroTik jika voucher sudah di-push
        try:
            mt = get_mt(result["server_id"])
            if mt:
                rows = mt.list_hotspot_users(username=result["kode"])
                if rows:
                    mt.edit_hotspot_user(rows[0]["id"], comment=comment)
        except Exception:
            pass
        return RedirectResponse(
            f"/beli/{slug}/agen?ok=beli&kode={result['kode']}", status_code=303
        )
    return RedirectResponse(
        f"/beli/{slug}/agen?error={result['msg']}", status_code=303
    )


@app.get("/beli/{slug}/agen/logout")
async def store_agen_logout(request: Request, slug: str):
    resp = RedirectResponse(f"/beli/{slug}/login", status_code=302)
    resp.delete_cookie("agen_session")
    return resp


# ── Panel Teknisi ─────────────────────────────────────────────────────────────

def _require_teknisi(request: Request, slug: str):
    """Pastikan user login sebagai teknisi dan parent ISP sesuai slug."""
    user = current_user_teknisi(request)
    if not user or user["role"] != "teknisi":
        return None, None
    isp = db.get_user(user.get("parent_id", "")) if user.get("parent_id") else None
    if not isp:
        return None, None
    isp_slug = isp.get("slug") or isp.get("username")
    if isp_slug != slug:
        return None, None
    return user, isp


@app.get("/teknisi/{slug}", response_class=HTMLResponse)
async def teknisi_login_page(request: Request, slug: str):
    isp = db.get_isp_by_slug(slug)
    if not isp:
        return HTMLResponse("<h2>Halaman tidak ditemukan</h2>", status_code=404)
    return tpl.TemplateResponse(request, "teknisi_login.html", _ctx(
        request, isp=isp, slug=slug, error=request.query_params.get("error")
    ))


@app.post("/teknisi/{slug}")
async def teknisi_login_post(
    request: Request, slug: str,
    username: str = Form(...), password: str = Form(...),
):
    isp = db.get_isp_by_slug(slug)
    if not isp:
        return HTMLResponse("<h2>Halaman tidak ditemukan</h2>", status_code=404)
    user = db.login(username, password)
    if user and user["role"] == "teknisi":
        resp = RedirectResponse(f"/teknisi/{slug}/panel", status_code=303)
        resp.set_cookie("teknisi_session", make_session(user["id"]), httponly=True, max_age=86400 * 7)
        return resp
    return tpl.TemplateResponse(request, "teknisi_login.html", _ctx(
        request, isp=isp, slug=slug, error="Username atau password salah"
    ))


@app.get("/teknisi/{slug}/panel", response_class=HTMLResponse)
async def teknisi_panel(request: Request, slug: str):
    user, isp = _require_teknisi(request, slug)
    if not user:
        return RedirectResponse(f"/teknisi/{slug}", status_code=302)
    servers = db.list_servers(isp["id"])
    server_id = request.query_params.get("server_id", servers[0]["id"] if servers else "")
    search = request.query_params.get("q", "")
    pelanggan = db.list_pppoe_users(isp["id"], server_id or None)
    if search:
        s = search.lower()
        pelanggan = [p for p in pelanggan if s in p["nama_pelanggan"].lower()
                     or s in p["username"].lower()
                     or s in (p.get("telepon") or "").lower()]
    pakets = db.list_paket_pppoe(isp["id"])
    return tpl.TemplateResponse(request, "panel_teknisi.html", _ctx(
        request, user=user, isp=isp, slug=slug,
        servers=servers, server_id=server_id,
        pelanggan=pelanggan, pakets=pakets, search=search,
        ok_msg=request.query_params.get("ok"),
        err_msg=request.query_params.get("error"),
    ))


@app.post("/teknisi/{slug}/panel/tambah")
async def teknisi_tambah_pppoe(
    request: Request, slug: str,
    nama_pelanggan: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    paket_id: int = Form(0),
    telepon: str = Form(""),
    alamat: str = Form(""),
    server_id: str = Form(...),
):
    user, isp = _require_teknisi(request, slug)
    if not user:
        return RedirectResponse(f"/teknisi/{slug}", status_code=302)
    # Simpan ke DB
    db.create_pppoe_user(isp["id"], server_id, nama_pelanggan, username, password, paket_id or None, telepon, alamat)
    # Push ke MikroTik
    server = db.get_server(server_id)
    paket = db.get_paket_pppoe(paket_id) if paket_id else None
    mt_ok = False
    if server:
        try:
            mt = MikroTik(server["vpn_ip"], server["api_port"], server["api_user"], server["api_password"])
            profile = paket["nama"] if paket else "default"
            mt.api.get_resource("/ppp/secret").add(
                name=username, password=password, profile=profile,
                comment=f"{nama_pelanggan} | {telepon}"
            )
            mt_ok = True
        except Exception:
            pass
    msg = "pppoe_ok" if mt_ok else "pppoe_db"
    return RedirectResponse(f"/teknisi/{slug}/panel?ok={msg}&server_id={server_id}", status_code=303)


@app.post("/teknisi/{slug}/panel/toggle/{pppoe_id}")
async def teknisi_toggle_pppoe(request: Request, slug: str, pppoe_id: int, aksi: str = Form(...)):
    user, isp = _require_teknisi(request, slug)
    if not user:
        return RedirectResponse(f"/teknisi/{slug}", status_code=302)
    pel = db.get_pppoe_user(pppoe_id)
    if not pel or pel["user_id"] != isp["id"]:
        return RedirectResponse(f"/teknisi/{slug}/panel", status_code=302)
    server = db.get_server(pel["server_id"])
    if server:
        try:
            mt = MikroTik(server["vpn_ip"], server["api_port"], server["api_user"], server["api_password"])
            secrets = mt.api.get_resource("/ppp/secret")
            rows = secrets.get(name=pel["username"])
            if rows:
                if aksi == "disable":
                    secrets.set(id=rows[0]["id"], disabled="yes")
                    db.update_pppoe_status(pppoe_id, "nonaktif")
                else:
                    secrets.set(id=rows[0]["id"], disabled="no")
                    db.update_pppoe_status(pppoe_id, "aktif")
        except Exception:
            pass
    server_id = pel.get("server_id", "")
    return RedirectResponse(f"/teknisi/{slug}/panel?server_id={server_id}", status_code=302)


@app.post("/teknisi/{slug}/panel/edit/{pppoe_id}")
async def teknisi_edit_pppoe(
    request: Request, slug: str, pppoe_id: int,
    nama_pelanggan: str = Form(...),
    telepon: str = Form(""),
    alamat: str = Form(""),
    tgl_bayar: int = Form(1),
):
    user, isp = _require_teknisi(request, slug)
    if not user:
        return RedirectResponse(f"/teknisi/{slug}", status_code=302)
    pel = db.get_pppoe_user(pppoe_id)
    if not pel or pel["user_id"] != isp["id"]:
        return RedirectResponse(f"/teknisi/{slug}/panel", status_code=302)
    db.update_pppoe_user(pppoe_id, nama_pelanggan, telepon, alamat, tgl_bayar)
    server_id = pel.get("server_id", "")
    return RedirectResponse(f"/teknisi/{slug}/panel?server_id={server_id}&ok=edit_ok", status_code=303)


@app.post("/teknisi/{slug}/panel/hapus/{pppoe_id}")
async def teknisi_hapus_pppoe(request: Request, slug: str, pppoe_id: int):
    user, isp = _require_teknisi(request, slug)
    if not user:
        return RedirectResponse(f"/teknisi/{slug}", status_code=302)
    pel = db.get_pppoe_user(pppoe_id)
    if not pel or pel["user_id"] != isp["id"]:
        return RedirectResponse(f"/teknisi/{slug}/panel", status_code=302)
    server_id = pel.get("server_id", "")
    server = db.get_server(pel["server_id"]) if pel.get("server_id") else None
    if server:
        try:
            mt = MikroTik(server["vpn_ip"], server["api_port"], server["api_user"], server["api_password"])
            secrets = mt.api.get_resource("/ppp/secret")
            rows = secrets.get(name=pel["username"])
            if rows:
                secrets.remove(id=rows[0]["id"])
        except Exception:
            pass
    db.delete_pppoe_user(pppoe_id)
    return RedirectResponse(f"/teknisi/{slug}/panel?server_id={server_id}&ok=hapus_ok", status_code=303)


@app.get("/teknisi/{slug}/logout")
async def teknisi_logout(request: Request, slug: str):
    resp = RedirectResponse(f"/teknisi/{slug}/login", status_code=302)
    resp.delete_cookie("teknisi_session")
    return resp


# ── Teknisi: Hotspot Bulanan ─────────────────────────────────────────────────

@app.get("/teknisi/{slug}/hotspot", response_class=HTMLResponse)
async def teknisi_hotspot(request: Request, slug: str):
    user, isp = _require_teknisi(request, slug)
    if not user:
        return RedirectResponse(f"/teknisi/{slug}", status_code=302)
    if not (isp or {}).get("fitur_hotspot_bulanan"):
        return RedirectResponse(f"/teknisi/{slug}/panel", status_code=302)
    from datetime import date as _date
    bulan = request.query_params.get("bulan", _date.today().strftime("%Y-%m"))
    iid = isp["id"]
    pelanggan = db.list_hotspot_pelanggan(iid)
    tagihan   = db.list_hotspot_tagihan(iid, bulan, "")
    servers   = db.list_servers(iid)
    stats     = db.stats_hotspot_tagihan(iid, bulan)
    profiles: list[str] = []
    for s in servers:
        mt = get_mt(s["id"])
        if mt:
            profiles = mt.list_hotspot_profiles()
            break
    return tpl.TemplateResponse(request, "panel_teknisi_hotspot.html", _ctx(
        request, user=user, isp=isp, slug=slug,
        pelanggan=pelanggan, tagihan=tagihan, servers=servers,
        profiles=profiles, stats=stats, sel_bulan=bulan,
        ok_msg=request.query_params.get("ok"),
        err_msg=request.query_params.get("error"),
    ))


@app.post("/teknisi/{slug}/hotspot/tambah", response_class=JSONResponse)
async def teknisi_hotspot_tambah(
    request: Request, slug: str,
    server_id: str = Form(...), nama: str = Form(...),
    nomor_wa: str = Form(""), username: str = Form(...),
    password: str = Form(...), profile: str = Form("default"),
    harga: int = Form(...), jatuh_tempo: int = Form(1),
    catatan: str = Form(""),
):
    user, isp = _require_teknisi(request, slug)
    if not user or not (isp or {}).get("fitur_hotspot_bulanan"):
        return JSONResponse({"ok": False, "msg": "Akses ditolak"})
    iid = isp["id"]
    mt = get_mt(server_id)
    if not mt:
        return JSONResponse({"ok": False, "msg": "Server tidak dapat dihubungi"})
    ok = mt.add_hotspot_user(username, password, profile)
    if not ok:
        return JSONResponse({"ok": False, "msg": "Gagal membuat akun di MikroTik (username mungkin sudah ada)"})
    pid = db.add_hotspot_pelanggan(iid, server_id, nama.strip(), nomor_wa.strip(),
                                    username.strip(), password, profile, harga, jatuh_tempo, catatan.strip())
    return JSONResponse({"ok": True, "id": pid})


@app.post("/teknisi/{slug}/hotspot/{pid}/edit", response_class=JSONResponse)
async def teknisi_hotspot_edit(
    request: Request, slug: str, pid: str,
    nama: str = Form(""), nomor_wa: str = Form(""),
    password: str = Form(""), profile: str = Form(""),
    harga: int = Form(0), jatuh_tempo: int = Form(0),
    catatan: str = Form(""),
):
    user, isp = _require_teknisi(request, slug)
    if not user or not (isp or {}).get("fitur_hotspot_bulanan"):
        return JSONResponse({"ok": False, "msg": "Akses ditolak"})
    iid = isp["id"]
    p = db.get_hotspot_pelanggan(pid, iid)
    if not p:
        return JSONResponse({"ok": False, "msg": "Pelanggan tidak ditemukan"})
    updates: dict = {}
    if nama:      updates["nama"] = nama.strip()
    if nomor_wa:  updates["nomor_wa"] = nomor_wa.strip()
    if password:
        updates["password"] = password
        mt = get_mt(p["server_id"])
        if mt:
            try:
                api = mt._conn()
                res = api.get_resource("/ip/hotspot/user")
                rows = res.get(name=p["username"])
                if rows:
                    res.set(id=rows[0]["id"], password=password)
            except Exception:
                pass
    if profile:     updates["profile"] = profile
    if harga:       updates["harga"] = harga
    if jatuh_tempo: updates["jatuh_tempo"] = jatuh_tempo
    if catatan is not None: updates["catatan"] = catatan
    db.update_hotspot_pelanggan(pid, iid, **updates)
    return JSONResponse({"ok": True})


@app.post("/teknisi/{slug}/hotspot/{pid}/hapus", response_class=JSONResponse)
async def teknisi_hotspot_hapus(request: Request, slug: str, pid: str):
    user, isp = _require_teknisi(request, slug)
    if not user or not (isp or {}).get("fitur_hotspot_bulanan"):
        return JSONResponse({"ok": False, "msg": "Akses ditolak"})
    iid = isp["id"]
    p = db.get_hotspot_pelanggan(pid, iid)
    if not p:
        return JSONResponse({"ok": False, "msg": "Tidak ditemukan"})
    mt = get_mt(p["server_id"])
    if mt:
        mt.remove_hotspot_user(p["username"])
    db.delete_hotspot_pelanggan(pid, iid)
    return JSONResponse({"ok": True})


@app.post("/teknisi/{slug}/hotspot/{pid}/disable", response_class=JSONResponse)
async def teknisi_hotspot_disable(request: Request, slug: str, pid: str):
    user, isp = _require_teknisi(request, slug)
    if not user or not (isp or {}).get("fitur_hotspot_bulanan"):
        return JSONResponse({"ok": False, "msg": "Akses ditolak"})
    iid = isp["id"]
    p = db.get_hotspot_pelanggan(pid, iid)
    if not p:
        return JSONResponse({"ok": False, "msg": "Tidak ditemukan"})
    mt = get_mt(p["server_id"])
    if mt:
        mt.disable_hotspot_user(p["username"])
    db.update_hotspot_pelanggan(pid, iid, status="nonaktif")
    return JSONResponse({"ok": True})


@app.post("/teknisi/{slug}/hotspot/{pid}/enable", response_class=JSONResponse)
async def teknisi_hotspot_enable(request: Request, slug: str, pid: str):
    user, isp = _require_teknisi(request, slug)
    if not user or not (isp or {}).get("fitur_hotspot_bulanan"):
        return JSONResponse({"ok": False, "msg": "Akses ditolak"})
    iid = isp["id"]
    p = db.get_hotspot_pelanggan(pid, iid)
    if not p:
        return JSONResponse({"ok": False, "msg": "Tidak ditemukan"})
    mt = get_mt(p["server_id"])
    if mt:
        mt.enable_hotspot_user(p["username"])
    db.update_hotspot_pelanggan(pid, iid, status="aktif")
    return JSONResponse({"ok": True})


@app.get("/beli/{slug}/cari", response_class=JSONResponse)
async def toko_cari_voucher(request: Request, slug: str, nomor: str = ""):
    """Cari voucher yang sudah dibeli berdasarkan nomor HP."""
    isp = db.get_isp_by_slug(slug)
    if not isp:
        return JSONResponse({"ok": False, "msg": "Toko tidak ditemukan"})
    if not nomor or len(nomor.strip()) < 8:
        return JSONResponse({"ok": False, "msg": "Masukkan nomor HP yang valid"})
    hasil = db.cari_voucher_by_nomor(isp["id"], nomor.strip())
    return JSONResponse({"ok": True, "data": hasil})


@app.get("/beli/{slug}", response_class=HTMLResponse)
async def toko_page(request: Request, slug: str):
    isp = db.get_isp_by_slug(slug)
    if not isp:
        return HTMLResponse("<h2>Toko tidak ditemukan</h2>", status_code=404)
    pakets  = db.list_paket_hotspot_publik(isp["id"])
    raw_servers = db.list_servers(isp["id"])

    def _check_online(srv):
        try:
            MikroTik(srv["vpn_ip"], srv["api_port"], srv["api_user"], srv["api_password"])
            return True
        except Exception:
            return False

    import concurrent.futures
    servers_with_status = []
    with concurrent.futures.ThreadPoolExecutor() as ex:
        results = list(ex.map(_check_online, raw_servers))
    for srv, online in zip(raw_servers, results):
        s = dict(srv)
        s["online"] = online
        servers_with_status.append(s)

    return tpl.TemplateResponse(request, "store.html", _ctx(
        request, isp=isp, pakets=pakets, servers=servers_with_status,
        slug=slug, fee_mode=(isp.get("fee_mode") or "customer"),
        mayar_fee_percent=MAYAR_FEE_PERCENT,
    ))


@app.post("/beli/{slug}/order", response_class=JSONResponse)
async def toko_order(
    request: Request, slug: str,
    paket_id: int = Form(...),
    server_id: str = Form(...),
    nomor_hp: str = Form(...),
):
    isp = db.get_isp_by_slug(slug)
    if not isp:
        return JSONResponse({"ok": False, "msg": "ISP tidak ditemukan"})
    paket = db.get_paket_hotspot(paket_id)
    if not paket or paket["user_id"] != isp["id"]:
        return JSONResponse({"ok": False, "msg": "Paket tidak valid"})
    # Cek koneksi MikroTik server
    server = db.get_server(server_id)
    if not server or server["user_id"] != isp["id"]:
        return JSONResponse({"ok": False, "msg": "Server tidak valid"})
    try:
        MikroTik(server["vpn_ip"], server["api_port"], server["api_user"], server["api_password"])
    except Exception:
        return JSONResponse({"ok": False, "msg": "Server MikroTik tidak dapat dihubungi, coba beberapa saat lagi."})

    nomor_hp = nomor_hp.strip().replace("-", "").replace(" ", "")

    # Hitung amount sesuai fee_mode tenant
    fee_mode = (isp.get("fee_mode") or "customer").lower()
    amounts  = _calc_order_amounts(int(paket["harga"]), fee_mode)
    bayar    = amounts["pelanggan_bayar"]

    # Simpan order dengan amount = pelanggan_bayar (yang akan masuk ke Mayar)
    order_id = db.create_order(isp["id"], paket_id, server_id, nomor_hp, bayar)

    # ── 1. Mayar.id (prioritas utama, paling murah & sudah live) ────────────
    if MAYAR_KEY:
        nama_pembeli = f"{nomor_hp} ({isp['nama']})"
        mayar_result = _mayar_create_payment(
            order_id, bayar, nama_pembeli, nomor_hp,
            f"Voucher Hotspot {paket['nama']} - {isp['nama']}",
            f"https://{APP_DOMAIN}/beli/sukses/{order_id}"
        )
        if mayar_result and mayar_result.get("link"):
            # Webhook Mayar kirim transactionId di data.id, list API pakai paymentLinkId.
            # Simpan keduanya supaya webhook handler bisa match dengan reliable.
            tok = f"mayar:{mayar_result['id']}:{mayar_result.get('transaction_id','')}"
            db.set_order_snap_token(order_id, tok)
            return JSONResponse({
                "ok": True,
                "payment_url": mayar_result["link"],
                "order_id": order_id,
                "gateway": "mayar",
            })

    return JSONResponse({"ok": False, "msg": "Gateway pembayaran tidak tersedia. Hubungi ISP Anda."})


def _credit_pppoe_saldo(tagihan_id: int):
    """Credit saldo tenant dan fee platform setelah tagihan PPPoE dibayar online (Mayar)."""
    import logging as _log
    try:
        t = db.get_tagihan(tagihan_id)
        if not t:
            return
        isp      = db.get_user(t["user_id"])
        fee_mode = (isp.get("fee_mode") or "customer").lower() if isp else "customer"
        harga    = int(t["amount"])
        amts     = _calc_order_amounts(harga, fee_mode)
        saldo_in = amts["saldo_tenant"]
        if saldo_in > 0:
            db.topup_saldo(t["user_id"], saldo_in,
                           f"PPPoE #{tagihan_id} {t.get('nama_pelanggan','')} bulan {t.get('bulan','')} ({fee_mode}-mode, fee Rp{amts['platform_fee']})",
                           sumber="mayar")
        pf_amount    = amts["platform_fee"]
        platform_uid = db.get_platform_config("platform_uid") or ""
        if pf_amount > 0 and platform_uid:
            db.topup_saldo(platform_uid, pf_amount,
                           f"Fee platform PPPoE #{tagihan_id} ({isp['nama'] if isp else t['user_id']})",
                           sumber="mayar")
        _log.warning(f"[saldo credit pppoe] #{tagihan_id}: tenant +{saldo_in}, platform_fee +{pf_amount}")
    except Exception as e:
        import logging as _log2
        _log2.warning(f"[saldo credit pppoe] error #{tagihan_id}: {e}")


def _credit_voucher_saldo(order_id: str):
    """Credit saldo tenant dan fee platform setelah order voucher dikonfirmasi."""
    import logging as _log
    try:
        order    = db.get_order(order_id)
        if not order:
            return
        paket    = db.get_paket_hotspot(order["paket_id"])
        isp      = db.get_user(order["user_id"])
        fee_mode = (isp.get("fee_mode") or "customer").lower() if isp else "customer"
        amts     = _calc_order_amounts(int(paket["harga"]), fee_mode)
        saldo_in = amts["saldo_tenant"]
        if saldo_in > 0:
            db.topup_saldo(order["user_id"], saldo_in,
                           f"Voucher #{order_id} ({fee_mode}-mode, fee Rp{amts['platform_fee']})",
                           sumber="mayar")
            con = sqlite3.connect(DB_PATH)
            con.execute(
                "INSERT OR IGNORE INTO transaksi (id,user_id,ref_id,ref_type,amount,keterangan,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"TRX-{order_id[-8:]}", order["user_id"], order_id, "voucher", saldo_in,
                 f"Voucher {paket['nama'] if paket else ''} → {order.get('nomor_hp','')}", int(time.time()))
            )
            con.commit(); con.close()
        pf_amount    = amts["platform_fee"]
        platform_uid = db.get_platform_config("platform_uid") or ""
        if pf_amount > 0 and platform_uid:
            db.topup_saldo(platform_uid, pf_amount,
                           f"Fee platform voucher #{order_id} ({isp['nama'] if isp else order['user_id']})",
                           sumber="mayar")
        _log.warning(f"[saldo credit] {order_id}: tenant +{saldo_in}, platform_fee +{pf_amount}")
    except Exception as e:
        import logging as _log2
        _log2.warning(f"[saldo credit] error {order_id}: {e}")


def _mt_push_voucher(server_id: str, kode: str, paket: dict):
    """Push voucher baru ke MikroTik hotspot user setelah pembayaran."""
    server = db.get_server(server_id)
    if not server:
        return
    mt = MikroTik(server["vpn_ip"], server["api_port"], server["api_user"], server["api_password"])
    profile      = paket.get("kecepatan") or "default"
    comment      = paket.get("nama", "")
    limit_uptime = paket.get("durasi") or ""
    mt.add_hotspot_user(kode, kode, profile=profile, comment=comment, limit_uptime=limit_uptime)




@app.post("/beli/mayar-notif")
async def toko_mayar_notif(request: Request):
    """Webhook Mayar.id untuk pembelian voucher hotspot.

    Mayar mengirim event payment.received dengan body:
    {
      "event": "payment.received",
      "data": {
        "id": "<transactionId>",      # ← INI transaction id, bukan paymentLinkId
        "transactionId": "<same>",
        "status": "SUCCESS",           # ← uppercase
        ...
      }
    }

    Verifikasi: cek X-Callback-Token header sesuai webhook_token.
    """
    import logging as _logging
    body_text = (await request.body()).decode("utf-8", errors="replace")
    _logging.warning(f"[Mayar webhook] body={body_text[:500]}")

    # Verify token header
    token_hdr = (request.headers.get("X-Callback-Token") or
                 request.headers.get("x-callback-token") or "")
    if MAYAR_WEBHOOK and token_hdr != MAYAR_WEBHOOK:
        _logging.warning(f"[Mayar webhook] invalid/missing token: '{token_hdr[:16]}'")
        return JSONResponse({"ok": False, "msg": "Invalid token"}, status_code=401)

    try:
        body = json.loads(body_text) if body_text else {}
    except Exception:
        body = {}

    data       = body.get("data") or body.get("payment") or body
    tx_id      = data.get("transactionId") or data.get("id") or ""
    pay_link   = data.get("paymentLinkId") or ""
    status_raw = (data.get("status") or "").lower()

    if not tx_id and not pay_link:
        return JSONResponse({"ok": False, "msg": "Missing payment id"})

    # Hanya terima status "success" dari Mayar (status lain = belum settlement)
    if status_raw != "success":
        _logging.warning(f"[Mayar webhook] tx={tx_id} status={status_raw} (bukan success, skip)")
        return JSONResponse({"ok": True, "status": status_raw})

    # Cari order: snap_token format "mayar:<paymentLinkId>:<transactionId>"
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # 1. Cek hotspot_orders dulu
    row = None
    is_tagihan = False
    if tx_id:
        row = con.execute(
            "SELECT id, snap_token FROM hotspot_orders WHERE snap_token LIKE ? AND status='pending'",
            (f"mayar:%:{tx_id}",)
        ).fetchone()
    if not row and pay_link:
        row = con.execute(
            "SELECT id, snap_token FROM hotspot_orders WHERE snap_token LIKE ? AND status='pending'",
            (f"mayar:{pay_link}:%",)
        ).fetchone()

    # 2. Kalau tidak ada di hotspot_orders, cek tagihan_pppoe
    tagihan_row = None
    if not row:
        if tx_id:
            tagihan_row = con.execute(
                "SELECT id, snap_token FROM tagihan_pppoe WHERE snap_token LIKE ? AND status!='paid'",
                (f"mayar:%:{tx_id}",)
            ).fetchone()
        if not tagihan_row and pay_link:
            tagihan_row = con.execute(
                "SELECT id, snap_token FROM tagihan_pppoe WHERE snap_token LIKE ? AND status!='paid'",
                (f"mayar:{pay_link}:%",)
            ).fetchone()
        if tagihan_row:
            is_tagihan = True
            row = tagihan_row

    con.close()
    if not row:
        _logging.warning(f"[Mayar webhook] order not found tx={tx_id} pay_link={pay_link}")
        return JSONResponse({"ok": True, "msg": "Order not found"})

    order_id = row["id"]

    # Verifikasi ulang ke Mayar API sebelum confirm — pastikan benar-benar paid
    snap = row["snap_token"] or ""
    parts = snap.split(":")
    link_id = parts[1] if len(parts) > 1 else pay_link
    if not _mayar_verify_payment(link_id, tx_id):
        _logging.warning(f"[Mayar webhook] REJECTED: verifikasi API gagal untuk order {order_id} tx={tx_id}")
        return JSONResponse({"ok": False, "msg": "Payment not verified"}, status_code=400)

    _logging.warning(f"[Mayar webhook] confirming order {order_id} is_tagihan={is_tagihan}")

    if is_tagihan:
        # ── Tagihan PPPoE dibayar via Mayar ──────────────────────────────────
        t = db.get_tagihan(int(order_id))
        if not t:
            return JSONResponse({"ok": True, "msg": "Tagihan not found"})
        isp = db.get_user(t["user_id"])
        ok = db.bayar_tagihan(int(order_id), t["user_id"], metode="Mayar", keterangan="Bayar Online via Mayar")
        if ok:
            _credit_pppoe_saldo(int(order_id))
            _reaktivasi_pppoe(t["pppoe_id"], t["user_id"])
            if t.get("telepon"):
                label    = _label_bulan(t["bulan"])
                tok      = _isp_wa_token(t["user_id"])
                nominal  = f"Rp {t['amount']:,}".replace(",", ".")
                isp_nama  = isp["nama"] if isp else ""
                link_nota = f"https://{APP_DOMAIN}/nota/{t['id']}"
                wa_result = send_wa(
                    t["telepon"],
                    _render_wa_template(t["user_id"], "pembayaran",
                        nama=t["nama_pelanggan"], nominal=nominal,
                        bulan=label, isp=isp_nama, link_nota=link_nota),
                    token=tok
                )
                _logging.warning(f"[Mayar webhook] WA tagihan sent to {t['telepon']}: {wa_result}")
        return JSONResponse({"ok": True})

    # ── Voucher hotspot ───────────────────────────────────────────────────────
    # confirm_order hanya simpan ke DB, MT push dilakukan terpisah agar hasilnya bisa dicek
    voucher = db.confirm_order(order_id)
    if not voucher:
        _logging.warning(f"[Mayar webhook] confirm_order returned None for {order_id}")
        return JSONResponse({"ok": True, "msg": "Already confirmed or error"})

    _logging.warning(f"[Mayar webhook] voucher generated: {voucher['kode']} for {order_id}")

    # Push ke MikroTik — catat hasilnya; kalau gagal, retry job akan mencoba ulang
    order_for_mt = db.get_order(order_id)
    if order_for_mt:
        paket_for_mt = db.get_paket_hotspot(order_for_mt["paket_id"])
        try:
            _mt_push_voucher(order_for_mt["server_id"], voucher["kode"], paket_for_mt or {})
            db.set_voucher_mt_pushed(voucher["kode"], pushed=True)
            _logging.warning(f"[Mayar webhook] MT push OK: {voucher['kode']}")
        except Exception as mt_err:
            _logging.warning(f"[Mayar webhook] MT push GAGAL ({mt_err}), akan dicoba ulang otomatis: {voucher['kode']}")

    order = order_for_mt  # sudah di-fetch sebelumnya

    # Credit saldo tenant + fee platform
    _credit_voucher_saldo(order_id)
    if order and order.get("nomor_hp"):
        paket  = db.get_paket_hotspot(order["paket_id"])
        isp    = db.get_user(order["user_id"])
        isp_id = order["user_id"]
        tok    = _isp_wa_token(isp_id)
        pesan_wa = _render_wa_template(
            isp_id, "voucher_online",
            isp=isp["nama"] if isp else "",
            paket=paket["nama"] if paket else "",
            durasi=paket["durasi"] if paket else "",
            kode=voucher["kode"],
        )
        wa_result = send_wa(
            order["nomor_hp"], pesan_wa,
            token=tok, user_id=isp_id, tipe="voucher_online"
        )
        _logging.warning(f"[Mayar webhook] WA send to {order['nomor_hp']}: {wa_result}")
    return JSONResponse({"ok": True})


@app.get("/beli/sukses/{order_id}", response_class=HTMLResponse)
async def toko_sukses(request: Request, order_id: str):
    order = db.get_order(order_id)
    if not order:
        return HTMLResponse("<h2>Order tidak ditemukan</h2>", status_code=404)
    voucher = None
    paket = None
    isp = None
    if order["status"] == "paid" and order.get("voucher_id"):
        from storage import Storage as _S
        con = db._conn()
        v = con.execute("SELECT * FROM voucher_hotspot WHERE id=?", (order["voucher_id"],)).fetchone()
        con.close()
        voucher = dict(v) if v else None
    if order.get("paket_id"):
        paket = db.get_paket_hotspot(order["paket_id"])
    if order.get("user_id"):
        isp = db.get_user(order["user_id"])
    _snap_tok = order.get("snap_token") or ""
    return tpl.TemplateResponse(request, "store_sukses.html", _ctx(
        request, order=order, voucher=voucher, paket=paket, isp=isp
    ))


# ── Superadmin Panel ──────────────────────────────────────────────────────────
# Pindah ke admin-web (admin.vpntunel.my.id/keuangan). Catch-all redirect supaya
# bookmark / link lama tetap jalan ke panel konsolidasi.

@app.get("/sa")
@app.get("/sa/{rest:path}")
async def sa_moved_redirect(request: Request, rest: str = ""):
    return RedirectResponse("https://admin.vpntunel.my.id/keuangan", status_code=302)


@app.get("/registrasi")
async def registrasi_moved_redirect(request: Request):
    """Shortlink dari notif WA → admin.vpntunel.my.id/registrasi."""
    return RedirectResponse("https://admin.vpntunel.my.id/registrasi", status_code=302)


# ── Add-Ons ───────────────────────────────────────────────────────────────────

def _addon_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _wa_set_webhook(token: str, webhook_url: str):
    """Set webhook URL + subscribe events via WuzAPI API dan DB."""
    if not WA_URL:
        return
    try:
        payload = {"webhookUrl": webhook_url}
        if webhook_url:
            payload["subscribe"] = ["Message"]
        requests.post(
            f"{WA_URL}/webhook",
            json=payload,
            headers={"Token": token},
            timeout=5
        )
    except Exception:
        pass
    # Update DB sebagai fallback (agar persist setelah WuzAPI restart)
    if WA_USERS_DB and Path(WA_USERS_DB).exists():
        try:
            con = sqlite3.connect(WA_USERS_DB)
            con.execute(
                "UPDATE users SET webhook=?, events='Message' WHERE token=?",
                (webhook_url, token)
            )
            con.commit()
            con.close()
        except Exception:
            pass


def _chatbot_reply(user_id: str, nomor: str, pesan: str) -> str | None:
    """Proses pesan masuk pelanggan, return teks balasan atau None jika tidak dikenali."""
    from datetime import date
    keyword = pesan.strip().lower()

    # Cari pelanggan berdasarkan nomor WA
    con = db._conn()
    # Normalisasi nomor: 628xxx → 08xxx atau sebaliknya
    nomor_clean = nomor.replace("@s.whatsapp.net", "").replace("@c.us", "").strip()
    pu = con.execute(
        "SELECT p.*, pk.nama as paket_nama, pk.kecepatan FROM pppoe_users p "
        "LEFT JOIN paket_pppoe pk ON pk.id=p.paket_id "
        "WHERE p.user_id=? AND (p.telepon=? OR p.telepon=? OR p.telepon=?)",
        (user_id, nomor_clean,
         "0" + nomor_clean[2:] if nomor_clean.startswith("62") else "62" + nomor_clean[1:],
         nomor_clean)
    ).fetchone()

    # Jika nomor tidak terdaftar sebagai pelanggan tenant ini, abaikan
    if not pu:
        return None

    isp = db.get_user(user_id)
    isp_nama = isp["nama"] if isp else "ISP"

    if keyword in ("menu", "help", "bantuan", "halo", "hi", "hello"):
        nama = pu["nama_pelanggan"] if pu else "Pelanggan"
        return (
            f"Halo *{nama}*! 👋\n\n"
            f"Selamat datang di layanan pelanggan *{isp_nama}*.\n\n"
            f"Ketik salah satu perintah berikut:\n"
            f"📋 *TAGIHAN* — Cek tagihan bulan ini\n"
            f"💳 *BAYAR* — Dapat link pembayaran\n"
            f"📡 *STATUS* — Cek status koneksi\n"
            f"ℹ️ *INFO* — Info paket & akun\n\n"
            f"_Nomor Anda: {nomor_clean}_"
        )

    if not pu:
        return (
            f"Maaf, nomor *{nomor_clean}* tidak terdaftar sebagai pelanggan *{isp_nama}*.\n\n"
            f"Hubungi admin untuk informasi lebih lanjut."
        )

    pu = dict(pu)
    bulan = date.today().strftime("%Y-%m")
    tagihan_list = db.list_tagihan(user_id, bulan)
    tagihan = next((t for t in tagihan_list if t["pppoe_id"] == pu["id"]), None)
    con.close()

    if keyword in ("tagihan", "invoice", "cek tagihan", "cek"):
        if not tagihan:
            return (
                f"Halo *{pu['nama_pelanggan']}*,\n\n"
                f"Belum ada tagihan bulan ini untuk akun *{pu['username']}*.\n"
                f"Tagihan akan muncul pada tgl *{pu.get('tgl_bayar', 1)}* setiap bulan."
            )
        status_label = {"paid": "✅ Lunas", "unpaid": "⏳ Belum Bayar", "overdue": "❗ Terlambat"}.get(tagihan["status"], tagihan["status"])
        return (
            f"📋 *Tagihan Bulan Ini*\n\n"
            f"Nama: *{pu['nama_pelanggan']}*\n"
            f"Paket: *{pu.get('paket_nama') or '-'}*\n"
            f"Nominal: *Rp {tagihan['amount']:,}*\n"
            f"Status: {status_label}\n"
            f"Jatuh Tempo: tgl *{(pu.get('tgl_bayar', 1) or 1) + (isp.get('grace_period') or 10)}*\n\n"
            f"Ketik *BAYAR* untuk link pembayaran."
        ).replace(",", ".")

    if keyword in ("bayar", "payment", "pay", "link bayar", "link"):
        if tagihan and tagihan["status"] == "paid":
            return f"✅ Tagihan bulan ini sudah *LUNAS*. Terima kasih *{pu['nama_pelanggan']}*!"
        if not tagihan:
            return f"Belum ada tagihan aktif untuk akun *{pu['username']}* bulan ini."
        link = f"https://{APP_DOMAIN}/bayar/tagihan/{tagihan['id']}"
        return (
            f"💳 *Link Pembayaran*\n\n"
            f"Halo *{pu['nama_pelanggan']}*,\n"
            f"Nominal: *Rp {tagihan['amount']:,}*\n\n"
            f"Klik link berikut untuk bayar:\n{link}\n\n"
            f"_Link berlaku 24 jam._"
        ).replace(",", ".")

    if keyword in ("status", "koneksi", "cek status", "online"):
        online_set = db.get_all_online_usernames()
        is_online = pu["username"] in online_set
        status_akun = {"aktif": "✅ Aktif", "suspended": "⛔ Suspended", "nonaktif": "❌ Nonaktif"}.get(pu["status"], pu["status"])
        koneksi = "🟢 *Online*" if is_online else "🔴 *Offline*"
        return (
            f"📡 *Status Koneksi*\n\n"
            f"Nama: *{pu['nama_pelanggan']}*\n"
            f"Username: `{pu['username']}`\n"
            f"Paket: *{pu.get('paket_nama') or '-'}* ({pu.get('kecepatan') or '-'})\n"
            f"Status Akun: {status_akun}\n"
            f"Koneksi: {koneksi}"
        )

    if keyword in ("info", "paket", "akun", "profil"):
        return (
            f"ℹ️ *Info Akun*\n\n"
            f"Nama: *{pu['nama_pelanggan']}*\n"
            f"Username: `{pu['username']}`\n"
            f"Paket: *{pu.get('paket_nama') or '-'}* ({pu.get('kecepatan') or '-'})\n"
            f"Tgl Bayar: setiap tgl *{pu.get('tgl_bayar', 1)}*\n"
            f"Status: *{pu['status']}*"
        )

    # Perintah tidak dikenali — tapi pelanggan terdaftar, kirim panduan
    return (
        "Maaf, perintah tidak dikenali. 😅\n\n"
        "Ketik salah satu perintah:\n"
        "📋 *TAGIHAN* · 💳 *BAYAR* · 📡 *STATUS* · ℹ️ *INFO* · 📖 *MENU*"
    )


@app.post("/wa/chatbot/{token}")
async def wa_chatbot_webhook(request: Request, token: str):
    """Webhook WuzAPI — terima pesan masuk dari pelanggan."""
    import json as _json
    # WuzAPI kirim sebagai form-urlencoded: instanceName=...&jsonData={...}
    try:
        form = await request.form()
        json_data_str = form.get("jsonData", "")
        if not json_data_str:
            # fallback: coba parse sebagai JSON
            body = await request.json()
            json_data_str = body.get("jsonData", "")
    except Exception:
        try:
            body = await request.json()
            json_data_str = body.get("jsonData", "")
        except Exception:
            return JSONResponse({"ok": False})

    # Cari user_id berdasarkan token WA
    con = db._conn()
    isp_row = con.execute(
        "SELECT u.id FROM users u "
        "JOIN wa_gateway wg ON wg.user_id = u.id "
        "WHERE wg.wa_token=? AND u.status='aktif'", (token,)
    ).fetchone()
    con.close()

    if not isp_row:
        return JSONResponse({"ok": False})
    user_id = isp_row[0]

    # Cek addon ai_chat aktif
    con2 = db._conn()
    addon_aktif = con2.execute(
        "SELECT ta.id FROM tenant_addons ta "
        "JOIN addons a ON a.id=ta.addon_id "
        "WHERE ta.user_id=? AND a.code='ai_chat' AND ta.status='active'", (user_id,)
    ).fetchone()
    con2.close()
    if not addon_aktif:
        return JSONResponse({"ok": False})

    # Parse jsonData string → event object
    try:
        event_obj = _json.loads(json_data_str) if json_data_str else {}
    except Exception:
        event_obj = {}

    event = event_obj.get("event", event_obj)
    info = event.get("Info", {})

    if info.get("IsFromMe"):
        return JSONResponse({"ok": True})  # Abaikan pesan dari diri sendiri

    if info.get("IsGroup"):
        return JSONResponse({"ok": True})  # Abaikan pesan grup

    sender_alt = info.get("SenderAlt", "")
    nomor = sender_alt.replace("@s.whatsapp.net", "").replace("@c.us", "").replace("@lid", "")
    if not nomor:
        # fallback ke Chat field
        chat = info.get("Chat", "")
        nomor = chat.replace("@s.whatsapp.net", "").replace("@c.us", "").replace("@lid", "")
    if not nomor:
        return JSONResponse({"ok": False})

    message = event.get("Message", {})
    teks = (
        message.get("conversation") or
        (message.get("extendedTextMessage") or {}).get("text") or ""
    ).strip()

    if not teks:
        return JSONResponse({"ok": True})

    balasan = _chatbot_reply(user_id, nomor, teks)
    if not balasan:
        return JSONResponse({"ok": True})  # Nomor tidak terdaftar, diam saja

    send_wa(nomor, balasan, token=token)
    return JSONResponse({"ok": True})

def _vpncmd(cmd: str) -> str:
    """Jalankan perintah vpncmd ke SoftEther server."""
    import subprocess
    full = (
        f'/opt/softether/vpncmd localhost:5555 /SERVER '
        f'/PASSWORD:vpntunnel2024 /HUB:VPNTUNEL /CMD {cmd}'
    )
    r = subprocess.run(full, shell=True, capture_output=True, text=True, timeout=15)
    return r.stdout + r.stderr

def _vpn_create_user(vpn_username: str, vpn_password: str) -> bool:
    out = _vpncmd(f'UserCreate {vpn_username} /GROUP:none /REALNAME:"{vpn_username}" /NOTE:billing')
    _vpncmd(f'UserPasswordSet {vpn_username} /PASSWORD:{vpn_password}')
    return "successfully" in out.lower() or "completed" in out.lower()

def _vpn_delete_user(vpn_username: str) -> bool:
    out = _vpncmd(f'UserDelete {vpn_username}')
    return "successfully" in out.lower() or "completed" in out.lower()

def _vpn_user_exists(vpn_username: str) -> bool:
    out = _vpncmd(f'UserGet {vpn_username}')
    return "user name" in out.lower()


@app.get("/addons", response_class=HTMLResponse)
async def addons_page(request: Request, ok: str = "", err: str = ""):
    user = require_login(request)
    con = _addon_db()
    addons = [dict(r) for r in con.execute(
        "SELECT * FROM addons WHERE is_active=1 AND code IN ('ai_chat','laporan_pro','monitor_online') ORDER BY harga"
    ).fetchall()]
    aktif = {r["addon_id"]: dict(r) for r in con.execute(
        "SELECT * FROM tenant_addons WHERE user_id=? AND status='active'", (user["id"],)
    ).fetchall()}
    vpn_akun = con.execute("SELECT * FROM vpn_users WHERE user_id=?", (user["id"],)).fetchone()
    vpn_akun = dict(vpn_akun) if vpn_akun else None
    con.close()
    servers = db.list_servers(user["id"])
    server = servers[0] if servers else None
    gw = db.get_wa_gateway(user["id"])
    wa_token_aktif = gw.get("wa_token") if gw else None
    monitor_aktif = db.is_addon_active(user["id"], "monitor_telegram")
    monitor_addon = next((a for a in db.list_addons() if a["id"] == "monitor_telegram"), {})
    _con_mo = _addon_db()
    _mo_row = _con_mo.execute(
        "SELECT ta.status FROM tenant_addons ta JOIN addons a ON a.id=ta.addon_id "
        "WHERE ta.user_id=? AND a.code='monitor_online' AND ta.status='active'", (user["id"],)
    ).fetchone()
    _con_mo.close()
    monitor_online_aktif = bool(_mo_row)
    return tpl.TemplateResponse(request, "addons.html", {
        "request": request, "active": "addons", "user": user,
        "addons": addons, "aktif": aktif, "vpn_akun": vpn_akun,
        "server": server, "wa_token_aktif": wa_token_aktif,
        "monitor_aktif": monitor_aktif, "monitor_addon": monitor_addon,
        "monitor_aktif_sidebar": monitor_aktif,
        "monitor_online_aktif": monitor_online_aktif,
        "ok": ok, "err": err,
        **{k: v for k, v in {"app_name": APP_NAME, "app_domain": APP_DOMAIN, "main_domain": MAIN_DOMAIN}.items()},
    })


@app.post("/addons/{addon_id}/aktifkan", response_class=JSONResponse)
async def addon_aktifkan(request: Request, addon_id: int):
    user = require_login(request)
    con = _addon_db()
    addon = con.execute("SELECT * FROM addons WHERE id=?", (addon_id,)).fetchone()
    if not addon:
        con.close()
        return JSONResponse({"ok": False, "msg": "Add-on tidak ditemukan"})

    addon = dict(addon)
    sudah = con.execute(
        "SELECT id FROM tenant_addons WHERE user_id=? AND addon_id=? AND status='active'",
        (user["id"], addon_id)
    ).fetchone()
    if sudah:
        con.close()
        return JSONResponse({"ok": False, "msg": "Add-on sudah aktif"})

    # Cek saldo
    saldo = con.execute("SELECT saldo FROM users WHERE id=?", (user["id"],)).fetchone()
    if not saldo or saldo["saldo"] < addon["harga"]:
        con.close()
        return JSONResponse({"ok": False, "msg": f"Saldo tidak cukup. Butuh Rp {addon['harga']:,}"})

    import datetime
    now = datetime.date.today()
    exp = now.replace(month=now.month % 12 + 1) if now.month < 12 else now.replace(year=now.year + 1, month=1)

    # Potong saldo
    con.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (addon["harga"], user["id"]))

    # Aktifkan add-on
    con.execute(
        "INSERT OR REPLACE INTO tenant_addons (user_id, addon_id, status, started_at, expires_at) VALUES (?,?,?,?,?)",
        (user["id"], addon_id, "active", str(now), str(exp))
    )

    # Jika VPN Remote → buat akun VPN otomatis
    if addon["code"] == "vpn_remote":
        vpn_user = f"isp_{user['username']}"
        vpn_pass = uuid.uuid4().hex[:10]
        _vpn_create_user(vpn_user, vpn_pass)
        con.execute(
            "INSERT OR REPLACE INTO vpn_users (user_id, vpn_username, vpn_password) VALUES (?,?,?)",
            (user["id"], vpn_user, vpn_pass)
        )

    # Jika AI Chat → set webhook WuzAPI
    if addon["code"] == "ai_chat":
        gw = db.get_wa_gateway(user["id"])
        if gw and gw.get("wa_token"):
            webhook_url = f"https://{APP_DOMAIN}/wa/chatbot/{gw['wa_token']}"
            _wa_set_webhook(gw["wa_token"], webhook_url)

    con.commit()
    con.close()
    return JSONResponse({"ok": True, "msg": f"Add-on '{addon['nama']}' berhasil diaktifkan!"})


@app.post("/addons/{addon_id}/nonaktifkan", response_class=JSONResponse)
async def addon_nonaktifkan(request: Request, addon_id: int):
    user = require_login(request)
    con = _addon_db()
    addon = con.execute("SELECT * FROM addons WHERE id=?", (addon_id,)).fetchone()
    if not addon:
        con.close()
        return JSONResponse({"ok": False, "msg": "Add-on tidak ditemukan"})

    con.execute(
        "UPDATE tenant_addons SET status='cancelled' WHERE user_id=? AND addon_id=?",
        (user["id"], addon_id)
    )

    addon_dict = dict(addon)
    if addon_dict["code"] == "vpn_remote":
        vpn = con.execute("SELECT vpn_username FROM vpn_users WHERE user_id=?", (user["id"],)).fetchone()
        if vpn:
            _vpn_delete_user(vpn["vpn_username"])
            con.execute("UPDATE vpn_users SET status='inactive' WHERE user_id=?", (user["id"],))

    if addon_dict["code"] == "ai_chat":
        gw = db.get_wa_gateway(user["id"])
        if gw and gw.get("wa_token"):
            _wa_set_webhook(gw["wa_token"], "")

    con.commit()
    con.close()
    return JSONResponse({"ok": True, "msg": "Add-on dinonaktifkan"})


# ── Monitor Telegram Add-on ───────────────────────────────────────────────────

from monitor_worker import run_monitor_tick, run_command_tick, tg_test as _tg_test

async def _run_monitor_tick():
    await run_monitor_tick(db, get_mt)

async def _run_command_tick():
    await run_command_tick(db)

scheduler.add_job(_run_monitor_tick, "interval", seconds=30,
                  id="monitor_pppoe_tick", replace_existing=True)
scheduler.add_job(_run_command_tick, "interval", seconds=15,
                  id="monitor_command_tick", replace_existing=True)


@app.get("/addon/monitor", response_class=HTMLResponse)
async def addon_monitor_page(request: Request):
    user = require_login(request)
    iid  = user["id"]
    addon_aktif = db.is_addon_active(iid, "monitor_telegram")
    cfg = db.get_monitor_config(iid) or {}
    addon_info = next((a for a in db.list_addons() if a["id"] == "monitor_telegram"), {})
    events = db.list_monitor_events(iid, limit=30)
    return tpl.TemplateResponse(request, "addon_monitor.html", _ctx(
        request, user=user,
        addon_aktif=addon_aktif,
        cfg=cfg,
        addon_info=addon_info,
        events=events,
        active="addon_monitor",
    ))


@app.post("/addon/monitor/aktifkan", response_class=JSONResponse)
async def addon_monitor_aktifkan(request: Request):
    user  = require_login(request)
    iid   = user["id"]
    addon = next((a for a in db.list_addons() if a["id"] == "monitor_telegram"), None)
    if not addon:
        return JSONResponse({"ok": False, "msg": "Add-on tidak ditemukan"})
    harga = addon["harga"]
    con = db._conn()
    row = con.execute("SELECT saldo FROM users WHERE id=?", (iid,)).fetchone()
    saldo = int(row["saldo"]) if row else 0
    if saldo < harga:
        con.close()
        return JSONResponse({"ok": False, "msg": f"Saldo tidak cukup. Butuh Rp {harga:,}, saldo Rp {saldo:,}".replace(",",".")})
    con.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (harga, iid))
    con.commit()
    con.close()
    db.activate_addon(iid, "monitor_telegram", bulan=1)
    db.add_transaksi(iid, "", "addon", -harga, f"Add-on {addon['nama']} - 30 hari")
    return JSONResponse({"ok": True, "msg": "Add-on aktif 30 hari"})


@app.post("/addon/monitor/nonaktifkan", response_class=JSONResponse)
async def addon_monitor_nonaktifkan(request: Request):
    user = require_login(request)
    iid  = user["id"]
    if not db.is_addon_active(iid, "monitor_telegram"):
        return JSONResponse({"ok": False, "msg": "Add-on sudah tidak aktif"})
    db.deactivate_addon(iid, "monitor_telegram")
    return JSONResponse({"ok": True, "msg": "Bot Monitoring Telegram dinonaktifkan"})


@app.post("/addon/monitor/config", response_class=JSONResponse)
async def addon_monitor_config(request: Request):
    user = require_login(request)
    iid  = user["id"]
    if not db.is_addon_active(iid, "monitor_telegram"):
        return JSONResponse({"ok": False, "msg": "Add-on belum aktif"})
    body = await request.json()
    bot_token     = body.get("bot_token", "").strip()
    group_chat_id = body.get("group_chat_id", "").strip()
    grace         = int(body.get("grace_seconds", 60))
    notify_down   = int(body.get("notify_on_down", 1))
    notify_up     = int(body.get("notify_on_up", 1))
    if not bot_token or not group_chat_id:
        return JSONResponse({"ok": False, "msg": "Token bot dan Group Chat ID wajib diisi"})
    db.save_monitor_config(iid, bot_token, group_chat_id, grace, notify_down, notify_up)
    return JSONResponse({"ok": True, "msg": "Konfigurasi disimpan"})


@app.post("/addon/monitor/test", response_class=JSONResponse)
async def addon_monitor_test(request: Request):
    user = require_login(request)
    iid  = user["id"]
    if not db.is_addon_active(iid, "monitor_telegram"):
        return JSONResponse({"ok": False, "msg": "Add-on belum aktif"})
    cfg = db.get_monitor_config(iid)
    if not cfg or not cfg.get("bot_token") or not cfg.get("group_chat_id"):
        return JSONResponse({"ok": False, "msg": "Konfigurasi belum lengkap"})
    ok, err = await asyncio.to_thread(_tg_test, cfg["bot_token"], cfg["group_chat_id"])
    if ok:
        return JSONResponse({"ok": True, "msg": "Pesan test berhasil dikirim ke grup Telegram"})
    return JSONResponse({"ok": False, "msg": f"Gagal: {err}"})


# ── Route: Tagihan SaaS (tenant view) ────────────────────────────────────────

@app.post("/tagihan-saas/{tagihan_id}/bayar-saldo", response_class=JSONResponse)
async def tagihan_saas_bayar_saldo(request: Request, tagihan_id: str):
    user = require_login(request)
    con = db._conn()
    row = con.execute("SELECT * FROM saas_tagihan WHERE id=? AND user_id=?", (tagihan_id, user["id"])).fetchone()
    if not row:
        con.close()
        return JSONResponse({"ok": False, "msg": "Tagihan tidak ditemukan"})
    row = dict(row)
    if row["status"] != "unpaid":
        con.close()
        return JSONResponse({"ok": False, "msg": "Tagihan sudah tidak perlu dibayar"})
    if user["saldo"] < row["total"]:
        con.close()
        return JSONResponse({"ok": False, "msg": f"Saldo tidak cukup. Saldo Anda: Rp {user['saldo']:,}, tagihan: Rp {row['total']:,}"})
    now_ts = int(time.time())
    platform_uid = db.get_platform_config("platform_uid") or ""
    isp_nama = user.get("nama") or user["username"]
    con.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (row["total"], user["id"]))
    con.execute(
        "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,sumber,created_at) VALUES (?,?,?,?,?,?)",
        (user["id"], row["total"], "debit", f"Biaya SaaS bulan {row['bulan']}", "platform", now_ts)
    )
    # Credit ke platform owner
    if platform_uid:
        con.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (row["total"], platform_uid))
        con.execute(
            "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,sumber,created_at) VALUES (?,?,?,?,?,?)",
            (platform_uid, row["total"], "kredit", f"Fee SaaS {row['bulan']} ({isp_nama})", "saas", now_ts)
        )
    con.execute("UPDATE saas_tagihan SET status='paid', metode_bayar='saldo', paid_at=? WHERE id=?",
                (now_ts, tagihan_id))
    # Reaktivasi jika sudah tidak ada tagihan unpaid
    if user.get("status") == "suspend_saas":
        sisa = con.execute(
            "SELECT COUNT(*) FROM saas_tagihan WHERE user_id=? AND status IN ('unpaid','waiting_payment')", (user["id"],)
        ).fetchone()[0]
        if sisa == 0:
            con.execute("UPDATE users SET status='aktif' WHERE id=?", (user["id"],))
    con.commit()
    con.close()
    return JSONResponse({"ok": True, "msg": "Tagihan berhasil dilunasi dari saldo"})


@app.post("/tagihan-saas/{tagihan_id}/bayar-qris", response_class=JSONResponse)
async def tagihan_saas_bayar_qris(request: Request, tagihan_id: str):
    user = require_login(request)
    con = db._conn()
    row = con.execute("SELECT * FROM saas_tagihan WHERE id=? AND user_id=?", (tagihan_id, user["id"])).fetchone()
    if not row:
        con.close()
        return JSONResponse({"ok": False, "msg": "Tagihan tidak ditemukan"})
    row = dict(row)
    if row["status"] not in ("unpaid",):
        con.close()
        return JSONResponse({"ok": False, "msg": "Tagihan sudah tidak perlu dibayar"})
    # Set status waiting_payment agar admin tahu ada QRIS pending
    con.execute("UPDATE saas_tagihan SET status='waiting_payment', metode_bayar='qris', paid_at=NULL WHERE id=?",
                (tagihan_id,))
    con.commit()
    con.close()
    # Notif WA ke platform SA
    sa_wa = db.get_platform_config("wa_number") or PLATFORM_OWNER_WA
    platform_token = db.get_platform_config("wa_token") or WA_TOKEN
    msg = (f"💳 *[SaaS] Konfirmasi Pembayaran QRIS*\n\n"
           f"ISP: *{user.get('nama', user['username'])}*\n"
           f"Tagihan: *{row['bulan']}*\n"
           f"Nominal: *Rp {row['total']:,}*\n\n"
           f"Cek panel admin → Tagihan SaaS untuk konfirmasi.")
    if sa_wa:
        send_wa(sa_wa, msg, token=platform_token)
    return JSONResponse({"ok": True, "msg": "Konfirmasi terkirim. Tagihan akan diverifikasi oleh platform."})


@app.get("/tagihan-saas", response_class=HTMLResponse)
async def tagihan_saas_page(request: Request):
    user = require_login(request)
    con = db._conn()
    tagihan = con.execute(
        "SELECT * FROM saas_tagihan WHERE user_id=? ORDER BY bulan DESC", (user["id"],)
    ).fetchall()
    tagihan = [dict(r) for r in tagihan]
    tarif   = int(db.get_platform_config("saas_tarif_pppoe") or 1000)
    minimum = int(db.get_platform_config("saas_minimum") or 25000)
    n_pppoe = con.execute(
        "SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND status='aktif'", (user["id"],)
    ).fetchone()[0]
    subtotal = n_pppoe * tarif
    estimasi = max(subtotal, minimum) if n_pppoe > 0 else 0
    con.close()
    platform_qris = db.get_platform_config("qris_image")
    return tpl.TemplateResponse(request, "tagihan_saas.html", {
        "request": request, "user": user,
        "tagihan": tagihan, "n_pppoe": n_pppoe,
        "tarif": tarif, "minimum": minimum, "estimasi": estimasi,
        "platform_qris": platform_qris,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
