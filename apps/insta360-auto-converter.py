# -*- encoding: utf-8 -*-

from datetime import datetime
from datetime import date
import logging
import sys
import io
import shutil
import os
import random
from pathlib import Path
import smtplib
import time
import glob
import subprocess
from configparser import ConfigParser
from email.mime.text import MIMEText

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

sys.path.append('.')
import google_photos_uploader as gphotos
from logging.handlers import RotatingFileHandler
from subprocess import Popen, PIPE
from math import ceil
from moviepy.video.io.ffmpeg_tools import ffmpeg_extract_subclip
from moviepy.editor import VideoFileClip

log_dir = '/insta360-auto-converter-data/logs'
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

logger = logging.getLogger('insta360-auto-converter-logger')
logFile = '{}/insta360-auto-converter-logger-'.format(log_dir) + time.strftime("%Y%m%d") + '.log'
handler = RotatingFileHandler(logFile, mode='a', maxBytes=50 * 1024 * 1024,
                              backupCount=5, encoding=None, delay=False)
logger.setLevel(logging.INFO)
logger.addHandler(handler)

config = ConfigParser()
config.read("/insta360-auto-converter-data/configs.txt")


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

class VideoProcessor:
    def __init__(self):
        self.SIZE_THREHOLD = 5 * (2 ** 30)
        self.SIZE_LIMIT = 7 * (2 ** 30)

        # self.SIZE_THREHOLD = 100 * (2 ** 20)
        # self.SIZE_LIMIT = 100 * (2 ** 20)
        pass

    def split_video(self, video, N=None):
        rtn = []
        if not N:
            # 1. get video file size
            file_size = os.path.getsize(video)
            log('{} file_size: {}'.format(video, file_size))
            if file_size < self.SIZE_LIMIT:
                rtn.append(video)
                log('{} clip file size lower than the limit, no need to split'.format(video))
                return rtn

            # 2. get N
            n = ceil(file_size / self.SIZE_THREHOLD)
            log('{} clip split to {} part'.format(video, n))

            # 3. do split
            rtn = self.split_video(video, n)

            # 4. rtn split video list
            return rtn

        else:
            # 1. get duration
            clip = VideoFileClip(video)
            duration = clip.duration
            sec_per_clip = int(ceil(duration) / N)
            log('{} clip duration: {}'.format(video, duration))

            # 2. split secs to n part
            start = 0
            end = sec_per_clip

            idx = 1
            while start < duration:
                clip_name = os.path.basename(video).replace('.mp4', '-{}.mp4'.format(idx))
                rtn.append(clip_name)
                idx += 1

                ffmpeg_extract_subclip(video, start, end, targetname=clip_name)
                start += sec_per_clip
                end += sec_per_clip
                if end > duration:
                    end = duration

            return rtn

def log(content, mail_out=False):
    log_content = '[{}] {}'.format(datetime.now().strftime('%Y-%m-%d %H:%M:%S'), content)
    if mail_out:
        logger.error(log_content)
    else:
        logger.info(log_content)
    print(log_content)
    if mail_out:
        send_mail(config["GMAIL_INFO"]["error_mail_to"], 'insta360-auto-converter Job Failed', content)


def silentremove(filename):
    try:
        os.remove(filename)
    except:
        pass


def send_mail(to, subject, body):
    s = config["GMAIL_INFO"]["pass"]
    gmail_user = config["GMAIL_INFO"]["id"]
    sent_from = gmail_user

    mime = MIMEText(body, "plain", "utf-8")
    mime["Subject"] = subject
    mime["From"] = config["GMAIL_INFO"]["id"]
    mime["To"] = to

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.ehlo()
        server.login(gmail_user, s)
        server.sendmail(sent_from, to, mime.as_string())
        server.close()
        log('Email sent!')
    except Exception as e:
        log('Send mail error: {}'.format(e))


def main():
    SDK_PATH = '/insta360-auto-converter/MediaSDK'
    working_folder = '/insta360-auto-converter/apps'
    gs = None
    auto_processing_remote_file = None
    auto_processing_file_name = ''

    ## sleep 3 secs flooded log handling
    LOG_FLAG = True
    NO_FOUND_IN_A_ROW = 0
    NO_FOUND_IN_A_ROW_LIMIT = 10

    while True:

        try:
            # 1. google drive init
            cred_path = '/insta360-auto-converter-data/auto-conversion.json'
            drive_id = config["GDRIVE_INFO"]["drive_id"]
            try:
                gs = GDriveService(cred_path, drive_id)
            except Exception as e:
                log('GDriveService init failed, exception: {}'.format(e))

            # 2. get all rawdata folders under the "insta360_autoflow folder"
            try:
                insta360_autoflow_folder_id = config["GDRIVE_INFO"]["working_folder_id"]
                all_folders = gs.retrieve_all_in_folder(insta360_autoflow_folder_id)
                all_folders = list(filter(lambda x: 'folder' in x['mimeType'], all_folders))
            except Exception as e:
                log('retrieve_all_in_folder failed: {}, folder id: {}'.format(e, insta360_autoflow_folder_id), True)

            # 3. find one need convert file pair
            need_convert_files = None
            auto_processing_remote_file = None
            for folder in all_folders:
                try:
                    need_convert_files = gs.get_need_convert_file_in_folder(folder)
                    if need_convert_files != None:
                        # download 2 files to local to convert
                        log('Download ins files {}'.format([need_convert_files['left'], need_convert_files['right']]))
                        gs.download_file([need_convert_files['left'], need_convert_files['right']])
                        break
                except Exception as e:
                    log('get_need_convert_file_in_folder failed: {}, folder info: {}'.format(e, folder), True)

            # 4. call 360 convert
            if LOG_FLAG:        
                log('Find any files need to be converted?: {}'.format('Yes' if need_convert_files else 'No'))
            if need_convert_files != None:
                NO_FOUND_IN_A_ROW = 0
                LOG_FLAG = True
                
                stabilize_flag = True
                retry = True
                convert_return_code = -1
                while retry:
                    try:
                        convert_fail_file_name = '{}.auto_broken'.format(need_convert_files['left']['name'])
                        auto_processing_remote_file = need_convert_files['auto_processing_file']
                        convert_name = need_convert_files['left']['name'].replace('.insv', '_convert.mp4').replace('.insp',
                                                                                                                   '_convert.jpg')
                        output_file_name = need_convert_files['left']['name'].replace('.insv', '.mp4').replace('.insp',
                                                                                                               '.jpg')
                        is_img = False if convert_name.endswith('.mp4') else True

                        log('start to use the SDK doing conversion for the file: {}'.format(need_convert_files['left']))

                        cmds = []
                        cmds.append("{}/stitcherSDKDemo".format(SDK_PATH))
                        cmds.append("-inputs")
                        cmds.append("{}/{}".format(working_folder, need_convert_files['left']['name']))
                        if is_img != True:
                            cmds.append("{}/{}".format(working_folder, need_convert_files['right']['name']))
                            cmds.append("-output_size")
                            cmds.append("5760x2880")
                            cmds.append("-bitrate")
                            cmds.append("200000000")
                        else:
                            cmds.append("-output_size")
                            cmds.append("6080x3040")
                        cmds.append("-stitch_type")
                        cmds.append("dynamicstitch")
                        if stabilize_flag:
                            cmds.append("-enable_flowstate")
                        cmds.append("-output")
                        cmds.append("{}/{}".format(working_folder, convert_name))
                        log("360 convert command: {}".format(cmds))
                        if is_img:
                            p = Popen(" ".join(cmds), stdin=PIPE, stdout=PIPE, stderr=PIPE, shell=True)
                            return_code = p.wait()
                            log("return_code of the conversion: {}".format(return_code))
                            if return_code == 139 and is_img and stabilize_flag:
                                stabilize_flag = False
                            elif return_code != 0:
                                raise RuntimeError("return_code of the conversion is not 0")
                            else:
                                retry = False
                        else:
                            p = Popen(" ".join(cmds), stdin=PIPE, stdout=PIPE, stderr=PIPE, shell=True)
                            rtn_code_overwrite = 0
                            for line in p.stdout:
                                line = str(line)
                                if 'process =' in line:
                                    line = line[-50:]
                                log(line)
                                time.sleep(1)
                                if 'Invalid data found when processing input' in line:
                                    p.stdin.write(b'\n')
                                    p.stdin.flush()
                                    rtn_code_overwrite = -1
                                    log('Error when converting file:{}, Invalid data found when processing input'.format(need_convert_files['left']['name']), True)
                                    break

                                if 'estimate audio frame duration' in line:
                                    p.stdin.write(b'\n')
                                    p.stdin.flush()
                                    log("end line: {}".format(line))
                                    log("rtn code: {}".format(p.returncode))
                                    break
                            return_code = p.wait()
                            convert_return_code = return_code
                            log("return_code of the conversion: {}".format(return_code))
                            if return_code == 139 and is_img and stabilize_flag:
                                stabilize_flag = False
                            elif return_code !=0:
                                raise RuntimeError("return_code of the conversion is not 0, rtn code: {}".format(return_code))
                            else:
                                retry = False

                    except Exception as e:
                        log(
                            'calling insta stitcherSDK failed: {}, left eye data info: {}, parent_dir_info: {}, error: {}'.format(
                                e, need_convert_files['left'], need_convert_files['parent_folder'], e), True)
                        convert_fail_file_name = '{}.auto_broken'.format(need_convert_files['left']['name'])
                        Path(convert_fail_file_name).touch()
                        gs.upload_file_to_folder(convert_fail_file_name, need_convert_files['parent_folder'], 'text/plain')

                # 4.1 split video if needed
                for filename in glob.glob("*insv"):
                    silentremove(filename)
                split_videos = []
                if not is_img:
                    vp = VideoProcessor()
                    split_videos = vp.split_video(convert_name)
                    log('split_videos: {}'.format(split_videos))

                # 4.2 inject 360 meta
                if convert_return_code ==0:
                    cmds = []
                    try:
                        if is_img:
                            cmds.append("./Image-ExifTool-12.10/exiftool")
                            cmds.append('-XMP-GPano:FullPanoHeightPixels="3040"')
                            cmds.append('-XMP-GPano:FullPanoWidthPixels="6080"')
                            cmds.append('-XMP-GPano:ProjectionType="equirectangular"')
                            cmds.append('-XMP-GPano:UsePanoramaViewer="True"')
                            cmds.append(convert_name)
                            subprocess.call(" ".join(cmds), shell=True)
                            os.rename(convert_name, output_file_name)

                        else:
                            for tmp_video in split_videos:
                                cmds = []
                                convert_name = tmp_video
                                output_file_name = tmp_video.replace('_convert', '')
                                log('injecting 360 meta to video: {}, output_file_name: {}'.format(convert_name, output_file_name))
                                cmds.append("python3")
                                cmds.append("spatial-media/spatialmedia")
                                cmds.append("-i")
                                cmds.append("--stereo=none")
                                cmds.append(convert_name)
                                cmds.append(output_file_name)
                                subprocess.call(" ".join(cmds), shell=True)
                                silentremove(convert_name)
                    except OSError as e:
                        log('inject 360 meta failed, filename: {}, cmds: {}, error: {}'.format(convert_name, " ".join(cmds),
                                                                                            e), True)
                        convert_fail_file_name = '{}.auto_broken'.format(need_convert_files['left']['name'])
                        Path(convert_fail_file_name).touch()
                        gs.upload_file_to_folder(convert_fail_file_name, need_convert_files['parent_folder'], 'text/plain')
                    except Exception as e:
                        log('inject 360 meta failed, filename: {}, cmds: {}, error: {}'.format(convert_name, " ".join(cmds),
                                                                                            e), True)

                    # 5. upload to both gdrive and gphotos
                    #service account has it's own upload limit quota, temporary stop uploading till changed to oauth
                    ## 5.1 upload to gdrive
                    #try:
                    #    mimetype = 'video/mp4' if 'insv' in need_convert_files['left']['name'] else 'image/jpg'
                    #    gs.upload_file_to_folder(output_file_name, need_convert_files['parent_folder'], mimetype)
                    #except Exception as e:
                    #    log('upload_file_to_folder failed, file name: {}, parent folder: {}, error: {}'.format(
                    #        output_file_name, need_convert_files['parent_folder'], e), True)
                    #    if 'Drive storage quota has been exceeded'.lower() in str(e).lower():
                    #        quota_exceeded_sleep_1_day = True

                    ## 5.2 gphotos
                    try:
                        if is_img:
                            gphotos.upload_to_album('{}/{}'.format(working_folder, output_file_name),
                                                    need_convert_files['parent_folder']['name'])
                        else:
                            for tmp_video in split_videos:
                                output_file_name = tmp_video.replace('_convert', '')
                                gphotos.upload_to_album('{}/{}'.format(working_folder, output_file_name),
                                                    need_convert_files['parent_folder']['name'])
                    except Exception as e:
                        log('google photos upload_to_album failed: {}, file_name: {}, parent folder info: {}'.format(e,
                                                                                                                    output_file_name,
                                                                                                                    need_convert_files[
                                                                                                                        'parent_folder']),
                            True)

                    # adding/uploading auto_done flag file
                    try:
                        auto_done_file_name = need_convert_files['left']['name'] + ".auto_done"
                        Path(auto_done_file_name).touch()
                        gs.upload_file_to_folder(auto_done_file_name, need_convert_files['parent_folder'], 'text/plain')
                    except Exception as e:
                        log('upload_file_to_folder failed, file name: {}, parent folder: {}, error: {}'.format(
                            auto_done_file_name, need_convert_files['parent_folder'], e), True)
            else:
                NO_FOUND_IN_A_ROW +=1
                if NO_FOUND_IN_A_ROW > NO_FOUND_IN_A_ROW_LIMIT:
                    if LOG_FLAG:
                        log('entering silent mode...')
                    LOG_FLAG = False


        except Exception as e:
            log('insta360-auto-converter has some error: {} at line: {}'.format(e, sys.exc_info()[2].tb_lineno), True)
        finally:
            try:
                for filename in glob.glob("core*"):
                    silentremove(filename)
                for filename in glob.glob("*mp4"):
                    silentremove(filename)
                silentremove('{}/{}'.format(working_folder, need_convert_files['left']['name']))
                if need_convert_files['right']:
                    silentremove('{}/{}'.format(working_folder, need_convert_files['right']['name']))
                if gs and auto_processing_remote_file:
                    gs.remove_file(auto_processing_remote_file['id'])
                    gs.service.close()
                silentremove(auto_processing_file_name)
                silentremove(auto_done_file_name)
                silentremove('{}/{}'.format(working_folder, convert_name))
                silentremove('{}/{}'.format(working_folder, convert_name + '_original'))
                silentremove('{}/{}'.format(working_folder, output_file_name))
                gs = None
            except Exception as e:
                log('finally handling cleanup has some error : {}'.format(e))

        if LOG_FLAG:        
            log('sleep 3 secs for getting next job...')
        sleep_sec = 3
        #if quota_exceeded_sleep_1_day:
        #    quota_exceeded_sleep_1_day = False
        time.sleep(sleep_sec)


if __name__ == '__main__':
    main()


