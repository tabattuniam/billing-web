"""BillingVPN — FastAPI billing web for PPPoE & Hotspot management."""
from __future__ import annotations
import time, yaml, requests, random, hashlib, json, uuid, sqlite3
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException
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
WA_ADMIN_TOKEN = CFG.get("wuzapi", {}).get("admin_token", "")
WA_USERS_DB    = CFG.get("wuzapi", {}).get("users_db", "")
MT_SERVER   = CFG.get("midtrans", {}).get("server_key", "")
MT_CLIENT   = CFG.get("midtrans", {}).get("client_key", "")
MT_PROD     = CFG.get("midtrans", {}).get("is_production", False)
MT_BASE     = "https://app.midtrans.com" if MT_PROD else "https://app.sandbox.midtrans.com"

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


def require_login(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


def _ctx(request: Request, **kw) -> dict:
    """Build template context without 'request' (Starlette 1.x adds it automatically)."""
    return {"app_name": APP_NAME, **kw}

# ── WuzAPI ────────────────────────────────────────────────────────────────────

def _normalize_wa(nomor: str) -> str:
    n = nomor.strip().replace("-", "").replace(" ", "")
    if n.startswith("0"):
        n = "62" + n[1:]
    return n

def send_wa(nomor: str, pesan: str, token: str = ""):
    """Kirim WA. Jika token diisi, gunakan token ISP sendiri; fallback ke sistem."""
    if not WA_URL or not nomor:
        return
    tok = token or WA_TOKEN
    if not tok:
        return
    try:
        requests.post(
            f"{WA_URL}/chat/send/text",
            json={"phone": _normalize_wa(nomor), "body": pesan},
            headers={"Token": tok},
            timeout=5
        )
    except Exception:
        pass


def _isp_wa_token(user_id: str) -> str:
    """Ambil WA token ISP. Fallback ke sistem jika belum setup."""
    gw = db.get_wa_gateway(user_id)
    if gw and gw.get("wa_token") and gw.get("status") == "connected":
        return gw["wa_token"]
    return WA_TOKEN


def _wa_create_user(user_id: str, nama: str) -> str:
    """Daftarkan user baru di WuzAPI DB. Return token."""
    token = f"billing_{user_id.lower()}"
    if not WA_USERS_DB or not Path(WA_USERS_DB).exists():
        return token
    uid = uuid.uuid4().hex
    try:
        con = sqlite3.connect(WA_USERS_DB)
        con.execute(
            "INSERT OR IGNORE INTO users (id,name,token,webhook,events,connected) VALUES (?,?,?,?,?,?)",
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


def _wa_get_qr(token: str) -> str:
    """Ambil QR code base64. Trigger connect dulu jika belum."""
    try:
        r = requests.post(f"{WA_URL}/session/connect",
                          json={}, headers={"Token": token}, timeout=10)
        data = r.json()
        if data.get("success"):
            status = _wa_session_status(token)
            if status.get("connected"):
                return ""  # sudah connect
        # ambil QR
        r2 = requests.get(f"{WA_URL}/session/qr", headers={"Token": token}, timeout=5)
        return r2.json().get("data", {}).get("QRCode", "")
    except Exception:
        return ""


def _wa_disconnect(token: str):
    try:
        requests.post(f"{WA_URL}/session/disconnect", json={},
                      headers={"Token": token}, timeout=5)
    except Exception:
        pass

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

        # Generate tagihan bulan ini jika belum ada
        db.generate_tagihan(user_id, bulan)

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


# ── MikroTik helper ───────────────────────────────────────────────────────────

def get_mt(server_id: str) -> MikroTik | None:
    s = db.get_server(server_id)
    if not s:
        return None
    return MikroTik(s["vpn_ip"], s["api_port"], s["api_user"], s["api_password"])

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
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie("session", make_session(user["id"]), httponly=True, max_age=86400 * 7)
    return resp


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
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie("session", make_session(user["id"]), httponly=True, max_age=86400 * 7)
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("session")
    return resp

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = require_login(request)
    stats = db.stats(user["id"], user["role"])
    return tpl.TemplateResponse(request, "dashboard.html", _ctx(request, user=user, stats=stats))

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
    return tpl.TemplateResponse(request, "pppoe_paket.html", _ctx(request, user=user, pakets=pakets))


@app.post("/pppoe/paket/tambah")
async def pppoe_paket_tambah(
    request: Request,
    nama: str = Form(...), kecepatan: str = Form(...), harga: int = Form(...)
):
    user = require_login(request)
    db.create_paket_pppoe(user["id"], nama, kecepatan, harga)
    return RedirectResponse("/pppoe/paket", status_code=302)

# ── PPPoE Users ───────────────────────────────────────────────────────────────

@app.get("/pppoe/users", response_class=HTMLResponse)
async def pppoe_users(request: Request, server_id: str = "", status: str = "", q: str = ""):
    user = require_login(request)
    servers = db.list_servers(user["id"])
    users = db.list_pppoe_users(user["id"], server_id if server_id else None)
    if status:
        users = [u for u in users if u["status"] == status]
    if q:
        q_lower = q.lower()
        users = [u for u in users if
                 q_lower in u["nama_pelanggan"].lower() or
                 q_lower in u["username"].lower() or
                 q_lower in (u["telepon"] or "").lower()]
    pakets = db.list_paket_pppoe(user["id"])
    return tpl.TemplateResponse(request, "pppoe_users.html", _ctx(
        request, user=user, users=users, servers=servers, pakets=pakets,
        sel_server=server_id, sel_status=status, q=q
    ))


@app.post("/pppoe/users/tambah")
async def pppoe_user_tambah(
    request: Request,
    server_id: str = Form(...), nama_pelanggan: str = Form(...),
    username: str = Form(...), password: str = Form(...),
    paket_id: int = Form(...), telepon: str = Form(""),
    alamat: str = Form(""), tgl_bayar: int = Form(1)
):
    user = require_login(request)
    paket = db.get_paket_pppoe(paket_id)
    pid = db.create_pppoe_user(user["id"], server_id, nama_pelanggan, username, password, paket_id, telepon, alamat, tgl_bayar)
    mt = get_mt(server_id)
    if mt and paket:
        mt.add_pppoe_secret(username, password, profile=paket["nama"])
    db.add_transaksi(user["id"], str(pid), "pppoe", paket["harga"] if paket else 0, f"Tambah PPPoE {username}")
    return RedirectResponse("/pppoe/users", status_code=302)


@app.post("/pppoe/users/hapus/{pid}")
async def pppoe_user_hapus(request: Request, pid: int):
    user = require_login(request)
    pu = db.get_pppoe_user(pid)
    if pu and pu["user_id"] == user["id"]:
        mt = get_mt(pu["server_id"])
        if mt:
            mt.remove_pppoe_secret(pu["username"])
        db.delete_pppoe_user(pid)
    return RedirectResponse("/pppoe/users", status_code=302)


@app.post("/pppoe/users/edit/{pid}")
async def pppoe_user_edit(
    request: Request, pid: int,
    nama_pelanggan: str = Form(...), telepon: str = Form(""),
    alamat: str = Form(""), tgl_bayar: int = Form(1)
):
    user = require_login(request)
    pu = db.get_pppoe_user(pid)
    if pu and pu["user_id"] == user["id"]:
        db.update_pppoe_user(pid, nama_pelanggan, telepon, alamat, tgl_bayar)
    return RedirectResponse("/pppoe/users", status_code=302)


@app.post("/pppoe/users/status/{pid}")
async def pppoe_user_status(request: Request, pid: int, status: str = Form(...)):
    user = require_login(request)
    pu = db.get_pppoe_user(pid)
    if pu and pu["user_id"] == user["id"]:
        mt = get_mt(pu["server_id"])
        if mt:
            if status == "nonaktif":
                mt.disable_pppoe_secret(pu["username"])
            else:
                mt.enable_pppoe_secret(pu["username"])
        db.update_pppoe_status(pid, status)
    return RedirectResponse("/pppoe/users", status_code=302)

# ── WA Gateway ───────────────────────────────────────────────────────────────

@app.get("/wa-gateway", response_class=HTMLResponse)
async def wa_gateway_page(request: Request):
    user = require_login(request)
    gw = db.get_wa_gateway(user["id"])
    status_data = {}
    if gw and gw.get("wa_token"):
        status_data = _wa_session_status(gw["wa_token"])
        connected = status_data.get("connected", False)
        nomor = status_data.get("jid", "").split(":")[0] if connected else ""
        db.update_wa_gateway_status(user["id"],
                                    "connected" if connected else "disconnected", nomor)
        gw = db.get_wa_gateway(user["id"])
    return tpl.TemplateResponse(request, "wa_gateway.html", _ctx(
        request, user=user, gw=gw, status_data=status_data, active="wa_gateway"
    ))


@app.post("/wa-gateway/setup", response_class=JSONResponse)
async def wa_gateway_setup(request: Request):
    user = require_login(request)
    gw = db.get_wa_gateway(user["id"])
    if gw and gw.get("wa_token"):
        token = gw["wa_token"]
    else:
        token = _wa_create_user(user["id"], user["nama"])
        db.upsert_wa_gateway(user["id"], token)
    # Trigger connect + ambil QR
    qr = _wa_get_qr(token)
    status = _wa_session_status(token)
    if status.get("connected"):
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
    connected = s.get("connected", False)
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


# ── Tagihan PPPoE ────────────────────────────────────────────────────────────

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
async def tagihan_page(request: Request, bulan: str = "", status: str = ""):
    user = require_login(request)
    if not bulan:
        bulan = _bulan_sekarang()
    tagihan = db.list_tagihan(user["id"], bulan, status if status else None)
    stats   = db.stats_tagihan(user["id"], bulan)
    bulans  = _bulan_list()
    return tpl.TemplateResponse(request, "pppoe_tagihan.html", _ctx(
        request, user=user, tagihan=tagihan, stats=stats,
        bulan=bulan, bulans=bulans, sel_status=status, active="pppoe_tagihan"
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
    ok = db.bayar_tagihan(tid, user["id"])
    return JSONResponse({"ok": ok})


@app.post("/pppoe/tagihan/{tid}/kirim-link", response_class=JSONResponse)
async def tagihan_kirim_link(request: Request, tid: int):
    """Generate Midtrans snap token untuk tagihan, kirim link bayar via WA."""
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
    snap_token = _mt_snap_token(
        order_id, t["amount"],
        t["nama_pelanggan"], t["telepon"],
        finish_url=f"https://{APP_DOMAIN}/bayar/tagihan/{tid}/sukses"
    )
    if not snap_token:
        return JSONResponse({"ok": False, "msg": "Gagal membuat link pembayaran Midtrans"})

    db.set_tagihan_snap_token(tid, snap_token, order_id)
    link = f"https://{APP_DOMAIN}/bayar/tagihan/{tid}"
    send_wa(
        t["telepon"],
        f"💳 *Tagihan Internet {label}*\n\n"
        f"Halo *{t['nama_pelanggan']}*,\n\n"
        f"Tagihan bulan *{label}* sebesar:\n"
        f"*Rp {t['amount']:,}*\n\n"
        f"Bayar sekarang via link berikut:\n"
        f"{link}\n\n"
        f"_Pembayaran akan dikonfirmasi otomatis._",
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
        pesan = (
            f"📋 *Tagihan Internet {t['paket_nama'] or ''}*\n\n"
            f"Halo *{t['nama_pelanggan']}*,\n\n"
            f"Tagihan bulan *{label}* sebesar *Rp {t['amount']:,}* "
            f"belum kami terima.\n\n"
            f"Mohon segera lakukan pembayaran.\n\n"
            f"Terima kasih 🙏"
        ).replace(",", ".")
        send_wa(t["telepon"], pesan, token=_isp_wa_token(user["id"]))
        terkirim += 1
    return JSONResponse({"ok": True, "terkirim": terkirim})


# ── Hotspot Paket ─────────────────────────────────────────────────────────────

@app.get("/hotspot/paket", response_class=HTMLResponse)
async def hotspot_paket(request: Request):
    user = require_login(request)
    pakets = db.list_paket_hotspot(user["id"])
    return tpl.TemplateResponse(request, "hotspot_paket.html", _ctx(request, user=user, pakets=pakets))


@app.post("/hotspot/paket/tambah")
async def hotspot_paket_tambah(
    request: Request,
    nama: str = Form(...), durasi: str = Form(...),
    kecepatan: str = Form(""), harga: int = Form(...)
):
    user = require_login(request)
    db.create_paket_hotspot(user["id"], nama, durasi, kecepatan, harga)
    return RedirectResponse("/hotspot/paket", status_code=302)

# ── Voucher Hotspot ───────────────────────────────────────────────────────────

@app.get("/hotspot/voucher", response_class=HTMLResponse)
async def hotspot_voucher(request: Request, server_id: str = "", status: str = ""):
    user = require_login(request)
    servers = db.list_servers(user["id"])
    pakets  = db.list_paket_hotspot(user["id"])
    vouchers = db.list_vouchers(user["id"], server_id or None, status or None)
    return tpl.TemplateResponse(request, "voucher.html", _ctx(
        request, user=user, vouchers=vouchers, servers=servers, pakets=pakets,
        sel_server=server_id, sel_status=status
    ))


@app.post("/hotspot/voucher/generate")
async def voucher_generate(
    request: Request,
    server_id: str = Form(...), paket_id: int = Form(...), jumlah: int = Form(...)
):
    user = require_login(request)
    jumlah = min(jumlah, 500)
    db.create_vouchers(user["id"], server_id, paket_id, jumlah)
    return RedirectResponse("/hotspot/voucher", status_code=302)


@app.post("/hotspot/voucher/hapus")
async def voucher_hapus(request: Request, server_id: str = Form(...), status: str = Form("tersedia")):
    user = require_login(request)
    db.delete_vouchers(user["id"], server_id, status)
    return RedirectResponse("/hotspot/voucher", status_code=302)


@app.get("/hotspot/voucher/print", response_class=HTMLResponse)
async def voucher_print(request: Request, server_id: str = "", paket_id: str = ""):
    user = require_login(request)
    vouchers = db.list_vouchers(user["id"], server_id or None, "tersedia")
    if paket_id:
        vouchers = [v for v in vouchers if str(v["paket_id"]) == paket_id]
    pakets = db.list_paket_hotspot(user["id"])
    return tpl.TemplateResponse(request, "voucher_print.html", _ctx(request, user=user, vouchers=vouchers, pakets=pakets))

# ── Agen Management ───────────────────────────────────────────────────────────

@app.get("/agen", response_class=HTMLResponse)
async def agen_page(request: Request):
    user = require_login(request)
    if user["role"] == "admin":
        agenlist = db.list_users(role="agen")
    elif user["role"] == "agen":
        agenlist = db.list_users(role="sub_agen", parent_id=user["id"])
    else:
        return RedirectResponse("/dashboard", status_code=302)
    return tpl.TemplateResponse(request, "agen.html", _ctx(request, user=user, agenlist=agenlist))


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
    if user["role"] == "admin" and role not in ("agen", "sub_agen"):
        role = "agen"
    if user["role"] == "agen":
        role = "sub_agen"
    parent_id = "" if user["role"] == "admin" else user["id"]
    db.create_user(nama, username, password, role, parent_id, nomor_wa)
    return RedirectResponse("/agen", status_code=302)


@app.post("/agen/status/{uid}")
async def agen_status(request: Request, uid: str, status: str = Form(...)):
    require_login(request)
    db.update_user_status(uid, status)
    return RedirectResponse("/agen", status_code=302)

# ── Saldo ─────────────────────────────────────────────────────────────────────

@app.get("/saldo", response_class=HTMLResponse)
async def saldo_page(request: Request):
    user = require_login(request)
    logs = db.list_saldo_log(user["id"])
    return tpl.TemplateResponse(request, "saldo.html", _ctx(request, user=user, logs=logs))


@app.post("/saldo/topup/{uid}")
async def saldo_topup(request: Request, uid: str, jumlah: int = Form(...), keterangan: str = Form("")):
    user = require_login(request)
    if user["role"] != "admin":
        return RedirectResponse("/saldo", status_code=302)
    db.topup_saldo(uid, jumlah, keterangan)
    return RedirectResponse("/agen", status_code=302)

# ── Transaksi ─────────────────────────────────────────────────────────────────

@app.get("/transaksi", response_class=HTMLResponse)
async def transaksi_page(request: Request):
    user = require_login(request)
    txs = db.list_transaksi(user["id"])
    return tpl.TemplateResponse(request, "transaksi.html", _ctx(request, user=user, txs=txs))



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
    # Coba konfirmasi via Midtrans jika belum paid
    if t["status"] != "paid" and t.get("order_id"):
        if _mt_verify(t["order_id"]):
            result = db.bayar_tagihan_by_order(t["order_id"])
            if result:
                t = result
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
        if t and t.get("telepon"):
            label = _label_bulan(t["bulan"])
            send_wa(
                t["telepon"],
                f"✅ *Pembayaran Diterima!*\n\n"
                f"Halo *{t['nama_pelanggan']}*,\n\n"
                f"Tagihan internet bulan *{label}* sebesar "
                f"*Rp {t['amount']:,}* telah kami terima.\n\n"
                f"Terima kasih sudah membayar tepat waktu 🙏"
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


@app.get("/beli/{slug}", response_class=HTMLResponse)
async def toko_page(request: Request, slug: str):
    isp = db.get_isp_by_slug(slug)
    if not isp:
        return HTMLResponse("<h2>Toko tidak ditemukan</h2>", status_code=404)
    pakets = db.list_paket_hotspot_publik(isp["id"])
    servers = db.list_servers(isp["id"])
    return tpl.TemplateResponse(request, "store.html", _ctx(
        request, isp=isp, pakets=pakets, servers=servers,
        slug=slug, mt_client=MT_CLIENT, mt_prod=MT_PROD
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
    # Cek stok
    pakets_publik = db.list_paket_hotspot_publik(isp["id"])
    stok = next((p["stok"] for p in pakets_publik if p["id"] == paket_id), 0)
    if stok < 1:
        return JSONResponse({"ok": False, "msg": "Stok voucher habis, hubungi ISP."})

    nomor_hp = nomor_hp.strip().replace("-", "").replace(" ", "")
    order_id = db.create_order(isp["id"], paket_id, server_id, nomor_hp, paket["harga"])
    snap_token = _mt_snap_token(order_id, paket["harga"], isp["nama"], nomor_hp)
    if snap_token:
        db.set_order_snap_token(order_id, snap_token)
        return JSONResponse({"ok": True, "snap_token": snap_token, "order_id": order_id})
    else:
        # Fallback tanpa Midtrans — langsung konfirmasi (development/testing)
        voucher = db.confirm_order(order_id)
        if voucher:
            return JSONResponse({"ok": True, "order_id": order_id,
                                 "snap_token": None, "kode": voucher["kode"]})
        return JSONResponse({"ok": False, "msg": "Gagal memproses order"})


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
        voucher = db.confirm_order(order_id)
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
        voucher_raw = db.confirm_order(order_id)
        order = db.get_order(order_id)
        voucher = voucher_raw
    return tpl.TemplateResponse(request, "store_sukses.html", _ctx(
        request, order=order, voucher=voucher, paket=paket, isp=isp
    ))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
