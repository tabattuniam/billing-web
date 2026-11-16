"""Buat akun demo + data contoh untuk testing BillingVPN.

Membuat 1 tenant ISP "Demo Net" beserta teknisi, agen, router, paket,
pelanggan PPPoE, tagihan, voucher hotspot, dan ODC/ODP.

Pemakaian:
    python scripts/seed_demo.py                     # pakai db_path dari configs/billing.yaml
    python scripts/seed_demo.py --db data/demo.db   # database tertentu
    python scripts/seed_demo.py --reset             # hapus data demo lama lalu buat ulang
    python scripts/seed_demo.py --hapus             # hapus semua data demo saja
    python scripts/seed_demo.py --password "xxx"    # password sendiri (wajib di production)

Akun yang dibuat (password default di DEFAULT_PASSWORD):
    Admin ISP : demo           → /login
    Teknisi   : demo-teknisi   → /teknisi/demo
    Agen      : demo-agen      → /beli/demo/login

Catatan keamanan:
- Data demo TIDAK menyentuh MikroTik sungguhan: router demo menunjuk ke
  127.0.0.1 port 9 dengan status 'nonaktif', dan semua data ditandai sudah ter-push.
- Nomor WA pelanggan demo dikosongkan dan auto-reminder dimatikan,
  jadi scheduler tidak mengirim WhatsApp ke siapa pun.
- Jangan jalankan di production dengan password default.
"""
from __future__ import annotations

import argparse
import datetime as dt
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from storage import Storage, _hash  # noqa: E402

DEFAULT_PASSWORD = "demo12345"
DEMO_ADMIN = "demo"
DEMO_TEKNISI = "demo-teknisi"
DEMO_AGEN = "demo-agen"
DEMO_USERNAMES = (DEMO_ADMIN, DEMO_TEKNISI, DEMO_AGEN)

# Tabel yang punya kolom user_id dan berisi data milik tenant
TENANT_TABLES = (
    "tagihan_pppoe", "pppoe_users", "paket_pppoe", "voucher_hotspot", "paket_hotspot",
    "hotspot_orders", "hotspot_tagihan", "hotspot_pelanggan", "odp", "odc",
    "mikrotik_servers", "transaksi", "saldo_log", "topup_orders", "wa_templates",
    "wa_gateway", "wa_log", "wa_otp", "activity_log", "agen_income", "saas_tagihan",
    "tenant_addons", "saldo_adjustments",
)

PELANGGAN = [
    # nama, alamat, paket_idx, tgl_bayar, status, tagihan_bulan_ini, terpasang
    ("Budi Santoso",    "Jl. Melati No. 3",       0,  1, "aktif",     "paid",    1),
    ("Siti Aminah",     "Jl. Mawar No. 12",       1,  1, "aktif",     "paid",    1),
    ("Agus Prasetyo",   "Jl. Kenanga No. 7",      1,  5, "aktif",     "unpaid",  1),
    ("Dewi Lestari",    "Perum Griya Asri B-4",   2,  5, "aktif",     "unpaid",  1),
    ("Rudi Hartono",    "Jl. Anggrek No. 21",     0, 10, "aktif",     "paid",    1),
    ("Rina Wulandari",  "Jl. Flamboyan No. 2",    1, 10, "aktif",     "unpaid",  1),
    ("Joko Susilo",     "Dusun Sukamaju RT 03",   0, 15, "aktif",     "unpaid",  1),
    ("Maya Sari",       "Jl. Cempaka No. 9",      2, 15, "aktif",     "paid",    1),
    ("Hendra Gunawan",  "Jl. Dahlia No. 14",      1, 20, "aktif",     None,      0),
    ("Fitri Handayani", "Jl. Teratai No. 5",      0, 20, "suspended", "overdue", 1),
    ("Bambang Irawan",  "Jl. Kamboja No. 30",     1, 25, "suspended", "overdue", 1),
    ("Yuni Astuti",     "Dusun Mekarsari RT 01",  0, 25, "nonaktif",  None,      1),
]


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _set(con: sqlite3.Connection, table: str, where_id, **fields):
    """UPDATE hanya kolom yang memang ada (skema berubah-ubah lewat migrasi)."""
    have = _cols(con, table)
    fields = {k: v for k, v in fields.items() if k in have}
    if not fields:
        return
    sets = ",".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), where_id))


def hapus_demo(db_path: str) -> int:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    ph = ",".join("?" * len(DEMO_USERNAMES))
    ids = [r["id"] for r in con.execute(f"SELECT id FROM users WHERE username IN ({ph})", DEMO_USERNAMES)]
    if not ids:
        con.close()
        return 0
    idph = ",".join("?" * len(ids))
    tables = _tables(con)
    for t in TENANT_TABLES:
        if t in tables and "user_id" in _cols(con, t):
            con.execute(f"DELETE FROM {t} WHERE user_id IN ({idph})", ids)
    con.execute(f"DELETE FROM users WHERE id IN ({idph})", ids)
    con.commit()
    con.close()
    return len(ids)


def buat_demo(db: Storage, password: str) -> dict:
    con = db._conn()
    now = int(time.time())
    today = dt.date.today()
    bulan_ini = today.strftime("%Y-%m")
    bulan_lalu = (today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    rnd = random.Random(42)

    # ── Akun ──────────────────────────────────────────────────────────────────
    con.close()
    admin_id = db.create_user("Demo Net", DEMO_ADMIN, password, "admin")
    tek_id = db.create_user("Teknisi Demo", DEMO_TEKNISI, password, "teknisi", parent_id=admin_id)
    agen_id = db.create_user("Agen Demo", DEMO_AGEN, password, "agen", parent_id=admin_id)

    con = db._conn()
    _set(con, "users", admin_id, slug="demo", auto_reminder=0, grace_period=5, fee_mode="customer")
    _set(con, "users", tek_id, slug=DEMO_TEKNISI)
    _set(con, "users", agen_id, slug=DEMO_AGEN, saldo=150000, komisi_persen=10)
    con.commit()
    con.close()

    # ── Router (dummy, tidak bisa dihubungi) ─────────────────────────────────
    server_id = db.create_server(admin_id, "Router Demo", "127.0.0.1", 9, "demo", "demo", "Kantor Demo Net")
    con = db._conn()
    _set(con, "mikrotik_servers", server_id, status="nonaktif")
    con.commit()
    con.close()

    # ── ODC / ODP ─────────────────────────────────────────────────────────────
    db.create_odc(admin_id, server_id, "ODC Pusat", "Simpang Tiga", "1:8", -3.0, -1.6012, 102.1124)
    odp_a = db.create_odp(admin_id, server_id, "ODP-A01", "Jl. Melati", 8, -1.6020, 102.1131, None, "")
    odp_b = db.create_odp(admin_id, server_id, "ODP-B01", "Jl. Kenanga", 8, -1.6035, 102.1150, None, "")

    # ── Paket PPPoE ───────────────────────────────────────────────────────────
    paket = [
        db.create_paket_pppoe(admin_id, "Hemat 10 Mbps", "10M", 150000),
        db.create_paket_pppoe(admin_id, "Rumahan 20 Mbps", "20M", 200000),
        db.create_paket_pppoe(admin_id, "Bisnis 50 Mbps", "50M", 350000),
    ]
    harga = {paket[0]: 150000, paket[1]: 200000, paket[2]: 350000}

    # ── Pelanggan PPPoE + tagihan ─────────────────────────────────────────────
    tiga_bulan_lalu = now - 90 * 86400
    con = db._conn()
    for i, (nama, alamat, pk_idx, tgl, status, tg_status, terpasang) in enumerate(PELANGGAN, 1):
        pid = paket[pk_idx]
        uname = "demo" + nama.split()[0].lower()
        con.close()
        uid = db.create_pppoe_user(
            admin_id, server_id, nama, uname, f"pw{i:03d}", pid,
            telepon="", alamat=alamat, tgl_bayar=tgl, mt_pushed=1,
            tgl_mulai=tiga_bulan_lalu, terpasang=terpasang,
        )
        con = db._conn()
        _set(con, "pppoe_users", uid, status=status,
             odp_id=odp_a if i % 2 else odp_b, odp_port=(i + 1) // 2)

        # Bulan lalu: semua lunas
        paid_lalu = int(dt.datetime.strptime(f"{bulan_lalu}-{min(tgl, 28):02d}", "%Y-%m-%d").timestamp())
        con.execute(
            "INSERT OR IGNORE INTO tagihan_pppoe (user_id,pppoe_id,bulan,amount,status,paid_at,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (admin_id, uid, bulan_lalu, harga[pid], "paid", paid_lalu, paid_lalu),
        )
        # Bulan ini: sesuai skenario
        if tg_status:
            paid_at = now - rnd.randint(0, 5) * 86400 if tg_status == "paid" else None
            cur = con.execute(
                "INSERT OR IGNORE INTO tagihan_pppoe (user_id,pppoe_id,bulan,amount,status,paid_at,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (admin_id, uid, bulan_ini, harga[pid], tg_status, paid_at, now),
            )
            if tg_status == "paid":
                _set(con, "tagihan_pppoe", cur.lastrowid, metode_bayar="tunai",
                     keterangan_bayar="Data demo")
    con.commit()
    con.close()

    # ── Hotspot ───────────────────────────────────────────────────────────────
    hs = [
        db.create_paket_hotspot(admin_id, "Voucher 1 Jam", "1h", "hs-2M", 2000, server_id),
        db.create_paket_hotspot(admin_id, "Voucher 1 Hari", "1d", "hs-5M", 5000, server_id),
        db.create_paket_hotspot(admin_id, "Voucher 1 Minggu", "7d", "hs-5M", 25000, server_id),
    ]
    kode_all = []
    for p, jml in zip(hs, (10, 10, 5)):
        kode_all += db.create_vouchers(admin_id, server_id, p, jml, comment="demo")
    # Tandai sudah ter-push supaya job retry tidak mencoba menghubungi router
    con = db._conn()
    if "mt_pushed" in _cols(con, "voucher_hotspot"):
        con.execute(
            f"UPDATE voucher_hotspot SET mt_pushed=1 WHERE kode IN ({','.join('?' * len(kode_all))})",
            kode_all,
        )
    con.commit()
    con.close()

    return {"admin_id": admin_id, "pelanggan": len(PELANGGAN), "voucher": len(kode_all)}


def main():
    ap = argparse.ArgumentParser(description="Seed akun demo BillingVPN")
    ap.add_argument("--db", help="path database SQLite (default: db_path di configs/billing.yaml)")
    ap.add_argument("--password", default=DEFAULT_PASSWORD, help="password untuk ketiga akun demo")
    ap.add_argument("--reset", action="store_true", help="hapus data demo lama lalu buat ulang")
    ap.add_argument("--hapus", action="store_true", help="hapus data demo saja, tidak membuat ulang")
    args = ap.parse_args()

    db_path = args.db
    if not db_path:
        import yaml
        cfg = yaml.safe_load((ROOT / "configs" / "billing.yaml").read_text())
        db_path = cfg["db_path"]
    db_path = str(Path(db_path).expanduser().resolve())

    if len(args.password) < 6:
        sys.exit("Password minimal 6 karakter.")

    db = Storage(db_path)  # sekaligus membuat/migrasi skema

    if args.hapus or args.reset:
        n = hapus_demo(db_path)
        print(f"Data demo lama dihapus ({n} akun).")
        if args.hapus:
            return

    con = sqlite3.connect(db_path)
    ph = ",".join("?" * len(DEMO_USERNAMES))
    sudah = con.execute(f"SELECT username FROM users WHERE username IN ({ph})", DEMO_USERNAMES).fetchall()
    con.close()
    if sudah:
        sys.exit(f"Akun demo sudah ada ({', '.join(r[0] for r in sudah)}). Pakai --reset untuk membuat ulang.")

    hasil = buat_demo(db, args.password)
    pw_info = "(default, lihat DEFAULT_PASSWORD di skrip ini)" if args.password == DEFAULT_PASSWORD else "(sesuai --password)"
    print(f"""
Akun demo siap di {db_path}
  Admin ISP : {DEMO_ADMIN:<14} → /login
  Teknisi   : {DEMO_TEKNISI:<14} → /teknisi/demo
  Agen      : {DEMO_AGEN:<14} → /beli/demo/login
  Password  : {pw_info}
  Data      : {hasil['pelanggan']} pelanggan PPPoE, {hasil['voucher']} voucher, 3 paket PPPoE, 3 paket hotspot, 2 ODP
""")


if __name__ == "__main__":
    main()
