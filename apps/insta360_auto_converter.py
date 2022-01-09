# -*- encoding: utf-8 -*-

from datetime import datetime
from datetime import date
import sys
import os
from pathlib import Path
import time
import glob
import subprocess
from configparser import ConfigParser
from subprocess import Popen, PIPE

sys.path.append('.')
from gdrive_service import GDriveService
import google_photos_uploader as gphotos
from utils import log
from utils import silentremove
from video_processor import VideoProcessor
from youtube_service import YoutubeHandler


config = ConfigParser()
config.read("/insta360-auto-converter-data/configs.txt")

in_app_confs = ConfigParser()
in_app_confs.read("/insta360-auto-converter/apps/in_app_configs.conf")


def main():
    SDK_PATH = '/insta360-auto-converter/MediaSDK'
    working_folder = '/insta360-auto-converter/apps'
    gs = None
    auto_processing_remote_file = None
    auto_processing_file_name = ''
    auto_done_file_name = ''

    ## sleep 3 secs flooded log handling
    LOG_FLAG = True
    NO_FOUND_IN_A_ROW = 0
    NO_FOUND_IN_A_ROW_LIMIT = 10

    ## youtube service related settings
    channel_id = config["YOUTUBE_SETTINGS"]["channel_id"]
    youtube_auth_json_path = '/insta360-auto-converter-data/youtube_auth.json'
    youtube_handler = YoutubeHandler(youtube_auth_json_path, channel_id)

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
                    if need_convert_files:
                        # download 2 files to local to convert
                        log('Download ins files {}'.format([need_convert_files['left'], need_convert_files['right']]))
                        gs.download_file([need_convert_files['left'], need_convert_files['right']])
                        break
                except Exception as e:
                    log('get_need_convert_file_in_folder failed: {}, folder info: {}'.format(e, folder), True)

            # 4. call 360 convert
            if LOG_FLAG:        
                log('Find any files need to be converted?: {}'.format('Yes' if need_convert_files else 'No'))
            if need_convert_files:
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
                                retry = False
                                raise RuntimeError("return_code of the conversion is not 0")
                            else:
                                retry = False
                        else:
                            p = Popen(" ".join(cmds), stdin=PIPE, stdout=PIPE, stderr=PIPE, shell=True)
                            rtn_code_overwrite = 0
                            tmp_output_file_size = 0
                            FILE_SIZE_NO_CHANGE_LIMIT = 180
                            same_size_cnt = 0
                            for line in p.stdout:
                                line = str(line)
                                if 'process =' in line:
                                    line = line[-50:]
                                log(line)
                                if os.path.exists(convert_name):
                                    current_file_size = os.path.getsize(convert_name)
                                    if tmp_output_file_size == current_file_size:
                                        same_size_cnt +=1
                                    else:
                                        same_size_cnt = max(0, same_size_cnt-1)
                                    tmp_output_file_size = current_file_size
                                if same_size_cnt >FILE_SIZE_NO_CHANGE_LIMIT:
                                    rtn_code_overwrite = -2
                                    log('Error when converting file:{}, output file size does not changed for a while, timeout reached'.format(need_convert_files['left']['name']), True)
                                    break

                                time.sleep(1)
                                if 'Invalid data found when processing input' in line or 'media Pipeline prepare failed' in line:
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
                            log("return_code of the conversion: {}".format(return_code))
                            if return_code == 139 and is_img and stabilize_flag:
                                stabilize_flag = False
                            elif return_code !=0:
                                raise RuntimeError("return_code of the conversion is not 0, rtn code: {}".format(return_code))
                            else:
                                retry = False

                        convert_return_code = return_code
                    except Exception as e:
                        log(
                            'calling insta stitcherSDK failed: {}, left eye data info: {}, parent_dir_info: {}, error: {}'.format(
                                e, need_convert_files['left'], need_convert_files['parent_folder'], e), True)
                        convert_fail_file_name = '{}.auto_broken'.format(need_convert_files['left']['name'])
                        Path(convert_fail_file_name).touch()
                        gs.upload_file_to_folder(convert_fail_file_name, need_convert_files['parent_folder'], 'text/plain')

                # 4.1 split video if needed
                split_videos = []
                log('buffer for flushing output file...')
                if not is_img:
                    for filename in glob.glob("*insv"):
                        silentremove(filename)
                    time.sleep(30)
                    vp = VideoProcessor()
                    split_videos = vp.split_video(convert_name)
                    log('split_videos: {}'.format(split_videos))
                else:
                    time.sleep(5)

                # 4.2 inject 360 meta
                if convert_return_code ==0:
                    cmds = []
                    try:
                        if is_img:
                            log('injecting 360 meta to image: {}, output_file_name: {}'.format(convert_name, output_file_name))
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
                            log('uploading image to google photos: {}')
                            gphotos.upload_to_album('{}/{}'.format(working_folder, output_file_name),
                                                    need_convert_files['parent_folder']['name'])
                        # (#2: only photos will upload to google photos )
                        else:
                            for tmp_video in split_videos:
                                vid = None
                                output_file_name = tmp_video.replace('_convert', '')
                                youtube_playlists = youtube_handler.get_playlists()
                                target_playlist_name = need_convert_files['parent_folder']['name']
                                target_playlist = youtube_handler.get_or_create_playlist(target_playlist_name, youtube_playlists)

                                vid = youtube_handler.initialize_upload(output_file_name, '{}/{}'.format(working_folder, output_file_name))
                                
                                # filtered_target_playlist = list(filter(lambda p: 'snippet' in p and 'title' in p['snippet'] and p['snippet']['title'] == target_playlist_name, youtube_playlists))
                                youtube_handler.set_video_to_playlist(vid, target_playlist['id'])
                                # gphotos.upload_to_album('{}/{}'.format(working_folder, output_file_name),
                                #                     need_convert_files['parent_folder']['name'])
                    except Exception as e:
                        log('media upload to album/playlist failed: {}, file_name: {}, parent folder info: {}'.format(e,
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
                for to_be_removed in in_app_confs['FILES_TO_CLEAN_UP']['glob_names'].split(','):
                    for filename in glob.glob(to_be_removed):
                        silentremove(filename)
                if need_convert_files:
                    silentremove('{}/{}'.format(working_folder, need_convert_files['left']['name']))
                    if need_convert_files['right']:
                        silentremove('{}/{}'.format(working_folder, need_convert_files['right']['name']))
                if gs and auto_processing_remote_file:
                    gs.remove_file(auto_processing_remote_file['id'])
                    gs.service.close()
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


