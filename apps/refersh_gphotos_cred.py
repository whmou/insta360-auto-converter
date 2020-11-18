from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
import json
import sys

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
            print("Error opening auth token file - {0}".format(err))
        except ValueError:
            print("Error loading auth tokens - Incorrect format")
    if not cred:
        cred = auth(scopes, auth_token_file)
    session = AuthorizedSession(cred)
    if auth_token_file:
        try:
            save_cred(cred, auth_token_file)
        except OSError as err:
            print("Could not save auth tokens - {0}".format(err))
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


if __name__== "__main__":
    get_authorized_session(sys.argv[1])

