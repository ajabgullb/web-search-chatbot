# syntax=docker/dockerfile:1

FROM node:24-alpine AS base
WORKDIR /app
ENV CI=true

# ---- dependencies -----------------------------------------------------------
# `npm ci` installs strictly from package-lock.json and fails if it drifts.
FROM base AS deps
COPY package.json package-lock.json ./
RUN npm ci

# ---- development ------------------------------------------------------------
# Source is bind-mounted by Compose; only node_modules is baked in.
FROM base AS dev
COPY --from=deps /app/node_modules ./node_modules
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# ---- build ------------------------------------------------------------------
FROM base AS build
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---- production -------------------------------------------------------------
# The unprivileged variant of the official image: it runs as uid 101 with its
# pid and temp files in writable paths. Stock nginx:alpine under `USER nginx`
# cannot start, because its master process writes to root-owned paths.
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
# Where /api/ is proxied, and the DNS server that resolves it (Docker's
# embedded resolver). Override both outside Docker Compose.
ENV API_UPSTREAM=http://api:8000 \
    NGINX_RESOLVER=127.0.0.11
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY nginx/security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --quiet --spider http://127.0.0.1:8080/ || exit 1
