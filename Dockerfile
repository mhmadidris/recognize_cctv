# syntax=docker/dockerfile:1

# =====================
# Stage 1: Builder
# =====================
FROM python:3.10-slim AS builder

RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    wget \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --upgrade pip pipenv
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install torchvision --index-url https://download.pytorch.org/whl/cpu
COPY Pipfile Pipfile.lock ./

# Install dependencies system-wide
RUN pipenv install --deploy --system
RUN pip install reportlab


# =====================
# Stage 2: Runtime
# =====================
FROM python:3.10-slim

# ---- System Libraries (IMPORTANT) ----
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    ffmpeg \
    wget \
    && rm -rf /var/lib/apt/lists/*

ENV FFMPEG_PATH="/usr/bin/ffmpeg"

# ---- Preload DeepFace Weights ----
RUN mkdir -p /root/.deepface/weights && \
    wget https://github.com/serengil/deepface_models/releases/download/v1.0/arcface_weights.h5 \
        -O /root/.deepface/weights/arcface_weights.h5 && \
    wget https://github.com/serengil/deepface_models/releases/download/v1.0/retinaface.h5 \
        -O /root/.deepface/weights/retinaface.h5

WORKDIR /app

# ---- Copy Python deps from builder ----
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# ---- Pre-download PaddleOCR models ----


# ---- Copy Application ----
COPY . .


# =====================
# Runtime Environment
# =====================
# ---- Disable GPU / CUDA ----
# ===== Paddle / OpenMP Safety =====
ENV DISABLE_MODEL_SOURCE_CHECK=True
ENV CUDA_VISIBLE_DEVICES=-1

# ---- Thread Control ----
ENV OMP_NUM_THREADS=1
ENV MKL_NUM_THREADS=1
ENV OPENBLAS_NUM_THREADS=1
ENV NUMEXPR_NUM_THREADS=1
ENV VECLIB_MAXIMUM_THREADS=1
ENV BLIS_NUM_THREADS=1
ENV TF_NUM_INTRAOP_THREADS=1
ENV TF_NUM_INTEROP_THREADS=1

# ---- Paddle Safety ----
ENV FLAGS_use_cuda=False
ENV FLAGS_use_mkldnn=False

# ---- Silence Logs ----
ENV TF_CPP_MIN_LOG_LEVEL=2
ENV GLOG_minloglevel=2
ENV FLAGS_log_level=2

# =====================
# App
# =====================
EXPOSE 8004

CMD ["bash", "-c", "DISABLE_MODEL_SOURCE_CHECK=True uvicorn main:app --host 0.0.0.0 --port 8004 --timeout-keep-alive 300 --workers ${UVICORN_WORKERS:-1}"]
