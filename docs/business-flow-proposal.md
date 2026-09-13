# Usulan perombakan flow absensi CCTV dan monitoring pengunjung

Status: rancangan untuk ditinjau. Aturan di bawah belum diterapkan.

## Temuan dari implementasi saat ini

- Absensi memakai company, karyawan, dan tanggal UTC sebagai identitas catatan harian. Check-in dan check-out pertama mengisi catatan tersebut; keluar tanpa masuk ditandai incomplete.
- Worker absensi memeriksa pengenalan wajah, cooldown, dan kebijakan/toleransi sebelum menyimpan ke database. Publikasi RabbitMQ dilakukan setelah commit; kegagalan publikasi dicatat di log.
- Event Visitor membuat session ID baru, mengosongkan counter, dan mereset koleksi wajah setiap start. Riwayat event SQL tetap disimpan.
- Pengunjung baru yang melewati garis masuk menambah jumlah unik dan jumlah masuk sekaligus. Kunjungan ulang belum dipisahkan dengan jelas dari pengunjung unik.
- Statistik masuk per jam memakai WIB dan seluruh event masuk tersimpan pada tanggal pilihan.

Referensi: `app/services/cctv_recognition.py`, `app/daos/cctv.py`, `app/services/event_visitor.py`, `app/daos/event_visitor.py`.

## Flow absensi yang diusulkan

1. Pilih company/cabang; atur zona waktu, kamera masuk/keluar, dan sumber jadwal kerja.
2. Sinkronkan karyawan dan wajah, lalu validasi kesiapan kamera.
3. Mulai monitoring. Kamera menghasilkan deteksi; deteksi yang memenuhi ambang pengenalan menjadi kandidat absensi.
4. Tentukan shift kerja yang relevan berdasarkan waktu lokal dan jadwal, termasuk shift melewati tengah malam.
5. Terapkan aturan masuk/keluar. Deteksi berulang tidak menghasilkan absensi tambahan untuk kejadian yang sama.
6. Simpan hasil absensi dan status pengiriman secara konsisten; pengiriman ke sistem tujuan dapat dicoba ulang tanpa menggandakan absensi.
7. Tampilkan catatan sah, pengecualian yang perlu ditinjau, dan status pengiriman. Koreksi menyimpan alasan dan jejak perubahan.
8. Menghentikan kamera menghentikan monitoring. Catatan absensi dan shift tetap tersedia.

Keputusan bisnis yang masih diperlukan:

- Satu catatan per hari atau per shift? Usulan: per shift bila jadwal tersedia.
- Waktu pulang memakai keluar pertama atau keluar terakhir yang memenuhi aturan? Bagaimana keluar saat istirahat?
- Masuk terlambat/di luar toleransi ditolak atau dicatat sebagai pengecualian?
- Jika kebijakan shift tidak tersedia, apakah pencatatan ditunda atau disimpan untuk ditinjau?
- Apakah aturan berasal dari aplikasi ini atau sistem HR yang sudah terhubung?

## Flow pengunjung yang diusulkan

1. Buat event/lokasi monitoring dengan nama, periode, zona waktu, dan kamera.
2. Aktifkan event, lalu mulai kamera. Event memiliki identitas yang tersimpan terpisah dari proses kamera.
3. Wajah baru melewati garis masuk: tambah pengunjung unik, kunjungan masuk, dan jumlah di dalam area.
4. Pengunjung yang berada di dalam melewati garis keluar: catat keluar dan kurangi jumlah di dalam area.
5. Pengunjung yang sama masuk kembali: tambah kunjungan masuk dan jumlah di dalam area; jumlah unik tetap.
6. Deteksi berulang tanpa perpindahan masuk/keluar tidak menambah angka.
7. Pause kamera dan lanjutkan event yang sama dengan identitas serta counter yang dipulihkan. Jika ada jeda monitoring, tampilkan bahwa okupansi dapat tidak lengkap.
8. Selesaikan event melalui aksi terpisah; riwayat dan laporan tetap tersedia.
9. Laporan dapat difilter berdasarkan event dan tanggal. Pisahkan jumlah unik, kunjungan masuk/keluar, dan estimasi okupansi.

Keputusan bisnis yang masih diperlukan:

- Monitoring berbasis event, lokasi harian, atau keduanya?
- Identitas pengunjung unik berlaku per event atau per hari? Usulan awal: per event.
- Satu kamera dua arah atau kamera masuk/keluar terpisah? Apakah beberapa kamera harus berbagi identitas?
- Bagaimana menangani keluar tanpa catatan masuk dan jeda kamera?
- Periode penyimpanan data pengunjung ditentukan oleh kebutuhan operasional.

## Dampak implementasi setelah aturan ditentukan

- Database: identitas event/shift dan status bisnis, kunjungan pengunjung, status pengiriman, serta riwayat koreksi jika dibutuhkan.
- Backend: pemrosesan masuk/keluar berdasarkan status sebelumnya, pemulihan setelah restart, dan pengiriman ulang yang tidak menggandakan catatan.
- UI: pengaturan bersama tetap tersedia; monitoring menampilkan konteks event/shift, aksi mulai/jeda/selesai, pengecualian, dan laporan.
- Data lama: tetap dipertahankan sebagai riwayat. Pemetaan ke event/shift baru harus eksplisit; data yang tidak tersedia tidak boleh ditebak.
- Validasi: shift lintas malam, deteksi berulang, pengunjung keluar-masuk lagi, restart kamera, kegagalan pengiriman, dan batas tanggal laporan.
