# Duluin Machine Learning

## Installation Tutorial

### Local Environment Setup

Firstly, make sure _pipenv_ library already installed on your machine. If not, run this command below:

```
$ pip install pipenv
```

Afterwards, don't forget to activate local environment with this command:

```
$ pipenv shell
```

If this is your first time, run the following command to install all libraries and dependencies:

```
$ pipenv install
```

(Optional) later, if any library is needed to install. Run similar command with additional library name, for instance:

```
$ pipenv install <library-name>
```

### Populate `.env` file

Use the appropriate configuration file from `environment/`, such as `environment/.env.staging`.

#### Required Environment Variables

| Variable | Description | Example |
| :--- | :--- | :--- |
| **DB_HOST** | Database host address | `localhost` |
| **DB_PORT** | Database port | `5432` |
| **DB_USERNAME** | Database username | `postgres` |
| **DB_PASSWORD** | Database password | `your_password` |
| **DB_NAME** | Database name | `recognize_cctv` |
| **DB_TYPE** | Database driver type | `postgresql` |
| **TRACK_IOU_THRESHOLD** | Minimum overlap to keep a face on the same track | `0.35` |
| **CCTV_MAX_CAMERAS_PER_COMPANY** | Maximum camera records allowed per company. `0` or empty disables the limit. | `2` |
| **TRACK_CONFIRM_HITS** | Consecutive tracked observations before confirming identity | `2` |
| **TRACK_MAX_MISSED_FRAMES** | Frames a track can disappear before cleanup | `12` |
| **VECTOR_DB_PATH** | Path for ChromaDB storage | `./vector_db` |
| **EVENT_VISITOR_VECTOR_DB_COLLECTION** | ChromaDB collection for anonymous event visitor embeddings. Reset on every Event Visitor Counter start. | `event_visitors` |
| **EVENT_VISITOR_SIMILARITY_THRESHOLD** | Minimum cosine similarity for treating a detected face as the same event visitor. | `0.70` |
| **EVENT_VISITOR_DETECT_EVERY_N_FRAMES** | Process face recognition every N frames to reduce CPU/GPU load. | `8` |
| **EVENT_VISITOR_TRACK_IOU_THRESHOLD** | Minimum face box overlap for keeping a detected face on the same event visitor track. | `0.25` |
| **EVENT_VISITOR_TRACK_MAX_MISSED_FRAMES** | Detection cycles a face track can disappear before cleanup. | `12` |
| **EVENT_VISITOR_GENDER_MODEL_PATH** | Optional OpenCV DNN gender model path. When empty, gender is reported as `unknown`. | `./models/gender/gender_net.caffemodel` |
| **EVENT_VISITOR_GENDER_CONFIG_PATH** | Optional OpenCV DNN gender model config path. | `./models/gender/deploy_gender.prototxt` |
| **EVENT_VISITOR_GENDER_CONFIDENCE_THRESHOLD** | Minimum model confidence for counting a visitor as male or female. | `0.65` |
| **ATTENDANCE_SNAPSHOT_ENABLED** | Save local attendance snapshot when CCTV attendance is recorded. | `true` |
| **ATTENDANCE_SNAPSHOT_BASE_URL** | Static URL prefix saved into `photo_in` / `photo_out`. | `/static/attendance_snapshots` |
| **ATTENDANCE_POLICY_URL** | Optional HRMS/policy endpoint called per employee attendance event to resolve shift assignment and status. | `https://hrms.example.com/api/attendance/policy` |
| **RABBITMQ_URL** | RabbitMQ connection URL | `amqp://guest:guest@localhost:5672/` |
| **RABBITMQ_EXCHANGE** | Optional exchange name. Leave empty to publish directly to durable queues. | empty |
| **RABBITMQ_ATTENDANCE_IN_ROUTING_KEY** | Queue/routing key for Workin check-in events. | `workin.attendance.in` |
| **RABBITMQ_ATTENDANCE_OUT_ROUTING_KEY** | Queue/routing key for Workin check-out events. | `workin.attendance.out` |
| **RABBITMQ_CAMERA_START_ROUTING_KEY** | Queue/routing key for CCTV camera-start events. | `workin.cctv.camera.start` |

### Run migration file to sync your database

1. **Manual Step:** Create the database manually in PostgreSQL (e.g., via DBeaver or `CREATE DATABASE recognize_cctv;`).
2. Run this command below to sync your current database schemas:

```
$ alembic upgrade heads
```

### Run Program

Execute this following code to run program with default port in 8000

```
$ uvicorn main:app --reload
```

### CCTV Recognition API

The CCTV recognition service runs as managed background workers inside the FastAPI application. It supports multi-camera attendance mode, where each camera can be configured with a `zone_type` to determine check-in or check-out behavior.

Full API documentation is available in [docs/cctv-api.md](docs/cctv-api.md).

#### API Reference

| Method | Endpoint | Description | Payload/Params |
| :--- | :--- | :--- | :--- |
| **POST** | `/api/v1/cctv/start` | Starts the CCTV system when called without `camera_id`; starts one camera when `camera_id` is provided. | Optional body: `{ "camera_id": "...", "employee_id": "..." }` |
| **POST** | `/api/v1/cctv/workin/attendance/in/response` | Receives Workin check-in processing response. | Body: Workin response payload |
| **POST** | `/api/v1/cctv/workin/attendance/out/response` | Receives Workin check-out processing response. | Body: Workin response payload |
| **POST** | `/api/v1/cctv/stop` | Gracefully stops recognition workers. | Optional query: `camera_id` |
| **GET** | `/api/v1/cctv/status` | Returns worker status and registered count. | Optional query: `camera_id` |
| **GET** | `/api/v1/cctv/stream` | Multi-part JPEG stream for video preview. | Optional query: `camera_id` |
| **GET** | `/api/v1/cctv/attendance` | Get current session attendance records (in RAM). | None |
| **GET** | `/api/v1/cctv/cameras/{id}` | List company cameras. | `company_id` (Path) |
| **POST** | `/api/v1/cctv/source/{id}` | Create/update a company camera source. | `company_id` (Path), Body: `CCTVCameraSourceUpdate` |
| **DELETE** | `/api/v1/cctv/cameras/{company_id}/{camera_id}` | Delete a company camera. | `company_id`, `camera_id` |
| **GET** | `/api/v1/cctv/settings/{id}` | Get dynamic settings for a specific company. | `company_id` (Path) |
| **POST** | `/api/v1/cctv/settings/{id}` | Update dynamic settings (threshold, cooldown). | `company_id` (Path), Body: `CCTVCompanySettingUpdate` |

#### Settings Update Payload Example
`POST /api/v1/cctv/settings/c8f745e0-aa6e-458b-bb70-4dda3e2accea`
```json
{
  "cooldown_seconds": 60,
  "enabled": true
}
```

Detected faces are matched against **ChromaDB**. Recognition confidence is configured from `SIMILARITY_THRESHOLD` in `.env`. In multi-camera mode, `zone_type` determines attendance direction: `in`/`attendance` cameras write `time_in`, while `out`/`exit`/`checkout` cameras write `time_out` in `attendance_cctv`.

Open `http://localhost:8000/api/v1/cctv/stream` in a browser to preview the feed.

### Event Visitor Counter

The admin dashboard shows hourly entries from stored Event Visitor events.
`GET /api/v1/event_visitor/statistics/hourly?date=2026-09-08` returns 24 hourly
buckets, total entries, and the busiest hour for that calendar date in WIB
(`Asia/Jakarta`, UTC+7). Counts include all sessions, only `in` events, and are
not limited by history pagination. Empty hours are zero; an empty day has no
peak hour. If several hours tie for the peak, the earliest is returned.
These are recorded entry events, not a cross-session unique-person count.

Run `alembic upgrade head` before using Event Visitor. CCTV settings (camera source,
line position/orientation, reverse direction, and model tier) are saved in
`event_visitor_settings` through the existing settings endpoints. The single
Event Visitor counter loads these settings on its first status request or start
after an application restart; it does not automatically start the camera.

Counted IN/OUT events are saved in `event_visitor_events`, including session ID,
anonymous visitor ID, label, camera source, gender, and UTC detection time.
`GET /api/v1/event_visitor/events?session_id=...&limit=100&offset=0` returns stored
history (omit `session_id` for all sessions; maximum limit is 500). History survives
new sessions and application restarts; live counters still reset on each start.
Existing records from past in-memory sessions are not backfilled. Face embeddings
continue to use ChromaDB. A processing/database write failure stops the counter
and exposes `last_error` in status instead of silently continuing without history.

The Event Visitor Counter uses face detection, FaceNet embeddings, simple face tracking, and the configured counting line to count anonymous unique visitors. On every `POST /api/v1/event_visitor/start`, the event visitor vector collection is reset, so the same face is counted once per event session and can be counted again in a future session.

`in_count` increases when a new unique visitor crosses the configured line in the IN direction. `out_count` increases when a tracked visitor crosses back in the OUT direction. `unique_visitor_count` is the session's unique visitor total, while `total_count` estimates current visitors inside as IN minus OUT.

If `EVENT_VISITOR_GENDER_MODEL_PATH` and `EVENT_VISITOR_GENDER_CONFIG_PATH` are configured, each new unique visitor is classified as `male`, `female`, or `unknown`. The unique gender totals are exposed as `male_count`, `female_count`, and `unknown_gender_count`.

## Logging

Application logs are emitted as JSON to stdout and file, so they are visible in `docker logs`.

Optional environment variables:

```
LOG_MASKING_ENABLED=true
LOG_MASK_FIELDS=nik,token,password,secret
LOG_UNMASK_FIELDS=company_id
LOG_MAX_VALUE_LENGTH=2000
LOG_MAX_COLLECTION_ITEMS=30
LOG_MAX_BODY_PREVIEW_LENGTH=1000
LOG_PRETTY_JSON=false
```

`LOG_MASK_FIELDS` adds more masked keys. `LOG_UNMASK_FIELDS` removes keys from the masking list.
`LOG_MAX_VALUE_LENGTH` limits long string values in logs. `LOG_MAX_COLLECTION_ITEMS` limits logged items in arrays/maps. `LOG_MAX_BODY_PREVIEW_LENGTH` limits raw body preview length.
`LOG_PRETTY_JSON=true` makes stdout/file logs multi-line formatted JSON for easier manual reading.
