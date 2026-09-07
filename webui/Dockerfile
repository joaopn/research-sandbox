FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends wget \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        aiohttp==3.9.5 \
        asyncssh==2.14.2

# Vendor xterm.js + addons at build time so the image carries no
# runtime internet dependency.
# Packages live under the @xterm/ npm scope since 5.4 (the unscoped `xterm`
# name stopped at 5.3.0). 6.0.0 is the first release carrying the IME
# composition fix (xterm.js #5024) behind duplicated/garbled input from
# predictive soft keyboards; its viewport rewrite is what style.css's
# scrollbar rules and app.js's touch scrolling are written against.
ARG XTERM_VERSION=6.0.0
ARG FIT_VERSION=0.11.0
ARG WEB_LINKS_VERSION=0.12.0
ARG WEBGL_VERSION=0.19.0
ARG SEARCH_VERSION=0.16.0
WORKDIR /app/static/vendor
RUN wget -q https://unpkg.com/@xterm/xterm@${XTERM_VERSION}/lib/xterm.js \
 && wget -q https://unpkg.com/@xterm/xterm@${XTERM_VERSION}/css/xterm.css \
 && wget -q https://unpkg.com/@xterm/addon-fit@${FIT_VERSION}/lib/addon-fit.js \
 && wget -q https://unpkg.com/@xterm/addon-web-links@${WEB_LINKS_VERSION}/lib/addon-web-links.js \
 && wget -q https://unpkg.com/@xterm/addon-webgl@${WEBGL_VERSION}/lib/addon-webgl.js \
 && wget -q https://unpkg.com/@xterm/addon-search@${SEARCH_VERSION}/lib/addon-search.js

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

# /app/state must exist in the image (before the chown) so the rs-webui-state
# named volume copy-ups uid-1000 ownership like rs-webui-tls does — a volume
# mounted at a path absent from the image initializes root-owned and the
# non-root webui process could never persist its origin-port map.
RUN useradd -m -u 1000 webui \
 && mkdir -p /app/tls /app/state \
 && chown -R webui:webui /app
USER webui

EXPOSE 7777
CMD ["python", "-u", "server.py"]
