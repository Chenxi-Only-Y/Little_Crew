# 导演台（React + Vite）生产镜像 —— 多阶段构建
#
# 为什么单独一个 Dockerfile：
#   根 Dockerfile 是 Python 后端镜像。前端要构建得先有 Node，
#   把两者塞进一个镜像只会让后端镜像多背一个 Node 运行时与整套 devDeps。
#
# 为什么用 nginx 托管、而不是让 FastAPI 直接服务 dist：
#   FastAPI 的鉴权是**全局中间件**（`auth_guard` 拦截除白名单外的所有路径）。
#   若由它托管静态文件，未登录访问页面会先被 401 挡住、连登录页都看不到。
#   静态资源与 API 分给 nginx 处理，边界更清楚。
#
# 注意：构建上下文是**仓库根**（不是 web/），因为要 COPY web/package.json。
#   见 docker-compose.yml 里 frontend 服务的 `context: .`。
#
# ⚠️ 网络：本仓库所在环境**无法访问 registry-1.docker.io**（实测超时），
#   因此本机 `docker compose build` 会失败在拉取基础镜像这一步——
#   与 Dockerfile 本身无关（未经改动的根 Dockerfile 同样失败）。
#   需要配置镜像加速器/代理后重试。npm 依赖源同理，可用下面的
#   `--build-arg NPM_REGISTRY=...` 指向可达的源（如 registry.npmmirror.com）。

# ---------- 阶段 1：构建 ----------
FROM node:22-alpine AS build

# npm 源可覆盖：国内网络下官方源可能不可达。
# 默认留官方源，避免替使用者决定用哪个镜像。
ARG NPM_REGISTRY=https://registry.npmjs.org/
RUN npm config set registry "$NPM_REGISTRY"

# 固定 pnpm 版本：pnpm-lock.yaml 是 lockfileVersion 9.0，与 11.x 一致。
# 不用 corepack —— 它在新版 Node 里已被移除/需单独启用，直接 npm 装更稳。
RUN npm install -g pnpm@11.22.0

WORKDIR /app

# 必须设 CI：`pnpm run build` 会先做依赖状态检查，发现 node_modules 与
# 锁文件不一致时想重建 modules 目录；而 Docker 构建里没有 TTY 可以确认，
# 于是直接中止：
#   [ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY] Aborted removal of modules
#   directory due to no TTY
# 设 CI=true 后 pnpm 走非交互路径（这也是 pnpm 在报错里给的指引）。
ENV CI=true

# 先只拷清单：依赖没变时这一层可复用缓存，改源码不会重装依赖
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY web/ ./
RUN pnpm run build

# ---------- 阶段 2：运行 ----------
FROM nginx:alpine

COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# nginx:alpine 自带前台运行；健康检查用的 wget 也在镜像里
CMD ["nginx", "-g", "daemon off;"]
