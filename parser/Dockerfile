# The parser container of this project: the cadmus-parser library (lib/cadmus-parser: the wrapper, the uid pool and the default processors) plus the
# format tools those processors shell out to. The tools run only inside the sandboxed parser processes.
# Build context: the repo root (docker build -f parser/Dockerfile .).
FROM debian:bookworm-slim
ENV PATH=/venv/bin:$PATH
ADD lib/cadmus-parser /cadmus
RUN apt-get update \
 && apt-get install -y --no-install-recommends unar libimage-exiftool-perl ffmpeg tesseract-ocr tesseract-ocr-nor tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/* \
 && /cadmus/server/install.sh s3,files
# GCS support after the processors: blackboxprotobuf pins a protobuf that gcsfs refuses (pip only warns)
RUN pip install --no-cache-dir "/cadmus/server[gcs]" && chmod -R a+rX /venv
VOLUME /scratch
EXPOSE 8080
ENTRYPOINT ["tini", "--", "/cadmus/server/entrypoint.sh"]
