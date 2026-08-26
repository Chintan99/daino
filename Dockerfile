FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY daino ./daino
COPY vasuki ./vasuki
RUN pip install --no-cache-dir .
ENTRYPOINT ["daino"]
CMD ["--help"]
