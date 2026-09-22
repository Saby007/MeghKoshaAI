ARG PYTHON_BUILD_IMAGE=cgr.dev/chainguard/python:latest-dev@sha256:b0bc807f4334fea6adaac0f4dfbde255b9938ca957facb26eaed8bb448fce473
ARG PYTHON_IMAGE=cgr.dev/chainguard/python:latest@sha256:b5decb00aa1cb65ab71bb3f6632a44bb8e6fd8d661de1f0342fd513a06837b9a
FROM ${PYTHON_BUILD_IMAGE} AS dependencies
USER 0
WORKDIR /build
COPY requirements.txt constraints.txt ./
RUN python -c 'import sys; assert sys.version_info[:3] == (3, 14, 7)' \
	&& python -m venv /build/venv \
	&& /build/venv/bin/python -m pip install --no-cache-dir --upgrade pip==26.2.1 \
	&& /build/venv/bin/python -m pip install --no-cache-dir --only-binary=:all: --target /opt/python --constraint constraints.txt --requirement requirements.txt \
	&& PYTHONPATH=/opt/python /build/venv/bin/python -m pip check

FROM ${PYTHON_IMAGE} AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	PYTHONPATH=/opt/python
COPY --from=dependencies /opt/python /opt/python
COPY --chown=65532:65532 main.py ./
COPY --chown=65532:65532 brand.py ./
COPY --chown=65532:65532 agents ./agents
COPY --chown=65532:65532 anomalies ./anomalies
COPY --chown=65532:65532 findings ./findings
COPY --chown=65532:65532 jobs ./jobs
COPY --chown=65532:65532 kql ./kql
COPY --chown=65532:65532 reports ./reports
COPY --chown=65532:65532 services ./services

USER 65532:65532
EXPOSE 8000
ENTRYPOINT []
CMD ["/usr/bin/python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
