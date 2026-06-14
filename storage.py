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

    def _migrate(self, con):
        cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
        if "slug" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN slug TEXT DEFAULT ''")
            con.execute("UPDATE users SET slug=username WHERE slug='' OR slug IS NULL")
            con.commit()
        if "komisi_persen" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN komisi_persen INTEGER DEFAULT 0")
            con.commit()
        for col_def in ("rek_bank TEXT DEFAULT ''", "rek_no TEXT DEFAULT ''", "rek_nama TEXT DEFAULT ''"):
            col_name = col_def.split()[0]
            if col_name not in cols:
                con.execute(f"ALTER TABLE users ADD COLUMN {col_def}")
                con.commit()

        pu_cols = {r[1] for r in con.execute("PRAGMA table_info(pppoe_users)")}
        if "mt_pushed" not in pu_cols:
            con.execute("ALTER TABLE pppoe_users ADD COLUMN mt_pushed INTEGER DEFAULT 1")
            con.commit()

        tg_cols = {r[1] for r in con.execute("PRAGMA table_info(tagihan_pppoe)")}
        for col_def in ("metode_bayar TEXT DEFAULT ''", "keterangan_bayar TEXT DEFAULT ''"):
            col_name = col_def.split()[0]
            if col_name not in tg_cols:
                con.execute(f"ALTER TABLE tagihan_pppoe ADD COLUMN {col_def}")
                con.commit()

        con.execute("""
            CREATE TABLE IF NOT EXISTS pppoe_active_cache (
                server_id   TEXT NOT NULL,
                username    TEXT NOT NULL,
                synced_at   INTEGER NOT NULL,
                PRIMARY KEY (server_id, username)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS wa_templates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                jenis       TEXT NOT NULL,
                isi         TEXT NOT NULL DEFAULT '',
                updated_at  INTEGER,
                UNIQUE(user_id, jenis)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                role        TEXT NOT NULL DEFAULT '',
                aksi        TEXT NOT NULL,
                detail      TEXT DEFAULT '',
                ip          TEXT DEFAULT '',
                created_at  INTEGER NOT NULL
            )
        """)
        al_cols = {r[1] for r in con.execute("PRAGMA table_info(activity_log)")}
        if "ip" not in al_cols:
            con.execute("ALTER TABLE activity_log ADD COLUMN ip TEXT DEFAULT ''")

        ph_cols = {r[1] for r in con.execute("PRAGMA table_info(paket_hotspot)")}
        if "server_id" not in ph_cols:
            con.execute("ALTER TABLE paket_hotspot ADD COLUMN server_id TEXT DEFAULT ''")

        vh_cols = {r[1] for r in con.execute("PRAGMA table_info(voucher_hotspot)")}
        if "comment" not in vh_cols:
            con.execute("ALTER TABLE voucher_hotspot ADD COLUMN comment TEXT DEFAULT ''")
        if "agen_id" not in vh_cols:
            con.execute("ALTER TABLE voucher_hotspot ADD COLUMN agen_id TEXT DEFAULT ''")
        if "mt_pushed" not in vh_cols:
            con.execute("ALTER TABLE voucher_hotspot ADD COLUMN mt_pushed INTEGER DEFAULT 0")

        u_cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
        if "qris_image" not in u_cols:
            con.execute("ALTER TABLE users ADD COLUMN qris_image TEXT DEFAULT ''")

        to_cols = {r[1] for r in con.execute("PRAGMA table_info(topup_orders)")}
        if "tipe" not in to_cols:
            con.execute("ALTER TABLE topup_orders ADD COLUMN tipe TEXT DEFAULT 'gateway'")
        if "isp_id" not in to_cols:
            con.execute("ALTER TABLE topup_orders ADD COLUMN isp_id TEXT DEFAULT ''")
        if "catatan_agen" not in to_cols:
            con.execute("ALTER TABLE topup_orders ADD COLUMN catatan_agen TEXT DEFAULT ''")

        con.execute("""
            CREATE TABLE IF NOT EXISTS agen_income (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                agen_id     TEXT NOT NULL,
                paket_id    INTEGER NOT NULL,
                jumlah      INTEGER NOT NULL,
                total       INTEGER NOT NULL,
                komisi_persen INTEGER DEFAULT 0,
                created_at  INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS hotspot_pelanggan (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                server_id   TEXT NOT NULL DEFAULT '',
                nama        TEXT NOT NULL,
                nomor_wa    TEXT DEFAULT '',
                username    TEXT NOT NULL,
                password    TEXT NOT NULL,
                profile     TEXT NOT NULL DEFAULT 'default',
                harga       INTEGER NOT NULL DEFAULT 0,
                jatuh_tempo INTEGER NOT NULL DEFAULT 1,
                status      TEXT NOT NULL DEFAULT 'aktif',
                catatan     TEXT DEFAULT '',
                created_at  INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS hotspot_tagihan (
                id          TEXT PRIMARY KEY,
                pelanggan_id TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                bulan       TEXT NOT NULL,
                amount      INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'unpaid',
                paid_at     INTEGER,
                created_at  INTEGER NOT NULL,
                UNIQUE(pelanggan_id, bulan)
            )
        """)
        hp_cols = {r[1] for r in con.execute("PRAGMA table_info(hotspot_pelanggan)")}
        if "catatan" not in hp_cols:
            con.execute("ALTER TABLE hotspot_pelanggan ADD COLUMN catatan TEXT DEFAULT ''")

        con.execute("""
            CREATE TABLE IF NOT EXISTS saldo_adjustments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                amount      INTEGER NOT NULL,
                saldo_before INTEGER NOT NULL,
                saldo_after  INTEGER NOT NULL,
                catatan     TEXT DEFAULT '',
                by_sa       INTEGER DEFAULT 1,
                created_at  INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS odc (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         TEXT NOT NULL,
                server_id       TEXT NOT NULL DEFAULT '',
                nama            TEXT NOT NULL,
                lokasi          TEXT DEFAULT '',
                tipe_splitter   TEXT DEFAULT '1:4',
                daya_masuk_dbm  REAL DEFAULT -3.0,
                lat             REAL DEFAULT NULL,
                lng             REAL DEFAULT NULL,
                created_at      INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS odp (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                server_id   TEXT NOT NULL DEFAULT '',
                nama        TEXT NOT NULL,
                lokasi      TEXT DEFAULT '',
                kapasitas   INTEGER NOT NULL DEFAULT 8,
                created_at  INTEGER NOT NULL
            )
        """)
        odp_cols = {r[1] for r in con.execute("PRAGMA table_info(odp)")}
        if "server_id" not in odp_cols:
            con.execute("ALTER TABLE odp ADD COLUMN server_id TEXT NOT NULL DEFAULT ''")
        if "lat" not in odp_cols:
            con.execute("ALTER TABLE odp ADD COLUMN lat REAL DEFAULT NULL")
        if "lng" not in odp_cols:
            con.execute("ALTER TABLE odp ADD COLUMN lng REAL DEFAULT NULL")
        if "odc_id" not in odp_cols:
            con.execute("ALTER TABLE odp ADD COLUMN odc_id INTEGER DEFAULT NULL")
        if "tap_tipe" not in odp_cols:
            con.execute("ALTER TABLE odp ADD COLUMN tap_tipe TEXT DEFAULT ''")
        pu_cols2 = {r[1] for r in con.execute("PRAGMA table_info(pppoe_users)")}
        if "odp_id" not in pu_cols2:
            con.execute("ALTER TABLE pppoe_users ADD COLUMN odp_id INTEGER DEFAULT NULL")
        if "odp_port" not in pu_cols2:
            con.execute("ALTER TABLE pppoe_users ADD COLUMN odp_port INTEGER DEFAULT NULL")

        con.execute("""
            CREATE TABLE IF NOT EXISTS platform_config (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.commit()

    # ── Platform Config ───────────────────────────────────────────────────────

    def get_platform_config(self, key: str) -> str:
        con = self._conn()
        row = con.execute("SELECT value FROM platform_config WHERE key=?", (key,)).fetchone()
        con.close()
        return row[0] if row else ""

    def set_platform_config(self, key: str, value: str):
        con = self._conn()
        con.execute(
            "INSERT INTO platform_config (key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, int(time.time()))
        )
        con.commit()
        con.close()

    def log_activity(self, user_id: str, role: str, aksi: str, detail: str = "", ip: str = ""):
        import time as _time
        con = self._conn()
        con.execute(
            "INSERT INTO activity_log (user_id, role, aksi, detail, ip, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, role, aksi, detail, ip, int(_time.time()))
        )
        con.commit()
        con.close()

    def get_activity_log(self, user_id: str = None, limit: int = 100) -> list[dict]:
        con = self._conn()
        if user_id:
            rows = con.execute(
                """SELECT a.*, u.nama, u.username, u.role as user_role
                   FROM activity_log a
                   LEFT JOIN users u ON u.id = a.user_id
                   WHERE a.user_id=? OR u.parent_id=?
                   ORDER BY a.created_at DESC LIMIT ?""",
                (user_id, user_id, limit)
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT a.*, u.nama, u.username, u.role as user_role
                   FROM activity_log a
                   LEFT JOIN users u ON u.id = a.user_id
                   ORDER BY a.created_at DESC LIMIT ?""",
                (limit,)
            ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def list_teknisi(self, parent_id: str) -> list[dict]:
        con = self._conn()
        rows = con.execute(
            "SELECT * FROM users WHERE role='teknisi' AND parent_id=? ORDER BY nama",
            (parent_id,)
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def update_online_cache(self, server_id: str, usernames: list[str]):
        con = self._conn()
        now = int(__import__("time").time())
        con.execute("DELETE FROM pppoe_active_cache WHERE server_id=?", (server_id,))
        for u in usernames:
            con.execute(
                "INSERT OR REPLACE INTO pppoe_active_cache (server_id, username, synced_at) VALUES (?,?,?)",
                (server_id, u, now)
            )
        con.commit()
        con.close()

    def get_online_usernames(self, server_id: str) -> set:
        con = self._conn()
        rows = con.execute(
            "SELECT username FROM pppoe_active_cache WHERE server_id=?", (server_id,)
        ).fetchall()
        con.close()
        return {r[0] for r in rows}

    def get_all_online_usernames(self) -> set:
        con = self._conn()
        rows = con.execute("SELECT username FROM pppoe_active_cache").fetchall()
        con.close()
        return {r[0] for r in rows}

    def get_online_cache_age(self, server_id: str) -> int | None:
        """Return seconds since last sync, or None if never synced."""
        con = self._conn()
        row = con.execute(
            "SELECT MAX(synced_at) FROM pppoe_active_cache WHERE server_id=?", (server_id,)
        ).fetchone()
        con.close()
        if row and row[0]:
            return int(__import__("time").time()) - row[0]
        return None

    def set_mt_pushed(self, pid: int, pushed: int = 1):
        con = self._conn()
        con.execute("UPDATE pppoe_users SET mt_pushed=? WHERE id=?", (pushed, pid))
        con.commit()
        con.close()

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
            slug        TEXT DEFAULT '',
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

        -- Topup saldo agen via QRIS
        CREATE TABLE IF NOT EXISTS topup_orders (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            amount      INTEGER NOT NULL,
            snap_token  TEXT DEFAULT '',
            status      TEXT DEFAULT 'pending',  -- pending | paid | expired
            paid_at     INTEGER,
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

        -- Order toko hotspot online (publik)
        CREATE TABLE IF NOT EXISTS hotspot_orders (
            id          TEXT PRIMARY KEY,    -- order_id unik
            user_id     TEXT NOT NULL,       -- tenant ISP pemilik
            paket_id    INTEGER NOT NULL,
            server_id   TEXT NOT NULL,
            voucher_id  INTEGER,             -- voucher yang di-assign setelah bayar
            nomor_hp    TEXT DEFAULT '',
            jumlah      INTEGER DEFAULT 1,
            amount      INTEGER NOT NULL,
            status      TEXT DEFAULT 'pending',  -- pending | paid | expired | failed
            snap_token  TEXT DEFAULT '',
            paid_at     INTEGER,
            created_at  INTEGER
        );

        -- Tagihan PPPoE bulanan
        CREATE TABLE IF NOT EXISTS tagihan_pppoe (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            pppoe_id    INTEGER NOT NULL,
            bulan       TEXT NOT NULL,      -- format: "2026-05"
            amount      INTEGER NOT NULL,
            status      TEXT DEFAULT 'unpaid',  -- unpaid | paid | overdue
            snap_token  TEXT DEFAULT '',
            order_id    TEXT DEFAULT '',
            paid_at     INTEGER,
            created_at  INTEGER,
            UNIQUE(pppoe_id, bulan)
        );

        -- Template notifikasi WA per ISP
        CREATE TABLE IF NOT EXISTS wa_templates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            jenis       TEXT NOT NULL,   -- penagihan | tagihan_link | pembayaran | aktivasi | reaktivasi | suspend
            isi         TEXT NOT NULL DEFAULT '',
            updated_at  INTEGER,
            UNIQUE(user_id, jenis)
        );

        -- WA Gateway per ISP
        CREATE TABLE IF NOT EXISTS wa_gateway (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL UNIQUE,
            wa_token    TEXT DEFAULT '',
            nomor       TEXT DEFAULT '',
            status      TEXT DEFAULT 'disconnected',
            updated_at  INTEGER
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
        self._migrate(con)
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
            "INSERT INTO users (id,nama,username,password,role,parent_id,nomor_wa,slug,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, nama, username, _hash(password), role, parent_id, nomor_wa, username, int(time.time()))
        )
        con.commit()
        con.close()
        return uid

    def get_user_by_slug(self, slug: str) -> dict | None:
        con = self._conn()
        row = con.execute(
            "SELECT * FROM users WHERE (slug=? OR username=?) AND status='aktif'", (slug, slug)
        ).fetchone()
        con.close()
        return dict(row) if row else None

    def slug_exists(self, slug: str, exclude_uid: str = "") -> bool:
        con = self._conn()
        row = con.execute(
            "SELECT id FROM users WHERE (slug=? OR username=?) AND id!=?", (slug, slug, exclude_uid)
        ).fetchone()
        con.close()
        return row is not None

    def update_slug(self, uid: str, slug: str):
        con = self._conn()
        con.execute("UPDATE users SET slug=? WHERE id=?", (slug, uid))
        con.commit()
        con.close()

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

    def update_user(self, uid: str, nama: str, nomor_wa: str):
        con = self._conn()
        con.execute("UPDATE users SET nama=?, nomor_wa=? WHERE id=?", (nama, nomor_wa, uid))
        con.commit()
        con.close()

    def update_profil(self, uid: str, nama: str, nomor_wa: str, password_hash: str | None = None):
        con = self._conn()
        if password_hash:
            con.execute("UPDATE users SET nama=?, nomor_wa=?, password=? WHERE id=?",
                        (nama, nomor_wa, password_hash, uid))
        else:
            con.execute("UPDATE users SET nama=?, nomor_wa=? WHERE id=?", (nama, nomor_wa, uid))
        con.commit()
        con.close()

    def set_komisi(self, uid: str, persen: int):
        persen = max(0, min(100, persen))
        con = self._conn()
        con.execute("UPDATE users SET komisi_persen=? WHERE id=?", (persen, uid))
        con.commit()
        con.close()

    def save_rekening(self, uid: str, bank: str, no: str, nama: str):
        con = self._conn()
        con.execute("UPDATE users SET rek_bank=?, rek_no=?, rek_nama=? WHERE id=?", (bank, no, nama, uid))
        con.commit()
        con.close()

    def update_user_field(self, uid: str, field: str, value):
        allowed = {"qris_image", "nomor_wa", "nama", "slug"}
        if field not in allowed:
            return
        con = self._conn()
        con.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, uid))
        con.commit()
        con.close()

    def reset_password(self, uid: str, password: str):
        con = self._conn()
        con.execute("UPDATE users SET password=? WHERE id=?", (_hash(password), uid))
        con.commit()
        con.close()

    def delete_user(self, uid: str):
        con = self._conn()
        con.execute("DELETE FROM users WHERE id=?", (uid,))
        con.commit()
        con.close()

    # ── Superadmin ────────────────────────────────────────────────────────────

    def list_tenants(self) -> list[dict]:
        con = self._conn()
        rows = con.execute(
            "SELECT * FROM users WHERE role='admin' ORDER BY nama"
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_tenant_stats(self, uid: str) -> dict:
        con = self._conn()
        pppoe   = con.execute("SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND status='aktif'", (uid,)).fetchone()[0]
        voucher = con.execute("SELECT COUNT(*) FROM voucher_hotspot WHERE user_id=? AND status='tersedia'", (uid,)).fetchone()[0]
        transaksi = con.execute("SELECT COUNT(*) FROM transaksi WHERE user_id=?", (uid,)).fetchone()[0]
        omzet   = con.execute("SELECT COALESCE(SUM(amount),0) FROM transaksi WHERE user_id=? AND status='paid'", (uid,)).fetchone()[0]
        servers = con.execute("SELECT COUNT(*) FROM mikrotik_servers WHERE user_id=?", (uid,)).fetchone()[0]
        agen    = con.execute("SELECT COUNT(*) FROM users WHERE parent_id=? AND role='agen'", (uid,)).fetchone()[0]
        con.close()
        return {
            "pppoe": pppoe, "voucher": voucher,
            "transaksi": transaksi, "omzet": omzet,
            "servers": servers, "agen": agen,
        }

    def adjust_saldo_admin(self, uid: str, amount: int, catatan: str) -> dict | None:
        """Tambah atau kurangi saldo tenant. amount bisa negatif."""
        con = self._conn()
        row = con.execute("SELECT saldo FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            con.close()
            return None
        saldo_before = int(row["saldo"])
        saldo_after = max(0, saldo_before + amount)
        con.execute("UPDATE users SET saldo=? WHERE id=?", (saldo_after, uid))
        con.execute(
            "INSERT INTO saldo_adjustments (user_id, amount, saldo_before, saldo_after, catatan, by_sa, created_at) VALUES (?,?,?,?,?,1,?)",
            (uid, amount, saldo_before, saldo_after, catatan, int(time.time()))
        )
        con.commit()
        con.close()
        return {"saldo_before": saldo_before, "saldo_after": saldo_after}

    def list_saldo_adjustments(self, uid: str = "", limit: int = 50) -> list[dict]:
        con = self._conn()
        if uid:
            rows = con.execute(
                "SELECT sa.*, u.nama, u.username FROM saldo_adjustments sa LEFT JOIN users u ON u.id=sa.user_id WHERE sa.user_id=? ORDER BY sa.created_at DESC LIMIT ?",
                (uid, limit)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT sa.*, u.nama, u.username FROM saldo_adjustments sa LEFT JOIN users u ON u.id=sa.user_id ORDER BY sa.created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def stats_agen(self, uid: str) -> dict:
        con = self._conn()
        pppoe   = con.execute("SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND status='aktif'", (uid,)).fetchone()[0]
        voucher = con.execute("SELECT COUNT(*) FROM voucher_hotspot WHERE user_id=? AND status='tersedia'", (uid,)).fetchone()[0]
        omzet   = con.execute("SELECT COALESCE(SUM(amount),0) FROM transaksi WHERE user_id=?", (uid,)).fetchone()[0]
        servers = con.execute("SELECT COUNT(*) FROM mikrotik_servers WHERE user_id=?", (uid,)).fetchone()[0]
        con.close()
        return {"pppoe": pppoe, "voucher": voucher, "omzet": omzet, "servers": servers}

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

    def update_server(self, sid: str, nama: str, vpn_ip: str, api_port: int,
                      api_user: str, api_password: str, lokasi: str):
        con = self._conn()
        if api_password:
            con.execute(
                "UPDATE mikrotik_servers SET nama=?,vpn_ip=?,api_port=?,api_user=?,api_password=?,lokasi=? WHERE id=?",
                (nama, vpn_ip, api_port, api_user, api_password, lokasi, sid)
            )
        else:
            con.execute(
                "UPDATE mikrotik_servers SET nama=?,vpn_ip=?,api_port=?,api_user=?,lokasi=? WHERE id=?",
                (nama, vpn_ip, api_port, api_user, lokasi, sid)
            )
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

    def create_pppoe_user(self, user_id, server_id, nama_pelanggan, username, password, paket_id, telepon="", alamat="", tgl_bayar=1, mt_pushed=0) -> int:
        con = self._conn()
        cur = con.execute(
            "INSERT INTO pppoe_users (user_id,server_id,nama_pelanggan,username,password,paket_id,telepon,alamat,tgl_bayar,mt_pushed,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, server_id, nama_pelanggan, username, password, paket_id, telepon, alamat, tgl_bayar, mt_pushed, int(time.time()))
        )
        con.commit()
        pid = cur.lastrowid
        con.close()
        return pid

    def list_pppoe_users(self, user_id: str, server_id: str = None, odp_id: int = None) -> list[dict]:
        con = self._conn()
        base = ("SELECT p.*, pk.nama as paket_nama, pk.kecepatan, "
                "s.nama as server_nama, o.nama as odp_nama "
                "FROM pppoe_users p "
                "LEFT JOIN paket_pppoe pk ON pk.id=p.paket_id "
                "LEFT JOIN mikrotik_servers s ON s.id=p.server_id "
                "LEFT JOIN odp o ON o.id=p.odp_id "
                "WHERE p.user_id=?")
        args: list = [user_id]
        if server_id:
            base += " AND p.server_id=?"
            args.append(server_id)
        if odp_id is not None:
            base += " AND p.odp_id=?"
            args.append(odp_id)
        base += " ORDER BY p.nama_pelanggan"
        rows = con.execute(base, args).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_pppoe_user(self, pid: int) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM pppoe_users WHERE id=?", (pid,)).fetchone()
        con.close()
        return dict(row) if row else None

    def update_pppoe_user(self, pid: int, nama_pelanggan: str, telepon: str, alamat: str, tgl_bayar: int,
                          username: str = None, password: str = None):
        con = self._conn()
        if username and password:
            con.execute(
                "UPDATE pppoe_users SET nama_pelanggan=?, telepon=?, alamat=?, tgl_bayar=?, username=?, password=? WHERE id=?",
                (nama_pelanggan, telepon, alamat, tgl_bayar, username, password, pid)
            )
        else:
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

    # ── ODP ───────────────────────────────────────────────────────────────────

    def list_odp(self, user_id: str, server_id: str = None) -> list[dict]:
        con = self._conn()
        q = ("SELECT o.*, s.nama as server_nama, COUNT(p.id) as terpakai, "
             "c.nama as odc_nama, c.tipe_splitter as odc_tipe, c.daya_masuk_dbm as odc_daya "
             "FROM odp o "
             "LEFT JOIN mikrotik_servers s ON s.id=o.server_id "
             "LEFT JOIN pppoe_users p ON p.odp_id=o.id "
             "LEFT JOIN odc c ON c.id=o.odc_id "
             "WHERE o.user_id=?")
        args: list = [user_id]
        if server_id:
            q += " AND o.server_id=?"
            args.append(server_id)
        q += " GROUP BY o.id ORDER BY o.server_id, o.odc_id, o.nama"
        rows = con.execute(q, args).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_odp(self, odp_id: int) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM odp WHERE id=?", (odp_id,)).fetchone()
        con.close()
        return dict(row) if row else None

    # ── ODC ───────────────────────────────────────────────────────────────────

    # Tabel daya loss per tap/splitter tipe (dalam dB, negatif = loss)
    TAP_LOSS = {
        "20/80": (-7.0, -1.0),   # (loss ke ODP, loss lanjut ke kabel)
        "30/70": (-5.2, -1.5),
        "50/50": (-3.0, -3.0),
        "10/90": (-10.0, -0.5),
        "1:4":   (-6.0, None),
        "1:8":   (-9.0, None),
        "1:16":  (-12.0, None),
        "1:32":  (-15.0, None),
    }
    SPLITTER_LOSS = {"1:4": -6.0, "1:8": -9.0, "1:16": -12.0, "1:32": -15.0}

    def list_odc(self, user_id: str, server_id: str = None) -> list[dict]:
        con = self._conn()
        q = ("SELECT o.*, s.nama as server_nama, COUNT(p.id) as jumlah_odp "
             "FROM odc o "
             "LEFT JOIN mikrotik_servers s ON s.id=o.server_id "
             "LEFT JOIN odp p ON p.odc_id=o.id "
             "WHERE o.user_id=?")
        params = [user_id]
        if server_id:
            q += " AND o.server_id=?"; params.append(server_id)
        q += " GROUP BY o.id ORDER BY o.nama"
        rows = con.execute(q, params).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_odc(self, odc_id: int) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM odc WHERE id=?", (odc_id,)).fetchone()
        con.close()
        return dict(row) if row else None

    def create_odc(self, user_id: str, server_id: str, nama: str, lokasi: str,
                   tipe_splitter: str, daya_masuk_dbm: float,
                   lat: float = None, lng: float = None) -> int:
        con = self._conn()
        cur = con.execute(
            "INSERT INTO odc (user_id,server_id,nama,lokasi,tipe_splitter,daya_masuk_dbm,lat,lng,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, server_id, nama, lokasi, tipe_splitter, daya_masuk_dbm, lat, lng, int(time.time()))
        )
        con.commit()
        oid = cur.lastrowid
        con.close()
        return oid

    def update_odc(self, odc_id: int, server_id: str, nama: str, lokasi: str,
                   tipe_splitter: str, daya_masuk_dbm: float,
                   lat: float = None, lng: float = None):
        con = self._conn()
        con.execute(
            "UPDATE odc SET server_id=?,nama=?,lokasi=?,tipe_splitter=?,daya_masuk_dbm=?,lat=?,lng=? WHERE id=?",
            (server_id, nama, lokasi, tipe_splitter, daya_masuk_dbm, lat, lng, odc_id)
        )
        con.commit()
        con.close()

    def delete_odc(self, odc_id: int):
        con = self._conn()
        con.execute("UPDATE odp SET odc_id=NULL, tap_tipe='' WHERE odc_id=?", (odc_id,))
        con.execute("DELETE FROM odc WHERE id=?", (odc_id,))
        con.commit()
        con.close()

    def def_odp_power(self, odc: dict, tap_tipe: str) -> float | None:
        """Hitung estimasi daya di ODP berdasarkan ODC input dan tipe tap."""
        masuk = odc.get("daya_masuk_dbm")
        if masuk is None:
            return None
        loss_odp, _ = self.TAP_LOSS.get(tap_tipe, (-9.0, None))
        # Loss splitter ODC ke cabang
        spl = self.SPLITTER_LOSS.get(odc.get("tipe_splitter", ""), 0)
        return round(masuk + spl + loss_odp, 1)

    # ── ODP ───────────────────────────────────────────────────────────────────

    def create_odp(self, user_id: str, server_id: str, nama: str, lokasi: str, kapasitas: int,
                   lat: float = None, lng: float = None,
                   odc_id: int = None, tap_tipe: str = "") -> int:
        con = self._conn()
        cur = con.execute(
            "INSERT INTO odp (user_id,server_id,nama,lokasi,kapasitas,lat,lng,odc_id,tap_tipe,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, server_id, nama, lokasi, kapasitas, lat, lng, odc_id, tap_tipe, int(time.time()))
        )
        con.commit()
        oid = cur.lastrowid
        con.close()
        return oid

    def update_odp(self, odp_id: int, server_id: str, nama: str, lokasi: str, kapasitas: int,
                   lat: float = None, lng: float = None,
                   odc_id: int = None, tap_tipe: str = ""):
        con = self._conn()
        con.execute(
            "UPDATE odp SET server_id=?,nama=?,lokasi=?,kapasitas=?,lat=?,lng=?,odc_id=?,tap_tipe=? WHERE id=?",
            (server_id, nama, lokasi, kapasitas, lat, lng, odc_id, tap_tipe, odp_id)
        )
        con.commit()
        con.close()

    def delete_odp(self, odp_id: int):
        con = self._conn()
        con.execute("UPDATE pppoe_users SET odp_id=NULL, odp_port=NULL WHERE odp_id=?", (odp_id,))
        con.execute("DELETE FROM odp WHERE id=?", (odp_id,))
        con.commit()
        con.close()

    def assign_odp(self, pppoe_id: int, odp_id, odp_port):
        con = self._conn()
        con.execute("UPDATE pppoe_users SET odp_id=?, odp_port=? WHERE id=?", (odp_id, odp_port, pppoe_id))
        con.commit()
        con.close()

    def list_pppoe_by_odp(self, user_id: str, odp_id: int) -> list[dict]:
        con = self._conn()
        rows = con.execute(
            "SELECT p.*, pk.nama as paket_nama, pk.kecepatan FROM pppoe_users p "
            "LEFT JOIN paket_pppoe pk ON pk.id=p.paket_id "
            "WHERE p.user_id=? AND p.odp_id=? ORDER BY p.odp_port",
            (user_id, odp_id)
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    # ── Paket Hotspot ─────────────────────────────────────────────────────────

    def create_paket_hotspot(self, user_id, nama, durasi, kecepatan, harga, server_id="") -> int:
        con = self._conn()
        cur = con.execute(
            "INSERT INTO paket_hotspot (user_id,nama,durasi,kecepatan,harga,server_id,created_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, nama, durasi, kecepatan, harga, server_id, int(time.time()))
        )
        con.commit()
        pid = cur.lastrowid
        con.close()
        return pid

    def update_paket_hotspot(self, pid: int, user_id: str, nama: str, durasi: str, kecepatan: str, harga: int, server_id: str = ""):
        con = self._conn()
        con.execute(
            "UPDATE paket_hotspot SET nama=?,durasi=?,kecepatan=?,harga=?,server_id=? WHERE id=? AND user_id=?",
            (nama, durasi, kecepatan, harga, server_id, pid, user_id)
        )
        con.commit()
        con.close()

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

    def create_vouchers(self, user_id: str, server_id: str, paket_id: int, jumlah: int, comment: str = "") -> list[str]:
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
                "INSERT INTO voucher_hotspot (user_id,server_id,paket_id,kode,comment,created_at) VALUES (?,?,?,?,?,?)",
                (user_id, server_id, paket_id, kode, comment, int(time.time()))
            )
            kodes.append(kode)
        con.commit()
        con.close()
        return kodes

    def set_voucher_mt_pushed(self, kode: str, pushed: bool = True):
        con = self._conn()
        con.execute("UPDATE voucher_hotspot SET mt_pushed=? WHERE kode=?", (1 if pushed else 0, kode))
        con.commit()
        con.close()

    def set_vouchers_mt_pushed(self, kodes: list[str], pushed: bool = True):
        """Bulk update mt_pushed untuk list kode."""
        if not kodes:
            return
        con = self._conn()
        val = 1 if pushed else 0
        con.executemany("UPDATE voucher_hotspot SET mt_pushed=? WHERE kode=?",
                        [(val, k) for k in kodes])
        con.commit()
        con.close()

    def list_vouchers(self, user_id: str, server_id: str = None, status: str = None, paket_id: str = None, comment: str = None) -> list[dict]:
        con = self._conn()
        q = """SELECT v.*, p.nama as paket_nama, p.durasi, p.kecepatan,
                      s.nama as server_nama, u.nama as agen_nama
               FROM voucher_hotspot v
               LEFT JOIN paket_hotspot p ON p.id=v.paket_id
               LEFT JOIN mikrotik_servers s ON s.id=v.server_id
               LEFT JOIN users u ON u.id=v.agen_id
               WHERE v.user_id=?"""
        args = [user_id]
        if server_id:
            q += " AND v.server_id=?"
            args.append(server_id)
        if status:
            q += " AND v.status=?"
            args.append(status)
        if paket_id:
            q += " AND v.paket_id=?"
            args.append(paket_id)
        if comment is not None:
            q += " AND v.comment=?"
            args.append(comment)
        q += " ORDER BY v.created_at DESC"
        rows = con.execute(q, args).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def list_voucher_comments_with_agen(self, user_id: str) -> list[dict]:
        """List comment unik beserta nama agen, nama paket dominan, dan jumlah tersedia (untuk panel ISP)."""
        con = self._conn()
        rows = con.execute(
            "SELECT v.comment, u.nama as agen_nama, v.agen_id, "
            "COUNT(CASE WHEN v.status='tersedia' THEN 1 END) as stok, "
            "(SELECT p.nama FROM paket_hotspot p "
            " WHERE p.id = (SELECT v2.paket_id FROM voucher_hotspot v2 "
            "               WHERE v2.user_id=v.user_id AND v2.comment=v.comment AND v2.agen_id=v.agen_id "
            "               GROUP BY v2.paket_id ORDER BY COUNT(*) DESC LIMIT 1) LIMIT 1) as paket_nama "
            "FROM voucher_hotspot v LEFT JOIN users u ON u.id=v.agen_id "
            "WHERE v.user_id=? AND v.comment!='' "
            "GROUP BY v.comment, v.agen_id ORDER BY v.comment",
            (user_id,)
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def list_voucher_comments(self, user_id: str, agen_id: str = "") -> list[str]:
        """Daftar comment unik. Jika agen_id diisi, hanya comment milik agen tersebut."""
        con = self._conn()
        if agen_id:
            rows = con.execute(
                "SELECT DISTINCT comment FROM voucher_hotspot "
                "WHERE user_id=? AND agen_id=? AND comment!='' ORDER BY comment",
                (user_id, agen_id)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT DISTINCT comment FROM voucher_hotspot WHERE user_id=? AND comment!='' ORDER BY comment",
                (user_id,)
            ).fetchall()
        con.close()
        return [r[0] for r in rows]

    def list_voucher_comments_detail(self, user_id: str, agen_id: str = "") -> list[dict]:
        """Daftar comment dengan paket_nama dominan dan stok tersedia. Untuk panel agen/ISP."""
        con = self._conn()
        if agen_id:
            rows = con.execute(
                "SELECT v.comment, "
                "COUNT(CASE WHEN v.status='tersedia' THEN 1 END) as stok, "
                "(SELECT p.nama FROM paket_hotspot p WHERE p.id = ("
                " SELECT v2.paket_id FROM voucher_hotspot v2 "
                " WHERE v2.user_id=v.user_id AND v2.comment=v.comment AND v2.agen_id=v.agen_id "
                " GROUP BY v2.paket_id ORDER BY COUNT(*) DESC LIMIT 1) LIMIT 1) as paket_nama "
                "FROM voucher_hotspot v "
                "WHERE v.user_id=? AND v.agen_id=? AND v.comment!='' "
                "GROUP BY v.comment ORDER BY v.comment",
                (user_id, agen_id)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT v.comment, "
                "COUNT(CASE WHEN v.status='tersedia' THEN 1 END) as stok, "
                "(SELECT p.nama FROM paket_hotspot p WHERE p.id = ("
                " SELECT v2.paket_id FROM voucher_hotspot v2 "
                " WHERE v2.user_id=v.user_id AND v2.comment=v.comment "
                " GROUP BY v2.paket_id ORDER BY COUNT(*) DESC LIMIT 1) LIMIT 1) as paket_nama "
                "FROM voucher_hotspot v "
                "WHERE v.user_id=? AND v.comment!='' "
                "GROUP BY v.comment ORDER BY v.comment",
                (user_id,)
            ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def count_vouchers_by_comment(self, user_id: str, agen_id: str = "") -> dict:
        """Jumlah voucher tersedia per comment. Jika agen_id diisi, filter per agen."""
        con = self._conn()
        if agen_id:
            rows = con.execute(
                "SELECT comment, COUNT(*) FROM voucher_hotspot "
                "WHERE user_id=? AND agen_id=? AND comment!='' AND status='tersedia' "
                "GROUP BY comment",
                (user_id, agen_id)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT comment, COUNT(*) FROM voucher_hotspot "
                "WHERE user_id=? AND comment!='' AND status='tersedia' "
                "GROUP BY comment",
                (user_id,)
            ).fetchall()
        con.close()
        return {r[0]: r[1] for r in rows}

    def delete_vouchers(self, user_id: str, server_id: str, status: str = "tersedia"):
        con = self._conn()
        con.execute("DELETE FROM voucher_hotspot WHERE user_id=? AND server_id=? AND status=?", (user_id, server_id, status))
        con.commit()
        con.close()

    def delete_vouchers_by_comment(self, user_id: str, comment: str, status: str = "tersedia"):
        con = self._conn()
        con.execute("DELETE FROM voucher_hotspot WHERE user_id=? AND comment=? AND status=?", (user_id, comment, status))
        con.commit()
        con.close()

    def delete_voucher_kode(self, user_id: str, kode: str) -> bool:
        con = self._conn()
        cur = con.execute("DELETE FROM voucher_hotspot WHERE user_id=? AND kode=? AND status='tersedia'", (user_id, kode))
        con.commit()
        con.close()
        return cur.rowcount > 0

    def get_voucher_by_kode(self, user_id: str, kode: str) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM voucher_hotspot WHERE user_id=? AND kode=?", (user_id, kode)).fetchone()
        con.close()
        return dict(row) if row else None

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

    def _ensure_tarik_saldo_table(self, con):
        con.execute("""
            CREATE TABLE IF NOT EXISTS tarik_saldo (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL,
                isp_id       TEXT NOT NULL,
                nominal      INTEGER NOT NULL,
                bank_name    TEXT DEFAULT '',
                rekening_no  TEXT DEFAULT '',
                rekening_name TEXT DEFAULT '',
                catatan      TEXT DEFAULT '',
                status       TEXT DEFAULT 'pending',  -- pending | approved | rejected
                admin_note   TEXT DEFAULT '',
                created_at   INTEGER,
                processed_at INTEGER
            )
        """)
        con.commit()

    def buat_tarik_saldo(self, user_id: str, isp_id: str, nominal: int,
                         bank_name: str, rekening_no: str, rekening_name: str,
                         catatan: str = "") -> int:
        con = self._conn()
        self._ensure_tarik_saldo_table(con)
        cur = con.execute(
            "INSERT INTO tarik_saldo (user_id,isp_id,nominal,bank_name,rekening_no,rekening_name,catatan,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, isp_id, nominal, bank_name, rekening_no, rekening_name, catatan, int(time.time()))
        )
        con.commit()
        rid = cur.lastrowid
        con.close()
        return rid

    def list_tarik_saldo(self, user_id: str, as_agen: bool = True) -> list[dict]:
        """List tarik saldo. as_agen=True filter by user_id, False filter by isp_id (admin view)."""
        con = self._conn()
        self._ensure_tarik_saldo_table(con)
        if as_agen:
            rows = con.execute(
                "SELECT t.*, u.nama as agen_nama, u.nomor_wa as agen_wa "
                "FROM tarik_saldo t JOIN users u ON u.id=t.user_id "
                "WHERE t.user_id=? ORDER BY t.created_at DESC", (user_id,)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT t.*, u.nama as agen_nama, u.nomor_wa as agen_wa "
                "FROM tarik_saldo t JOIN users u ON u.id=t.user_id "
                "WHERE t.isp_id=? ORDER BY t.created_at DESC", (user_id,)
            ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_tarik_saldo(self, rid: int) -> dict | None:
        con = self._conn()
        self._ensure_tarik_saldo_table(con)
        row = con.execute(
            "SELECT t.*, u.nama as agen_nama, u.nomor_wa as agen_wa, u.saldo as agen_saldo "
            "FROM tarik_saldo t JOIN users u ON u.id=t.user_id WHERE t.id=?", (rid,)
        ).fetchone()
        con.close()
        return dict(row) if row else None

    def approve_tarik_saldo(self, rid: int, isp_id: str, admin_note: str = "") -> dict | None:
        con = self._conn()
        self._ensure_tarik_saldo_table(con)
        row = con.execute(
            "SELECT * FROM tarik_saldo WHERE id=? AND isp_id=? AND status='pending'", (rid, isp_id)
        ).fetchone()
        if not row:
            con.close()
            return None
        row = dict(row)
        now = int(time.time())
        # Deduct saldo agen
        con.execute("UPDATE users SET saldo=MAX(0, saldo-?) WHERE id=?", (row["nominal"], row["user_id"]))
        con.execute("UPDATE tarik_saldo SET status='approved', admin_note=?, processed_at=? WHERE id=?",
                    (admin_note, now, rid))
        con.execute(
            "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,created_at) VALUES (?,?,?,?,?)",
            (row["user_id"], row["nominal"], "debit",
             f"Tarik saldo disetujui — {row['bank_name']} {row['rekening_no']}", now)
        )
        con.commit()
        con.close()
        return row

    def reject_tarik_saldo(self, rid: int, isp_id: str, admin_note: str = "") -> dict | None:
        con = self._conn()
        self._ensure_tarik_saldo_table(con)
        row = con.execute(
            "SELECT * FROM tarik_saldo WHERE id=? AND isp_id=? AND status='pending'", (rid, isp_id)
        ).fetchone()
        if not row:
            con.close()
            return None
        row = dict(row)
        now = int(time.time())
        con.execute("UPDATE tarik_saldo SET status='rejected', admin_note=?, processed_at=? WHERE id=?",
                    (admin_note, now, rid))
        con.commit()
        con.close()
        return row

    def list_tarik_saldo_all(self) -> list[dict]:
        """SA: list semua tarik saldo dari semua tenant ISP."""
        con = self._conn()
        self._ensure_tarik_saldo_table(con)
        rows = con.execute(
            "SELECT t.*, u.nama as isp_nama, u.nomor_wa as isp_wa "
            "FROM tarik_saldo t JOIN users u ON u.id=t.user_id "
            "ORDER BY t.created_at DESC LIMIT 200"
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    TARIK_FEE = 5_000

    def approve_tarik_saldo_sa(self, rid: int, admin_note: str = "") -> dict | None:
        """SA approve tarik saldo ISP: potong nominal + biaya admin Rp 5.000."""
        con = self._conn()
        self._ensure_tarik_saldo_table(con)
        row = con.execute(
            "SELECT * FROM tarik_saldo WHERE id=? AND status='pending'", (rid,)
        ).fetchone()
        if not row:
            con.close()
            return None
        row = dict(row)
        now = int(time.time())
        total_potong = row["nominal"] + self.TARIK_FEE
        con.execute("UPDATE users SET saldo=MAX(0, saldo-?) WHERE id=?", (total_potong, row["user_id"]))
        con.execute("UPDATE tarik_saldo SET status='approved', admin_note=?, processed_at=? WHERE id=?",
                    (admin_note, now, rid))
        con.execute(
            "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,created_at) VALUES (?,?,?,?,?)",
            (row["user_id"], row["nominal"], "debit",
             f"Tarik saldo disetujui platform — {row['bank_name']} {row['rekening_no']}", now)
        )
        con.execute(
            "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,created_at) VALUES (?,?,?,?,?)",
            (row["user_id"], self.TARIK_FEE, "debit", f"Biaya admin tarik saldo #{rid}", now)
        )
        con.commit()
        con.close()
        return row

    def reject_tarik_saldo_sa(self, rid: int, admin_note: str = "") -> dict | None:
        """SA reject tarik saldo ISP tanpa cek isp_id."""
        con = self._conn()
        self._ensure_tarik_saldo_table(con)
        row = con.execute(
            "SELECT * FROM tarik_saldo WHERE id=? AND status='pending'", (rid,)
        ).fetchone()
        if not row:
            con.close()
            return None
        row = dict(row)
        now = int(time.time())
        con.execute("UPDATE tarik_saldo SET status='rejected', admin_note=?, processed_at=? WHERE id=?",
                    (admin_note, now, rid))
        con.commit()
        con.close()
        return row

    def count_tarik_pending(self, isp_id: str) -> int:
        con = self._conn()
        self._ensure_tarik_saldo_table(con)
        n = con.execute("SELECT COUNT(*) FROM tarik_saldo WHERE isp_id=? AND status='pending'",
                        (isp_id,)).fetchone()[0]
        con.close()
        return n

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

    # ── Topup Order ───────────────────────────────────────────────────────────

    def create_topup_order(self, user_id: str, amount: int) -> str:
        oid = "TOP-" + uuid.uuid4().hex[:10].upper()
        con = self._conn()
        con.execute(
            "INSERT INTO topup_orders (id,user_id,amount,created_at) VALUES (?,?,?,?)",
            (oid, user_id, amount, int(time.time()))
        )
        con.commit()
        con.close()
        return oid

    def set_topup_snap_token(self, oid: str, token: str):
        con = self._conn()
        con.execute("UPDATE topup_orders SET snap_token=? WHERE id=?", (token, oid))
        con.commit()
        con.close()

    def get_topup_order(self, oid: str) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM topup_orders WHERE id=?", (oid,)).fetchone()
        con.close()
        return dict(row) if row else None

    def confirm_topup(self, oid: str):
        """Tandai topup paid dan tambah saldo user."""
        con = self._conn()
        order = con.execute("SELECT * FROM topup_orders WHERE id=? AND status='pending'", (oid,)).fetchone()
        if not order:
            con.close()
            return
        order = dict(order)
        con.execute("UPDATE topup_orders SET status='paid', paid_at=? WHERE id=?", (int(time.time()), oid))
        con.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (order["amount"], order["user_id"]))
        con.execute(
            "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,created_at) VALUES (?,?,?,?,?)",
            (order["user_id"], order["amount"], "kredit", f"Topup via QRIS #{oid}", int(time.time()))
        )
        con.commit()
        con.close()

    def list_topup_orders(self, user_id: str, limit: int = 20) -> list[dict]:
        con = self._conn()
        rows = con.execute(
            "SELECT * FROM topup_orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def create_topup_manual(self, user_id: str, isp_id: str, amount: int, catatan: str = "") -> str:
        """Buat topup manual (QRIS ISP), menunggu approval ISP."""
        oid = "MAN-" + uuid.uuid4().hex[:10].upper()
        con = self._conn()
        con.execute(
            "INSERT INTO topup_orders (id,user_id,isp_id,amount,tipe,catatan_agen,created_at) VALUES (?,?,?,?,?,?,?)",
            (oid, user_id, isp_id, amount, "manual", catatan, int(time.time()))
        )
        con.commit()
        con.close()
        return oid

    def list_topup_manual_isp(self, isp_id: str, status: str = "pending") -> list[dict]:
        """List permintaan topup manual untuk ISP beserta nama agen."""
        con = self._conn()
        rows = con.execute(
            "SELECT t.*, u.nama as agen_nama, u.nomor_wa as agen_wa "
            "FROM topup_orders t JOIN users u ON u.id=t.user_id "
            "WHERE t.isp_id=? AND t.tipe='manual' AND t.status=? "
            "ORDER BY t.created_at DESC",
            (isp_id, status)
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def count_topup_manual_pending(self, isp_id: str) -> int:
        con = self._conn()
        n = con.execute(
            "SELECT COUNT(*) FROM topup_orders WHERE isp_id=? AND tipe='manual' AND status='pending'",
            (isp_id,)
        ).fetchone()[0]
        con.close()
        return n

    def approve_topup_manual(self, oid: str, isp_id: str) -> dict | None:
        con = self._conn()
        order = con.execute(
            "SELECT * FROM topup_orders WHERE id=? AND isp_id=? AND tipe='manual' AND status='pending'",
            (oid, isp_id)
        ).fetchone()
        if not order:
            con.close()
            return None
        order = dict(order)
        con.execute("UPDATE topup_orders SET status='paid', paid_at=? WHERE id=?", (int(time.time()), oid))
        con.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (order["amount"], order["user_id"]))
        con.execute(
            "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,created_at) VALUES (?,?,?,?,?)",
            (order["user_id"], order["amount"], "kredit", f"Topup QRIS disetujui #{oid}", int(time.time()))
        )
        con.commit()
        con.close()
        return order

    def reject_topup_manual(self, oid: str, isp_id: str) -> dict | None:
        con = self._conn()
        order = con.execute(
            "SELECT * FROM topup_orders WHERE id=? AND isp_id=? AND tipe='manual' AND status='pending'",
            (oid, isp_id)
        ).fetchone()
        if not order:
            con.close()
            return None
        con.execute("UPDATE topup_orders SET status='rejected' WHERE id=?", (oid,))
        con.commit()
        con.close()
        return dict(order)

    def approve_topup_manual_sa(self, oid: str) -> dict | None:
        """SA approve topup manual: agen saldo + dan ISP saldo + (platform titip ke ISP)."""
        con = self._conn()
        order = con.execute(
            "SELECT * FROM topup_orders WHERE id=? AND tipe='manual' AND status='pending'",
            (oid,)
        ).fetchone()
        if not order:
            con.close()
            return None
        order = dict(order)
        now = int(time.time())
        con.execute("UPDATE topup_orders SET status='paid', paid_at=? WHERE id=?", (now, oid))
        # Agen saldo bertambah
        con.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (order["amount"], order["user_id"]))
        con.execute(
            "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,created_at) VALUES (?,?,?,?,?)",
            (order["user_id"], order["amount"], "kredit", f"Topup QRIS disetujui platform #{oid}", now)
        )
        # ISP saldo bertambah saat SA approve topup
        if order.get("isp_id"):
            con.execute("UPDATE users SET saldo=saldo+? WHERE id=?", (order["amount"], order["isp_id"]))
            con.execute(
                "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,created_at) VALUES (?,?,?,?,?)",
                (order["isp_id"], order["amount"], "kredit", f"Topup agen disetujui platform #{oid}", now)
            )
        con.commit()
        con.close()
        return order

    def reject_topup_manual_sa(self, oid: str) -> dict | None:
        """SA reject topup manual tanpa cek isp_id."""
        con = self._conn()
        order = con.execute(
            "SELECT * FROM topup_orders WHERE id=? AND tipe='manual' AND status='pending'",
            (oid,)
        ).fetchone()
        if not order:
            con.close()
            return None
        con.execute("UPDATE topup_orders SET status='rejected' WHERE id=?", (oid,))
        con.commit()
        con.close()
        return dict(order)

    # ── Generate Voucher Agen (debit saldo) ───────────────────────────────────

    def generate_voucher_agen(self, agen_uid: str, isp_uid: str, paket_id: int, jumlah: int, comment: str = "", server_id: str = "") -> dict:
        """Generate N voucher untuk agen, potong saldo, return kodes."""
        import random, string as _string
        con = self._conn()
        try:
            paket = con.execute(
                "SELECT * FROM paket_hotspot WHERE id=? AND user_id=? AND status='aktif'",
                (paket_id, isp_uid)
            ).fetchone()
            if not paket:
                return {"ok": False, "msg": "Paket tidak ditemukan"}
            paket = dict(paket)

            agen = con.execute("SELECT * FROM users WHERE id=?", (agen_uid,)).fetchone()
            if not agen:
                return {"ok": False, "msg": "Akun tidak valid"}
            agen = dict(agen)

            # Hitung harga setelah komisi
            komisi_persen = agen.get("komisi_persen") or 0
            harga_agen = int(paket["harga"] * (100 - komisi_persen) / 100)
            total = harga_agen * jumlah
            total_isp = total  # ISP terima nett setelah komisi

            if agen["saldo"] < total:
                return {
                    "ok": False,
                    "msg": f"Saldo tidak cukup. Butuh Rp {total:,}, saldo Rp {agen['saldo']:,}"
                }

            # Gunakan server yang dipilih agen; fallback ke server pertama ISP
            if server_id:
                srv = con.execute(
                    "SELECT * FROM mikrotik_servers WHERE id=? AND user_id=? AND status='aktif'",
                    (server_id, isp_uid)
                ).fetchone()
                if not srv:
                    server_id = ""
            if not server_id:
                srv = con.execute(
                    "SELECT * FROM mikrotik_servers WHERE user_id=? AND status='aktif' ORDER BY created_at LIMIT 1",
                    (isp_uid,)
                ).fetchone()
                server_id = dict(srv)["id"] if srv else ""

            # Generate kode unik
            kodes = []
            for _ in range(jumlah):
                while True:
                    kode = "".join(random.choices(_string.ascii_uppercase + _string.digits, k=8))
                    exists = con.execute("SELECT id FROM voucher_hotspot WHERE kode=?", (kode,)).fetchone()
                    if not exists:
                        break
                con.execute(
                    "INSERT INTO voucher_hotspot (user_id,server_id,paket_id,kode,comment,agen_id,created_at) VALUES (?,?,?,?,?,?,?)",
                    (isp_uid, server_id, paket_id, kode, comment, agen_uid, int(time.time()))
                )
                kodes.append(kode)

            # Debit saldo agen saja — ISP tidak dapat kredit saldo sistem
            # (ISP sudah terima uang fisik via QRIS manual topup)
            komisi_ket = f" (komisi {komisi_persen}%)" if komisi_persen else ""
            con.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (total, agen_uid))
            con.execute(
                "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,created_at) VALUES (?,?,?,?,?)",
                (agen_uid, total, "debit",
                 f"Generate {jumlah}x voucher {paket['nama']}{komisi_ket}", int(time.time()))
            )
            # Catat pendapatan agen ke tabel agen_income untuk laporan ISP
            con.execute(
                "INSERT OR IGNORE INTO agen_income (user_id,agen_id,paket_id,jumlah,total,komisi_persen,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (isp_uid, agen_uid, paket_id, jumlah, total_isp, komisi_persen, int(time.time()))
            )
            con.commit()
            return {"ok": True, "kodes": kodes, "paket": paket["nama"],
                    "server_id": server_id, "msg": "Berhasil"}
        except Exception as e:
            con.rollback()
            return {"ok": False, "msg": str(e)}
        finally:
            con.close()

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

    # ── WA Gateway ────────────────────────────────────────────────────────────

    def get_wa_gateway(self, user_id: str) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM wa_gateway WHERE user_id=?", (user_id,)).fetchone()
        con.close()
        return dict(row) if row else None

    def upsert_wa_gateway(self, user_id: str, wa_token: str):
        con = self._conn()
        con.execute("""
            INSERT INTO wa_gateway (user_id, wa_token, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET wa_token=excluded.wa_token, updated_at=excluded.updated_at
        """, (user_id, wa_token, int(time.time())))
        con.commit()
        con.close()

    def update_wa_gateway_status(self, user_id: str, status: str, nomor: str = ""):
        con = self._conn()
        con.execute(
            "UPDATE wa_gateway SET status=?, nomor=?, updated_at=? WHERE user_id=?",
            (status, nomor, int(time.time()), user_id)
        )
        con.commit()
        con.close()

    def list_wa_gateways_aktif(self) -> list[dict]:
        """Semua ISP yang WA gateway-nya terhubung (untuk scheduler reminder)."""
        con = self._conn()
        rows = con.execute(
            "SELECT g.*, u.nama as isp_nama FROM wa_gateway g "
            "LEFT JOIN users u ON u.id=g.user_id "
            "WHERE g.status='connected'"
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    # ── Tagihan PPPoE ─────────────────────────────────────────────────────────

    def generate_tagihan(self, user_id: str, bulan: str, tgl_bayar: int = None) -> int:
        """Buat tagihan pelanggan aktif untuk bulan tertentu.
        Jika tgl_bayar diisi, hanya buat untuk pelanggan dengan tgl_bayar tersebut."""
        con = self._conn()
        if tgl_bayar:
            pelanggan = con.execute(
                "SELECT p.id, p.paket_id, pk.harga FROM pppoe_users p "
                "LEFT JOIN paket_pppoe pk ON pk.id=p.paket_id "
                "WHERE p.user_id=? AND p.status='aktif' AND p.tgl_bayar=?",
                (user_id, tgl_bayar)
            ).fetchall()
        else:
            pelanggan = con.execute(
                "SELECT p.id, p.paket_id, pk.harga FROM pppoe_users p "
                "LEFT JOIN paket_pppoe pk ON pk.id=p.paket_id "
                "WHERE p.user_id=? AND p.status='aktif'", (user_id,)
            ).fetchall()
        now = int(time.time())
        dibuat = 0
        for p in pelanggan:
            harga = p["harga"] or 0
            try:
                con.execute(
                    "INSERT OR IGNORE INTO tagihan_pppoe (user_id,pppoe_id,bulan,amount,created_at) VALUES (?,?,?,?,?)",
                    (user_id, p["id"], bulan, harga, now)
                )
                if con.execute("SELECT changes()").fetchone()[0]:
                    dibuat += 1
            except Exception:
                pass
        con.commit()
        con.close()
        return dibuat

    def list_tagihan(self, user_id: str, bulan: str = None, status: str = None) -> list[dict]:
        con = self._conn()
        q = """SELECT t.*, p.nama_pelanggan, p.telepon, p.username as pppoe_username,
                      pk.nama as paket_nama, pk.kecepatan, s.nama as server_nama
               FROM tagihan_pppoe t
               LEFT JOIN pppoe_users p ON p.id=t.pppoe_id
               LEFT JOIN paket_pppoe pk ON pk.id=p.paket_id
               LEFT JOIN mikrotik_servers s ON s.id=p.server_id
               WHERE t.user_id=?"""
        args = [user_id]
        if bulan:
            q += " AND t.bulan=?"
            args.append(bulan)
        if status:
            q += " AND t.status=?"
            args.append(status)
        q += " ORDER BY p.nama_pelanggan"
        rows = con.execute(q, args).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_tagihan(self, tid: int) -> dict | None:
        con = self._conn()
        row = con.execute("""
            SELECT t.*, p.nama_pelanggan, p.telepon, p.username as pppoe_username,
                   pk.nama as paket_nama, pk.kecepatan, s.nama as server_nama, u.nama as isp_nama
            FROM tagihan_pppoe t
            LEFT JOIN pppoe_users p ON p.id=t.pppoe_id
            LEFT JOIN paket_pppoe pk ON pk.id=p.paket_id
            LEFT JOIN mikrotik_servers s ON s.id=p.server_id
            LEFT JOIN users u ON u.id=t.user_id
            WHERE t.id=?
        """, (tid,)).fetchone()
        con.close()
        return dict(row) if row else None

    def set_tagihan_snap_token(self, tid: int, snap_token: str, order_id: str):
        con = self._conn()
        con.execute("UPDATE tagihan_pppoe SET snap_token=?, order_id=? WHERE id=?",
                    (snap_token, order_id, tid))
        con.commit()
        con.close()

    def bayar_tagihan_by_order(self, order_id: str) -> dict | None:
        """Tandai tagihan paid berdasarkan order_id Midtrans. Return tagihan dict."""
        con = self._conn()
        row = con.execute(
            "SELECT * FROM tagihan_pppoe WHERE order_id=? AND status!='paid'", (order_id,)
        ).fetchone()
        if not row:
            con.close()
            return None
        now = int(time.time())
        con.execute("UPDATE tagihan_pppoe SET status='paid', paid_at=? WHERE id=?", (now, row["id"]))
        con.commit()
        # kembalikan lengkap dengan nama pelanggan
        full = con.execute("""
            SELECT t.*, p.nama_pelanggan, p.telepon, pk.nama as paket_nama, u.nama as isp_nama
            FROM tagihan_pppoe t
            LEFT JOIN pppoe_users p ON p.id=t.pppoe_id
            LEFT JOIN paket_pppoe pk ON pk.id=p.paket_id
            LEFT JOIN users u ON u.id=t.user_id
            WHERE t.id=?
        """, (row["id"],)).fetchone()
        con.close()
        return dict(full) if full else None

    def bayar_tagihan(self, tid: int, user_id: str, metode: str = "Manual", keterangan: str = "") -> bool:
        con = self._conn()
        row = con.execute(
            "SELECT * FROM tagihan_pppoe WHERE id=? AND user_id=? AND status!='paid'",
            (tid, user_id)
        ).fetchone()
        if not row:
            con.close()
            return False
        now = int(time.time())
        con.execute(
            "UPDATE tagihan_pppoe SET status='paid', paid_at=?, metode_bayar=?, keterangan_bayar=? WHERE id=?",
            (now, metode, keterangan, tid)
        )
        con.commit()
        con.close()
        return True

    def tagihan_overdue(self, user_id: str, bulan: str, hari_ini: int) -> int:
        """Tandai tagihan unpaid yang sudah lewat jatuh tempo sebagai overdue. Return jumlah."""
        con = self._conn()
        # Cari tagihan unpaid bulan ini yang tgl_bayar sudah lewat
        rows = con.execute("""
            SELECT t.id FROM tagihan_pppoe t
            LEFT JOIN pppoe_users p ON p.id=t.pppoe_id
            WHERE t.user_id=? AND t.bulan=? AND t.status='unpaid' AND p.tgl_bayar < ?
        """, (user_id, bulan, hari_ini)).fetchall()
        n = 0
        for r in rows:
            con.execute("UPDATE tagihan_pppoe SET status='overdue' WHERE id=?", (r[0],))
            n += 1
        con.commit()
        con.close()
        return n

    def stats_tagihan(self, user_id: str, bulan: str) -> dict:
        con = self._conn()
        total   = con.execute("SELECT COUNT(*) FROM tagihan_pppoe WHERE user_id=? AND bulan=?", (user_id, bulan)).fetchone()[0]
        paid    = con.execute("SELECT COUNT(*) FROM tagihan_pppoe WHERE user_id=? AND bulan=? AND status='paid'", (user_id, bulan)).fetchone()[0]
        unpaid  = con.execute("SELECT COUNT(*) FROM tagihan_pppoe WHERE user_id=? AND bulan=? AND status='unpaid'", (user_id, bulan)).fetchone()[0]
        overdue = con.execute("SELECT COUNT(*) FROM tagihan_pppoe WHERE user_id=? AND bulan=? AND status='overdue'", (user_id, bulan)).fetchone()[0]
        omzet   = con.execute("SELECT COALESCE(SUM(amount),0) FROM tagihan_pppoe WHERE user_id=? AND bulan=? AND status='paid'", (user_id, bulan)).fetchone()[0]
        con.close()
        return {"total": total, "paid": paid, "unpaid": unpaid, "overdue": overdue, "omzet": omzet}

    # ── Toko Hotspot Online ───────────────────────────────────────────────────

    def get_isp_by_slug(self, slug: str) -> dict | None:
        """Cari ISP berdasarkan slug atau username toko."""
        con = self._conn()
        row = con.execute(
            "SELECT * FROM users WHERE (slug=? OR username=?) AND status='aktif'", (slug, slug)
        ).fetchone()
        con.close()
        return dict(row) if row else None

    def get_voucher_tersedia(self, user_id: str, paket_id: int) -> dict | None:
        """Ambil satu voucher tersedia milik ISP untuk paket tertentu."""
        con = self._conn()
        row = con.execute(
            "SELECT * FROM voucher_hotspot WHERE user_id=? AND paket_id=? AND status='tersedia' ORDER BY created_at LIMIT 1",
            (user_id, paket_id)
        ).fetchone()
        con.close()
        return dict(row) if row else None

    def mark_voucher_sold(self, vid: int):
        con = self._conn()
        con.execute("UPDATE voucher_hotspot SET status='dipakai', dipakai_at=? WHERE id=?", (int(time.time()), vid))
        con.commit()
        con.close()

    def beli_voucher_saldo(self, buyer_uid: str, isp_uid: str, paket_id: int) -> dict:
        """Beli voucher menggunakan saldo. Return {'ok': bool, 'kode': str, 'msg': str}."""
        con = self._conn()
        try:
            paket = con.execute("SELECT * FROM paket_hotspot WHERE id=? AND user_id=? AND status='aktif'", (paket_id, isp_uid)).fetchone()
            if not paket:
                return {"ok": False, "msg": "Paket tidak ditemukan"}
            paket = dict(paket)

            buyer = con.execute("SELECT * FROM users WHERE id=?", (buyer_uid,)).fetchone()
            if not buyer:
                return {"ok": False, "msg": "Akun tidak valid"}
            buyer = dict(buyer)

            if buyer["saldo"] < paket["harga"]:
                return {"ok": False, "msg": f"Saldo tidak cukup (saldo: Rp {buyer['saldo']:,}, harga: Rp {paket['harga']:,})"}

            voucher = con.execute(
                "SELECT * FROM voucher_hotspot WHERE user_id=? AND paket_id=? AND status='tersedia' ORDER BY created_at LIMIT 1",
                (isp_uid, paket_id)
            ).fetchone()
            if not voucher:
                return {"ok": False, "msg": "Stok voucher habis"}
            voucher = dict(voucher)

            # Atomik: debit saldo agen + tandai voucher + catat log
            con.execute("UPDATE users SET saldo=saldo-? WHERE id=?", (paket["harga"], buyer_uid))
            con.execute("UPDATE voucher_hotspot SET status='dipakai', dipakai_at=? WHERE id=?", (int(time.time()), voucher["id"]))
            con.execute(
                "INSERT INTO saldo_log (user_id,jumlah,tipe,keterangan,created_at) VALUES (?,?,?,?,?)",
                (buyer_uid, paket["harga"], "debit", f"Beli voucher {paket['nama']} - {voucher['kode']}", int(time.time()))
            )
            con.commit()
            return {"ok": True, "kode": voucher["kode"], "paket": paket["nama"], "msg": "Berhasil"}
        except Exception as e:
            con.rollback()
            return {"ok": False, "msg": str(e)}
        finally:
            con.close()

    def list_paket_hotspot_publik(self, user_id: str) -> list[dict]:
        """Paket hotspot aktif untuk toko publik. server_id kosong = tampil di semua server."""
        con = self._conn()
        rows = con.execute("""
            SELECT p.*
            FROM paket_hotspot p
            WHERE p.user_id=? AND p.status='aktif'
            ORDER BY p.harga
        """, (user_id,)).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def create_order(self, user_id: str, paket_id: int, server_id: str,
                     nomor_hp: str, amount: int) -> str:
        import uuid as _uuid
        order_id = "ORD-" + _uuid.uuid4().hex[:10].upper()
        con = self._conn()
        con.execute(
            "INSERT INTO hotspot_orders (id,user_id,paket_id,server_id,nomor_hp,amount,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (order_id, user_id, paket_id, server_id, nomor_hp, amount, int(time.time()))
        )
        con.commit()
        con.close()
        return order_id

    def get_order(self, order_id: str) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM hotspot_orders WHERE id=?", (order_id,)).fetchone()
        con.close()
        return dict(row) if row else None

    def set_order_snap_token(self, order_id: str, snap_token: str):
        con = self._conn()
        con.execute("UPDATE hotspot_orders SET snap_token=? WHERE id=?", (snap_token, order_id))
        con.commit()
        con.close()

    def confirm_order(self, order_id: str, mt_callback=None) -> dict | None:
        """Tandai order paid, generate voucher baru on-demand, return voucher.
        mt_callback(server_id, kode, paket) dipanggil untuk push ke MikroTik."""
        import uuid as _uuid
        con = self._conn()
        order = con.execute("SELECT * FROM hotspot_orders WHERE id=? AND status='pending'",
                            (order_id,)).fetchone()
        if not order:
            con.close()
            return None
        order = dict(order)

        # Generate kode voucher unik
        kode = _uuid.uuid4().hex[:8].upper()
        while con.execute("SELECT id FROM voucher_hotspot WHERE kode=?", (kode,)).fetchone():
            kode = _uuid.uuid4().hex[:8].upper()

        paket = con.execute("SELECT * FROM paket_hotspot WHERE id=?", (order["paket_id"],)).fetchone()
        paket = dict(paket) if paket else {}

        now = int(time.time())
        # Simpan voucher baru ke DB
        vid = con.execute(
            "INSERT INTO voucher_hotspot (user_id, paket_id, kode, status, created_at, dipakai_at) "
            "VALUES (?,?,?,?,?,?)",
            (order["user_id"], order["paket_id"], kode, "dipakai", now, now)
        ).lastrowid

        con.execute("UPDATE hotspot_orders SET status='paid', voucher_id=?, paid_at=? WHERE id=?",
                    (vid, now, order_id))
        con.commit()
        con.close()

        # Push ke MikroTik (opsional, via callback)
        if mt_callback:
            try:
                mt_callback(order["server_id"], kode, paket)
            except Exception:
                pass

        return {"id": vid, "kode": kode, "paket_id": order["paket_id"],
                "user_id": order["user_id"], "status": "dipakai"}

    def cari_voucher_by_nomor(self, isp_uid: str, nomor_hp: str) -> list[dict]:
        """Cari voucher yang sudah dibeli berdasarkan nomor HP."""
        con = self._conn()
        # Normalisasi nomor: 08xxx → 628xxx
        nomor = nomor_hp.strip().replace(" ", "").replace("-", "")
        if nomor.startswith("0"):
            nomor62 = "62" + nomor[1:]
        elif nomor.startswith("+62"):
            nomor62 = nomor[1:]
        else:
            nomor62 = nomor
        rows = con.execute("""
            SELECT o.id as order_id, o.nomor_hp, o.amount, o.paid_at, o.created_at,
                   v.kode, p.nama as paket_nama, p.durasi, p.kecepatan
            FROM hotspot_orders o
            LEFT JOIN voucher_hotspot v ON v.id = o.voucher_id
            LEFT JOIN paket_hotspot p ON p.id = o.paket_id
            WHERE o.user_id=? AND o.status='paid'
              AND (o.nomor_hp=? OR o.nomor_hp=?)
            ORDER BY o.paid_at DESC
            LIMIT 20
        """, (isp_uid, nomor_hp.strip(), nomor62)).fetchall()
        con.close()
        return [dict(r) for r in rows]

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self, user_id: str, role: str) -> dict:
        con = self._conn()
        agen    = con.execute("SELECT COUNT(*) FROM users WHERE parent_id=? AND role IN ('agen','sub_agen')", (user_id,)).fetchone()[0]
        servers = con.execute("SELECT COUNT(*) FROM mikrotik_servers WHERE user_id=?", (user_id,)).fetchone()[0]
        pppoe   = con.execute("SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND status='aktif'", (user_id,)).fetchone()[0]
        voucher = con.execute("SELECT COUNT(*) FROM voucher_hotspot WHERE user_id=? AND status='tersedia'", (user_id,)).fetchone()[0]
        con.close()
        return {"agen": agen, "servers": servers, "pppoe": pppoe, "voucher": voucher}

    def laporan_pendapatan(self, user_id: str, tahun: str) -> list[dict]:
        """Pendapatan PPPoE + Voucher per bulan dalam satu tahun."""
        import calendar
        con = self._conn()
        result = []
        for m in range(1, 13):
            bulan = f"{tahun}-{str(m).zfill(2)}"
            _, last_day = calendar.monthrange(int(tahun), m)
            ts_start = int(time.mktime((int(tahun), m, 1, 0, 0, 0, 0, 0, -1)))
            ts_end   = int(time.mktime((int(tahun), m, last_day, 23, 59, 59, 0, 0, -1)))

            pppoe = con.execute(
                "SELECT COALESCE(SUM(amount),0), COUNT(*) FROM tagihan_pppoe "
                "WHERE user_id=? AND bulan=? AND status='paid'",
                (user_id, bulan)
            ).fetchone()
            # Voucher dari hotspot_orders (paid_at epoch)
            voucher = con.execute(
                "SELECT COALESCE(SUM(amount),0), COUNT(*) FROM hotspot_orders "
                "WHERE user_id=? AND status='paid' AND paid_at BETWEEN ? AND ?",
                (user_id, ts_start, ts_end)
            ).fetchone()

            total_unpaid = con.execute(
                "SELECT COUNT(*) FROM tagihan_pppoe WHERE user_id=? AND bulan=? AND status!='paid'",
                (user_id, bulan)
            ).fetchone()[0]

            result.append({
                "bulan":          bulan,
                "omzet_pppoe":    pppoe[0],
                "lunas_pppoe":    pppoe[1],
                "omzet_voucher":  voucher[0] if voucher else 0,
                "lunas_voucher":  voucher[1] if voucher else 0,
                "total":          pppoe[0] + (voucher[0] if voucher else 0),
                "unpaid":         total_unpaid,
            })
        con.close()
        return result

    # ── Hotspot Bulanan ───────────────────────────────────────────────────────

    def add_hotspot_pelanggan(self, user_id: str, server_id: str, nama: str,
                               nomor_wa: str, username: str, password: str,
                               profile: str, harga: int, jatuh_tempo: int,
                               catatan: str = "") -> str:
        pid = "HP-" + uuid.uuid4().hex[:8].upper()
        con = self._conn()
        con.execute(
            "INSERT INTO hotspot_pelanggan "
            "(id,user_id,server_id,nama,nomor_wa,username,password,profile,harga,jatuh_tempo,catatan,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, user_id, server_id, nama, nomor_wa, username, password,
             profile, harga, jatuh_tempo, catatan, int(time.time()))
        )
        con.commit()
        con.close()
        return pid

    def list_hotspot_pelanggan(self, user_id: str, status: str = "") -> list[dict]:
        con = self._conn()
        q = "SELECT * FROM hotspot_pelanggan WHERE user_id=?"
        args = [user_id]
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY nama"
        rows = con.execute(q, args).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def get_hotspot_pelanggan(self, pid: str, user_id: str = "") -> dict | None:
        con = self._conn()
        if user_id:
            row = con.execute("SELECT * FROM hotspot_pelanggan WHERE id=? AND user_id=?", (pid, user_id)).fetchone()
        else:
            row = con.execute("SELECT * FROM hotspot_pelanggan WHERE id=?", (pid,)).fetchone()
        con.close()
        return dict(row) if row else None

    def update_hotspot_pelanggan(self, pid: str, user_id: str, **kwargs) -> bool:
        allowed = {"nama","nomor_wa","password","profile","harga","jatuh_tempo","status","catatan","server_id"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        con = self._conn()
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [pid, user_id]
        con.execute(f"UPDATE hotspot_pelanggan SET {sets} WHERE id=? AND user_id=?", vals)
        con.commit()
        con.close()
        return True

    def delete_hotspot_pelanggan(self, pid: str, user_id: str) -> bool:
        con = self._conn()
        con.execute("DELETE FROM hotspot_pelanggan WHERE id=? AND user_id=?", (pid, user_id))
        con.execute("DELETE FROM hotspot_tagihan WHERE pelanggan_id=? AND user_id=?", (pid, user_id))
        con.commit()
        con.close()
        return True

    def get_or_create_hotspot_tagihan(self, pelanggan_id: str, user_id: str, bulan: str, amount: int) -> dict:
        con = self._conn()
        row = con.execute(
            "SELECT * FROM hotspot_tagihan WHERE pelanggan_id=? AND bulan=?", (pelanggan_id, bulan)
        ).fetchone()
        if row:
            con.close()
            return dict(row)
        tid = "HT-" + uuid.uuid4().hex[:8].upper()
        con.execute(
            "INSERT INTO hotspot_tagihan (id,pelanggan_id,user_id,bulan,amount,created_at) VALUES (?,?,?,?,?,?)",
            (tid, pelanggan_id, user_id, bulan, amount, int(time.time()))
        )
        con.commit()
        row = con.execute("SELECT * FROM hotspot_tagihan WHERE id=?", (tid,)).fetchone()
        con.close()
        return dict(row)

    def list_hotspot_tagihan(self, user_id: str, bulan: str = "", status: str = "") -> list[dict]:
        con = self._conn()
        q = ("SELECT t.*, p.nama as pelanggan_nama, p.nomor_wa as pelanggan_wa, p.username "
             "FROM hotspot_tagihan t JOIN hotspot_pelanggan p ON p.id=t.pelanggan_id "
             "WHERE t.user_id=?")
        args = [user_id]
        if bulan:
            q += " AND t.bulan=?"
            args.append(bulan)
        if status:
            q += " AND t.status=?"
            args.append(status)
        q += " ORDER BY p.nama"
        rows = con.execute(q, args).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def bayar_hotspot_tagihan(self, tid: str, user_id: str) -> dict | None:
        con = self._conn()
        row = con.execute(
            "SELECT * FROM hotspot_tagihan WHERE id=? AND user_id=?", (tid, user_id)
        ).fetchone()
        if not row:
            con.close()
            return None
        con.execute(
            "UPDATE hotspot_tagihan SET status='paid', paid_at=? WHERE id=?",
            (int(time.time()), tid)
        )
        con.commit()
        con.close()
        return dict(row)

    def stats_hotspot_tagihan(self, user_id: str, bulan: str) -> dict:
        con = self._conn()
        total  = con.execute("SELECT COUNT(*) FROM hotspot_tagihan WHERE user_id=? AND bulan=?", (user_id, bulan)).fetchone()[0]
        paid   = con.execute("SELECT COUNT(*) FROM hotspot_tagihan WHERE user_id=? AND bulan=? AND status='paid'", (user_id, bulan)).fetchone()[0]
        unpaid = total - paid
        omzet  = con.execute("SELECT COALESCE(SUM(amount),0) FROM hotspot_tagihan WHERE user_id=? AND bulan=? AND status='paid'", (user_id, bulan)).fetchone()[0]
        con.close()
        return {"total": total, "paid": paid, "unpaid": unpaid, "omzet": omzet}

    def get_hotspot_jatuh_tempo_hari_ini(self, user_id: str, hari: int, bulan: str) -> list[dict]:
        """Pelanggan yang jatuh tempo hari ini dan belum ada tagihan bulan ini."""
        con = self._conn()
        rows = con.execute(
            "SELECT p.* FROM hotspot_pelanggan p "
            "WHERE p.user_id=? AND p.status='aktif' AND p.jatuh_tempo=? "
            "AND NOT EXISTS (SELECT 1 FROM hotspot_tagihan t WHERE t.pelanggan_id=p.id AND t.bulan=?)",
            (user_id, hari, bulan)
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def laporan_pendapatan_agen(self, isp_id: str, tahun: str) -> list[dict]:
        """Pendapatan dari agen (voucher) per bulan — tidak masuk saldo ISP."""
        import calendar
        con = self._conn()
        result = []
        for m in range(1, 13):
            _, last_day = calendar.monthrange(int(tahun), m)
            ts_start = int(time.mktime((int(tahun), m, 1, 0, 0, 0, 0, 0, -1)))
            ts_end   = int(time.mktime((int(tahun), m, last_day, 23, 59, 59, 0, 0, -1)))
            row = con.execute(
                "SELECT COALESCE(SUM(total),0), COUNT(*) FROM agen_income "
                "WHERE user_id=? AND created_at BETWEEN ? AND ?",
                (isp_id, ts_start, ts_end)
            ).fetchone()
            result.append({
                "bulan":  f"{tahun}-{str(m).zfill(2)}",
                "total":  row[0] if row else 0,
                "jumlah": row[1] if row else 0,
            })
        con.close()
        return result

    def laporan_topup_agen(self, isp_id: str, tahun: str) -> list[dict]:
        """Rekapitulasi topup manual agen per bulan dalam satu tahun."""
        import calendar
        con = self._conn()
        result = []
        for m in range(1, 13):
            _, last_day = calendar.monthrange(int(tahun), m)
            ts_start = int(time.mktime((int(tahun), m, 1, 0, 0, 0, 0, 0, -1)))
            ts_end   = int(time.mktime((int(tahun), m, last_day, 23, 59, 59, 0, 0, -1)))
            row = con.execute(
                "SELECT COALESCE(SUM(amount),0), COUNT(*) FROM topup_orders "
                "WHERE isp_id=? AND tipe='manual' AND status='paid' AND paid_at BETWEEN ? AND ?",
                (isp_id, ts_start, ts_end)
            ).fetchone()
            result.append({
                "bulan":  f"{tahun}-{str(m).zfill(2)}",
                "total":  row[0] if row else 0,
                "jumlah": row[1] if row else 0,
            })
        con.close()
        return result

    # ── WA Templates ─────────────────────────────────────────────────────────

    def get_wa_template(self, user_id: str, jenis: str) -> dict | None:
        con = self._conn()
        row = con.execute("SELECT * FROM wa_templates WHERE user_id=? AND jenis=?", (user_id, jenis)).fetchone()
        con.close()
        return dict(row) if row else None

    def save_wa_template(self, user_id: str, jenis: str, isi: str):
        con = self._conn()
        con.execute("""
            INSERT INTO wa_templates (user_id, jenis, isi, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, jenis) DO UPDATE SET isi=excluded.isi, updated_at=excluded.updated_at
        """, (user_id, jenis, isi, int(time.time())))
        con.commit()
        con.close()

    def delete_wa_template(self, user_id: str, jenis: str):
        con = self._conn()
        con.execute("DELETE FROM wa_templates WHERE user_id=? AND jenis=?", (user_id, jenis))
        con.commit()
        con.close()

    def list_wa_templates(self, user_id: str) -> dict:
        con = self._conn()
        rows = con.execute("SELECT * FROM wa_templates WHERE user_id=?", (user_id,)).fetchall()
        con.close()
        return {r["jenis"]: dict(r) for r in rows}

    def laporan_pelanggan_baru(self, user_id: str, tahun: str) -> list[dict]:
        """Pelanggan baru per bulan dalam satu tahun."""
        import calendar
        con = self._conn()
        result = []
        for m in range(1, 13):
            bulan_str = f"{tahun}-{str(m).zfill(2)}"
            # epoch range untuk bulan ini
            _, last_day = calendar.monthrange(int(tahun), m)
            ts_start = int(time.mktime((int(tahun), m, 1, 0, 0, 0, 0, 0, 0)))
            ts_end   = int(time.mktime((int(tahun), m, last_day, 23, 59, 59, 0, 0, 0)))
            count = con.execute(
                "SELECT COUNT(*) FROM pppoe_users WHERE user_id=? AND created_at BETWEEN ? AND ?",
                (user_id, ts_start, ts_end)
            ).fetchone()[0]
            result.append({"bulan": bulan_str, "count": count})
        con.close()
        return result
