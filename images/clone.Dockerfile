# The git clone step CI pipelines use, through gitcache.
FROM debian:bookworm-slim
ARG APT
ARG PKGS_STATE
RUN /bin/bash -c 'echo "man-db man-db/auto-update boolean false" | debconf-set-selections'
RUN echo "packages as of ${PKGS_STATE}" \
    && $APT update \
    && $APT dist-upgrade -y \
    && $APT install -y --no-install-recommends git git-lfs python3 python3-pip python3-portalocker python3-pytimeparse python3-coloredlogs bash \
    && pip3 install --break-system-packages https://github.com/seeraven/gitcache/releases/download/v1.0.22/gitcache-1.0.22-py3-none-any.whl \
    && ln -s gitcache /usr/local/bin/git \
    && $APT remove --autoremove --purge -y python3-pip \
    && $APT clean \
    && rm -rf /var/lib/apt/lists/*
ADD clone-scripts/* /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/clone"]
