FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /srv/smartchat

COPY pyproject.toml README.md ./
COPY packages ./packages
COPY apps ./apps
COPY fixtures ./fixtures

RUN pip install -e ./packages/py_contracts -e .

# Single image, five entrypoints — command set by compose:
#   api / ws-gateway / worker / beat / flow-engine / edge
EXPOSE 8000 8001 8002
CMD ["uvicorn", "apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
