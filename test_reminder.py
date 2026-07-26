#!/usr/bin/env python3
"""Test script untuk validasi logika reminder baru."""
from datetime import date, timedelta
import calendar

def test_reminder_logic(tgl_bayar: int, bulan: str, test_date: date):
    """Simulasi logika reminder untuk tanggal tertentu."""
    year, month = int(bulan[:4]), int(bulan[5:])
    max_day = calendar.monthrange(year, month)[1]
    
    tgl_bayar_actual = min(tgl_bayar, max_day)
    
    try:
        tgl_bayar_date = date(year, month, tgl_bayar_actual)
        jatuh_tempo_date = tgl_bayar_date + timedelta(days=10)
        
        days_until_due = (jatuh_tempo_date - test_date).days
        
        return {
            "tgl_bayar": tgl_bayar,
            "tgl_bayar_date": tgl_bayar_date,
            "jatuh_tempo_date": jatuh_tempo_date,
            "days_until_due": days_until_due,
            "will_send": days_until_due in [3, 1, 0, -1],
            "reminder_type": {
                3: "H-3 (3 hari lagi)",
                1: "H-1 (besok)",
                0: "H-0 (hari ini)",
                -1: "H+1 (sudah lewat)"
            }.get(days_until_due, "Tidak kirim")
        }
    except ValueError as e:
        return {"error": str(e)}

print("=" * 80)
print("🧪 TEST LOGIKA REMINDER TAGIHAN PPPoE")
print("=" * 80)
print()

# Test case 1: Pelanggan tgl_bayar = 1, bulan Juli 2026
print("📋 TEST CASE 1: Pelanggan tgl_bayar = 1 (Juli 2026)")
print("-" * 80)

bulan = "2026-07"
tgl_bayar = 1

# Test untuk beberapa hari
test_dates = [
    date(2026, 7, 2),   # Hari ini
    date(2026, 7, 8),   # H-3
    date(2026, 7, 10),  # H-1
    date(2026, 7, 11),  # H-0
    date(2026, 7, 12),  # H+1
]

for test_date in test_dates:
    result = test_reminder_logic(tgl_bayar, bulan, test_date)
    
    status = "✅ KIRIM" if result["will_send"] else "⏸️  SKIP"
    
    print(f"{test_date.strftime('%Y-%m-%d (%a)')}: {status:12} | "
          f"Jatuh tempo: {result['jatuh_tempo_date'].strftime('%d %b')} | "
          f"{result['reminder_type']}")

print()
print("-" * 80)

# Test case 2: Berbagai tanggal bayar
print("📋 TEST CASE 2: Berbagai tanggal bayar (hari ini: 2 Jul 2026)")
print("-" * 80)

today = date(2026, 7, 2)
test_tgl_bayar = [1, 5, 10, 15, 20, 25]

for tgl in test_tgl_bayar:
    result = test_reminder_logic(tgl, bulan, today)
    status = "✅ KIRIM" if result["will_send"] else "⏸️  SKIP"
    
    print(f"tgl_bayar = {tgl:2d}: {status:12} | "
          f"Jatuh tempo: {result['jatuh_tempo_date'].strftime('%d %b')} | "
          f"Sisa: {result['days_until_due']:3d} hari")

print()
print("=" * 80)
print("✅ Test selesai")
print()
print("💡 Untuk test real reminder, tunggu tanggal 8 Juli 2026 (H-3 pertama)")
print("   atau ubah system date untuk testing")
