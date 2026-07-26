# Changelog: Reminder Tagihan PPPoE

## 2026-07-02: Perbaikan Logika Reminder dengan Grace Period

### ❌ Logika Lama (Sebelum)
- Reminder dikirim berdasarkan `tgl_bayar` langsung
- H-3, H-1, H-0, H+1 dihitung dari tanggal bayar
- **Contoh:** Pelanggan tgl_bayar=1 → reminder H-3 dikirim tgl 29 bulan sebelumnya

### ✅ Logika Baru (Sekarang)
- **Grace period 10 hari ditambahkan**
- Jatuh tempo = `tgl_bayar + 10 hari`
- Reminder dikirim H-3, H-1, H-0, H+1 dari **jatuh tempo** (bukan tgl_bayar)

### 📊 Contoh Perhitungan

**Pelanggan dengan tgl_bayar = 1:**

```
Tanggal Bayar: 1 Juli 2026
Grace Period:  +10 hari
─────────────────────────────
Jatuh Tempo:   11 Juli 2026
```

**Jadwal Reminder:**
- **8 Juli 2026 (H-3):** 🔔 "Jatuh tempo 3 hari lagi (11 Jul 2026)"
- **10 Juli 2026 (H-1):** ⚠️ "Jatuh tempo besok (11 Jul 2026)"
- **11 Juli 2026 (H-0):** 🚨 "Jatuh tempo hari ini (11 Jul 2026)"
- **12 Juli 2026 (H+1):** ❗ "Sudah melewati jatuh tempo (sejak 11 Jul 2026)"

### 🔧 File yang Diubah
- `/home/ubuntu/projects/billing-web/app.py` → fungsi `_run_auto_reminder()`

### ⏰ Jadwal Eksekusi
- Auto reminder tetap jalan **setiap hari jam 08:00 WIB**
- Perubahan berlaku sejak restart aplikasi pada 2026-07-02 10:27 WIB

### 📝 Catatan
- Pelanggan tetap bisa bayar kapan saja (tidak harus menunggu reminder)
- Link bayar online tetap dikirim di setiap reminder
- Status tagihan tetap unpaid sampai pembayaran dikonfirmasi

---

**Dimodifikasi oleh:** AI Tebe  
**Tanggal:** 2 Juli 2026  
**Request:** Pak Tebe
