FROM python:3.14-slim
WORKDIR /app
COPY server.py ./
COPY static ./static
USER 1000
EXPOSE 8088
CMD ["python", "server.py"]
