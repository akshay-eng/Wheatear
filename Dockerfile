FROM node:22-bookworm-slim AS ui-build

WORKDIR /src/UI
COPY UI/package.json UI/package-lock.json ./
RUN npm ci
COPY UI/ ./
RUN npm run build


FROM mcr.microsoft.com/dotnet/sdk:9.0-bookworm-slim AS pac-build

RUN dotnet tool install \
    --tool-path /opt/pac \
    Microsoft.PowerApps.CLI.Tool \
    --version 1.52.1


FROM mcr.microsoft.com/dotnet/aspnet:9.0-bookworm-slim AS dotnet-runtime


FROM python:3.12-slim-bookworm AS python-build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src
COPY engine/pyproject.toml engine/README.md ./engine/
COPY engine/wheatear ./engine/wheatear
COPY engine/assets ./engine/assets
RUN python -m pip wheel --wheel-dir /wheels './engine[anthropic,google,copilot-studio]'


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    WHEATEAR_HOST=0.0.0.0 \
    WHEATEAR_UI_DIR=/app/UI/dist \
    WHEATEAR_ASSETS_DIR=/app/assets \
    DOTNET_ROOT=/usr/share/dotnet \
    PORT=8080 \
    HOME=/home/wheatear \
    PATH=/opt/pac:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin wheatear

RUN apt-get update \
    && apt-get install -y --no-install-recommends libicu72 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-build /wheels /wheels
RUN python -m pip install /wheels/* \
    && rm -rf /wheels

WORKDIR /app
COPY --from=dotnet-runtime /usr/share/dotnet /usr/share/dotnet
COPY --from=pac-build /opt/pac /opt/pac
COPY --from=ui-build /src/UI/dist /app/UI/dist
COPY engine/assets /app/assets

RUN mkdir -p /home/wheatear/.config /home/wheatear/.local/share \
    && chown -R wheatear:wheatear /home/wheatear /app

USER wheatear

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"

CMD ["wheatear-web"]
