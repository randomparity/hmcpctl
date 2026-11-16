FROM ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

ADD --checksum=sha256:6bac2a01979e210d9eac1d4d56747ec709ea60654744d66705dc3c36e7629e50 \
    https://snapshot.ubuntu.com/ubuntu/20260813T000000Z/pool/main/c/ca-certificates/ca-certificates_20260601~24.04.1_all.deb \
    /tmp/ca-certificates.deb

RUN ubuntu_snapshot=20260813T000000Z \
    && ubuntu_sources=/etc/apt/sources.list.d/ubuntu.sources \
    && old_uri=http://ports.ubuntu.com/ubuntu-ports/ \
    && snapshot_uri="https://snapshot.ubuntu.com/ubuntu/${ubuntu_snapshot}/" \
    && test "$(grep -Fxc "URIs: ${old_uri}" "${ubuntu_sources}" || true)" = 2 \
    && sed -i \
        "s|${old_uri}|${snapshot_uri}|" \
        "${ubuntu_sources}" \
    && ! grep -Fq "${old_uri}" "${ubuntu_sources}" \
    && test "$(grep -Fxc "URIs: ${snapshot_uri}" "${ubuntu_sources}" || true)" = 2 \
    && dpkg --unpack /tmp/ca-certificates.deb \
    && cat /usr/share/ca-certificates/mozilla/*.crt > /etc/ssl/certs/ca-certificates.crt \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive \
        apt-get install --yes --no-remove --no-install-recommends --fix-broken \
    && DEBIAN_FRONTEND=noninteractive \
        apt-get install --yes --no-remove --no-install-recommends \
        ca-certificates \
        curl \
        build-essential \
        git \
        just=1.21.0-1 \
        libssl-dev \
        pkg-config \
        python3 \
        python3-dev \
    && rm /tmp/ca-certificates.deb \
    && find /var/lib/apt/lists -mindepth 1 -delete

RUN uv_version=0.13.8 \
    && uv_sha256=4b7857be36535a31dc1e670b79d663bc278f1177e53d3e499e1ed300b72cbd14 \
    && archive="uv-powerpc64le-unknown-linux-gnu.tar.gz" \
    && curl --fail --location --proto '=https' --tlsv1.2 \
        --output "${archive}" \
        "https://github.com/astral-sh/uv/releases/download/${uv_version}/${archive}" \
    && echo "${uv_sha256}  ${archive}" | sha256sum --check --strict \
    && tar --extract --gzip --file "${archive}" \
    && install --mode 0755 uv-powerpc64le-unknown-linux-gnu/uv /usr/local/bin/uv \
    && rm --recursive "${archive}" uv-powerpc64le-unknown-linux-gnu

ENV PATH="/root/.cargo/bin:${PATH}"

RUN rustup_version=1.29.0 \
    && rustup_sha256=4bfff85bd3967d988e14567aa9cc6ab0ea386f0ffeff0f9f14d23f0103bf1f97 \
    && rust_version=1.97.1 \
    && rustup_init="rustup-init" \
    && curl --fail --location --proto '=https' --tlsv1.2 \
        --output "${rustup_init}" \
        "https://static.rust-lang.org/rustup/archive/${rustup_version}/powerpc64le-unknown-linux-gnu/rustup-init" \
    && echo "${rustup_sha256}  ${rustup_init}" | sha256sum --check --strict \
    && chmod 0755 "${rustup_init}" \
    && ./"${rustup_init}" --no-modify-path --profile minimal \
        --default-toolchain "${rust_version}" --default-host powerpc64le-unknown-linux-gnu -y \
    && rm "${rustup_init}"

WORKDIR /workspace

CMD ["bash", "-euo", "pipefail", "-c", \
    "architecture=$(uname -m) && echo \"runtime architecture: ${architecture}\" && test \"${architecture}\" = \"ppc64le\" && git config --global --add safe.directory /workspace && uv sync --locked --no-install-package prek && UV_NO_SYNC=1 just verify"]
