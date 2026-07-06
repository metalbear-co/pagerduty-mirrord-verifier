FROM python:3.12-slim
WORKDIR /srv
COPY app/ /srv/app/
RUN pip install --no-cache-dir flask==3.0.3 httpx==0.27.2 prometheus_client==0.21.0
EXPOSE 8080
CMD ["python", "-m", "app", "serve"]
