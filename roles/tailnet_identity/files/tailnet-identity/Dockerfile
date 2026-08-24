FROM python:3.12-slim

RUN useradd --create-home --shell /bin/false identity
WORKDIR /app
COPY app.py .

USER identity
EXPOSE 9100
CMD ["python", "app.py"]
