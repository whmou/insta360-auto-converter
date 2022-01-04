import argparse
import json
import logging
import os.path
import time
from logging.handlers import RotatingFileHandler
from configparser import ConfigParser
from datetime import datetime
from datetime import date
from email.mime.text import MIMEText
import smtplib
from utils import log

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


config = ConfigParser()
config.read("/insta360-auto-converter-data/configs.txt")


def parse_args(arg_input=None):
    parser = argparse.ArgumentParser(description='Upload photos to Google Photos.')
    parser.add_argument('--auth ', metavar='auth_file', dest='auth_file',
                    help='file for reading/storing user authentication tokens')
    parser.add_argument('--album', metavar='album_name', dest='album_name',
                    help='name of photo album to create (if it doesn\'t exist). Any uploaded photos will be added to this album.')
    parser.add_argument('--log', metavar='log_file', dest='log_file',
                    help='name of output file for log messages')
    parser.add_argument('photos', metavar='photo',type=str, nargs='*',
                    help='filename of a photo to upload')
    return parser.parse_args(arg_input)


def auth(scopes, cred_file):
    flow = InstalledAppFlow.from_client_secrets_file(
        cred_file,
        scopes=scopes)

    credentials = flow.run_local_server(host='localhost',
                                        port=8080,
                                        authorization_prompt_message="",
                                        success_message='The auth flow is complete; you may close this window.',
                                        open_browser=True)

    return credentials

def get_authorized_session(auth_token_file):

    scopes=['https://www.googleapis.com/auth/photoslibrary',
            'https://www.googleapis.com/auth/photoslibrary.sharing']

    cred = None

    if auth_token_file:
        try:
            cred = Credentials.from_authorized_user_file(auth_token_file, scopes)
        except OSError as err:
            logger.debug("Error opening auth token file - {0}".format(err))
        except ValueError:
            logger.debug("Error loading auth tokens - Incorrect format")


    if not cred:
        cred = auth(scopes, auth_token_file)

    session = AuthorizedSession(cred)


    if auth_token_file:
        try:
            save_cred(cred, auth_token_file)
        except OSError as err:
            logger.debug("Could not save auth tokens - {0}".format(err))

    return session


def save_cred(cred, auth_file):

    cred_dict = {
        'token': cred.token,
        'refresh_token': cred.refresh_token,
        'id_token': cred.id_token,
        'scopes': cred.scopes,
        'token_uri': cred.token_uri,
        'client_id': cred.client_id,
        'client_secret': cred.client_secret
    }

    with open(auth_file, 'w') as f:
        print(json.dumps(cred_dict), file=f)

# Generator to loop through all albums

def getAlbums(session, appCreatedOnly=False):
    rtn = []
    params = {
            'excludeNonAppCreatedData': appCreatedOnly
    }

    while True:
        albums = session.get('https://photoslibrary.googleapis.com/v1/albums', params=params).json()
        if 'albums' in albums:
            for a in albums["albums"]:
                rtn.append(a)
            if 'nextPageToken' in albums:
                params["pageToken"] = albums["nextPageToken"]
            else:
                break
        if len(albums) == 0:
            break
        time.sleep(0.25)
    return rtn


def create_or_retrieve_album(session, album_title):

    # Find albums created by this app to see if one matches album_title
    log("create_or_retrieve_album: -- \'{0}\'".format(album_title))
    albums = getAlbums(session, False)
    log("got {} albums".format(len(albums)))
    for a in albums:
        if 'title' in a and a["title"].lower() == album_title.lower():
            album_id = a["id"]
            log("Uploading into EXISTING photo album -- \'{0}\'".format(album_title))
            return album_id

    # No matches, create new album
    create_album_body = json.dumps({"album":{"title": album_title}})
    resp = session.post('https://photoslibrary.googleapis.com/v1/albums', create_album_body).json()
    log("Create new album - Server response: {}".format(resp))

    if "id" in resp:
        log("Uploading into NEW photo album -- \'{0}\'".format(album_title))
        return resp['id']
    else:
        log("Could not find or create photo album '\{0}\'. Server Response: {1}".format(album_title, resp), True)
        return None

def upload_photos(session, photo_file_list, album_name):

    # 1. get album
    album_id = None
    try:
        album_id = create_or_retrieve_album(session, album_name) if album_name else None
    except Exception as e:
        log('get album error: {}'.format(e), True)

    # interrupt upload if an upload was requested but could not be created
    if album_name and not album_id:
        return

    TRIED = 0
    TRY_LIMIT = 3
    DONE_FLAG = False
    for photo_file_name in photo_file_list:
        while TRIED < TRY_LIMIT and not DONE_FLAG:
            TRIED +=1
            file_size = os.stat(photo_file_name).st_size
            headers = {
                "Content-Length": "0",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Content-Type": "image/jpeg" if 'jpg' in photo_file_name else 'video/mp4',
                "X-Goog-Upload-File-Name": os.path.basename(photo_file_name),
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Raw-Size": str(file_size)
            }

            log("Uploading photo -- \'{}\'".format(photo_file_name))
            init_res = session.post('https://photoslibrary.googleapis.com/v1/uploads', headers=headers)
            log("init_res code: {}".format(init_res.status_code))

            try:
                if (init_res.status_code == 200):
                    init_res_headers = init_res.headers
                    real_upload_url = init_res_headers.get("X-Goog-Upload-URL")
                    upload_granularity = int(init_res_headers.get("X-Goog-Upload-Chunk-Granularity"))
                    number_of_req_s = int(file_size / upload_granularity)
                    log('google photos uploading, number_of_req_s: {}'.format(number_of_req_s))

                    with open(photo_file_name, mode="rb") as f_d:
                        for i in range(number_of_req_s):
                            current_chunk = f_d.read(upload_granularity)
                            offset = i * upload_granularity
                            part_size = len(current_chunk)
                            headers = {
                                "Content-Length": str(part_size),
                                "X-Goog-Upload-Command": "upload",
                                "X-Goog-Upload-Offset": str(offset),
                            }
                            # log('google photos uploading chunk {}/{}, part_size: {}'.format(i+1, number_of_req_s, part_size))
                            res = session.post(real_upload_url, headers=headers, data=current_chunk)
                            # log('google photos uploaded chunk {}/{}, response: {}'.format(i+1, number_of_req_s, res))

                        log('google photos uploading last chunk for {}'.format(photo_file_name))
                        current_chunk = f_d.read(upload_granularity)
                        headers = {
                            "Content-Length": str(len(current_chunk)),
                            "X-Goog-Upload-Command": "upload, finalize",
                            "X-Goog-Upload-Offset": str(number_of_req_s * upload_granularity),
                        }
                        upload_token = session.post(real_upload_url, headers=headers, data=current_chunk)
                        log('google photos uploaded last chunk for {}, response: {}'.format(photo_file_name, upload_token))
                        create_body = json.dumps({"albumId": album_id, "newMediaItems": [
                            {"description": "", "simpleMediaItem": {"uploadToken": upload_token.content.decode()}}]}, indent=4)

                        resp = session.post('https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate',
                                            create_body).json()
                        log('google photos creating newMediaItems, response: {}'.format(resp))
                        if "newMediaItemResults" in resp:
                            status = resp["newMediaItemResults"][0]["status"]
                            if status.get("code") and (status.get("code") > 0):
                                log("Could not add \'{0}\' to library -- {1}".format(os.path.basename(photo_file_name),
                                                                                            status["message"]), True)
                            else:
                                DONE_FLAG = True
                                log(
                                    "Added \'{}\' to library and album \'{}\' ".format(os.path.basename(photo_file_name),
                                                                                    album_name))
                        else:
                            log("Could not add \'{0}\' to library. Server Response -- {1}".format(
                                os.path.basename(photo_file_name), resp), True)

                else:
                    log("Could not upload \'{0}\'.".format(os.path.basename(photo_file_name)), True)
            except Exception as e:
                log('google photos uploading for file: {}, error: {}'.format(photo_file_name, e), True)


def upload_to_album(file_path, album_name):
    session = get_authorized_session('/insta360-auto-converter-data/gphotos_auth.json')
    photos_list = [file_path]
    upload_photos(session, photos_list, album_name)
    session.close()
