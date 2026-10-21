# Pinned by digest (eMudhra CA incident, Oct 2026). This python:3.12-slim
# (3.12.15, Debian trixie) ships ca-certificates 20250419+deb13u1, which drops
# "emSign ECC Root CA - G3"; the earlier 20250419 build trusted it. urllib
# verifies TLS against this system bundle. Move the pin on purpose, after
# checking the new image's ca-certificates.
FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

RUN useradd --create-home --shell /bin/false identity
WORKDIR /app
COPY app.py .

USER identity
EXPOSE 9100
CMD ["python", "app.py"]
