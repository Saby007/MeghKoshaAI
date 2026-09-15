ARG API_IMAGE
FROM cgr.dev/chainguard/python:latest-dev@sha256:b0bc807f4334fea6adaac0f4dfbde255b9938ca957facb26eaed8bb448fce473 AS test-dependencies
USER 0
WORKDIR /build
COPY constraints.txt ./
RUN python -c 'import sys; assert sys.version_info[:3] == (3, 14, 7)' \
	&& python -m venv /build/venv \
	&& /build/venv/bin/python -m pip install --no-cache-dir --upgrade pip==26.2.1 \
	&& /build/venv/bin/python -m pip install --no-cache-dir --only-binary=:all: --target /opt/test-packages --constraint constraints.txt pytest pypdf pytest-socket

FROM ${API_IMAGE}
COPY --from=test-dependencies /opt/test-packages /opt/test-packages
COPY --chown=65532:65532 tests /app/tests
COPY --chown=65532:65532 pytest.ini /app/pytest.ini
WORKDIR /app
ENV PYTHONPATH=/opt/python:/opt/test-packages:/app \
	MEGHKOSHA_AI_ENABLED=false \
	AZURE_TENANT_ID=11111111-1111-1111-1111-111111111111 \
	MEGHKOSHA_API_CLIENT_ID=33333333-3333-3333-3333-333333333333 \
	MEGHKOSHA_WEB_CLIENT_ID=44444444-4444-4444-4444-444444444444
USER 65532:65532
ENTRYPOINT []
CMD ["/usr/bin/python", "-m", "pytest", "tests", "--ignore=tests/test_packaging.py", "-p", "no:cacheprovider", "-q"]