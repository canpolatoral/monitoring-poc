# Python version must match the OpenTelemetry Operator's Python auto-instrumentation image (3.14, glibc).
FROM python:3.14-slim
RUN pip install --no-cache-dir flask==3.1.* requests==2.32.* psycopg2-binary==2.9.*
WORKDIR /app
COPY app.py .
USER 1000
EXPOSE 8080
CMD ["python", "app.py"]
