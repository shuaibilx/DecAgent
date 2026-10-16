FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
COPY ops/runner/requirements.lock /opt/runner/requirements.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /opt/runner/requirements.lock \
    && mkdir -p /workspace /baseline /opt/runner/decagent/policy \
    && touch /opt/runner/decagent/__init__.py /opt/runner/decagent/policy/__init__.py \
    && chown 10001:10001 /workspace /baseline
COPY decagent/policy/redaction.py decagent/policy/paths.py /opt/runner/decagent/policy/
COPY ops/runner/runner.py ops/runner/workspace.py ops/runner/boundary_probe.py ops/runner/snapshot.py /opt/runner/
COPY decagent/tools/repair/check_runner.py /opt/runner/decagent_check.py
LABEL decagent.runner.version="1" decagent.runner.python="3.11"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/runner \
    HOME=/tmp/home TMPDIR=/tmp PIP_DISABLE_PIP_VERSION_CHECK=1 UV_NO_MANAGED_PYTHON=1 \
    UV_CACHE_DIR=/workspace/.cache/uv
USER 10001:10001
WORKDIR /workspace
CMD ["python", "-c", "print('DecAgent runner; commands require broker')"]
