import argparse
import json
import logging
import os.path
import time
from logging.handlers import RotatingFileHandler

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

log_dir = '/tmp/123/insta360-auto-converter-data/logs'
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

logger = logging.getLogger('insta360-auto-converter-logger')
logFile = '{}/insta360-auto-converter-logger-gphotos-'.format(log_dir) + time.strftime("%Y%m%d-%H%M%S")+ '.log'
handler = RotatingFileHandler(logFile, mode='a', maxBytes=50*1024*1024,
                                 backupCount=5, encoding=None, delay=False)
logger.setLevel(logging.INFO)
logger.addHandler(handler)


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
        time.sleep(0.25)
    return rtn


def create_or_retrieve_album(session, album_title):

    # Find albums created by this app to see if one matches album_title
    logger.info("create_or_retrieve_album: -- \'{0}\'".format(album_title))
    print("create_or_retrieve_album: -- \'{0}\'".format(album_title))
    albums = getAlbums(session, False)
    logger.info("got {} albums".format(len(albums)))
    print("got {} albums".format(len(albums)))
    for a in albums:
        if 'title' in a and a["title"].lower() == album_title.lower():
            album_id = a["id"]
            logger.info("Uploading into EXISTING photo album -- \'{0}\'".format(album_title))
            print("Uploading into EXISTING photo album -- \'{0}\'".format(album_title))
            return album_id

    # No matches, create new album

    create_album_body = json.dumps({"album":{"title": album_title}})
    #print(create_album_body)
    resp = session.post('https://photoslibrary.googleapis.com/v1/albums', create_album_body).json()

    logger.debug("Server response: {}".format(resp))

    if "id" in resp:
        logger.info("Uploading into NEW photo album -- \'{0}\'".format(album_title))
        return resp['id']
    else:
        logger.error("Could not find or create photo album '\{0}\'. Server Response: {1}".format(album_title, resp))
        return None

def upload_photos(session, photo_file_list, album_name):

    album_id = create_or_retrieve_album(session, album_name) if album_name else None

    # interrupt upload if an upload was requested but could not be created
    if album_name and not album_id:
        return

    session.headers["Content-type"] = "application/octet-stream"
    session.headers["X-Goog-Upload-Protocol"] = "raw"



    for photo_file_name in photo_file_list:

        try:
            photo_file = open(photo_file_name, mode='rb')
            photo_bytes = photo_file.read()
        except OSError as err:
            logger.error("Could not read file \'{0}\' -- {1}".format(photo_file_name, err))
            continue

        session.headers["X-Goog-Upload-File-Name"] = os.path.basename(photo_file_name)

        logger.info("Uploading photo -- \'{}\'".format(photo_file_name))

        upload_token = session.post('https://photoslibrary.googleapis.com/v1/uploads', photo_bytes, json=None, timeout=86400)

        if (upload_token.status_code == 200) and (upload_token.content):

            create_body = json.dumps({"albumId":album_id, "newMediaItems":[{"description":"","simpleMediaItem":{"uploadToken":upload_token.content.decode()}}]}, indent=4)

            resp = session.post('https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate', create_body).json()

            logger.debug("Server response: {}".format(resp))

            if "newMediaItemResults" in resp:
                status = resp["newMediaItemResults"][0]["status"]
                if status.get("code") and (status.get("code") > 0):
                    logger.error("Could not add \'{0}\' to library -- {1}".format(os.path.basename(photo_file_name), status["message"]))
                else:
                    logger.info("Added \'{}\' to library and album \'{}\' ".format(os.path.basename(photo_file_name), album_name))
            else:
                logging.error("Could not add \'{0}\' to library. Server Response -- {1}".format(os.path.basename(photo_file_name), resp))

        else:
            logger.error("Could not upload \'{0}\'. Server Response - {1}".format(os.path.basename(photo_file_name), upload_token))

    try:
        del(session.headers["Content-type"])
        del(session.headers["X-Goog-Upload-Protocol"])
        del(session.headers["X-Goog-Upload-File-Name"])
    except KeyError:
        pass


def upload_to_album(file_path, album_name):
    session = get_authorized_session('/insta360-auto-converter-data/gphotos_auth.json')
    photos_list = [file_path]
    upload_photos(session, photos_list, album_name)
    session.close()
