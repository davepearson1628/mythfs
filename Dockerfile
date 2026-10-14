FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# MythTV v36 Python bindings, FUSE3 and Trio.
RUN set -eux; \
    sed -i -E '/^Components:/ { \
      /multiverse/! s/$/ multiverse/; \
    }' /etc/apt/sources.list.d/ubuntu.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      fuse3 \
      libmyth-python \
      mythtv-common \
      python3-pyfuse3 \
      python3-trio \
      python3-requests \
      python3-lxml \
      python3-mysqldb; \
    rm -rf /var/lib/apt/lists/*

COPY mythfs.py /usr/local/bin/mythfs.py

RUN chmod 755 /usr/local/bin/mythfs.py \
    && python3 -m py_compile /usr/local/bin/mythfs.py \
    && python3 -c \
       "import pyfuse3, trio; from MythTV import MythDB; print('MythFS dependencies OK')"

COPY entrypoint.sh /usr/local/bin/mythfs-entrypoint.sh
RUN chmod 755 /usr/local/bin/mythfs-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/mythfs-entrypoint.sh"]
CMD ["--config", "/etc/mythfs.conf"]
