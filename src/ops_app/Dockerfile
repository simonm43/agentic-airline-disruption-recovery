FROM python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL -o /tmp/packages-microsoft-prod.deb https://packages.microsoft.com/config/debian/13/packages-microsoft-prod.deb \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && rm /tmp/packages-microsoft-prod.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18=18.7.1.1-1 libgssapi-krb5-2 \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1
COPY airline_systems/requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes --progress-bar off --quiet -r requirements.txt
COPY airline_systems/ ./airline_systems/
COPY ops_app/ ./ops_app/
COPY tests/ /tests/
RUN PYTHONPATH=/app python -m unittest discover -s /tests -v
COPY check-requirements.txt /tmp/check-requirements.txt
RUN python -m pip install --target=/tmp/check-tools --require-hashes --progress-bar off --quiet -r /tmp/check-requirements.txt \
    && PYTHONPATH=/tmp/check-tools:/app python -m mypy --strict --ignore-missing-imports airline_systems ops_app \
    && PYTHONPATH=/tmp/check-tools:/app python -m ruff check --line-length 100 --select E,F,I airline_systems ops_app /tests \
    && PYTHONPATH=/tmp/check-tools:/app python -m ruff format --check --line-length 100 airline_systems ops_app /tests \
    && rm -rf /tmp/check-tools
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER 10001
EXPOSE 8080
CMD ["python", "-m", "ops_app.app"]
