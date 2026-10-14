# Fetch the MythTV movie grabber without installing mythtv-common
# or its extensive dependencies in the final runtime image.
FROM ubuntu:26.04 AS grabber

ENV DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
    sed -i -E '/^Components:/ { \
      /multiverse/! s/$/ multiverse/; \
    }' /etc/apt/sources.list.d/ubuntu.sources; \
    apt-get update; \
    mkdir -p /grabber; \
    cd /tmp; \
    apt-get download mythtv-common; \
    dpkg-deb -x mythtv-common_*.deb /grabber; \
    test -s /grabber/usr/share/mythtv/metadata/Movie/tmdb3.py

# The runtime only needs the MythTV Python bindings,
# FUSE3, Trio and the movie grabber.
FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN set -eux; \
    sed -i -E '/^Components:/ { \
      /multiverse/! s/$/ multiverse/; \
    }' /etc/apt/sources.list.d/ubuntu.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      fuse3 \
      libmyth-python \
      python3-pyfuse3 \
      python3-trio; \
    rm -rf /var/lib/apt/lists/*

COPY mythfs.py /usr/local/bin/mythfs.py
COPY entrypoint.sh /usr/local/bin/mythfs-entrypoint.sh

COPY --from=grabber \
    /grabber/usr/share/mythtv/metadata/Movie/tmdb3.py \
    /usr/share/mythtv/metadata/Movie/tmdb3.py

RUN chmod 755 \
      /usr/local/bin/mythfs.py \
      /usr/local/bin/mythfs-entrypoint.sh \
      /usr/share/mythtv/metadata/Movie/tmdb3.py \
    && python3 -m py_compile /usr/local/bin/mythfs.py \
    && python3 -c \
      "import pyfuse3, trio; from MythTV import MythDB; import MythTV.tmdb3; print('MythFS dependencies OK')" \
    && python3 /usr/share/mythtv/metadata/Movie/tmdb3.py -t

ENTRYPOINT ["/usr/local/bin/mythfs-entrypoint.sh"]
CMD ["--config", "/etc/mythfs.conf"]
