from google.oauth2 import service_account
from utils import log
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
import io
import shutil
import random
from pathlib import Path
import os


class GDriveService:
    def __init__(self, cred_path, drive_id):
        self.SCOPES = ['https://www.googleapis.com/auth/drive.metadata',
                       'https://www.googleapis.com/auth/drive']
        self.drive_id = drive_id
        self.cred_path = cred_path
        self.creds = service_account.Credentials.from_service_account_file(cred_path, scopes=self.SCOPES)
        self.service = build('drive', 'v3', credentials=self.creds)

    def createRemoteFolder(self, folderName, parentID=None):
        # Create a folder on Drive, returns the newely created folders ID
        body = {
            'name': folderName,
            'mimeType': "application/vnd.google-apps.folder"
        }
        if parentID:
            body['parents'] = [parentID]
        root_folder = self.service.files().create(body=body).execute()
        return root_folder['id']

    def upload_file_to_folder(self, local_file_path, parent_dir, mimetype):
        output_file_name = os.path.basename(local_file_path)
        file_metadata = {'name': output_file_name, 'parents': [parent_dir['id']]}
        # mimetype = 'video/mp4' if 'insv' in need_convert_files['left']['name'] else 'image/jpg'  # or text/plain
        media = MediaFileUpload(output_file_name, mimetype=mimetype,
                                resumable=True)
        log('uploading {} to google drive, parent file = {}'.format(output_file_name, parent_dir))
        file = self.service.files().create(body=file_metadata,
                                           media_body=media,
                                           fields='id').execute()

        log('uploaded {} to google drive, file ID: {}'.format(output_file_name, file.get('id')))
        return file

    def remove_file(self, file_id):
        file = self.service.files().delete(fileId=file_id).execute()
        return file

    def retrieve_all_in_folder(self, parent_dir_id):
        result = []
        page_token = None
        while True:
            try:
                query = "'%s' in parents and trashed = false" % (parent_dir_id)
                results = self.service.files().list(
                    q=query,
                    pageToken=page_token,
                    corpora='drive',
                    pageSize=100,  # max = 1000, Default: 100
                    driveId=self.drive_id,
                    supportsTeamDrives=True,
                    includeTeamDriveItems=True,
                    fields="nextPageToken, files(id, name, mimeType)",
                ).execute()
                items = results.get('files', [])
                page_token = results.get('nextPageToken', None)
                result.extend(items)
                if not page_token:
                    break
            except Exception as e:
                log('An error occurred when retrieve_all_in_folder: {}, error: {}'.format(parent_dir_id, e), True)
                break

        return result

    def download_file(self, files):
        for file in files:
            if file:
                file_id = file['id']
                request = self.service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while done is False:
                    status, done = downloader.next_chunk()
                    log("Download {} {}%...".format(file['name'], int(status.progress() * 100)))
                log('Writing out file {}...'.format(file['name']))
                fh.seek(0)
                with open(file['name'], 'wb') as f:
                    shutil.copyfileobj(fh, f, length=16 * 1024 * 1024)

    def get_need_convert_file_in_folder(self, folder):
        folder_id = folder['id']
        all_files = self.retrieve_all_in_folder(folder_id)
        left_eye_videos = []
        right_eye_videos = []

        left_eye_photos = []

        auto_processing_files = []
        auto_done_files = []
        auto_broken_files = []
        rtn = None

        # 1. classify all files
        for file in all_files:
            name = file['name']
            if name.startswith('._') or ').ins' in name:
                continue
            if name.endswith('.insv') and '_00_' in name:
                left_eye_videos.append(file)
            elif name.endswith('.insv') and '_10_' in name:
                right_eye_videos.append(file)
            elif name.endswith('.insp') and '_00_' in name:
                left_eye_photos.append(file)
            elif name.endswith('.auto_processing'):  # ex: test_00.insv.auto_processing
                auto_processing_files.append(file)
            elif name.endswith('.auto_done'):  # ex: test_00.insv.auto_done
                auto_done_files.append(file)
            elif name.endswith('.auto_broken'):  # ex: test_00.insv.auto_done
                auto_broken_files.append(file)

        # 2. check not done pairs (video first)
        pair_found = False
        random.shuffle(left_eye_videos)
        auto_processing_gfile = None
        for lv in left_eye_videos:
            auto_processing_file_name = '{}.auto_processing'.format(lv['name'])
            auto_broken_file_name = '{}.auto_broken'.format(lv['name'])
            for rv in right_eye_videos:
                if lv['name'].replace('_00_', '_10_') == rv['name'] and '{}.auto_done'.format(
                        lv['name']) not in list(
                    map(lambda x: x['name'], auto_done_files)) and auto_processing_file_name not in list(
                    map(lambda x: x['name'], auto_processing_files)) and auto_broken_file_name not in list(
                    map(lambda x: x['name'], auto_broken_files)):
                    pair_found = True
                    # os.mknod(auto_processing_file_name)
                    Path(auto_processing_file_name).touch()
                    auto_processing_gfile = self.upload_file_to_folder(auto_processing_file_name, folder, None)
                    rtn = {'left': lv, 'right': rv, 'parent_folder': folder,
                           'auto_processing_file': auto_processing_gfile}
                    break
            if pair_found:
                break

        # 3. check photos (only one left file, no right file)
        if not pair_found:
            random.shuffle(left_eye_photos)
            for lf in left_eye_photos:
                auto_processing_file_name = '{}.auto_processing'.format(lf['name'])
                auto_broken_file_name = '{}.auto_broken'.format(lf['name'])
                rf = None
                if '{}.auto_done'.format(lf['name']) not in list(
                        map(lambda x: x['name'], auto_done_files)) and auto_processing_file_name not in list(
                    map(lambda x: x['name'], auto_processing_files)) and auto_broken_file_name not in list(
                    map(lambda x: x['name'], auto_broken_files)):
                    # os.mknod(auto_processing_file_name)
                    Path(auto_processing_file_name).touch()
                    auto_processing_gfile = self.upload_file_to_folder(auto_processing_file_name, folder, None)
                    rtn = {'left': lf, 'right': rf, 'parent_folder': folder,
                           'auto_processing_file': auto_processing_gfile}
                    break

        return rtn