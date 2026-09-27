FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates ./templates
RUN mkdir -p /data
ENV PYTHONUNBUFFERED=1
EXPOSE 5000
CMD ["sh", "-c", "python -c 'import os,pathlib; p=pathlib.Path(\"/app/templates/index.html\"); p.write_text(p.read_text().replace(\"__TIME_ZONE__\", os.getenv(\"TZ\", \"UTC\")))' && exec python app.py"]
