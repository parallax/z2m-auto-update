FROM python:3.12-alpine

ARG BUILD_VERSION=dev
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="addon" \
      org.opencontainers.image.source="https://github.com/parallax/z2m-auto-update" \
      org.opencontainers.image.description="Queues Zigbee2MQTT OTA updates one at a time" \
      org.opencontainers.image.licenses="MIT"

# Pure-Python builds of aiohttp and friends so every architecture installs without a compiler.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AIOHTTP_NO_EXTENSIONS=1 \
    MULTIDICT_NO_EXTENSIONS=1 \
    YARL_NO_EXTENSIONS=1 \
    FROZENLIST_NO_EXTENSIONS=1 \
    PROPCACHE_NO_EXTENSIONS=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY z2m_auto_update ./z2m_auto_update

EXPOSE 8099
CMD ["python", "-m", "z2m_auto_update"]
