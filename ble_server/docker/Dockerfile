FROM python:3.11-slim

LABEL org.opencontainers.image.title="CUKTECH BLE Server"
LABEL org.opencontainers.image.description="CUKTECH 10 GaN Charger BLE Server for Home Assistant"
LABEL org.opencontainers.image.source="https://github.com/kairui1108/cuktech-ble-ha"

# 使用阿里云 Debian 镜像源加速构建
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null \
    || true

RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    dbus \
    tzdata \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the cuktech_ble package + all dependencies from pyproject.toml
COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple .

COPY web/ web/
COPY state.py state_protocol_v2.py history.py config.py ble_manager.py ha_server.py bemfa_client.py energy.py downsample.py xiaomi_cloud.py ./

RUN mkdir -p /data

# Copy default config (user can override by mounting their own)
COPY config.yaml.example ./config.yaml

EXPOSE 8199

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
