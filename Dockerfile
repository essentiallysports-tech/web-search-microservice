# syntax=docker/dockerfile:1.7
#
# One build knob:
#
#   EXTRAS — pip extras to install. Default "[llm]" for the /research endpoint.
#            Use "[]" for a build that cannot synthesize at all.
#
# There is no browser in this image and no option to add one. The extraction
# ladder is trafilatura -> http_retry -> firecrawl: two free HTTP tiers, then a
# managed scraper that renders from its own egress IP.
#
# A headless-Chromium tier used to live here behind an INSTALL_BROWSER flag. It
# was deleted rather than left switched off, because its measured wins were
# `blocked` rescues rather than `empty` ones — and beating a bot wall is a
# property of the egress address, not of the renderer, so those wins erode on a
# datacenter IP exactly where they would be needed. Firecrawl scrapes from its
# own address, so blocking converts into cost instead of failure.
#
# What that removal bought, measured: 2.22GB -> 366MB, no ~1GB-per-context RAM
# ceiling, no /dev/shm sizing, no Playwright version drift, no zombie contexts,
# and extraction success 15/18 -> 18/18. It costs +$0.17 per 1k requests.

ARG PYTHON_VERSION=3.12

# --------------------------------------------------------------------- base
FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv

# ------------------------------------------------------------------ builder
FROM base AS builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

ARG EXTRAS="[llm]"
# pyproject is the single source of dependency truth. The pip cache mount keeps
# rebuilds fast even though app/ is copied before the install.
COPY pyproject.toml README.md ./
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install ".${EXTRAS}"

# Drop build-time-only weight: pip itself, and the bytecode pip wrote despite
# PYTHONDONTWRITEBYTECODE (which only governs *our* interpreter's writes).
#
# This works from the builder stage specifically. Deleting files in a later RUN of
# the *runtime* stage only writes a whiteout — the bytes stay in the earlier layer
# and the image does not shrink at all. `COPY --from` below copies the result of
# this prune rather than its history, which is what makes it real.
RUN python -m pip uninstall -y pip setuptools wheel 2>/dev/null || true \
 && find /opt/venv -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true \
 && find /opt/venv -type f -name "*.pyc" -delete 2>/dev/null || true

# Fail the build, not a live request, if the app cannot import what it needs.
RUN python -c "import app.main; print('app imports OK')"

# ------------------------------------------------------------------ runtime
FROM base AS runtime

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Nothing reachable from a running container needs documentation, man pages, or
# locale data.
RUN rm -rf /var/lib/apt/lists/* /var/cache/apt/* \
           /usr/share/doc /usr/share/man /usr/share/info \
           /usr/share/groff /usr/share/lintian /usr/share/locale

COPY app ./app
COPY pyproject.toml README.md ./

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=4).status==200 else 1)"

# One worker per container by default: it keeps Prometheus counters accurate
# (no multiprocess collector needed) and makes `--scale api=N` the scaling story.
ENV WEB_CONCURRENCY=1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]
