FROM python:3.14-slim
WORKDIR /app
COPY mock.py faultctl.py ./
USER 1000
EXPOSE 8080 8443 5432 9000
CMD ["python", "mock.py"]
