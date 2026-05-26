"""Storage — SQLite billing VPN."""
from __future__ import annotations
import json, sqlite3, time, uuid, hashlib
from pathlib import Path


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


class Storage:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self):
        con = self._conn()
        con.executescript("""
        -- Users: admin, agen, sub_agen
        CREATE TABLE IF NOT EXISTS users (
            id          TEXT PRIMARY KEY,
            nama        TEXT NOT NULL,
            username    TEXT NOT NULL UNIQUE,
            password    TEXT NOT NULL,
            role        TEXT DEFAULT 'agen',  -- admin | agen | sub_agen
            parent_id   TEXT DEFAULT '',
            nomor_wa    TEXT DEFAULT '',
            saldo       INTEGER DEFAULT 0,
            status      TEXT DEFAULT 'aktif',
            created_at  INTEGER
        );

        -- Server MikroTik milik agen (terhubung via VPN)
        CREATE TABLE IF NOT EXISTS mikrotik_servers (
            id              TEXT PRIMARY KEY,
            user_id         TEXT NOT NULL,
            nama            TEXT NOT NULL,
            vpn_ip          TEXT NOT NULL,
            api_port        INTEGER DEFAULT 8728,
            api_user        TEXT DEFAULT 'admin',
            api_password    TEXT DEFAULT '',
            lokasi          TEXT DEFAULT '',
            status          TEXT DEFAULT 'aktif',
            last_ping       INTEGER DEFAULT 0,
            created_at      INTEGER
        );

        -- Paket PPPoE
        CREATE TABLE IF NOT EXISTS paket_pppoe (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            nama        TEXT NOT NULL,
            kecepatan   TEXT NOT NULL,
            harga       INTEGER NOT NULL,
            status      TEXT DEFAULT 'aktif',
            created_at  INTEGER
        );

        -- User PPPoE
        CREATE TABLE IF NOT EXISTS pppoe_users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         TEXT NOT NULL,
            server_id       TEXT NOT NULL,
            nama_pelanggan  TEXT NOT NULL,
            username        TEXT NOT NULL,
            password        TEXT NOT NULL,
            paket_id        INTEGER,
            telepon         TEXT DEFAULT '',
            alamat          TEXT DEFAULT '',
            tgl_bayar       INTEGER DEFAULT 1,
            status          TEXT DEFAULT 'aktif',
            created_at      INTEGER
        );

        -- Paket Hotspot
        CREATE TABLE IF NOT EXISTS paket_hotspot (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            nama        TEXT NOT NULL,
            durasi      TEXT NOT NULL,
            kecepatan   TEXT DEFAULT '',
            harga       INTEGER NOT NULL,
            status      TEXT DEFAULT 'aktif',
            created_at  INTEGER
        );

        -- Voucher Hotspot
        CREATE TABLE IF NOT EXISTS voucher_hotspot (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            server_id   TEXT NOT NULL,
            paket_id    INTEGER NOT NULL,
            kode        TEXT NOT NULL UNIQUE,
            status      TEXT DEFAULT 'tersedia',  -- tersedia | dipakai | expired
            dipakai_at  INTEGER,
            created_at  INTEGER
        );

        -- Transaksi / tagihan
        CREATE TABLE IF NOT EXISTS transaksi (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            ref_id      TEXT NOT NULL,
            ref_type    TEXT NOT NULL,  -- pppoe | voucher | topup
            amount      INTEGER NOT NULL,
            keterangan  TEXT DEFAULT '',
            status      TEXT DEFAULT 'lunas',
            created_at  INTEGER
        );

        -- Deposit / saldo log agen
        CREATE TABLE IF NOT EXISTS saldo_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            jumlah      INTEGER NOT NULL,
            tipe        TEXT NOT NULL,  -- kredit | debit
            keterangan  TEXT DEFAULT '',
            created_at  INTEGER
        );

        -- OTP login via WhatsApp
        CREATE TABLE IF NOT EXISTS wa_otp (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            otp         TEXT NOT NULL,
            expires_at  INTEGER NOT NULL,
            used        INTEGER DEFAULT 0
        );

        -- Registrasi tenant ISP dari vpntunel.my.id/daftar
        CREATE TABLE IF NOT EXISTS tenant_registrasi (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            nama_isp            TEXT NOT NULL,
            nama_pemilik        TEXT NOT NULL,
            nomor_wa            TEXT NOT NULL,
            kota                TEXT DEFAULT '',
            paket               TEXT DEFAULT 'Starter',
            estimasi_pelanggan  TEXT DEFAULT '',
            catatan             TEXT DEFAULT '',
            status              TEXT DEFAULT 'pending',  -- pending | aktif | ditolak
            created_at          INTEGER
        );
        """)
        con.commit()
        self._seed_admin(con)
        con.close()

    def _seed_admin(self, con):
        exists = con.execute("SELECT id FROM users WHERE role='admin'").fetchone()
        if not exists:
            con.execute(
                "INSERT INTO users (id,nama,username,password,role,created_at) VALUES (?,?,?,?,?,?)",
                ("ADMIN001", "Administrator", "admin", _hash("billing@2024"), "admin", int(time.time()))
            )
            con.commit()

    # ── Auth ─────────────────────────────────────────────────────────────────

    def login(self, username: str, password: str) -> dict | None:
        con = self._conn()
        row = con.execute(
            "SELECT * FROM users WHERE username=? AND password=? AND status='aktif'",
            (username.strip(), _hash(password))
        ).fetchone()
        con.close()
        return dict(row) if row else None

    def get_user(self, uid: str) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        con.close()
        return dict(row) if row else None

    # ── Users / Agen ─────────────────────────────────────────────────────────

    def create_user(self, nama, username, password, role, parent_id="", nomor_wa="") -> str:
        uid = uuid.uuid4().hex[:8].upper()
        con = self._conn()
        con.execute(
            "INSERT INTO users (id,nama,username,password,role,parent_id,nomor_wa,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (uid, nama, username, _hash(password), role, parent_id, nomor_wa, int(time.time()))
        )
        con.commit()
        con.close()
        return uid

    def list_users(self, role=None, parent_id=None) -> list[dict]:
        con = self._conn()
        if role and parent_id:
            rows = con.execute("SELECT * FROM users WHERE role=? AND parent_id=? ORDER BY nama", (role, parent_id)).fetchall()
        elif role:
            rows = con.execute("SELECT * FROM users WHERE role=? ORDER BY nama", (role,)).fetchall()
        elif parent_id:
            rows = con.execute("SELECT * FROM users WHERE parent_id=? ORDER BY nama", (parent_id,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM users WHERE role != 'admin' ORDER BY nama").fetchall()
        con.close()
        return [dict(r) for r in rows]

    def update_user_status(self, uid: str, status: str):
        con = self._conn()
        con.execute("UPDATE users SET status=? WHERE id=?", (status, uid))
        con.commit()
        con.close()

    def username_exists(self, username: str) -> bool:
        con = self._conn()
        row = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        con.close()
        return row is not None

    # ── MikroTik Servers ─────────────────────────────────────────────────────

    def create_server(self, user_id, nama, vpn_ip, api_port, api_user, api_password, lokasi="") -> str:
        sid = uuid.uuid4().hex[:8].upper()
        con = self._conn()
        con.execute(
            "INSERT INTO mikrotik_servers (id,user_id,nama,vpn_ip,api_port,api_user,api_password,lokasi,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, user_id, nama, vpn_ip, api_port, api_user, api_password, lokasi, int(time.time()))
        )
        con.commit()
        con.close()
        return sid

    def get_server(self, sid: str) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM mikrotik_servers WHERE id=?", (sid,)).fetchone()
        con.close()
        return dict(row) if row else None

    def list_servers(self, user_id: str) -> list[dict]:
        con = self._conn()
        rows = con.execute("SELECT * FROM mikrotik_servers WHERE user_id=? ORDER BY nama", (user_id,)).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def update_server_ping(self, sid: str):
        con = self._conn()
        con.execute("UPDATE mikrotik_servers SET last_ping=? WHERE id=?", (int(time.time()), sid))
        con.commit()
        con.close()

    def delete_server(self, sid: str):
        con = self._conn()
        con.execute("DELETE FROM mikrotik_servers WHERE id=?", (sid,))
        con.commit()
        con.close()

    # ── Paket PPPoE ──────────────────────────────────────────────────────────

    def create_paket_pppoe(self, user_id, nama, kecepatan, harga) -> int:
        con = self._conn()
        cur = con.execute(
            "INSERT INTO paket_pppoe (user_id,nama,kecepatan,harga,created_at) VALUES (?,?,?,?,?)",
            (user_id, nama, kecepatan, harga, int(time.time()))
        )
        con.commit()
        pid = cur.lastrowid
        con.close()
        return pid

    def list_paket_pppoe(self, user_id: str) -> list[dict]:
        con = self._conn()
        rows = con.execute("SELECT * FROM paket_pppoe WHERE user_id=? AND status='aktif' ORDER BY harga", (user_id,)).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_paket_pppoe(self, pid: int) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM paket_pppoe WHERE id=?", (pid,)).fetchone()
        con.close()
        return dict(row) if row else None

    # ── PPPoE Users ──────────────────────────────────────────────────────────

    def create_pppoe_user(self, user_id, server_id, nama_pelanggan, username, password, paket_id, telepon="", alamat="", tgl_bayar=1) -> int:
        con = self._conn()
        cur = con.execute(
            "INSERT INTO pppoe_users (user_id,server_id,nama_pelanggan,username,password,paket_id,telepon,alamat,tgl_bayar,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, server_id, nama_pelanggan, username, password, paket_id, telepon, alamat, tgl_bayar, int(time.time()))
        )
        con.commit()
        pid = cur.lastrowid
        con.close()
        return pid

    def list_pppoe_users(self, user_id: str, server_id: str = None) -> list[dict]:
        con = self._conn()
        if server_id:
            rows = con.execute("SELECT p.*, pk.nama as paket_nama, pk.kecepatan FROM pppoe_users p LEFT JOIN paket_pppoe pk ON pk.id=p.paket_id WHERE p.user_id=? AND p.server_id=? ORDER BY p.nama_pelanggan", (user_id, server_id)).fetchall()
        else:
            rows = con.execute("SELECT p.*, pk.nama as paket_nama, pk.kecepatan, s.nama as server_nama FROM pppoe_users p LEFT JOIN paket_pppoe pk ON pk.id=p.paket_id LEFT JOIN mikrotik_servers s ON s.id=p.server_id WHERE p.user_id=? ORDER BY p.nama_pelanggan", (user_id,)).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_pppoe_user(self, pid: int) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM pppoe_users WHERE id=?", (pid,)).fetchone()
        con.close()
        return dict(row) if row else None

    def update_pppoe_user(self, pid: int, nama_pelanggan: str, telepon: str, alamat: str, tgl_bayar: int):
        con = self._conn()
        con.execute(
            "UPDATE pppoe_users SET nama_pelanggan=?, telepon=?, alamat=?, tgl_bayar=? WHERE id=?",
            (nama_pelanggan, telepon, alamat, tgl_bayar, pid)
        )
        con.commit()
        con.close()

    def update_pppoe_status(self, pid: int, status: str):
        con = self._conn()
        con.execute("UPDATE pppoe_users SET status=? WHERE id=?", (status, pid))
        con.commit()
        con.close()

    def delete_pppoe_user(self, pid: int):
        con = self._conn()
        con.execute("DELETE FROM pppoe_users WHERE id=?", (pid,))
        con.commit()
        con.close()

    # ── Paket Hotspot ─────────────────────────────────────────────────────────

    def create_paket_hotspot(self, user_id, nama, durasi, kecepatan, harga) -> int:
        con = self._conn()
        cur = con.execute(
            "INSERT INTO paket_hotspot (user_id,nama,durasi,kecepatan,harga,created_at) VALUES (?,?,?,?,?,?)",
            (user_id, nama, durasi, kecepatan, harga, int(time.time()))
        )
        con.commit()
        pid = cur.lastrowid
        con.close()
        return pid

    def list_paket_hotspot(self, user_id: str) -> list[dict]:
        con = self._conn()
        rows = con.execute("SELECT * FROM paket_hotspot WHERE user_id=? AND status='aktif' ORDER BY harga", (user_id,)).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_paket_hotspot(self, pid: int) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM paket_hotspot WHERE id=?", (pid,)).fetchone()
        con.close()
        return dict(row) if row else None

    # ── Voucher Hotspot ───────────────────────────────────────────────────────

    def create_vouchers(self, user_id: str, server_id: str, paket_id: int, jumlah: int) -> list[str]:
        import random, string
        kodes = []
        con = self._conn()
        for _ in range(jumlah):
            while True:
                kode = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
                exists = con.execute("SELECT id FROM voucher_hotspot WHERE kode=?", (kode,)).fetchone()
                if not exists:
                    break
            con.execute(
                "INSERT INTO voucher_hotspot (user_id,server_id,paket_id,kode,created_at) VALUES (?,?,?,?,?)",
                (user_id, server_id, paket_id, kode, int(time.time()))
            )
            kodes.append(kode)
        con.commit()
        con.close()
        return kodes

    def list_vouchers(self, user_id: str, server_id: str = None, status: str = None) -> list[dict]:
        con = self._conn()
        q = "SELECT v.*, p.nama as paket_nama, p.durasi, p.kecepatan FROM voucher_hotspot v LEFT JOIN paket_hotspot p ON p.id=v.paket_id WHERE v.user_id=?"
        args = [user_id]
        if server_id:
            q += " AND v.server_id=?"
            args.append(server_id)
        if status:
            q += " AND v.status=?"
            args.append(status)
        q += " ORDER BY v.created_at DESC"
        rows = con.execute(q, args).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def delete_vouchers(self, user_id: str, server_id: str, status: str = "tersedia"):
        con = self._conn()
        con.execute("DELETE FROM voucher_hotspot WHERE user_id=? AND server_id=? AND status=?", (user_id, server_id, status))
        con.commit()
        con.close()

    def list_servers_all(self) -> list[dict]:
        con = self._conn()
        rows = con.execute(
            "SELECT s.*, u.nama as user_nama FROM mikrotik_servers s LEFT JOIN users u ON u.id=s.user_id ORDER BY s.nama"
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    # ── Transaksi ─────────────────────────────────────────────────────────────

    def add_transaksi(self, user_id: str, ref_id: str, ref_type: str, amount: int, keterangan: str = ""):
        import uuid as _uuid
        tid = _uuid.uuid4().hex[:12].upper()
        con = self._conn()
        con.execute(
            "INSERT INTO transaksi (id,user_id,ref_id,ref_type,amount,keterangan,created_at) VALUES (?,?,?,?,?,?,?)",
            (tid, user_id, ref_id, ref_type, amount, keterangan, int(time.time()))
        )
        con.commit()
        con.close()

    def list_transaksi(self, user_id: str, limit: int = 100) -> list[dict]:
        con = self._conn()
        rows = con.execute(
            "SELECT * FROM transaksi WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    # ── Saldo ─────────────────────────────────────────────────────────────────

    def topup_saldo(self, uid: str, jumlah: int, keterangan: str = ""):
        con = self._conn()
        con.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (jumlah, uid))
        con.execute(
            "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,created_at) VALUES (?,?,?,?,?)",
            (uid, jumlah, "kredit", keterangan, int(time.time()))
        )
        con.commit()
        con.close()

    def debit_saldo(self, uid: str, jumlah: int, keterangan: str = ""):
        con = self._conn()
        con.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (jumlah, uid))
        con.execute(
            "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,created_at) VALUES (?,?,?,?,?)",
            (uid, jumlah, "debit", keterangan, int(time.time()))
        )
        con.commit()
        con.close()

    def list_saldo_log(self, user_id: str) -> list[dict]:
        con = self._conn()
        rows = con.execute(
            "SELECT * FROM saldo_log WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
            (user_id,)
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    # ── WA OTP Login ──────────────────────────────────────────────────────────

    def get_user_by_wa(self, nomor_wa: str) -> dict | None:
        con = self._conn()
        row = con.execute(
            "SELECT * FROM users WHERE nomor_wa=? AND status='aktif'", (nomor_wa,)
        ).fetchone()
        con.close()
        return dict(row) if row else None

    def create_otp(self, user_id: str, otp: str, ttl: int = 300):
        con = self._conn()
        con.execute("DELETE FROM wa_otp WHERE user_id=?", (user_id,))
        con.execute(
            "INSERT INTO wa_otp (user_id,otp,expires_at) VALUES (?,?,?)",
            (user_id, otp, int(time.time()) + ttl)
        )
        con.commit()
        con.close()

    def verify_otp(self, user_id: str, otp: str) -> bool:
        con = self._conn()
        row = con.execute(
            "SELECT id FROM wa_otp WHERE user_id=? AND otp=? AND used=0 AND expires_at>?",
            (user_id, otp, int(time.time()))
        ).fetchone()
        if row:
            con.execute("UPDATE wa_otp SET used=1 WHERE id=?", (row[0],))
            con.commit()
        con.close()
        return row is not None

    # ── Tenant Registrasi ─────────────────────────────────────────────────────

    def create_registrasi(self, nama_isp, nama_pemilik, nomor_wa, kota, paket, estimasi_pelanggan, catatan) -> int:
        con = self._conn()
        cur = con.execute(
            "INSERT INTO tenant_registrasi (nama_isp,nama_pemilik,nomor_wa,kota,paket,estimasi_pelanggan,catatan,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (nama_isp, nama_pemilik, nomor_wa, kota, paket, estimasi_pelanggan, catatan, int(time.time()))
        )
        con.commit()
        rid = cur.lastrowid
        con.close()
        return rid

    def list_registrasi(self, status: str = None) -> list[dict]:
        con = self._conn()
        if status:
            rows = con.execute("SELECT * FROM tenant_registrasi WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM tenant_registrasi ORDER BY created_at DESC").fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_registrasi(self, rid: int) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM tenant_registrasi WHERE id=?", (rid,)).fetchone()
        con.close()
        return dict(row) if row else None

    def update_registrasi_status(self, rid: int, status: str):
        con = self._conn()
        con.execute("UPDATE tenant_registrasi SET status=? WHERE id=?", (status, rid))
        con.commit()
        con.close()

    def count_registrasi(self) -> dict:
        con = self._conn()
        total   = con.execute("SELECT COUNT(*) FROM tenant_registrasi").fetchone()[0]
        pending = con.execute("SELECT COUNT(*) FROM tenant_registrasi WHERE status='pending'").fetchone()[0]
        aktif   = con.execute("SELECT COUNT(*) FROM tenant_registrasi WHERE status='aktif'").fetchone()[0]
        ditolak = con.execute("SELECT COUNT(*) FROM tenant_registrasi WHERE status='ditolak'").fetchone()[0]
        con.close()
        return {"total": total, "pending": pending, "aktif": aktif, "ditolak": ditolak}

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self, user_id: str, role: str) -> dict:
        con = self._conn()
        agen    = con.execute("SELECT COUNT(*) FROM users WHERE parent_id=?", (user_id,)).fetchone()[0]
        servers = con.execute("SELECT COUNT(*) FROM mikrotik_servers WHERE user_id=?", (user_id,)).fetchone()[0]
        pppoe   = con.execute("SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND status='aktif'", (user_id,)).fetchone()[0]
        voucher = con.execute("SELECT COUNT(*) FROM voucher_hotspot WHERE user_id=? AND status='tersedia'", (user_id,)).fetchone()[0]
        con.close()
        return {"agen": agen, "servers": servers, "pppoe": pppoe, "voucher": voucher}
