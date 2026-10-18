# syntax=docker/dockerfile:1
# auditrail 平台镜像。镜像不包含 .env、token 或真实 runs/ 数据；
# 运行所需配置全部经环境变量注入，runs/ 与 platform/ 经卷挂载。
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 依赖层（利用构建缓存）：声明文件先行
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        "fastapi>=0.115" "uvicorn>=0.30" "python-dotenv>=1.0" "zstandard>=0.22" \
        "psycopg[binary]>=3.1" "redis>=5" "PyJWT>=2.8" "cryptography>=41"

# 应用代码（.dockerignore 排除 .env/runs/platform/node_modules/dist 等）
COPY auditrail ./auditrail
COPY backend ./backend
RUN pip install --no-cache-dir --no-deps .

# 非 root 运行（最小权限）
RUN useradd --create-home --uid 10001 auditrail && \
    mkdir -p /data/runs /data/platform && chown -R auditrail:auditrail /data
USER auditrail

ENV AUDITRAIL_RUNS_DIR=/data/runs \
    AUDITRAIL_PLATFORM_DIR=/data/platform \
    AUDITRAIL_LOAD_DOTENV=0

EXPOSE 18080

# 默认入口：API。worker / migration 由 compose 或编排器覆盖 command。
CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "18080"]
