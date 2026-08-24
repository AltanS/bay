FROM python:3.12-slim

RUN useradd --create-home --shell /bin/false webhook
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bay_alert.py .
COPY app.py .

USER webhook
EXPOSE 9000
CMD ["python", "app.py"]
