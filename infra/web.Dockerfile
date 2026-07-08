# Admin SPA (apps/web) — built with Vite, served by nginx as static files.
# The public edge (BaoTa nginx for chat.chilling.com.hk) reverse-proxies /api,
# /ws, /hooks, /s, /js, /widget-app to the backend containers; this container
# only serves the SPA shell + hashed assets with a history-fallback to
# index.html. The SPA calls the API same-origin (/api/v1, /ws/agent), so no
# build-time API base is needed.
FROM node:20-alpine AS build
WORKDIR /w
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web ./
RUN npm run build

FROM nginx:alpine
COPY infra/nginx/web.conf /etc/nginx/conf.d/default.conf
COPY --from=build /w/dist /usr/share/nginx/html
EXPOSE 80
