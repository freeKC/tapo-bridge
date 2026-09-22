FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pytapo aiofiles aiohttp

COPY bridge.py onvif_service.py /app/
WORKDIR /app

EXPOSE 8080
ENTRYPOINT ["python", "-u", "/app/bridge.py"]
