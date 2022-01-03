from utils import log
import os
from math import ceil

from moviepy.video.io.ffmpeg_tools import ffmpeg_extract_subclip
from moviepy.editor import VideoFileClip

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