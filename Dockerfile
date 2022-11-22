# FROM debian:bullseye as base
FROM python:3.10-bullseye as base
RUN rm /bin/sh && ln -s /bin/bash /bin/sh
WORKDIR /workspace

RUN mkdir -pv "/workspace/bin" && echo 'echo test' > '/workspace/bin/test.sh' && chmod +x '/workspace/bin/test.sh'

ENV PATH="/workspace/bin:${PATH}"

FROM base as builder

# Install os deps
RUN apt update && \
    apt-get install -y build-essential clang cmake curl libudev-dev pkg-config && \
    rm -rf /var/lib/apt/lists/*

# Setup rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- --default-toolchain 1.59.0 -y
ENV PATH="/root/.cargo/bin:${PATH}"

ARG SOLANA_VERSION=1.14.7

# Get the solana source
RUN curl https://codeload.github.com/solana-labs/solana/tar.gz/refs/tags/v$SOLANA_VERSION | tar xvz
RUN mv /workspace/solana-$SOLANA_VERSION /workspace/solana

# Build the solana-test-validator
WORKDIR /workspace/solana

RUN ./cargo build --bin solana-test-validator --release
RUN mv target/release/solana-test-validator /workspace/bin/

# build cli solana for program dumps
RUN ./cargo build --bin solana --release
RUN mv target/release/solana /workspace/bin/

FROM base as final

## Install os deps
RUN apt-get -y update && \
    apt-get install -y bzip2 git && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /workspace/bin/* /workspace/bin/

COPY . /workspace/drift-clone/
WORKDIR /workspace/drift-clone
RUN pip install -r req.txt
RUN bash setup.sh

ENTRYPOINT bash run.sh