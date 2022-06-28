From ubuntu:focal

RUN echo -n "deb http://security.ubuntu.com/ubuntu bionic-security main" >> /etc/apt/sources.list
RUN apt-get update && apt-get install -y python3-pip aptitude
RUN aptitude install -y build-essential vim libimage-exiftool-perl 
RUN apt-get update && apt-cache policy libssl1.0-dev && apt-get install -y libssl1.0-dev

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get install -y --no-install-recommends ffmpeg
ENV DEBIAN_FRONTEND=""
RUN pip3 install --upgrade pip
RUN pip3 install  google-api-python-client google_auth_oauthlib requests_toolbelt moviepy
RUN sed 's/_DEFAULT_TIMEOUT = 120  # in seconds/_DEFAULT_TIMEOUT = 86400/g' /usr/local/lib/python3.8/dist-packages/google/auth/transport/requests.py > tmp.txt && mv tmp.txt /usr/local/lib/python3.8/dist-packages/google/auth/transport/requests.py
COPY . /insta360-auto-converter/
WORKDIR /insta360-auto-converter/MediaSDK
ENV LD_LIBRARY_PATH=LD_LIBRARY_PATH:/insta360-auto-converter/MediaSDK/lib/
RUN g++ -Wno-error -std=c++11  example/main.cc -o stitcherSDKDemo -I/insta360-auto-converter/MediaSDK/include/ -L/insta360-auto-converter/MediaSDK/lib/ -L/insta360-auto-converter/MediaSDK/lib/ -lMediaSDK
WORKDIR /insta360-auto-converter/apps
CMD ["python3", "insta360_auto_converter.py"]
