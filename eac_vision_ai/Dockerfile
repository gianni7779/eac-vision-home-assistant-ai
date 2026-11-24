FROM python:3.12-slim-bookworm

ARG BUILD_ARCH
RUN test "${BUILD_ARCH:-amd64}" = "amd64" \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir aiohttp==3.13.2 numpy==2.3.3 onnxruntime==1.30.0 opencv-python-headless==5.0.0.93

COPY bin/go2rtc_linux_amd64 /usr/local/bin/go2rtc
RUN echo '20b46c6910734cb05ecefb234066f36868ff19b42ba1418a7a32e13a3e035712  /usr/local/bin/go2rtc' | sha256sum -c - \
    && chmod 755 /usr/local/bin/go2rtc
COPY ai/yolox_tiny.onnx /opt/eac/ai/yolox_tiny.onnx
COPY ai/LICENSE-YOLOX.txt /opt/eac/ai/LICENSE-YOLOX.txt
COPY server.py core.py ai.py /opt/eac/
COPY web/ /opt/eac/web/
COPY run.sh /run.sh
RUN chmod 755 /run.sh
CMD ["/run.sh"]
