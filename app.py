"""BillingVPN — FastAPI billing web for PPPoE & Hotspot management."""
from __future__ import annotations
import time, yaml, requests, random, hashlib, json, uuid, sqlite3
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
SECRET_KEY  = CFG["app"]["secret_key"]
PORT        = CFG["app"].get("port", 8094)
DB_PATH     = CFG["db_path"]
WA_URL      = CFG.get("wuzapi", {}).get("url", "")
WA_TOKEN    = CFG.get("wuzapi", {}).get("token", "")
WA_ADMIN_TOKEN   = CFG.get("wuzapi", {}).get("admin_token", "")
WA_USERS_DB      = CFG.get("wuzapi", {}).get("users_db", "")
PLATFORM_OWNER_WA = CFG.get("platform", {}).get("owner_wa", "")
SA_USERNAME = CFG.get("superadmin", {}).get("username", "superadmin")
SA_PASSWORD = CFG.get("superadmin", {}).get("password", "")
MT_SERVER   = CFG.get("midtrans", {}).get("server_key", "")
MT_CLIENT   = CFG.get("midtrans", {}).get("client_key", "")
MT_PROD     = CFG.get("midtrans", {}).get("is_production", False)
MT_BASE     = "https://app.midtrans.com" if MT_PROD else "https://app.sandbox.midtrans.com"

DK_MERCHANT = CFG.get("duitku", {}).get("merchant_code", "")
DK_API_KEY  = CFG.get("duitku", {}).get("api_key", "")
DK_PROD     = CFG.get("duitku", {}).get("is_production", False)
DK_METHOD   = CFG.get("duitku", {}).get("payment_method", "M2")
DK_BASE     = "https://passport.duitku.com/webapi" if DK_PROD else "https://sandbox.duitku.com/webapi"

db  = Storage(DB_PATH)

scheduler = AsyncIOScheduler(timezone="Asia/Jakarta")

@asynccontextmanager
async def lifespan(application: FastAPI):
    scheduler.start()
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


tpl.env.filters["rp"] = rp
tpl.env.filters["ts_date"] = ts_date

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
    return {"app_name": APP_NAME, "app_domain": APP_DOMAIN, **kw}

# ── WuzAPI ────────────────────────────────────────────────────────────────────

def _normalize_wa(nomor: str) -> str:
    n = nomor.strip().replace("-", "").replace(" ", "")
    if n.startswith("0"):
        n = "62" + n[1:]
    return n

def send_wa(nomor: str, pesan: str, token: str = "") -> tuple[bool, str]:
    """Kirim WA. Return (ok, error_message)."""
    if not WA_URL:
        return False, "WA_URL tidak dikonfigurasi"
    if not nomor:
        return False, "Nomor HP kosong"
    tok = token or WA_TOKEN
    if not tok:
        return False, "Token WA belum dikonfigurasi"
    try:
        r = requests.post(
            f"{WA_URL}/chat/send/text",
            json={"phone": _normalize_wa(nomor), "body": pesan},
            headers={"Token": tok},
            timeout=8
        )
        data = r.json()
        if data.get("success") or data.get("code") == 200:
            return True, ""
        return False, data.get("message") or str(data)
    except Exception as e:
        return False, str(e)


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
    """Jalan tiap hari jam 08:00 WIB — kirim WA reminder + link bayar otomatis."""
    from datetime import date, timedelta
    today = date.today()
    bulan = today.strftime("%Y-%m")
    hari  = today.day

    # Reminder H-3, H-1, H+1 (setelah jatuh tempo)
    tgl_targets = {today.day, (today + timedelta(days=2)).day,
                   (today + timedelta(days=1)).day, (today - timedelta(days=1)).day}

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

        # Generate tagihan untuk pelanggan yang tgl_bayar = hari ini (jika belum ada)
        db.generate_tagihan(user_id, bulan, tgl_bayar=hari)

        # Ambil tagihan unpaid/overdue yang jatuh temponya hari ini atau target
        tagihan_list = db.list_tagihan(user_id, bulan)
        for t in tagihan_list:
            if t["status"] == "paid":
                continue
            if not t.get("telepon"):
                continue
            tgl = t.get("tgl_bayar") or 1
            if tgl not in tgl_targets:
                continue

            label = _label_bulan(bulan)
            link  = f"https://{APP_DOMAIN}/bayar/tagihan/{t['id']}"

            # Tentukan pesan berdasarkan posisi hari
            if tgl == (today + timedelta(days=2)).day:
                emoji = "🔔"
                keterangan = f"Tagihan bulan *{label}* jatuh tempo *3 hari lagi* (tgl {tgl})."
            elif tgl == (today + timedelta(days=1)).day:
                emoji = "⚠️"
                keterangan = f"Tagihan bulan *{label}* jatuh tempo *besok* (tgl {tgl})."
            elif tgl == today.day:
                emoji = "🚨"
                keterangan = f"Tagihan bulan *{label}* jatuh tempo *hari ini* (tgl {tgl})."
            else:
                emoji = "❗"
                keterangan = f"Tagihan bulan *{label}* sudah *melewati jatuh tempo*."

            pesan = (
                f"{emoji} *Reminder Tagihan Internet*\n\n"
                f"Halo *{t['nama_pelanggan']}*,\n\n"
                f"{keterangan}\n\n"
                f"Jumlah: *Rp {t['amount']:,}*\n\n"
                f"Bayar sekarang:\n{link}\n\n"
                f"_Abaikan jika sudah membayar._"
            ).replace(",", ".")
            send_wa(t["telepon"], pesan, token=tok)


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
    from datetime import date
    today = date.today()
    bulan = today.strftime("%Y-%m")
    hari_ini = today.day

    # Tandai overdue dulu
    con = db._conn()
    isps = con.execute("SELECT DISTINCT user_id FROM pppoe_users WHERE status='aktif'").fetchall()
    con.close()
    for isp_row in isps:
        db.tagihan_overdue(isp_row[0], bulan, hari_ini)

    # Cari tagihan overdue yang pppoe_users-nya masih aktif
    con = db._conn()
    rows = con.execute("""
        SELECT t.id, t.user_id, t.pppoe_id,
               p.username, p.server_id, p.telepon, p.nama_pelanggan
        FROM tagihan_pppoe t
        JOIN pppoe_users p ON p.id = t.pppoe_id
        WHERE t.status = 'overdue' AND p.status = 'aktif'
    """).fetchall()
    con.close()

    for r in rows:
        try:
            # Disable di MikroTik
            mt = get_mt(r["server_id"])
            if mt:
                mt.disable_pppoe_secret(r["username"])

            # Update status DB
            db.update_pppoe_status(r["pppoe_id"], "nonaktif")

            # WA notif
            if r["telepon"]:
                isp = db.get_user(r["user_id"])
                isp_nama = isp["nama"] if isp else ""
                tok = _isp_wa_token(r["user_id"])
                send_wa(
                    r["telepon"],
                    _render_wa_template(r["user_id"], "suspend",
                        nama=r["nama_pelanggan"], isp=isp_nama),
                    token=tok
                )
        except Exception:
            pass

scheduler.add_job(_run_auto_suspend, CronTrigger(hour=10, minute=0, timezone="Asia/Jakarta"),
                  id="auto_suspend", replace_existing=True)


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
    return tpl.TemplateResponse(request, "profil.html", _ctx(request, user=user, active="profil"))


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
    user = require_login(request)
    stats = db.stats(user["id"], user["role"])
    return tpl.TemplateResponse(request, "dashboard.html", _ctx(request, user=user, stats=stats))


@app.get("/bantuan", response_class=HTMLResponse)
async def bantuan_page(request: Request):
    user = require_login(request)
    return tpl.TemplateResponse(request, "bantuan.html", _ctx(request, user=user))


@app.get("/laporan", response_class=HTMLResponse)
async def laporan_page(request: Request, tahun: str = ""):
    user = require_login(request)
    from datetime import date
    if not tahun:
        tahun = str(date.today().year)
    tahun_list = [str(date.today().year - i) for i in range(3)]
    iid = _isp_id(user)
    bulanan        = db.laporan_pendapatan(iid, tahun)
    pelanggan      = db.laporan_pelanggan_baru(iid, tahun)
    topup_manual   = db.laporan_topup_agen(iid, tahun)
    pendapatan_agen = db.laporan_pendapatan_agen(iid, tahun)
    # Summary bulan ini
    bulan_ini = date.today().strftime("%Y-%m")
    stats_bln = db.stats_tagihan(iid, bulan_ini)
    # Total setahun
    total_tahun         = sum(b["total"] for b in bulanan)
    total_topup_manual  = sum(t["total"] for t in topup_manual)
    total_pend_agen     = sum(t["total"] for t in pendapatan_agen)
    total_semua         = total_tahun + total_topup_manual
    return tpl.TemplateResponse(request, "laporan.html", _ctx(
        request, user=user, active="laporan",
        tahun=tahun, tahun_list=tahun_list,
        bulanan=bulanan, pelanggan=pelanggan,
        topup_manual=topup_manual, total_topup_manual=total_topup_manual,
        pendapatan_agen=pendapatan_agen, total_pend_agen=total_pend_agen,
        stats_bln=stats_bln, total_tahun=total_tahun, total_semua=total_semua,
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
    api_password: str = Form(""), lokasi: str = Form("")
):
    user = require_login(request)
    db.create_server(user["id"], nama, vpn_ip, api_port, api_user, api_password, lokasi)
    return RedirectResponse("/servers", status_code=302)


@app.post("/servers/edit/{sid}")
async def server_edit(
    request: Request, sid: str,
    nama: str = Form(...), vpn_ip: str = Form(...),
    api_port: int = Form(8728), api_user: str = Form("admin"),
    api_password: str = Form(""), lokasi: str = Form("")
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
async def pppoe_users(request: Request, server_id: str = "", status: str = "", q: str = "", odp_id: str = ""):
    user = require_login(request)
    iid = _isp_id(user)
    servers = db.list_servers(iid)
    # Semua ODP (untuk JS data mapping), filter dropdown sesuai server dipilih
    odp_list = db.list_odp(iid)
    odp_filter_list = db.list_odp(iid, server_id if server_id else None)
    _odp_id = int(odp_id) if odp_id else None
    users = db.list_pppoe_users(iid, server_id if server_id else None, odp_id=_odp_id)
    if status:
        users = [u for u in users if u["status"] == status]
    if q:
        q_lower = q.lower()
        users = [u for u in users if
                 q_lower in u["nama_pelanggan"].lower() or
                 q_lower in u["username"].lower() or
                 q_lower in (u["telepon"] or "").lower()]
    pakets = db.list_paket_pppoe(iid)
    online_set = db.get_all_online_usernames()
    # Status tagihan bulan ini per pelanggan
    from datetime import date
    bulan_ini = date.today().strftime("%Y-%m")
    tagihan_bulan = db.list_tagihan(iid, bulan=bulan_ini)
    tagihan_map = {t["pppoe_id"]: t["status"] for t in tagihan_bulan}
    overdue_ids = {t["pppoe_id"] for t in tagihan_bulan if t["status"] == "overdue"}
    # Hitung usia cache
    cache_ages = []
    for s in servers:
        age = db.get_online_cache_age(s["id"])
        if age is not None:
            cache_ages.append(age)
    cache_age = min(cache_ages) if cache_ages else None
    push_msg = request.query_params.get("push")
    return tpl.TemplateResponse(request, "pppoe_users.html", _ctx(
        request, user=user, users=users, servers=servers, pakets=pakets,
        sel_server=server_id, sel_status=status, q=q, sel_odp=odp_id,
        odp_list=odp_list,
        online_set=online_set, overdue_ids=overdue_ids,
        tagihan_map=tagihan_map, bulan_ini=bulan_ini,
        odp_filter_list=odp_filter_list,
        cache_age=cache_age, push_msg=push_msg,
    ))


@app.post("/pppoe/users/tambah")
async def pppoe_user_tambah(
    request: Request,
    server_id: str = Form(...), nama_pelanggan: str = Form(...),
    username: str = Form(...), password: str = Form(...),
    paket_id: int = Form(...), telepon: str = Form(""),
    alamat: str = Form(""), tgl_bayar: int = Form(1),
    odp_id: str = Form(""), odp_port: str = Form("")
):
    user = require_login(request)
    iid = _isp_id(user)
    isp = db.get_user(iid)
    paket = db.get_paket_pppoe(paket_id)
    pid = db.create_pppoe_user(iid, server_id, nama_pelanggan, username, password, paket_id, telepon, alamat, tgl_bayar, mt_pushed=0)
    if odp_id.strip():
        db.assign_odp(pid, int(odp_id), int(odp_port) if odp_port.strip() else None)
    mt = get_mt(server_id)
    pushed = False
    if mt:
        profile = paket["nama"] if paket else "default"
        pushed = mt.add_pppoe_secret(username, password, profile=profile)
    if pushed:
        db.set_mt_pushed(pid, 1)
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


@app.post("/pppoe/users/hapus/{pid}")
async def pppoe_user_hapus(request: Request, pid: int):
    user = require_login(request)
    iid = _isp_id(user)
    pu = db.get_pppoe_user(pid)
    if pu and pu["user_id"] == iid:
        mt = get_mt(pu["server_id"])
        if mt:
            mt.remove_pppoe_secret(pu["username"])
        db.delete_pppoe_user(pid)
        _log(request, user, "Hapus Pelanggan PPPoE", f"{pu['nama_pelanggan']} ({pu['username']})")
    return RedirectResponse("/pppoe/users", status_code=302)


@app.post("/pppoe/users/edit/{pid}")
async def pppoe_user_edit(
    request: Request, pid: int,
    nama_pelanggan: str = Form(...), telepon: str = Form(""),
    alamat: str = Form(""), tgl_bayar: int = Form(1),
    username: str = Form(""), password: str = Form(""),
):
    user = require_login(request)
    iid = _isp_id(user)
    pu = db.get_pppoe_user(pid)
    if pu and pu["user_id"] == iid:
        new_user = username.strip() or pu["username"]
        new_pass = password.strip() or pu["password"]
        db.update_pppoe_user(pid, nama_pelanggan, telepon, alamat, tgl_bayar, new_user, new_pass)
        # Update password di MikroTik jika ada perubahan
        mt = get_mt(pu["server_id"])
        if mt and (new_user != pu["username"] or new_pass != pu["password"]):
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
        profile = paket["nama"] if paket else "default"
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
            paket["id"] if paket else None, telepon, "", tgl_bayar
        )
        imported += 1
    return JSONResponse({"imported": imported})


# ── WA Gateway ───────────────────────────────────────────────────────────────

@app.get("/wa-gateway", response_class=HTMLResponse)
async def wa_gateway_page(request: Request):
    user = require_login(request)
    gw = db.get_wa_gateway(user["id"])
    status_data = {}
    if gw and gw.get("wa_token"):
        status_data = _wa_session_status(gw["wa_token"])
        connected = _wa_is_logged_in(status_data)
        nomor = status_data.get("jid", "").split(":")[0] if connected else ""
        db.update_wa_gateway_status(user["id"],
                                    "connected" if connected else "disconnected", nomor)
        gw = db.get_wa_gateway(user["id"])
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
    ok, err = send_wa(nomor, pesan, token=tok)
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
async def tagihan_page(request: Request, bulan: str = "", status: str = "", q: str = ""):
    user = require_login(request)
    if not bulan:
        bulan = _bulan_sekarang()
    tagihan = db.list_tagihan(user["id"], bulan, status if status else None)
    if q:
        q_lower = q.lower()
        tagihan = [t for t in tagihan if
                   q_lower in (t.get("nama_pelanggan") or "").lower() or
                   q_lower in (t.get("pppoe_username") or "").lower()]
    stats   = db.stats_tagihan(user["id"], bulan)
    bulans  = _bulan_list()
    return tpl.TemplateResponse(request, "pppoe_tagihan.html", _ctx(
        request, user=user, tagihan=tagihan, stats=stats,
        bulan=bulan, bulans=bulans, sel_status=status, q=q, active="pppoe_tagihan"
    ))


@app.post("/pppoe/tagihan/generate", response_class=JSONResponse)
async def tagihan_generate(request: Request, bulan: str = Form("")):
    user = require_login(request)
    if not bulan:
        bulan = _bulan_sekarang()
    n = db.generate_tagihan(user["id"], bulan)
    return JSONResponse({"ok": True, "dibuat": n, "bulan": bulan})


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
            label   = _label_bulan(t["bulan"])
            tok     = _isp_wa_token(t["user_id"])
            nominal = f"Rp {t['amount']:,}".replace(",", ".")
            send_wa(
                t["telepon"],
                _render_wa_template(t["user_id"], "pembayaran",
                    nama=t["nama_pelanggan"], nominal=nominal,
                    bulan=label, isp=user["nama"]),
                token=tok
            )
    return JSONResponse({"ok": ok})


@app.post("/pppoe/tagihan/{tid}/kirim-link", response_class=JSONResponse)
async def tagihan_kirim_link(request: Request, tid: int):
    """Generate link bayar Duitku untuk tagihan, kirim via WA."""
    user = require_login(request)
    t = db.get_tagihan(tid)
    if not t or t["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "msg": "Tagihan tidak ditemukan"})
    if t["status"] == "paid":
        return JSONResponse({"ok": False, "msg": "Tagihan sudah lunas"})
    if not t.get("telepon"):
        return JSONResponse({"ok": False, "msg": "Nomor WA pelanggan belum diisi"})

    label = _label_bulan(t["bulan"])
    order_id = f"TGH-{tid}-{int(time.time())}"
    return_url = f"https://{APP_DOMAIN}/bayar/tagihan/{tid}/sukses"
    keterangan = f"Tagihan Internet {label} - {t['nama_pelanggan']}"

    # Coba Duitku dulu, fallback ke Midtrans jika tidak dikonfigurasi
    if DK_MERCHANT:
        payment_url = _duitku_create_invoice(
            order_id, t["amount"], t["nama_pelanggan"],
            t["telepon"], keterangan, return_url
        )
        if not payment_url:
            return JSONResponse({"ok": False, "msg": "Gagal membuat link pembayaran Duitku"})
        db.set_tagihan_snap_token(tid, payment_url, order_id)
    else:
        snap_token = _mt_snap_token(
            order_id, t["amount"], t["nama_pelanggan"], t["telepon"],
            finish_url=return_url
        )
        if not snap_token:
            return JSONResponse({"ok": False, "msg": "Gagal membuat link pembayaran"})
        db.set_tagihan_snap_token(tid, snap_token, order_id)

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
        send_wa(t["telepon"], pesan, token=_isp_wa_token(user["id"]))
        terkirim += 1
    return JSONResponse({"ok": True, "terkirim": terkirim})


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
async def hotspot_voucher(request: Request, server_id: str = "", status: str = "", paket_id: str = "", comment: str = ""):
    user = require_login(request)
    servers  = db.list_servers(user["id"])
    pakets   = db.list_paket_hotspot(user["id"])
    comments = db.list_voucher_comments(user["id"])
    comments_detail = db.list_voucher_comments_with_agen(user["id"])
    vouchers = db.list_vouchers(user["id"], server_id or None, status or None, paket_id or None, comment or None)
    return tpl.TemplateResponse(request, "voucher.html", _ctx(
        request, user=user, vouchers=vouchers, servers=servers, pakets=pakets,
        comments=comments, comments_detail=comments_detail,
        sel_server=server_id, sel_status=status, sel_paket=paket_id, sel_comment=comment
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
    vouchers = db.list_vouchers(
        user["id"],
        server_id or None,
        "tersedia",
        paket_id or None,
        comment or None
    )
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
    return tpl.TemplateResponse(request, "tim.html", _ctx(
        request, user=user, agenlist=agenlist, teknisi_list=teknisi_list,
        stats=stats, isp_slug=isp_slug, app_domain=APP_DOMAIN,
        ok=request.query_params.get("ok"),
        error=request.query_params.get("error"),
    ))


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
async def saldo_page(request: Request, bulan: str = ""):
    from datetime import date, datetime
    user = require_login(request)
    if not bulan:
        bulan = date.today().strftime("%Y-%m")
    logs_all = db.list_saldo_log(user["id"])

    # Filter bulan
    def _in_bulan(ts, bln):
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m") == bln
        except Exception:
            return False

    logs = [l for l in logs_all if _in_bulan(l["created_at"], bulan)]

    total_kredit = sum(l["jumlah"] for l in logs if l["tipe"] == "kredit")
    total_debit  = sum(l["jumlah"] for l in logs if l["tipe"] == "debit")

    saved_rek = {
        "bank": user.get("rek_bank") or "",
        "no":   user.get("rek_no") or "",
        "nama": user.get("rek_nama") or "",
    }
    pending_topup_count = db.count_topup_manual_pending(user["id"]) if user.get("role") == "admin" else 0

    # Tarik saldo pending
    con = db._conn()
    tarik_pending = con.execute(
        "SELECT COUNT(*) FROM tarik_saldo WHERE user_id=? AND status='pending'", (user["id"],)
    ).fetchone()[0]
    con.close()

    return tpl.TemplateResponse(request, "saldo.html", _ctx(
        request, user=user, logs=logs, saved_rek=saved_rek,
        sel_bulan=bulan,
        stats={"kredit": total_kredit, "debit": total_debit,
               "total_log": len(logs), "tarik_pending": tarik_pending},
        pending_topup_count=pending_topup_count))




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


@app.post("/sa/platform/tarik/{rid}/approve", response_class=JSONResponse)
async def sa_tarik_approve(request: Request, rid: int):
    if not current_sa(request): return JSONResponse({"ok": False, "detail": "Unauthorized"}, status_code=403)
    body = await request.json()
    admin_note = (body.get("note") or "").strip()
    row = db.approve_tarik_saldo_sa(rid, admin_note)
    if not row:
        return JSONResponse({"ok": False, "detail": "Request tidak ditemukan atau sudah diproses"})
    # WA notif ke ISP
    isp = db.get_user(row["user_id"])
    if isp and isp.get("nomor_wa"):
        platform_token = db.get_platform_config("wa_token") or WA_TOKEN
        send_wa(isp["nomor_wa"], (
            f"✅ *Tarik Saldo Disetujui*\n\n"
            f"Nominal: *Rp {row['nominal']:,}*\n"
            f"Rekening: {row['bank_name']} — {row['rekening_no']}\n"
            f"Dana akan ditransfer segera.\n"
            f"{('Catatan SA: ' + admin_note) if admin_note else ''}"
        ), token=platform_token)
    return JSONResponse({"ok": True})


@app.post("/sa/platform/tarik/{rid}/reject", response_class=JSONResponse)
async def sa_tarik_reject(request: Request, rid: int):
    if not current_sa(request): return JSONResponse({"ok": False, "detail": "Unauthorized"}, status_code=403)
    body = await request.json()
    admin_note = (body.get("note") or "").strip()
    row = db.reject_tarik_saldo_sa(rid, admin_note)
    if not row:
        return JSONResponse({"ok": False, "detail": "Request tidak ditemukan atau sudah diproses"})
    # WA notif ke ISP
    isp = db.get_user(row["user_id"])
    if isp and isp.get("nomor_wa"):
        platform_token = db.get_platform_config("wa_token") or WA_TOKEN
        send_wa(isp["nomor_wa"], (
            f"❌ *Tarik Saldo Ditolak*\n\n"
            f"Nominal: *Rp {row['nominal']:,}*\n"
            f"{('Alasan: ' + admin_note) if admin_note else 'Hubungi platform untuk info lebih lanjut.'}"
        ), token=platform_token)
    return JSONResponse({"ok": True})


@app.get("/sa/platform/tarik-json", response_class=JSONResponse)
async def sa_tarik_json(request: Request):
    if not current_sa(request): return JSONResponse({"ok": False, "detail": "Unauthorized"}, status_code=403)
    rows = db.list_tarik_saldo_all()
    return JSONResponse({"ok": True, "rows": rows})


@app.post("/saldo/topup/{uid}")
async def saldo_topup(request: Request, uid: str, jumlah: int = Form(...), keterangan: str = Form("")):
    user = require_login(request)
    if user["role"] != "admin":
        return RedirectResponse("/saldo", status_code=302)
    db.topup_saldo(uid, jumlah, keterangan)
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


@app.post("/saldo/topup-manual/{oid}/approve", response_class=JSONResponse)
async def topup_manual_approve(request: Request, oid: str):
    # Approval sekarang dilakukan oleh SA platform, bukan ISP
    return JSONResponse({"ok": False, "msg": "Persetujuan topup dikelola oleh platform vpntunel."})


@app.post("/saldo/topup-manual/{oid}/reject", response_class=JSONResponse)
async def topup_manual_reject(request: Request, oid: str):
    return JSONResponse({"ok": False, "msg": "Persetujuan topup dikelola oleh platform vpntunel."})

# ── Hotspot Bulanan ───────────────────────────────────────────────────────────

@app.get("/hotspot/bulanan", response_class=HTMLResponse)
async def hotspot_bulanan(request: Request, bulan: str = "", status: str = ""):
    user = require_login(request)
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
        send_wa(p["nomor_wa"], msg, token=_isp_wa_token(iid))
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
                  p.nama as paket_nama, 'hotspot_order' as tipe
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



# ── Bayar Tagihan PPPoE (Publik) ─────────────────────────────────────────────

@app.get("/bayar/tagihan/{tid}", response_class=HTMLResponse)
async def bayar_tagihan_page(request: Request, tid: int):
    t = db.get_tagihan(tid)
    if not t:
        return HTMLResponse("<h2>Tagihan tidak ditemukan</h2>", status_code=404)
    return tpl.TemplateResponse(request, "bayar_tagihan.html", _ctx(
        request, t=t, mt_client=MT_CLIENT, mt_prod=MT_PROD
    ))


@app.get("/bayar/tagihan/{tid}/sukses", response_class=HTMLResponse)
async def bayar_tagihan_sukses(request: Request, tid: int):
    t = db.get_tagihan(tid)
    if not t:
        return HTMLResponse("<h2>Tagihan tidak ditemukan</h2>", status_code=404)
    if t["status"] != "paid" and t.get("order_id"):
        snap = t.get("snap_token") or ""
        if snap.startswith("https://"):
            if _duitku_verify(t["order_id"]):
                result = db.bayar_tagihan_by_order(t["order_id"])
                if result:
                    t = result
                    _reaktivasi_pppoe(t["pppoe_id"], t["user_id"])
        else:
            if _mt_verify(t["order_id"]):
                result = db.bayar_tagihan_by_order(t["order_id"])
                if result:
                    t = result
                    _reaktivasi_pppoe(t["pppoe_id"], t["user_id"])
    return tpl.TemplateResponse(request, "bayar_tagihan.html", _ctx(
        request, t=t, mt_client=MT_CLIENT, mt_prod=MT_PROD, sukses=(t["status"] == "paid")
    ))


@app.post("/bayar/tagihan/notif")
async def bayar_tagihan_notif(request: Request):
    """Webhook Midtrans untuk tagihan PPPoE."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False})

    order_id    = body.get("order_id", "")
    status_code = body.get("status_code", "")
    gross_amount = body.get("gross_amount", "")
    sig_key     = body.get("signature_key", "")

    if not order_id.startswith("TGH-"):
        return JSONResponse({"ok": False})  # bukan tagihan PPPoE
    if sig_key != _mt_sig(order_id, status_code, gross_amount):
        return JSONResponse({"ok": False, "msg": "Invalid signature"})

    tx_status = body.get("transaction_status", "")
    if tx_status in ("capture", "settlement"):
        t = db.bayar_tagihan_by_order(order_id)
        if t:
            # Reaktivasi PPPoE jika sebelumnya disuspend
            _reaktivasi_pppoe(t["pppoe_id"], t["user_id"])
            if t.get("telepon"):
                label = _label_bulan(t["bulan"])
                tok = _isp_wa_token(t["user_id"])
                isp_nama = t.get("isp_nama", "")
                nominal = f"Rp {t['amount']:,}".replace(",", ".")
                send_wa(
                    t["telepon"],
                    _render_wa_template(t["user_id"], "pembayaran",
                        nama=t["nama_pelanggan"], nominal=nominal,
                        bulan=label, isp=isp_nama),
                    token=tok
                )
    return JSONResponse({"ok": True})


@app.post("/bayar/tagihan/duitku-notif")
async def bayar_tagihan_duitku_notif(request: Request):
    """Webhook Duitku untuk tagihan PPPoE."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False})

    merchant_order_id = body.get("merchantOrderId", "")
    amount            = str(body.get("amount", ""))
    result_code       = body.get("resultCode", "")
    sig               = body.get("signature", "")

    if not merchant_order_id.startswith("TGH-"):
        return JSONResponse({"ok": False})

    if sig != _dk_sig_callback(amount, merchant_order_id):
        return JSONResponse({"ok": False, "msg": "Invalid signature"})

    if result_code == "00":
        t = db.bayar_tagihan_by_order(merchant_order_id)
        if t:
            _reaktivasi_pppoe(t["pppoe_id"], t["user_id"])
            if t.get("telepon"):
                label   = _label_bulan(t["bulan"])
                tok     = _isp_wa_token(t["user_id"])
                nominal = f"Rp {t['amount']:,}".replace(",", ".")
                send_wa(
                    t["telepon"],
                    _render_wa_template(t["user_id"], "pembayaran",
                        nama=t["nama_pelanggan"], nominal=nominal,
                        bulan=label, isp=t.get("isp_nama", "")),
                    token=tok
                )
    return JSONResponse({"ok": True})


# ── Toko Hotspot Online (Publik) ─────────────────────────────────────────────

def _mt_snap_token(order_id: str, amount: int, nama: str, nomor_hp: str,
                   finish_url: str = "") -> str | None:
    """Buat Midtrans Snap token untuk pembayaran."""
    if not MT_SERVER:
        return None
    import base64
    auth = base64.b64encode(f"{MT_SERVER}:".encode()).decode()
    payload = {
        "transaction_details": {"order_id": order_id, "gross_amount": amount},
        "customer_details": {"first_name": nama, "phone": nomor_hp},
        "callbacks": {
            "finish": finish_url or f"https://{APP_DOMAIN}/beli/sukses/{order_id}"
        }
    }
    try:
        r = requests.post(
            f"{MT_BASE}/snap/v1/transactions",
            json=payload,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            timeout=10
        )
        data = r.json()
        return data.get("token")
    except Exception:
        return None


def _mt_verify(order_id: str) -> bool:
    """Verifikasi status pembayaran Midtrans."""
    if not MT_SERVER:
        return False
    import base64
    auth = base64.b64encode(f"{MT_SERVER}:".encode()).decode()
    try:
        r = requests.get(
            f"{MT_BASE}/v2/{order_id}/status",
            headers={"Authorization": f"Basic {auth}"},
            timeout=10
        )
        data = r.json()
        status = data.get("transaction_status", "")
        return status in ("capture", "settlement")
    except Exception:
        return False


def _mt_sig(order_id: str, status_code: str, gross_amount: str) -> str:
    raw = f"{order_id}{status_code}{gross_amount}{MT_SERVER}"
    return hashlib.sha512(raw.encode()).hexdigest()


# ── Duitku helpers ────────────────────────────────────────────────────────────

def _dk_sig_create(merchant_order_id: str, amount: int) -> str:
    raw = f"{DK_MERCHANT}{merchant_order_id}{amount}{DK_API_KEY}"
    return hashlib.md5(raw.encode()).hexdigest()

def _dk_sig_callback(amount: str, merchant_order_id: str) -> str:
    raw = f"{DK_MERCHANT}{amount}{merchant_order_id}{DK_API_KEY}"
    return hashlib.md5(raw.encode()).hexdigest()

def _dk_sig_check(merchant_order_id: str) -> str:
    raw = f"{DK_MERCHANT}{merchant_order_id}{DK_API_KEY}"
    return hashlib.md5(raw.encode()).hexdigest()


def _duitku_create_invoice(order_id: str, amount: int, nama: str,
                           telepon: str, keterangan: str,
                           return_url: str) -> str | None:
    """Buat invoice Duitku. Return payment URL atau None jika gagal."""
    if not DK_MERCHANT or not DK_API_KEY:
        return None
    if order_id.startswith("TOP-"):
        callback_url = f"https://{APP_DOMAIN}/panel/topup/duitku-notif"
    elif order_id.startswith("ORD-"):
        callback_url = f"https://{APP_DOMAIN}/beli/duitku-notif"
    else:
        callback_url = f"https://{APP_DOMAIN}/bayar/tagihan/duitku-notif"
    payload = {
        "merchantCode":    DK_MERCHANT,
        "paymentAmount":   amount,
        "merchantOrderId": order_id,
        "productDetails":  keterangan or "Tagihan Internet",
        "customerVaName":  nama[:50],
        "email":           "noreply@vpntunel.my.id",
        "phoneNumber":     telepon or "",
        "itemDetails": [{"name": keterangan or "Tagihan Internet",
                         "price": amount, "quantity": 1}],
        "callbackUrl":     callback_url,
        "returnUrl":       return_url,
        "signature":       _dk_sig_create(order_id, amount),
        "expiryPeriod":    1440,
        "paymentMethod":   DK_METHOD,
    }
    try:
        r = requests.post(
            f"{DK_BASE}/api/merchant/v2/inquiry",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15
        )
        import logging as _logging
        _logging.warning(f"[Duitku] HTTP {r.status_code} raw: {r.text[:500]}")
        data = r.json()
        _logging.warning(f"[Duitku] order={order_id} status={data.get('statusCode')} msg={data.get('statusMessage')} url={data.get('paymentUrl')}")
        if data.get("statusCode") == "00":
            return data.get("paymentUrl")
        return None
    except Exception as e:
        import logging as _logging
        _logging.warning(f"[Duitku] Exception: {e}")
        return None


def _duitku_verify(merchant_order_id: str) -> bool:
    """Cek status transaksi Duitku. Return True jika sudah lunas."""
    if not DK_MERCHANT or not DK_API_KEY:
        return False
    payload = {
        "merchantCode":    DK_MERCHANT,
        "merchantOrderId": merchant_order_id,
        "signature":       _dk_sig_check(merchant_order_id),
    }
    try:
        r = requests.post(
            f"{DK_BASE}/api/merchant/transactionStatus",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        data = r.json()
        return data.get("statusCode") == "00"
    except Exception:
        return False


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


@app.get("/panel", response_class=HTMLResponse)
async def panel_agen(request: Request):
    user = _require_agen(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    # Ambil data ISP parent
    isp = db.get_user(user["parent_id"]) if user.get("parent_id") else None
    pakets = db.list_paket_hotspot(isp["id"]) if isp else []
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
        mt_client=MT_CLIENT, mt_prod=MT_PROD,
        platform_qris=platform_qris
    ))


@app.post("/panel/topup", response_class=JSONResponse)
async def panel_topup(request: Request, amount: int = Form(...)):
    """Buat order topup saldo via QRIS."""
    user = _require_agen(request)
    if not user:
        return JSONResponse({"ok": False, "msg": "Tidak terotorisasi"})
    if amount < 10000:
        return JSONResponse({"ok": False, "msg": "Minimal topup Rp 10.000"})
    oid = db.create_topup_order(user["id"], amount)
    return_url = f"https://{APP_DOMAIN}/panel?ok=topup"
    if DK_MERCHANT:
        payment_url = _duitku_create_invoice(
            oid, amount, user["nama"], user.get("nomor_wa", ""),
            f"Topup Saldo Agen - {user['nama']}", return_url
        )
        if not payment_url:
            return JSONResponse({"ok": False, "msg": "Gagal membuat transaksi Duitku"})
        db.set_topup_snap_token(oid, payment_url)
        return JSONResponse({"ok": True, "payment_url": payment_url, "order_id": oid})
    else:
        snap_token = _mt_snap_token(
            oid, amount, user["nama"], user.get("nomor_wa", ""),
            finish_url=return_url
        )
        if snap_token:
            db.set_topup_snap_token(oid, snap_token)
            return JSONResponse({"ok": True, "snap_token": snap_token, "order_id": oid})
        return JSONResponse({"ok": False, "msg": "Gagal membuat transaksi. Cek konfigurasi Midtrans."})


@app.post("/panel/topup/notif")
async def panel_topup_notif(request: Request):
    """Webhook Midtrans untuk topup saldo agen."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False})
    oid = data.get("order_id", "")
    if not oid.startswith("TOP-"):
        return JSONResponse({"ok": False})
    status = data.get("transaction_status", "")
    fraud = data.get("fraud_status", "")
    gross = data.get("gross_amount", "0")
    sig_recv = data.get("signature_key", "")
    code = data.get("status_code", "200")
    if sig_recv and sig_recv != _mt_sig(oid, code, gross):
        return JSONResponse({"ok": False, "msg": "invalid signature"})
    if status in ("capture", "settlement") and fraud != "challenge":
        db.confirm_topup(oid)
    return JSONResponse({"ok": True})


@app.post("/panel/topup/duitku-notif")
async def panel_topup_duitku_notif(request: Request):
    """Webhook Duitku untuk topup saldo agen."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False})
    oid         = body.get("merchantOrderId", "")
    amount      = str(body.get("amount", ""))
    result_code = body.get("resultCode", "")
    sig         = body.get("signature", "")
    if not oid.startswith("TOP-"):
        return JSONResponse({"ok": False})
    if sig != _dk_sig_callback(amount, oid):
        return JSONResponse({"ok": False, "msg": "Invalid signature"})
    if result_code == "00":
        db.confirm_topup(oid)
    return JSONResponse({"ok": True})


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
    vouchers = db.list_vouchers(isp["id"], None, "tersedia", None, comment or None) if comment else []
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
async def store_agen_beli(request: Request, slug: str, paket_id: int = Form(...)):
    """Proses pembelian voucher menggunakan saldo."""
    isp = db.get_isp_by_slug(slug)
    if not isp:
        return HTMLResponse("<h2>Toko tidak ditemukan</h2>", status_code=404)
    user = current_user(request)
    if not user or user["role"] not in ("agen", "sub_agen"):
        return RedirectResponse(f"/beli/{slug}/login", status_code=302)
    result = db.beli_voucher_saldo(user["id"], isp["id"], paket_id)
    if result["ok"]:
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
        slug=slug, mt_client=MT_CLIENT, mt_prod=MT_PROD,
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
    order_id = db.create_order(isp["id"], paket_id, server_id, nomor_hp, paket["harga"])

    # Duitku (prioritas utama)
    if DK_MERCHANT:
        payment_url = _duitku_create_invoice(
            order_id, paket["harga"], isp["nama"], nomor_hp,
            f"Voucher Hotspot {paket['nama']}",
            f"https://{APP_DOMAIN}/beli/sukses/{order_id}"
        )
        if payment_url:
            db.set_order_snap_token(order_id, payment_url)
            return JSONResponse({"ok": True, "payment_url": payment_url, "order_id": order_id})

    # Fallback Midtrans
    snap_token = _mt_snap_token(order_id, paket["harga"], isp["nama"], nomor_hp)
    if snap_token:
        db.set_order_snap_token(order_id, snap_token)
        return JSONResponse({"ok": True, "snap_token": snap_token, "order_id": order_id})

    return JSONResponse({"ok": False, "msg": "Gateway pembayaran tidak tersedia"})


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


@app.post("/beli/notif")
async def toko_notif(request: Request):
    """Webhook Midtrans payment notification."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False})

    order_id      = body.get("order_id", "")
    status_code   = body.get("status_code", "")
    gross_amount  = body.get("gross_amount", "")
    sig_key       = body.get("signature_key", "")

    if sig_key != _mt_sig(order_id, status_code, gross_amount):
        return JSONResponse({"ok": False, "msg": "Invalid signature"})

    tx_status = body.get("transaction_status", "")
    if tx_status in ("capture", "settlement"):
        voucher = db.confirm_order(order_id, mt_callback=_mt_push_voucher)
        if voucher:
            order = db.get_order(order_id)
            if order and order.get("nomor_hp"):
                paket = db.get_paket_hotspot(order["paket_id"])
                isp = db.get_user(order["user_id"])
                send_wa(
                    order["nomor_hp"],
                    f"✅ *Pembayaran Berhasil!*\n\n"
                    f"Terima kasih sudah berlangganan *{isp['nama'] if isp else ''}*\n\n"
                    f"📦 Paket: {paket['nama'] if paket else ''}\n"
                    f"⏱ Durasi: {paket['durasi'] if paket else ''}\n\n"
                    f"🎟 *Kode Voucher Kamu:*\n\n"
                    f"  `{voucher['kode']}`\n\n"
                    f"Masukkan kode ini di halaman login hotspot WiFi."
                )
    return JSONResponse({"ok": True})


@app.post("/beli/duitku-notif")
async def toko_duitku_notif(request: Request):
    """Webhook Duitku untuk pembelian voucher hotspot."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False})

    merchant_order_id = body.get("merchantOrderId", "")
    amount            = str(body.get("amount", ""))
    result_code       = body.get("resultCode", "")
    sig               = body.get("signature", "")

    if not merchant_order_id.startswith("ORD-"):
        return JSONResponse({"ok": False})

    if sig != _dk_sig_callback(amount, merchant_order_id):
        return JSONResponse({"ok": False, "msg": "Invalid signature"})

    if result_code == "00":
        voucher = db.confirm_order(merchant_order_id, mt_callback=_mt_push_voucher)
        if voucher:
            order = db.get_order(merchant_order_id)
            if order and order.get("nomor_hp"):
                paket = db.get_paket_hotspot(order["paket_id"])
                isp   = db.get_user(order["user_id"])
                tok   = _isp_wa_token(order["user_id"])
                send_wa(
                    order["nomor_hp"],
                    f"✅ *Pembayaran Berhasil!*\n\n"
                    f"Terima kasih sudah berlangganan *{isp['nama'] if isp else ''}*\n\n"
                    f"📦 Paket: {paket['nama'] if paket else ''}\n"
                    f"⏱ Durasi: {paket['durasi'] if paket else ''}\n\n"
                    f"🎟 *Kode Voucher Kamu:*\n\n"
                    f"  `{voucher['kode']}`\n\n"
                    f"Masukkan kode ini di halaman login hotspot WiFi.",
                    token=tok
                )
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
    # Coba konfirmasi via Midtrans jika belum paid
    if order["status"] == "pending" and _mt_verify(order_id):
        voucher_raw = db.confirm_order(order_id, mt_callback=_mt_push_voucher)
        order = db.get_order(order_id)
        voucher = voucher_raw
    return tpl.TemplateResponse(request, "store_sukses.html", _ctx(
        request, order=order, voucher=voucher, paket=paket, isp=isp
    ))


# ── Superadmin Panel ──────────────────────────────────────────────────────────

def _sa_hash(pw: str) -> str:
    import hashlib
    return hashlib.sha256(pw.encode()).hexdigest()

SA_TOKEN_COOKIE = "sa_session"

def current_sa(request: Request) -> bool:
    token = request.cookies.get(SA_TOKEN_COOKIE)
    if not token:
        return False
    try:
        val = signer.loads(token, max_age=86400 * 2)
        return val == "superadmin"
    except Exception:
        return False

def require_sa(request: Request):
    if not current_sa(request):
        raise HTTPException(status_code=302, headers={"Location": "/sa/login"})


@app.get("/sa/login", response_class=HTMLResponse)
async def sa_login_page(request: Request):
    if current_sa(request):
        return RedirectResponse("/sa", status_code=302)
    return tpl.TemplateResponse(request, "sa_login.html", _ctx(request))


@app.post("/sa/login")
async def sa_login_post(request: Request,
                        username: str = Form(""), password: str = Form("")):
    if username == SA_USERNAME and _sa_hash(password) == _sa_hash(SA_PASSWORD):
        token = signer.dumps("superadmin")
        resp = RedirectResponse("/sa", status_code=302)
        resp.set_cookie(SA_TOKEN_COOKIE, token, httponly=True, max_age=86400 * 2)
        return resp
    return tpl.TemplateResponse(request, "sa_login.html", _ctx(request, error="Username atau password salah"))


@app.get("/sa/logout")
async def sa_logout(request: Request):
    resp = RedirectResponse("/sa/login", status_code=302)
    resp.delete_cookie(SA_TOKEN_COOKIE)
    return resp


@app.get("/sa", response_class=HTMLResponse)
async def sa_dashboard(request: Request):
    require_sa(request)
    tenants = db.list_tenants()
    stats = {t["id"]: db.get_tenant_stats(t["id"]) for t in tenants}
    adjustments = db.list_saldo_adjustments(limit=30)
    platform_qris = db.get_platform_config("qris_image")
    platform_wa = db.get_platform_config("wa_number")
    pending_topup_count = 0
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT COUNT(*) FROM topup_orders WHERE tipe='manual' AND status='pending'").fetchone()
    if row:
        pending_topup_count = row[0]
    con.close()
    reg_stats = db.count_registrasi()
    registrasi_list = db.list_registrasi()
    tarik_list = db.list_tarik_saldo_all()
    tarik_pending_count = sum(1 for t in tarik_list if t["status"] == "pending")
    return tpl.TemplateResponse(request, "sa_dashboard.html", _ctx(
        request, tenants=tenants, stats=stats, adjustments=adjustments,
        platform_qris=platform_qris, platform_wa=platform_wa,
        pending_topup_count=pending_topup_count,
        registrasi_list=registrasi_list, reg_stats=reg_stats,
        tarik_list=tarik_list, tarik_pending_count=tarik_pending_count,
        ok=request.query_params.get("ok"), err=request.query_params.get("error")
    ))


@app.get("/registrasi")
async def registrasi_redirect(request: Request):
    """Shortlink dari notif WA → langsung ke SA tab registrasi."""
    return RedirectResponse("/sa?tab=registrasi", status_code=302)


@app.post("/sa/registrasi/{rid}/approve", response_class=JSONResponse)
async def sa_registrasi_approve(request: Request, rid: int,
                                 username: str = Form(...), password: str = Form(...)):
    require_sa(request)
    reg = db.get_registrasi(rid)
    if not reg:
        return JSONResponse({"ok": False, "msg": "Data tidak ditemukan"})
    # Cek duplikat username
    _con = sqlite3.connect(DB_PATH); _con.row_factory = sqlite3.Row
    existing = _con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    _con.close()
    if existing:
        return JSONResponse({"ok": False, "msg": f"Username '{username}' sudah dipakai"})
    # Buat akun tenant
    uid = db.create_user(
        nama=reg["nama_isp"], username=username, password=password,
        role="admin", nomor_wa=reg["nomor_wa"]
    )
    # Update status registrasi
    db.update_registrasi_status(rid, "aktif")
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE tenant_registrasi SET username_tenant=?, password_tenant=? WHERE id=?",
                (username, password, rid))
    con.commit(); con.close()
    # Notif WA ke ISP baru pakai WA platform
    platform_token = db.get_platform_config("wa_token") or WA_TOKEN
    send_wa(reg["nomor_wa"],
            f"✅ *Akun ISP Anda Sudah Aktif!*\n\n"
            f"Nama ISP: *{reg['nama_isp']}*\n"
            f"Username: *{username}*\n"
            f"Password: *{password}*\n\n"
            f"Login di: https://billing.vpntunel.my.id/login",
            token=platform_token)
    return JSONResponse({"ok": True, "msg": f"Akun {username} berhasil dibuat dan diaktifkan"})


@app.post("/sa/registrasi/{rid}/tolak", response_class=JSONResponse)
async def sa_registrasi_tolak(request: Request, rid: int):
    require_sa(request)
    reg = db.get_registrasi(rid)
    if not reg:
        return JSONResponse({"ok": False, "msg": "Data tidak ditemukan"})
    db.update_registrasi_status(rid, "ditolak")
    send_wa(reg["nomor_wa"],
            f"❌ Maaf, pendaftaran ISP *{reg['nama_isp']}* ditolak. "
            f"Hubungi admin vpntunel untuk info lebih lanjut.")
    return JSONResponse({"ok": True})


@app.post("/sa/tenant/{uid}/adjust-saldo", response_class=JSONResponse)
async def sa_adjust_saldo(request: Request, uid: str,
                          amount: int = Form(0), catatan: str = Form("")):
    require_sa(request)
    if amount == 0:
        return JSONResponse({"ok": False, "msg": "Jumlah tidak boleh 0"})
    result = db.adjust_saldo_admin(uid, amount, catatan)
    if not result:
        return JSONResponse({"ok": False, "msg": "Tenant tidak ditemukan"})
    return JSONResponse({"ok": True, "saldo_before": result["saldo_before"], "saldo_after": result["saldo_after"]})


@app.post("/sa/tenant/{uid}/reset-password")
async def sa_reset_password(request: Request, uid: str,
                             password: str = Form("")):
    require_sa(request)
    if len(password) < 6:
        return RedirectResponse(f"/sa?error=password_terlalu_pendek", status_code=302)
    db.reset_password(uid, password)
    return RedirectResponse("/sa?ok=password_direset", status_code=302)


@app.post("/sa/tenant/{uid}/status")
async def sa_toggle_status(request: Request, uid: str,
                            status: str = Form("")):
    require_sa(request)
    if status not in ("aktif", "nonaktif"):
        return RedirectResponse("/sa", status_code=302)
    db.update_user_status(uid, status)
    return RedirectResponse("/sa?ok=status_diubah", status_code=302)


@app.get("/sa/tenant/{uid}/adjustments", response_class=JSONResponse)
async def sa_tenant_adjustments(request: Request, uid: str):
    require_sa(request)
    rows = db.list_saldo_adjustments(uid=uid, limit=50)
    return JSONResponse(rows)


@app.post("/sa/platform/topup/{oid}/approve", response_class=JSONResponse)
async def sa_topup_approve(request: Request, oid: str):
    require_sa(request)
    order = db.approve_topup_manual_sa(oid)
    if not order:
        return JSONResponse({"ok": False, "msg": "Order tidak ditemukan atau sudah diproses"})
    agen = db.get_user(order["user_id"])
    platform_token = db.get_platform_config("wa_token") or WA_TOKEN
    if agen:
        send_wa(agen.get("nomor_wa", ""),
                f"✅ Topup saldo Rp {order['amount']:,} telah dikonfirmasi oleh platform. Saldo Anda bertambah.",
                token=platform_token)
    return JSONResponse({"ok": True, "msg": f"Topup Rp {order['amount']:,} disetujui"})


@app.post("/sa/platform/topup/{oid}/reject", response_class=JSONResponse)
async def sa_topup_reject(request: Request, oid: str):
    require_sa(request)
    order = db.reject_topup_manual_sa(oid)
    if not order:
        return JSONResponse({"ok": False, "msg": "Order tidak ditemukan atau sudah diproses"})
    agen = db.get_user(order["user_id"])
    platform_token = db.get_platform_config("wa_token") or WA_TOKEN
    if agen:
        send_wa(agen.get("nomor_wa", ""),
                f"❌ Permintaan topup Rp {order['amount']:,} ditolak platform. Hubungi admin untuk info lebih lanjut.",
                token=platform_token)
    return JSONResponse({"ok": True, "msg": f"Topup Rp {order['amount']:,} ditolak"})


@app.post("/sa/platform/settings", response_class=JSONResponse)
async def sa_platform_settings(request: Request, wa_number: str = Form(""), catatan: str = Form("")):
    """SA simpan pengaturan platform (nomor WA, dll)."""
    require_sa(request)
    if wa_number:
        db.set_platform_config("wa_number", wa_number.strip())
    return RedirectResponse("/sa?ok=settings_saved&tab=qris", status_code=302)


@app.post("/sa/platform/wa-setup", response_class=JSONResponse)
async def sa_platform_wa_setup(request: Request):
    """Buat/ambil WA session platform dan return QR code."""
    require_sa(request)
    token = _wa_create_user("platform", "Platform vpntunel")
    db.set_platform_config("wa_token", token)
    qr = _wa_get_qr(token)
    status = _wa_session_status(token)
    if _wa_is_logged_in(status):
        nomor = status.get("jid", "").split(":")[0]
        db.set_platform_config("wa_connected", "1")
        db.set_platform_config("wa_nomor", nomor)
        return JSONResponse({"ok": True, "connected": True, "nomor": nomor})
    return JSONResponse({"ok": True, "connected": False, "qr": qr})


@app.get("/sa/platform/wa-status", response_class=JSONResponse)
async def sa_platform_wa_status(request: Request):
    """Cek status WA platform."""
    require_sa(request)
    token = db.get_platform_config("wa_token")
    if not token:
        return JSONResponse({"connected": False})
    s = _wa_session_status(token)
    connected = _wa_is_logged_in(s)
    nomor = s.get("jid", "").split(":")[0] if connected else ""
    if connected:
        db.set_platform_config("wa_connected", "1")
        db.set_platform_config("wa_nomor", nomor)
    else:
        db.set_platform_config("wa_connected", "0")
    return JSONResponse({"connected": connected, "nomor": nomor, "name": s.get("name", "")})


@app.post("/sa/platform/wa-disconnect", response_class=JSONResponse)
async def sa_platform_wa_disconnect(request: Request):
    """Disconnect WA platform."""
    require_sa(request)
    token = db.get_platform_config("wa_token")
    if token:
        _wa_disconnect(token)
        db.set_platform_config("wa_connected", "0")
        db.set_platform_config("wa_nomor", "")
    return JSONResponse({"ok": True})


@app.post("/sa/platform/qris-upload")
async def sa_platform_qris_upload(request: Request, qris_file: UploadFile = File(...)):
    """Super-admin upload QRIS platform (dipakai semua agen untuk topup)."""
    require_sa(request)
    import base64
    data = await qris_file.read()
    if len(data) > 500_000:
        return RedirectResponse("/sa?error=File+terlalu+besar+(maks+500KB)", status_code=302)
    b64 = base64.b64encode(data).decode()
    ext = qris_file.filename.rsplit(".", 1)[-1].lower() if "." in qris_file.filename else "png"
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
    db.set_platform_config("qris_image", f"data:{mime};base64,{b64}")
    return RedirectResponse("/sa?ok=qris_platform_saved", status_code=302)


@app.get("/sa/platform/topup-json", response_class=JSONResponse)
async def sa_platform_topup_json(request: Request, status: str = "pending"):
    require_sa(request)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    orders = con.execute(
        "SELECT t.*, u.nama as agen_nama, u.nomor_wa as agen_wa, "
        "i.nama as isp_nama "
        "FROM topup_orders t "
        "JOIN users u ON u.id=t.user_id "
        "LEFT JOIN users i ON i.id=t.isp_id "
        "WHERE t.tipe='manual' AND t.status=? "
        "ORDER BY t.created_at DESC LIMIT 100",
        (status,)
    ).fetchall()
    con.close()
    return JSONResponse([dict(o) for o in orders])


@app.get("/sa/platform/topup-requests", response_class=HTMLResponse)
async def sa_platform_topup_requests(request: Request, status: str = "pending"):
    """Super-admin lihat semua topup manual dari seluruh agen."""
    require_sa(request)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    orders = con.execute(
        "SELECT t.*, u.nama as agen_nama, u.nomor_wa as agen_wa, "
        "i.nama as isp_nama "
        "FROM topup_orders t "
        "JOIN users u ON u.id=t.user_id "
        "LEFT JOIN users i ON i.id=t.isp_id "
        "WHERE t.tipe='manual' AND t.status=? "
        "ORDER BY t.created_at DESC",
        (status,)
    ).fetchall()
    con.close()
    orders = [dict(o) for o in orders]
    platform_qris = db.get_platform_config("qris_image")
    return tpl.TemplateResponse(request, "sa_topup_requests.html", _ctx(
        request, orders=orders, sel_status=status, platform_qris=platform_qris
    ))


# ── Add-Ons ───────────────────────────────────────────────────────────────────

def _addon_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

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
    addons = [dict(r) for r in con.execute("SELECT * FROM addons WHERE is_active=1 ORDER BY kategori, harga").fetchall()]
    aktif = {r["addon_id"]: dict(r) for r in con.execute(
        "SELECT * FROM tenant_addons WHERE user_id=? AND status='active'", (user["id"],)
    ).fetchall()}
    vpn_akun = con.execute("SELECT * FROM vpn_users WHERE user_id=?", (user["id"],)).fetchone()
    vpn_akun = dict(vpn_akun) if vpn_akun else None
    con.close()
    return tpl.TemplateResponse(request, "addons.html", {
        "request": request, "active": "addons", "user": user,
        "addons": addons, "aktif": aktif, "vpn_akun": vpn_akun,
        "ok": ok, "err": err,
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

    if dict(addon)["code"] == "vpn_remote":
        vpn = con.execute("SELECT vpn_username FROM vpn_users WHERE user_id=?", (user["id"],)).fetchone()
        if vpn:
            _vpn_delete_user(vpn["vpn_username"])
            con.execute("UPDATE vpn_users SET status='inactive' WHERE user_id=?", (user["id"],))

    con.commit()
    con.close()
    return JSONResponse({"ok": True, "msg": "Add-on dinonaktifkan"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
