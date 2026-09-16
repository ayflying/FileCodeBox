# 第一阶段：构建前端主题
# 前端产物与 CPU 架构无关，使用原生构建平台避免在 QEMU 中运行 Node/pnpm。
FROM --platform=$BUILDPLATFORM node:20-alpine AS frontend-builder

# 前端主题按 commit 钉死，禁止浮动（与 docker-image.yml 中的同名变量保持一致）。
#
# 2024 主题已迁至本 fork 自维护：ayflying/FileCodeBoxFronted（公开仓库）。
# 基线是「snake_case 迁移之前」的最后一个可用版本 2f0a04d2a5c1（2026-09-07），
# 在其之上增加了 P2P 直传分享与取件功能（ec15a1b，2026-09-16）。
# 本仓库后端仍使用 camelCase 的公开配置契约（uploadSize / allowedFileTypes /
# expireStyle / enableChunk / openUpload ...），而上游前端自 2026-09-09 起（commit
# c6b6b64869「rename config keys to snake_case」）改为只读 snake_case 字段，配套的是
# 上游后端 2.6.0 的契约迁移。若继续跟随上游 main，前端拿不到 expire_style，
# /#/send 会在 setup 阶段直接抛
#   TypeError: Cannot read properties of undefined (reading '0')
# 页面白屏。待后端契约同步到上游 2.6.0+ 后再考虑解除该限制。
#
# 2023 主题保持上游钉版：5d5e77d97b42 = 2025-03-02（上游 2026-09-14 才切 snake_case）。
ARG FRONTEND_2024_REF=ec15a1b1ffe1cd83ad6e9b7ca541e5cca748199e
ARG FRONTEND_2023_REF=5d5e77d97b4278bfd543b26e50ed13e5fbf72452

RUN apk add --no-cache git python3 make g++

RUN corepack enable && \
    corepack prepare pnpm@9.15.9 --activate

WORKDIR /build

# 克隆并构建固定版本的 2024 主题（本 fork 自维护，含 P2P 直传功能）
RUN git clone --filter=blob:none --no-checkout https://github.com/ayflying/FileCodeBoxFronted.git /build/fronted-2024 && \
    cd /build/fronted-2024 && \
    git fetch --depth 1 origin "${FRONTEND_2024_REF}" && \
    git checkout --detach FETCH_HEAD && \
    pnpm install --frozen-lockfile --prod=false && \
    VITE_GIT_COMMIT="$(git rev-parse HEAD)" pnpm run build

# 克隆并构建固定版本的 2023 主题
RUN git clone --filter=blob:none --no-checkout https://github.com/vastsa/FileCodeBoxFronted2023.git /build/fronted-2023 && \
    cd /build/fronted-2023 && \
    git fetch --depth 1 origin "${FRONTEND_2023_REF}" && \
    git checkout --detach FETCH_HEAD && \
    npm install --legacy-peer-deps && \
    npm run build

# 第二阶段：构建最终镜像
FROM python:3.12-slim-bookworm
ARG APP_VERSION
ARG VCS_REF=unknown
# 默认值与 frontend-builder 阶段一致，仅用于写入镜像 LABEL；CI 会显式传入。
ARG FRONTEND_2024_REF=ec15a1b1ffe1cd83ad6e9b7ca541e5cca748199e
ARG FRONTEND_2023_REF=5d5e77d97b4278bfd543b26e50ed13e5fbf72452
LABEL author="Lan"
LABEL email="xzu@live.com"
LABEL org.opencontainers.image.version="${APP_VERSION}"
LABEL org.opencontainers.image.revision="${VCS_REF}"
LABEL org.opencontainers.image.filecodebox.frontend-2024-revision="${FRONTEND_2024_REF}"
LABEL org.opencontainers.image.filecodebox.frontend-2023-revision="${FRONTEND_2023_REF}"

WORKDIR /app

# 复制项目文件（通过 .dockerignore 排除不必要的文件）
COPY . .

# 分支镜像使用带提交号的开发版本；正式镜像使用 VERSION 中的版本。
ENV APP_VERSION="${APP_VERSION}"

# 设置时区
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo 'Asia/Shanghai' > /etc/timezone

# 从构建阶段复制编译好的前端主题
COPY --from=frontend-builder /build/fronted-2024/dist ./themes/2024
COPY --from=frontend-builder /build/fronted-2023/dist ./themes/2023

# 安装系统安全更新 + Python 依赖
# 清理 apt 缓存，降低镜像噪音与扫描面
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir -r requirements.txt \
 && pip cache purge || true

# 环境变量配置
ENV HOST="0.0.0.0" \
    PORT=12345 \
    WORKERS=1 \
    APP_ENV="production" \
    LOG_LEVEL="warning" \
    ACCESS_LOG="false" \
    FORWARDED_ALLOW_IPS=""

EXPOSE 12345

# 生产环境启动命令
# FORWARDED_ALLOW_IPS 默认为空：仅信任直连 IP，避免任意客户端伪造 X-Forwarded-*。
# 若前面有反向代理，请显式设置为代理网段，例如 "10.0.0.0/8,172.16.0.0/12"。
CMD ["sh", "-c", "access_log_arg=--no-access-log; if [ \"${APP_ENV:-development}\" != \"production\" ] || [ \"${ACCESS_LOG:-false}\" = \"true\" ]; then access_log_arg=--access-log; fi; exec uvicorn main:app --host \"$HOST\" --port \"$PORT\" --workers \"$WORKERS\" --log-level \"$LOG_LEVEL\" \"$access_log_arg\" --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-}\""]
