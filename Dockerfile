FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/teleroute

RUN groupadd --system teleroute \
    && useradd --system --gid teleroute --home-dir /nonexistent --shell /usr/sbin/nologin teleroute

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=teleroute:teleroute app ./app
COPY --chown=teleroute:teleroute scripts ./scripts
COPY --chown=teleroute:teleroute docs ./docs

USER teleroute

EXPOSE 8000

CMD ["python", "scripts/run_production.py"]
