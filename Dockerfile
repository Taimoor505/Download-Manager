FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# JS runtime for yt-dlp's YouTube support
COPY --from=denoland/deno:bin-2.5.6 /deno /usr/local/bin/deno

WORKDIR /opt/vidgrab
COPY . .
RUN pip install --no-cache-dir ".[server]"

ENV VIDGRAB_DIR=/downloads
EXPOSE 8765
CMD ["vidgrab", "serve", "--host", "0.0.0.0", "--port", "8765"]
