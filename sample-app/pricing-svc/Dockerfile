FROM python:3.12-slim
WORKDIR /srv
COPY app.py /srv/app.py
RUN pip install --no-cache-dir flask==3.0.3
EXPOSE 8080
CMD ["python", "app.py"]
