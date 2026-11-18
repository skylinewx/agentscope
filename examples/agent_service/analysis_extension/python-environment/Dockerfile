FROM python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
COPY requirements.lock /opt/analysis/requirements.lock
RUN python -m pip install --no-cache-dir --disable-pip-version-check --only-binary=:all: --require-hashes -r /opt/analysis/requirements.lock && python -m pip check
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
WORKDIR /tmp
