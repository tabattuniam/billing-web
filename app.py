"""BillingVPN — FastAPI billing web for PPPoE & Hotspot management."""
from __future__ import annotations
import time, yaml, requests, random
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeTimedSerializer, BadSignature

from storage import Storage
from mikrotik import MikroTik

# ── Config ────────────────────────────────────────────────────────────────────
_cfg_path = Path(__file__).parent / "configs" / "billing.yaml"
CFG = yaml.safe_load(_cfg_path.read_text())

APP_NAME   = CFG["app"]["name"]
SECRET_KEY = CFG["app"]["secret_key"]
PORT       = CFG["app"].get("port", 8094)
DB_PATH    = CFG["db_path"]
WA_URL     = CFG.get("wuzapi", {}).get("url", "")
WA_TOKEN   = CFG.get("wuzapi", {}).get("token", "")

db  = Storage(DB_PATH)
app = FastAPI()
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

def send_wa(nomor: str, pesan: str):
    if not WA_URL or not nomor:
        return
    try:
        requests.post(
            f"{WA_URL}/chat/send/text",
            json={"phone": nomor.strip().lstrip("0"), "body": pesan},
            headers={"Token": WA_TOKEN},
            timeout=5
        )
    except Exception:
        pass

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
async def pppoe_users(request: Request, server_id: str = ""):
    user = require_login(request)
    servers = db.list_servers(user["id"])
    users = db.list_pppoe_users(user["id"], server_id if server_id else None)
    pakets = db.list_paket_pppoe(user["id"])
    return tpl.TemplateResponse(request, "pppoe_users.html", _ctx(
        request, user=user, users=users, servers=servers, pakets=pakets, sel_server=server_id
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



if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
