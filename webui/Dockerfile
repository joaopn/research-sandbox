FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends wget \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        aiohttp==3.9.5 \
        asyncssh==2.14.2

# Vendor xterm.js + addons at build time so the image carries no
# runtime internet dependency.
ARG XTERM_VERSION=5.3.0
ARG FIT_VERSION=0.8.0
ARG WEB_LINKS_VERSION=0.9.0
ARG WEBGL_VERSION=0.16.0
ARG SEARCH_VERSION=0.13.0
WORKDIR /app/static/vendor
RUN wget -q https://unpkg.com/xterm@${XTERM_VERSION}/lib/xterm.js \
 && wget -q https://unpkg.com/xterm@${XTERM_VERSION}/css/xterm.css \
 && wget -q https://unpkg.com/xterm-addon-fit@${FIT_VERSION}/lib/xterm-addon-fit.js \
 && wget -q https://unpkg.com/xterm-addon-web-links@${WEB_LINKS_VERSION}/lib/xterm-addon-web-links.js \
 && wget -q https://unpkg.com/xterm-addon-webgl@${WEBGL_VERSION}/lib/xterm-addon-webgl.js \
 && wget -q https://unpkg.com/xterm-addon-search@${SEARCH_VERSION}/lib/xterm-addon-search.js

WORKDIR /app
COPY server.py services.py ./
COPY static/index.html static/
COPY static/app.js static/
COPY static/style.css static/

# Per-workflow Explain docs (STAGE_WORKFLOW_EXPLAIN): render webui/explain/*.md
# (+ inline interactive SVG) into static HTML at build. Pure-Python (Python-
# Markdown) — no node, no Chromium. Pinned as an ARG: the webui image is built via
# `docker compose build webui` (scoped ./webui context), which does NOT thread
# versions.env, so this ARG is its named home (the XTERM_VERSION precedent). Runs
# BEFORE the chown below so /app/static/explain/* is webui-owned.
ARG MD_LIB_VERSION=3.7
RUN pip install --no-cache-dir markdown==${MD_LIB_VERSION}
COPY explain/ explain/
COPY render_explain.py ./
RUN python render_explain.py explain static/explain

RUN useradd -m -u 1000 webui \
 && mkdir -p /app/tls \
 && chown -R webui:webui /app
USER webui

EXPOSE 7777
CMD ["python", "-u", "server.py"]
