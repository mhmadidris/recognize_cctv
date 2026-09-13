# CCTV Attendance API

Base URL: `/api/v1/cctv`

Semua response sukses memakai format:

```json
{
  "message": "...",
  "result": {},
  "errors": null
}
```

## Endpoint

| Method | Endpoint | Keterangan |
| --- | --- | --- |
| GET | `/config` | Konfigurasi runtime dari environment |
| GET | `/settings/{company_id}` | Ambil pengaturan company |
| POST | `/settings/{company_id}` | Simpan pengaturan company |
| POST | `/prepare` | Siapkan runtime recognition |
| POST | `/source/{company_id}` | Simpan atau ubah kamera company |
| GET | `/cameras/{company_id}` | Daftar kamera company |
| DELETE | `/cameras/{company_id}/{camera_id}` | Hapus kamera |
| POST | `/start` | Jalankan semua kamera atau kamera tertentu |
| POST | `/stop` | Hentikan semua kamera atau kamera tertentu |
| GET | `/status` | Status runtime dan worker |
| GET | `/stream` | Stream kamera dalam format MJPEG |
| GET | `/attendance` | Data attendance dari database |
| POST | `/source` | Override source kamera sementara |
| GET | `/source/{company_id}` | Ambil kamera utama company |
| POST | `/workin/attendance/in/response` | Callback hasil check-in |
| POST | `/workin/attendance/out/response` | Callback hasil check-out |

## Settings

### GET `/settings/{company_id}`

Contoh response:

```json
{
  "message": "CCTV settings retrieved.",
  "result": {
    "setting_id": "uuid",
    "company_id": "uuid",
    "setup_status": "configured",
    "company_setting_exists": true,
    "enabled": true,
    "confidence_threshold": 0.75,
    "cooldown_seconds": 30,
    "allow_attendance_after_tolerance": true,
    "time_tolerance_minutes": 15,
    "created_at": "2026-09-01T10:00:00+07:00",
    "updated_at": "2026-09-01T10:00:00+07:00"
  },
  "errors": null
}
```

### POST `/settings/{company_id}`

Request body:

```json
{
  "enabled": true,
  "cooldown_seconds": 30,
  "allow_attendance_after_tolerance": false,
  "time_tolerance_minutes": 15
}
```

Field:

| Field | Type | Default | Keterangan |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | Mengaktifkan CCTV company |
| `cooldown_seconds` | integer | `30` | Jeda pencatatan employee yang sama |
| `allow_attendance_after_tolerance` | boolean | `true` | Izinkan attendance di luar tolerance |
| `time_tolerance_minutes` | integer | `0` | Toleransi keterlambatan atau terlalu awal |

Jika `allow_attendance_after_tolerance=false`, recognition menolak:

- check-in setelah `shift.start_time + time_tolerance_minutes`;
- check-out sebelum `shift.end_time - time_tolerance_minutes`.

### GET `/config`

```json
{
  "message": "CCTV runtime configuration.",
  "result": {
    "company_id": "uuid",
    "attendance_policy_enabled": true
  },
  "errors": null
}
```

## Cameras

### POST `/source/{company_id}`

Request body:

```json
{
  "camera_id": null,
  "name": "Kamera Masuk",
  "camera_source": "0",
  "branch_id": null,
  "zone_type": "in"
}
```

`zone_type` yang digunakan untuk attendance adalah `in` dan `out`. `camera_source` dapat berupa index webcam, path video, atau URL RTSP.

### GET `/cameras/{company_id}`

Response `result` berupa array kamera:

```json
[
  {
    "id": "uuid",
    "company_id": "uuid",
    "branch_id": null,
    "name": "Kamera Masuk",
    "camera_source": "0",
    "rtsp_url": "0",
    "zone_type": "in",
    "status": "offline",
    "last_online_at": null,
    "created_at": "2026-09-01T10:00:00+07:00",
    "updated_at": null
  }
]
```

### DELETE `/cameras/{company_id}/{camera_id}`

Response:

```json
{
  "message": "Company CCTV camera deleted.",
  "result": { "id": "uuid" },
  "errors": null
}
```

## Runtime

### POST `/prepare`

Menyiapkan model dan data employee tanpa membuka kamera.

### POST `/start`

Tanpa query/body, jalankan seluruh kamera yang terdaftar. Untuk satu kamera:

```http
POST /api/v1/cctv/start?camera_id={camera_id}
```

### POST `/stop`

Tanpa query, hentikan seluruh kamera. Untuk satu kamera:

```http
POST /api/v1/cctv/stop?camera_id={camera_id}
```

### GET `/status`

Response `result` berisi status system, runtime, dan worker kamera. Field utama:

```json
{
  "system_started": true,
  "system_ready": true,
  "running": true,
  "initializing": false,
  "company_id": "uuid",
  "worker_count": 2,
  "running_count": 2,
  "attendance_count": 10,
  "error": null,
  "workers": []
}
```

### GET `/stream`

Menghasilkan `multipart/x-mixed-replace` dengan boundary `frame`.

Untuk kamera tertentu:

```http
GET /api/v1/cctv/stream?camera_id={camera_id}
```

## Attendance

### GET `/attendance`

Query parameter:

| Parameter | Wajib | Keterangan |
| --- | --- | --- |
| `date` | tidak | Format `YYYY-MM-DD`, default hari ini |
| `camera_id` | tidak | Filter berdasarkan kamera |
| `source` | tidak | `db` (default) atau `session` |

Contoh:

```http
GET /api/v1/cctv/attendance?date=2026-09-01&camera_id={camera_id}
```

Response `result` berbentuk object dengan key `employee_id`:

```json
{
  "employee-uuid": {
    "id": "attendance-uuid",
    "company_id": "company-uuid",
    "employee_id": "employee-uuid",
    "employee_name": "Nama Employee",
    "time_in": "09:05:00",
    "time_out": null,
    "attendance_date": "2026-09-01",
    "attendance_status": "present",
    "checkin_status": "late",
    "checkout_status": null,
    "photo_in": "/static/attendance_snapshots/employee-uuid/2026-09-01_in.png",
    "photo_out": null,
    "shift_assignment_id": "shift-assignment-uuid",
    "reason": "late_checkin",
    "explanation": "Check-in melewati batas waktu"
  }
}
```

## Shift Policy API

Jika `ATTENDANCE_POLICY_URL` diisi, service mengirim request policy untuk setiap event attendance:

```json
{
  "company_id": "uuid",
  "employee_id": "uuid",
  "attendance_type": "in",
  "detected_at": "2026-09-01T02:30:00+00:00",
  "attendance_date": "2026-09-01",
  "status": "active",
  "branch_id": null,
  "camera_id": "uuid"
}
```

Response dapat berupa object policy atau response assignment HRMS dengan `data[]`. Untuk `data[]`, service memilih assignment dengan employee dan company yang sesuai, `status=active`, serta tanggal event berada dalam rentang `start_date` dan `end_date`. Jika ada beberapa hasil, `start_date` terbaru dipilih.

Field policy yang digunakan:

`shift_assignment_id`, `shift_type_id`, `shift_type_name`, `shift_start_time`, `shift_end_time`, `attendance_status`, `checkin_status`, `checkout_status`, `reason`, `explanation`, `explanation_out`, `leave_type_id`, `leave_id`, `attendance_request_id`, dan `status`.

## Callback Workin

### POST `/workin/attendance/in/response`
### POST `/workin/attendance/out/response`

Request body:

```json
{
  "camera_id": "uuid",
  "employee_id": "uuid",
  "status": "success",
  "message": "Attendance recorded",
  "result": {},
  "errors": null
}
```

## Error

| HTTP | Arti |
| --- | --- |
| `404` | Resource tidak ditemukan |
| `409` | Runtime belum siap atau state tidak valid |
| `422` | Request atau konfigurasi tidak valid |
| `502` | Gagal publish event ke RabbitMQ |
| `500` | Error internal server |

Format error FastAPI:

```json
{
  "detail": "Pesan error"
}
```

## Environment

| Variable | Keterangan |
| --- | --- |
| `HRMS_BRANCH_ID` | Branch default, opsional |
| `HRMS_TOKEN` | Token API HRMS |
| `ATTENDANCE_POLICY_URL` | Endpoint policy shift, opsional |
| `ATTENDANCE_SNAPSHOT_ENABLED` | Simpan snapshot attendance |
| `ATTENDANCE_SNAPSHOT_BASE_URL` | Prefix URL snapshot |
| `RABBITMQ_URL` | Connection RabbitMQ |
