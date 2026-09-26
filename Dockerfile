FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates ./templates
RUN mkdir -p /data
ENV PYTHONUNBUFFERED=1
EXPOSE 5000
CMD ["python", "app.py"]
