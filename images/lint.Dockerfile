# The formatting tools.
FROM debian-bookworm-base
ARG APT
RUN $APT update \
    && $APT install --no-install-recommends -y \
        clang-format-14 \
        clang-format-15 \
        clang-format-16 \
        clang-format-19 \
        eatmydata \
        git \
        jsonnet \
    && rm -rf /var/lib/apt/lists/*
