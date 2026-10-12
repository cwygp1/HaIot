FROM python:3.10-slim-buster

ENV LANG C.UTF-8
ENV TZ=Asia/Seoul

RUN pip install --no-cache-dir paho-mqtt==1.6.1

COPY apps /apps

WORKDIR /share

CMD ["python", "/apps/main.py"]
