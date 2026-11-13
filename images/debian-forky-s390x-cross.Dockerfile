# The s390x cross compiler with s390x dev libs, and qemu-user to run what it builds: s390x is the
# big-endian target we test on.  forky rather than testing so that this doesn't move when forky
# releases.  No session.foundation repo: it publishes nothing for s390x.
FROM debian-forky-base
ARG APT
RUN dpkg --add-architecture s390x \
    && $APT update \
    && $APT --no-install-recommends install -y \
        automake \
        build-essential \
        ca-certificates \
        ccache \
        cmake \
        crossbuild-essential-s390x \
        eatmydata \
        file \
        git \
        libcli11-dev \
        libtool \
        make \
        ninja-build \
        nlohmann-json3-dev \
        patch \
        pkg-config \
        qemu-user \
        libevent-dev:s390x \
        libfmt-dev:s390x \
        libgnutls28-dev:s390x \
        libngtcp2-crypto-gnutls-dev:s390x \
        libngtcp2-dev:s390x \
        libprotobuf-dev:s390x \
        libsimdutf-dev:s390x \
        libsodium-dev:s390x \
        libspdlog-dev:s390x \
        libsqlite3-dev:s390x \
        libutf8proc-dev:s390x \
        libzstd-dev:s390x \
        nettle-dev:s390x \
        pkgconf:s390x \
    && rm -rf /var/lib/apt/lists/*
