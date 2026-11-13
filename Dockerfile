# Every distro's DISTRO-base, DISTRO-builder and DISTRO images: the stages base, builder and final
# hold what each one installs.  The images built on one of them are in images/.
# rebuild-docker-images.py builds both with `docker buildx bake`, passes the build arguments, and
# adds the layer that upgrades the installed packages on top of every image it pushes.  No stage
# keeps apt's lists: they are stale by the time an image is used, and every consumer updates them.

ARG DISTRO_IMAGE


FROM ${DISTRO_IMAGE} AS base
ARG APT
# A new value rebuilds every image of the distro from the upstream image, instead of carrying its
# upgrades in the layer on top; changed once a week, so that layer stays small.
ARG PKGS_STATE
RUN /bin/bash -c 'echo "man-db man-db/auto-update boolean false" | debconf-set-selections'
RUN echo "packages as of ${PKGS_STATE}" \
    && $APT update \
    && $APT dist-upgrade -y \
    && $APT autoremove -y \
    && rm -rf /var/lib/apt/lists/*


# (distro)-builder: the tools for building debs, without what a package's debian/control pulls in.
FROM base AS builder
ARG APT
RUN $APT update \
    && $APT --no-install-recommends install -y \
        ccache \
        devscripts \
        equivs \
        g++ \
        git \
        git-buildpackage \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*


# (distro): the image most builds use, with most dependencies of our packages.
FROM builder AS final
ARG APT
# deb.session.foundation suite; @SUITE@ for ubuntu's lts/rolling, which are moving aliases the repo
# doesn't publish, so the image's own codename is used.  /etc/os-release rather than lsb_release,
# which isn't installed.
ARG SESSION_SUITE
COPY session-foundation.gpg /usr/share/keyrings/session-foundation.gpg
RUN suite="${SESSION_SUITE}" \
    && if [ "$suite" = "@SUITE@" ]; then . /etc/os-release && test -n "$VERSION_CODENAME" && suite="$VERSION_CODENAME"; fi \
    && printf 'Types: deb\nURIs: https://deb.session.foundation\nSuites: %s\nComponents: main\nSigned-By: /usr/share/keyrings/session-foundation.gpg\n' \
        "$suite" > /etc/apt/sources.list.d/session.sources
RUN $APT update \
    && $APT --no-install-recommends install -y \
        automake \
        ccache \
        cmake \
        curl \
        eatmydata \
        g++ \
        gdb \
        git \
        libboost-program-options-dev \
        libboost-serialization-dev \
        libboost-thread-dev \
        libcurl4-gnutls-dev \
        libevent-dev \
        libfmt-dev \
        libgnutls28-dev \
        libgtest-dev \
        libhidapi-dev \
        libjemalloc-dev \
        libminiupnpc-dev \
        libngtcp2-crypto-gnutls-dev \
        libngtcp2-dev \
        liboxenc-dev \
        liboxen-quic-dev \
        libprotobuf-dev \
        libreadline-dev \
        libsodium-dev \
        libspdlog-dev \
        libsqlite3-dev \
        libssl-dev \
        libsystemd-dev \
        libtool \
        libunbound-dev \
        libunwind8-dev \
        libusb-1.0.0-dev \
        libutf8proc-dev \
        libuv1-dev \
        libzmq3-dev \
        libzstd-dev \
        lsb-release \
        make \
        meson \
        nettle-dev \
        ninja-build \
        openssh-client \
        patch \
        pkg-config \
        postgresql-client \
        pybind11-dev \
        python3-coloredlogs \
        python3-cryptography \
        python3-dev \
        python3-flask \
        python3-nacl \
        python3-openssl \
        python3-pil \
        python3-pip \
        python3-protobuf \
        python3-psycopg2 \
        python3-pybind11 \
        python3-pycryptodome \
        python3-pytest \
        python3-qrencode \
        python3-setuptools \
        python3-sqlalchemy \
        python3-sqlalchemy-utils \
        python3-tabulate \
        python3-uwsgidecorators \
        qttools5-dev \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/*
