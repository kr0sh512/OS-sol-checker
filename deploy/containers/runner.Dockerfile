# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv
FROM python:3.12.10-slim-bookworm AS jail-build
RUN test "$(dpkg --print-architecture)" = amd64 \
    && apt-get update && apt-get install -y --no-install-recommends \
       gcc-12 g++ make git ca-certificates flex bison pkg-config protobuf-compiler \
       libprotobuf-dev libnl-route-3-dev libc6-dev libgcc-12-dev libcrypt-dev linux-libc-dev binutils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build/nsjail
# nsjail 3.4; the kafel gitlink is pinned transitively by this exact source commit.
RUN git init && git remote add origin https://github.com/google/nsjail.git \
    && git fetch --depth 1 origin 079d70dda4aa1edd9512cfd25ff1e47e316dc355 \
    && git checkout --detach FETCH_HEAD && git submodule update --init --recursive \
    && make -j2
COPY sandbox/check_policies.c /build/check_policies.c
COPY deploy/nsjail/*.policy /build/policies/
RUN gcc-12 -std=gnu17 -O2 -Wall -Wextra -Werror -I/build/nsjail/kafel/include \
       /build/check_policies.c /build/nsjail/kafel/libkafel.a -o /build/check-policies \
    && /build/check-policies /build/policies/compile.policy /build/policies/run.policy
COPY sandbox/jail_init.c sandbox/jail_probe.c /build/
RUN gcc-12 -std=gnu17 -O2 -Wall -Wextra -Werror -fPIE -pie -Wl,-z,relro,-z,now \
       /build/jail_init.c -o /build/checker-init \
    && gcc-12 -std=gnu17 -O2 -Wall -Wextra -Werror -fPIE -pie -Wl,-z,relro,-z,now \
       /build/jail_probe.c -o /build/checker-probe
COPY sandbox/build_rootfs.py /build/build_rootfs.py
RUN python /build/build_rootfs.py /build/rootfs

FROM python:3.12.10-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends \
       libprotobuf32 libnl-3-200 libnl-route-3-200 util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /work/runner /run/user /run/checker-cgroup /opt/checker
WORKDIR /opt/checker
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY migrations ./migrations
COPY deploy/container_entrypoint.py ./deploy/container_entrypoint.py
COPY deploy/nsjail ./deploy/nsjail
COPY --from=jail-build /build/nsjail/nsjail /usr/local/bin/nsjail
COPY --from=jail-build /build/rootfs /opt/checker/rootfs
COPY sandbox/build_manifest.py /build_manifest.py
RUN uv sync --frozen --no-dev --no-editable \
    && /opt/checker/.venv/bin/python /build_manifest.py /opt/checker/runner-manifest.json \
    && rm /build_manifest.py
ENV PATH="/opt/checker/.venv/bin:$PATH" PYTHONPATH=/opt/checker/src \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Root is only in the trusted outer container; jail UID/GID=65534 and capabilities=0.
USER 0:0
ENTRYPOINT ["/opt/checker/.venv/bin/python", "/opt/checker/deploy/container_entrypoint.py"]
CMD ["checker-runner"]
