FROM python:3.9-alpine

RUN           mkdir /app
COPY          requirements.txt /app

RUN apk update && \
 apk add postgresql-libs && \
 apk add --virtual .build-deps gcc musl-dev postgresql-dev && \
 pip install -r /app/requirements.txt --no-cache-dir && \
 apk --purge del .build-deps

COPY          app.py /app

ENTRYPOINT    ["python", "/app/app.py"]
