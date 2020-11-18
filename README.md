# insta360-auto-converter
Automatically take insv (insp) files from google drive and convert them to 360 mp4 (jpg) and upload to google photos

![image](https://user-images.githubusercontent.com/23136724/99520953-bfa20400-29ce-11eb-9d28-5244a4614edc.png)


## Why this project?
I have a insta360 one X, and I record with it very often, I got full 128GB insv (insp) files almost every trip.
### Pain points:
1. To convert 128GB files to mp4 means I need ~128x2 = 256GB free on my mac which is sometimes too much.
2. Using the official 360Studio software to convert them will cost high CPU usage, almost can't do other things.
3. I'll have to upload them to google photos to enjoy the 360 experience in cardboard or on my phone/mac, but using the google photos web UI is a terrible experience.

## Prerequisite:
1. You know what `python3` and `docker` are and how to run them.
2. You have `enough space` for your google drive and photos (Gsuite for biz for education).
3. You have a cloud linux machine which can run `docker` (or a spare mac to run it 24/7), ex: AWS EC2, Google cloud instance...

### Getting Start:

* Clone this project to your instance that runs the `docker` or [(download the zip)](https://github.com/whmou/insta360-auto-converter/archive/main.zip)

* Due to security and NDA issues, you have 4 sub tasks to do which might take a while, but if those pain points can be eliminated, I think it's worth it.
1. Follow the guide to get insta360 Media SDK [(link)](https://docs.google.com/document/d/1ob-R5ThN-1azgNpgDqXDr433MFnuSa72c6_hRM4jyY0/edit?usp=sharing)
2. Follow the guide to get your own Google Drive service account credential. [(link)](https://docs.google.com/document/d/1-hhtCnrqRcazClOKOpwbrPocnDZIUy07m_MafZAFZM0/edit?usp=sharing)
3. Follow the guide to get your own Google photos OAtuh credential. [(link)](https://docs.google.com/document/d/1NEnDdgkJIp0a97D7uGtXbwle2uhmd0_cdLKD9-Whrxo/edit?usp=sharing)
4. Follow the guide to setup the config files [(link)](https://docs.google.com/document/d/1y_sskH7c9jXu_5y5FjqztmesNgY2fz5fIMomOZSbkwQ/edit?usp=sharing)

#### Put it all together

1. Check if you are ready to build the docker image
    * MediaSDK folder is in this project folder
    insta360-auto-converter
    |- apps
    |- Dockerfile
    |- MediaSDK
    * You followed the guide above and have the metadata folder somewhere else, ex: /Users/wmou/Documents/insta360-auto-converter-data
    |- auto-conversion.json 
    |- configs.txt 
    |- gphotos_auth.json 
  
2. Build the docker image
under the folder which contains the Dockerfile, the last "." is required.
```bash
$ sudo docker build -t insta360-auto-converter .
```

3. run the docker image as container
please note that you folder you mount with `-v` should be your own metadata folder.
```bash
$ sudo docker run -d -v /Users/wmou/Documents/insta360-auto-converter-data:/insta360-auto-converter-data insta360-auto-converter
```

### Where/How do I put insv (insp) files on the Google Drive?
1. In the google drive setup sub task, you should have a working folder, then upload a folder under that working folder, which contains your insv (insp) files.
2. Then this auto-converter will automatically upload the mp4 (jpg) to the Album of your google photos as the same name as the subfolder you uploaded.
3. for example, "inst360_autoflow" is my working folder, then sub folder will be “測試1_360raw", and contains with many insv, insp files.

![image](https://user-images.githubusercontent.com/23136724/99519497-ec551c00-29cc-11eb-9a3b-c6cdc212a805.png)