FROM python:3.10-slim-buster

ENV LANG C.UTF-8
ENV TZ=Asia/Seoul

COPY apps /apps

WORKDIR /share

CMD ["python", "/apps/main.py"]
