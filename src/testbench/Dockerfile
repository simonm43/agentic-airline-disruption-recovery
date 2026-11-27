FROM python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1
COPY testbench/requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes --progress-bar off --quiet -r requirements.txt
COPY testbench/ ./testbench/
COPY tests/ /tests/
RUN PYTHONPATH=/app python -m unittest discover -s /tests -v
COPY check-requirements.txt /tmp/check-requirements.txt
RUN python -m pip install --target=/tmp/check-tools --require-hashes --progress-bar off --quiet -r /tmp/check-requirements.txt \
    && PYTHONPATH=/tmp/check-tools:/app python -m mypy --strict --ignore-missing-imports testbench \
    && PYTHONPATH=/tmp/check-tools:/app python -m ruff check --line-length 100 --select E,F,I testbench /tests \
    && PYTHONPATH=/tmp/check-tools:/app python -m ruff format --check --line-length 100 testbench /tests \
    && rm -rf /tmp/check-tools
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER 10001
EXPOSE 8080
CMD ["python", "-m", "testbench.app"]
