# Pinned by digest (eMudhra CA incident, Oct 2026). This python:3.12-slim
# (3.12.15, Debian trixie) ships ca-certificates 20250419+deb13u1, which drops
# "emSign ECC Root CA - G3"; the earlier 20250419 build trusted it. urllib
# verifies TLS against this system bundle. Move the pin on purpose, after
# checking the new image's ca-certificates.
FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

# Fixed UID. The container is started as "<uid>:<git_deploy_build_gid>" so
# that it shares the host's build-control group and can write the 2770
# triggers/ and state/ directories without them being world-writable.
# Keep in step with git_deploy_webhook_uid in roles/git_deploy/defaults.
RUN useradd --uid 10001 --create-home --shell /bin/false webhook
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bay_alert.py .
COPY app.py .

USER webhook
EXPOSE 9000
CMD ["python", "app.py"]
