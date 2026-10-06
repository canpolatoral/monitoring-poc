FROM python:3.14-slim
RUN pip install --no-cache-dir requests==2.32.*
WORKDIR /app
COPY loadgen.py .
USER 1000
CMD ["python", "loadgen.py"]
