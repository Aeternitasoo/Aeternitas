# Runtime tag is recorded with its actual image digest during release validation.
FROM python:3.14.4-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/state
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 1000 aeternitas \
    && useradd --uid 1000 --gid 1000 --home-dir /state --no-create-home aeternitas \
    && mkdir -p /data /synthetic /state/.config/aeternitas \
    && chown -R aeternitas:aeternitas /data /state /synthetic
# .dockerignore is an explicit source-file whitelist. No credentials, data/live,
# raw downloads, .git, local venv, arbitrary docs or home files enter the context.
COPY --chown=aeternitas:aeternitas . /app
USER 1000:1000
ENTRYPOINT ["python", "scripts/container_app.py"]
CMD ["api"]
