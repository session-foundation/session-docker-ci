# The Android SDK and NDK.
FROM debian-testing-base
ARG APT
RUN /bin/bash -c 'sed -i "s/main/main non-free/g" /etc/apt/sources.list.d/debian.sources'
RUN $APT update \
    && $APT install --no-install-recommends -y \
        android-sdk \
        automake \
        ccache \
        cmake \
        curl \
        git \
        google-android-ndk-r29-installer \
        libtool \
        make \
        ninja-build \
        openjdk-21-jdk \
        openssh-client \
        patch \
        pkg-config \
        python3 \
        wget \
        xz-utils \
        zip \
    && update-java-alternatives -s java-1.21.0-openjdk-amd64 \
    && git clone https://github.com/Shadowstyler/android-sdk-licenses.git /tmp/android-sdk-licenses \
    && cp -a /tmp/android-sdk-licenses/*-license /usr/lib/android-sdk/licenses \
    && rm -rf /tmp/android-sdk-licenses /var/lib/apt/lists/*
