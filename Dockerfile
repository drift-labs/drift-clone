FROM python:3.10-buster
COPY . /drift-clone
RUN curl https://sh.rustup.rs -sSf | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"
RUN sh -c "$(curl -sSfL https://release.solana.com/v1.14.7/install)"
ENV PATH="/root/.local/share/solana/install/active_release/bin:${PATH}"
WORKDIR /drift-clone 
RUN pip install -r req.txt
RUN bash setup.sh
