FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
WORKDIR /opt/decagent
COPY requirements.lock /opt/decagent/requirements.lock
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY decagent /opt/decagent/decagent
COPY ops/control/start.py /opt/decagent/start.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/decagent
ENTRYPOINT ["python", "/opt/decagent/start.py"]
CMD ["api"]
