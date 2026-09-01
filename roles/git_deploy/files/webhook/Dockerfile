FROM python:3.12-slim

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
