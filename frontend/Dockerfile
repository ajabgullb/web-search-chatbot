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
# nginx:alpine already runs as the unprivileged `nginx` user.
FROM nginx:1.27-alpine AS runtime
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
USER nginx
HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --quiet --spider http://127.0.0.1:8080/ || exit 1
CMD ["nginx", "-g", "daemon off;"]
