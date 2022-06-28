import sys, os
import os.path
from pathlib import Path
import time
from logging.handlers import RotatingFileHandler
from configparser import ConfigParser
from datetime import datetime
from datetime import date
from email.mime.text import MIMEText
import random

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from utils import log


class YoutubeHandler:
    _youtube_service = None
    _auth_token_file = ''
    _channel_id = ''
    _MAX_RETRIES = 5

    def __init__(self, auth_token, channel_id):
        self._auth_token_file = auth_token
        self._channel_id = channel_id
        self._youtube_service = self.get_youtube_upload_authenticated_service()

    # def get_youtube_authorized_session(self, auth_token_file):
    #     scopes = ['https://www.googleapis.com/auth/youtube',
    #             'https://www.googleapis.com/auth/youtube.upload']
    #     cred = None
    #     if auth_token_file:
    #         try:
    #             cred = Credentials.from_authorized_user_file(auth_token_file, scopes)
    #         except OSError as err:
    #             log("Error opening auth token file - {0}".format(err))
    #         except ValueError:
    #             log("Error loading auth tokens - Incorrect format")
    #     if not cred:
    #         cred = auth(scopes, auth_token_file)

    #     session = AuthorizedSession(cred)
    #     if auth_token_file:
    #         try:
    #             self.save_cred(cred, auth_token_file)
    #         except OSError as err:
    #             log("Could not save auth tokens - {0}".format(err))
    #     return session


    # def save_cred(self, cred, auth_file):
    #     cred_dict = {
    #         'token': cred.token,
    #         'refresh_token': cred.refresh_token,
    #         'id_token': cred.id_token,
    #         'scopes': cred.scopes,
    #         'token_uri': cred.token_uri,
    #         'client_id': cred.client_id,
    #         'client_secret': cred.client_secret
    #     }
    #     with open(auth_file, 'w') as f:
    #         log(json.dumps(cred_dict), file=f)


    def get_playlists(self):
        rtn = []
        pageToken = None
        while True:
            # playlist = self._youtube_service.get('https://www.googleapis.com/youtube/v3/playlists', params=params).json()
            playlist_req = self._youtube_service.playlists().list(
                part="snippet",
                maxResults=50,
                mine=True,
                pageToken= pageToken
            )
            playlist = playlist_req.execute()
            if 'items' in playlist:
                for p in playlist["items"]:
                    rtn.append(p)
                if 'nextPageToken' in playlist:
                    pageToken = playlist["nextPageToken"]
                else:
                    break
            else:
                break
            time.sleep(1)
        return rtn


    def get_medias_in_playlist(self, p):

        rtn = []
        # params = {
        #     "part": "snippet",
        #     "maxResults": 50,
        #     "playlistId": p['id']
        # }
        pageToken = None
        while True:
            request = self._youtube_service.playlistItems().list(
                part="snippet",
                maxResults=50,
                playlistId =  p['id'],
                pageToken = pageToken
            )
            medias = request.execute()
            # medias = self._youtube_service.get('https://www.googleapis.com/youtube/v3/playlistItems', params=params).json()
            if 'items' in medias:
                for m in medias["items"]:
                    rtn.append(m)
                if 'nextPageToken' in medias:
                    pageToken = medias["nextPageToken"]
                else:
                    break
            else:
                break
            time.sleep(0.25)
        return rtn


    def create_playlist(self, p_name):
        params = {
            "part": "snippet",
            "snippet": {
                "title": p_name
            }
        }
        data = {
            "kind": "youtube#playlist",
            "snippet": {
                "title": p_name
            }
        }
        # res = self._youtube_service.post('https://www.googleapis.com/youtube/v3/playlists', json=data, params=params).json()
        request = self._youtube_service.playlists().insert(
            part="snippet",
                body={
                "snippet": {
                    "title": p_name
                }
            }
        )
        res = request.execute()
        log(res)
        return res


    def get_or_create_playlist(self, p_name, hist_playlists):
        found = False
        plist = None
        log('hist_playlists:' + str(hist_playlists))
        for playlist in hist_playlists:
            if 'title' in playlist['snippet'] and p_name == playlist['snippet']['title']:
                found = True
                return playlist

        if not found:
            res = self.create_playlist(p_name)
            return res
        

    def get_youtube_upload_authenticated_service(self):
        cred = None
        scopes = ['https://www.googleapis.com/auth/youtube',
                'https://www.googleapis.com/auth/youtube.upload']
        if self._auth_token_file:
            try:
                cred = Credentials.from_authorized_user_file(self._auth_token_file, scopes)
            except OSError as err:
                log("Error opening youtube auth token file - {0}".format(err))
            except ValueError:
                log("Error loading youtube auth tokens - Incorrect format")

        return build('youtube', 'v3', credentials=cred)

    def initialize_upload(self, title, vid_path):
        # youtube_session = self.get_youtube_upload_authenticated_service('{}/youtube_auth.json'.format(DATA_DIR))
        body = {
            'snippet': {
                'title': title
            },
            "status": {
                    "privacyStatus": "unlisted"
                }
        }

        # Call the API's videos.insert method to create and upload the video.
        insert_request = self._youtube_service.videos().insert(
            part=",".join(body.keys()),
            body=body,
            # The chunksize parameter specifies the size of each chunk of data, in
            # bytes, that will be uploaded at a time. Set a higher value for
            # reliable connections as fewer chunks lead to faster uploads. Set a lower
            # value for better recovery on less reliable connections.
            #
            # Setting "chunksize" equal to -1 in the code below means that the entire
            # file will be uploaded in a single HTTP request. (If the upload fails,
            # it will still be retried where it left off.) This is usually a best
            # practice, but if you're using Python older than 2.6 or if you're
            # running on App Engine, you should set the chunksize to something like
            # 1024 * 1024 (1 megabyte).
            media_body=MediaFileUpload(vid_path, chunksize=-1, resumable=True)
        )

        vid = self.resumable_upload(insert_request)
        return vid

    # This method implements an exponential backoff strategy to resume a
    # failed upload.
    def resumable_upload(self, insert_request):
        response = None
        error = None
        retry = 0
        while response is None:
            try:
                log ("Uploading file...")
                status, response = insert_request.next_chunk()
                if response is not None:
                    if 'id' in response:
                        log ("Video id '%s' was successfully uploaded." % response['id'])
                        return response['id']
            except Exception as e:
                exc_type, exc_obj, exc_tb = sys.exc_info()
                fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                log(exc_type, fname, exc_tb.tb_lineno)

            if error is not None:
                log (error)
                retry += 1
            if retry > self._MAX_RETRIES:
                exit("No longer attempting to retry.")

            max_sleep = 2 ** retry
            sleep_seconds = random.random() * max_sleep
            log ("Sleeping %f seconds and then retrying..." % sleep_seconds)
            time.sleep(sleep_seconds)

    def set_video_to_playlist(self, vid, playlist):
        # youtube_session = self.get_youtube_upload_authenticated_service('{}/youtube_auth.json'.format(DATA_DIR))
        request = self._youtube_service.playlistItems().insert(
            part="snippet",
            body={
            "snippet": {
                "playlistId": playlist['id'],
                "resourceId": {
                "videoId": vid,
                "kind": "youtube#video"
                }
            }
            }
        )
        response = request.execute()
        log(response)
