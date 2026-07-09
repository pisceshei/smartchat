# --- visitor widget build (loader.js + iframe chat app) ---
# Built here so the api container can serve /js/project_{key}.js and
# /widget-app/ in a single-container-capable deployment. dist/ is gitignored,
# so it must be produced at image-build time, never copied from the context.
FROM node:20-alpine AS widget-build
WORKDIR /w
COPY apps/widget/package.json apps/widget/package-lock.json ./
RUN npm ci
COPY apps/widget ./
# full build = loader + chat + size-check.mjs (25KB loader / 120KB chat gzip
# budget gate) so an oversized bundle fails the image build instead of shipping
RUN npm run build

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /srv/smartchat

COPY pyproject.toml README.md ./
COPY packages ./packages
COPY apps ./apps

RUN pip install -e ./packages/py_contracts -e .

# Freshly-built widget assets (overrides anything from the context).
COPY --from=widget-build /w/dist ./apps/widget/dist

# Single image, five entrypoints — command set by compose:
#   api / ws-gateway / worker / beat / flow-engine / edge
EXPOSE 8000 8001 8002
CMD ["uvicorn", "apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
