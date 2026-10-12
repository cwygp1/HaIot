FROM python:3.11-slim-bookworm

ENV LANG=C.UTF-8
ENV TZ=Asia/Seoul

RUN pip install --no-cache-dir "paho-mqtt>=2.0,<3"

COPY run.sh /
COPY pycommax /pycommax

WORKDIR /share

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
