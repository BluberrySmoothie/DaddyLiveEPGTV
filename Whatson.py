"""
whatson.py

Generates a scrolling schedule video from the DaddyLiveStream JSON schedule.
Refreshes every hour on the hour.

Usage:
    py whatson.py

Dependencies (install with):
    py -m pip install requests python-dateutil pytz moviepy imageio-ffmpeg pillow

Important:
- Place an optional background audio file named "background.mp3" next to this script if you want music.
- Output is written to ./output/schedule.mp4 (6 minutes total - 3 loops of 2 minutes each)

Behavior summary:
- Fetches JSON from https://daddylivestream.com/schedule/schedule-generated.php
  with header Referer: https://daddylivestream.com/
- Skips categories containing 'TV Show' or 'TV Shows' (case-insensitive).
- Keeps any event where (event_start + 3 hours) >= now (UK local time).
- Sorts events within each category by time (UK).
- Creates a scrolling video that starts blank, scrolls up from bottom, and loops 3 times.
- Repeats hourly on the hour.
"""
import os
import io
import sys
import time
import math
import shutil
import subprocess
from datetime import datetime, timedelta

import requests
from dateutil import parser as dparser
from dateutil import tz
import pytz
from moviepy.editor import (
    ImageClip, AudioFileClip, VideoClip, CompositeVideoClip, concatenate_videoclips
)
from PIL import Image, ImageDraw, ImageFont
import numpy as np

# Config
SCHEDULE_URL = "https://daddylivestream.com/schedule/schedule-generated.php"
REFERER = "https://daddylivestream.com/"
OUTPUT_DIR = "output"
VIDEO_FILENAME = os.path.join(OUTPUT_DIR, "schedule.mp4")
TEMP_VIDEO_FILENAME = os.path.join(OUTPUT_DIR, "schedule_temp.mp4")
BACKGROUND_AUDIO = "background.mp3"  # optional - if missing, silent audio used
VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720
FPS = 24
# Duration of generated mp4 (seconds). We'll create a smooth scroll that lasts this long.
VIDEO_DURATION_SECONDS = 120
# text settings
# Common Windows font paths - script will try to find one automatically
FONT_PATHS_TO_TRY = [
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",  # Arial Bold
    r"C:\Windows\Fonts\calibri.ttf",
    r"C:\Windows\Fonts\verdana.ttf",
    r"C:\Windows\Fonts\tahoma.ttf",
]
FONT_PATH = None  # Will be auto-detected from above list
FONT_SIZE_TITLE = 80  # Category titles
FONT_SIZE_LINE = 30  # Event names
FONT_SIZE_CHANNEL = 15  # Channel names
LINE_SPACING = 20
CATEGORY_SPACING = 56
EVENT_SPACING = 20  # Gap between events
# Keep events until this many hours after start
KEEP_AFTER_HOURS = 3

# ensure output dir exists
os.makedirs(OUTPUT_DIR, exist_ok=True)

def fetch_schedule_json(url=SCHEDULE_URL):
    headers = {"Referer": REFERER, "User-Agent": "schedule-generator/1.0"}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

def choose_date_from_top_key(json_obj):
    # The JSON uses a top-level key like "Friday 10th Oct 2025 - Schedule Time UK GMT"
    # We'll try to parse the first top-level key to get the date.
    top_keys = list(json_obj.keys())
    if not top_keys:
        raise ValueError("Empty JSON (no top-level keys).")
    first = top_keys[0]
    # pick up something like "Friday 10th Oct 2025"
    # take substring before '-' if present
    date_part = first.split(" - ")[0].strip()
    # Remove any weekday prefix like "Friday "
    try:
        parsed = dparser.parse(date_part, dayfirst=True, fuzzy=True)
        return parsed.date()
    except Exception as e:
        # fallback to today's date in UK
        print("Warning: couldn't parse date from top key; falling back to today's UK date:", e)
        return datetime.now(tz=tz.gettz("Europe/London")).date()

def build_event_list(json_obj):
    # find the date
    schedule_date = choose_date_from_top_key(json_obj)
    tz_london = pytz.timezone("Europe/London")
    events = []  # dicts with keys: category, dt (aware), event, channels (list)
    for top_key, value in json_obj.items():
        # value is a dict of categories
        if not isinstance(value, dict):
            # in your sample top-level key *is* the date and value contains categories
            # if the JSON already is categories (no top level date), handle that too
            continue
        for category, evlist in value.items():
            # skip TV Show categories (case-insensitive substring)
            if "tv show" in category.lower() or "tv shows" in category.lower():
                continue
            # each evlist is a list of events
            if not isinstance(evlist, list):
                continue
            for ev in evlist:
                time_str = ev.get("time", "").strip()
                event_title = ev.get("event", "").strip()
                channels = ev.get("channels", []) or []
                # parse time like '15:57' and combine with schedule_date
                if not time_str:
                    continue
                try:
                    # some times like '00:00' may imply next day if schedule is late-night;
                    # we'll parse naive time and then attach the schedule_date.
                    t = dparser.parse(time_str).time()
                    naive_dt = datetime.combine(schedule_date, t)
                    aware_dt = tz_london.localize(naive_dt)
                    # Heuristic: if event time is before 06:00 and schedule_date is today,
                    # it's possible it's actually after midnight (still same date in file).
                    # We'll leave as-is; user can tell me if adjustments needed.
                except Exception:
                    # if parsing fails, skip
                    continue
                events.append({
                    "category": category,
                    "dt": aware_dt,
                    "event": event_title,
                    "channels": channels
                })
    return events

def filter_and_sort_events(events):
    tz_london = pytz.timezone("Europe/London")
    now = datetime.now(tz=tz_london)
    keep = []
    for e in events:
        start = e["dt"]
        # If event start + KEEP_AFTER_HOURS >= now, keep (i.e. not older than 3 hours after start)
        if start + timedelta(hours=KEEP_AFTER_HOURS) >= now:
            keep.append(e)
    # sort by category then time
    keep_sorted = sorted(keep, key=lambda x: (x["category"].lower(), x["dt"]))
    # group ordering by category preserve order
    grouped = {}
    for e in keep_sorted:
        grouped.setdefault(e["category"], []).append(e)
    return grouped

def build_text_lines(grouped):
    """
    Returns a list of text lines (strings) that will be rendered and scrolled.
    Format:
    [Category]
    HH:MM - Event
    Channel1 / Channel2
    (blank line)
    """
    tz_london = pytz.timezone("Europe/London")
    lines = []
    now = datetime.now(tz=tz_london)
    for cat, evs in grouped.items():
        lines.append(f"[{cat}]")
        for e in evs:
            start = e["dt"]
            time_label = start.strftime("%H:%M")
            
            # FIX: Handle channels safely - they can be dicts, strings, or other types
            channels_list = e.get("channels", []) or []
            channel_names = []
            
            if isinstance(channels_list, list):
                for c in channels_list:
                    if isinstance(c, dict):
                        # It's a dictionary with channel_name
                        channel_names.append(c.get("channel_name", ""))
                    elif isinstance(c, str):
                        # It's already a string
                        channel_names.append(c)
            elif isinstance(channels_list, dict):
                # Sometimes channels might be a dict instead of a list
                # Try to extract channel_name if it exists
                if "channel_name" in channels_list:
                    channel_names.append(channels_list["channel_name"])
            
            channels = " / ".join([name for name in channel_names if name]) or ""
            
            # Event line
            line = f"{time_label} - {e['event']}"
            lines.append(line)
            # Channel line (separate)
            if channels:
                lines.append(f"  {channels}")  # Indented with channels marker
            else:
                lines.append("  ")  # Empty placeholder
            # Gap before next event
            lines.append("")
        # spacing between categories
        lines.append("")
    return lines

def render_text_image(lines, width, padding=40):
    """
    Render a long vertical image containing the lines of text.
    Return PIL.Image object.
    """
    # Auto-detect font if not specified
    font_path_to_use = FONT_PATH
    if not font_path_to_use:
        print("Searching for system fonts...")
        for path in FONT_PATHS_TO_TRY:
            print(f"  Checking: {path} ... ", end="")
            if os.path.isfile(path):
                font_path_to_use = path
                print("FOUND!")
                break
            else:
                print("not found")
        
        if not font_path_to_use:
            # Try to list what fonts ARE available
            print("\nNo fonts found in standard locations. Checking Windows Fonts folder...")
            fonts_dir = r"C:\Windows\Fonts"
            if os.path.isdir(fonts_dir):
                ttf_fonts = [f for f in os.listdir(fonts_dir) if f.lower().endswith('.ttf')]
                if ttf_fonts:
                    print(f"Found {len(ttf_fonts)} TTF fonts. Using first one: {ttf_fonts[0]}")
                    font_path_to_use = os.path.join(fonts_dir, ttf_fonts[0])
                else:
                    print("No TTF fonts found!")
    
    # choose fonts
    if font_path_to_use and os.path.isfile(font_path_to_use):
        try:
            font_title = ImageFont.truetype(font_path_to_use, FONT_SIZE_TITLE)
            font_line = ImageFont.truetype(font_path_to_use, FONT_SIZE_LINE)
            font_channel = ImageFont.truetype(font_path_to_use, FONT_SIZE_CHANNEL)
            print(f"✓ Loaded TrueType fonts: Title={FONT_SIZE_TITLE}px, Line={FONT_SIZE_LINE}px, Channel={FONT_SIZE_CHANNEL}px")
            print(f"  Using font file: {font_path_to_use}")
        except Exception as e:
            print(f"ERROR: Could not load TrueType font: {e}")
            print("Falling back to default font (will be very small)")
            font_title = ImageFont.load_default()
            font_line = ImageFont.load_default()
            font_channel = ImageFont.load_default()
    else:
        print("WARNING: No TrueType font found! Text will be very small.")
        print("Please set FONT_PATH in the script to a valid .ttf file path")
        font_title = ImageFont.load_default()
        font_line = ImageFont.load_default()
        font_channel = ImageFont.load_default()
    
    # compute height
    # Lines starting with '[' are category titles (bold)
    # Lines starting with '  ' (two spaces) are channels
    # Other lines are event titles
    y = padding
    line_metrics = []
    max_w = 0
    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            # Category title
            bbox = font_title.getbbox(line)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            line_metrics.append((line, w, h, 'title'))
            y += h + CATEGORY_SPACING
            max_w = max(max_w, w)
        elif line.startswith("  "):
            # Channel line (smaller font)
            bbox = font_channel.getbbox(line)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            line_metrics.append((line, w, h, 'channel'))
            y += h + LINE_SPACING
            max_w = max(max_w, w)
        elif line.strip() == "":
            # Empty line for spacing
            line_metrics.append((line, 0, EVENT_SPACING, 'empty'))
            y += EVENT_SPACING
        else:
            # Event line
            bbox = font_line.getbbox(line)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            line_metrics.append((line, w, h, 'event'))
            y += h + LINE_SPACING
            max_w = max(max_w, w)
    
    y += padding
    img_w = max(width, max_w + padding * 2)
    img_h = max(y, 300)  # ensure some height
    im = Image.new("RGB", (img_w, img_h), color=(10, 10, 30))
    draw = ImageDraw.Draw(im)
    cur_y = padding
    
    for (line, w, h, line_type) in line_metrics:
        x = padding
        if line_type == 'title':
            # Category title - bold effect by drawing multiple times
            draw.text((x, cur_y), line, font=font_title, fill=(255, 240, 200))
            draw.text((x+1, cur_y), line, font=font_title, fill=(255, 240, 200))
            draw.text((x, cur_y+1), line, font=font_title, fill=(255, 240, 200))
            cur_y += h + CATEGORY_SPACING
        elif line_type == 'channel':
            # Channel line - slightly dimmed
            draw.text((x, cur_y), line, font=font_channel, fill=(180, 180, 200))
            cur_y += h + LINE_SPACING
        elif line_type == 'event':
            # Event line
            draw.text((x, cur_y), line, font=font_line, fill=(220, 220, 220))
            cur_y += h + LINE_SPACING
        elif line_type == 'empty':
            # Just add spacing
            cur_y += h
    
    return im

def make_scrolling_video_from_image(img, out_path, duration=VIDEO_DURATION_SECONDS,
                                    video_size=(VIDEO_WIDTH, VIDEO_HEIGHT), fps=FPS,
                                    audio_path=None):
    """
    Create a vertical scrolling video from a tall image:

    - Starts with blank screen
    - Image scrolls up from bottom
    - Continues until last item scrolls off top
    - Loops 3 times
    - Use MoviePy to animate and write to out_path (MP4).
    - Optionally overlay audio (looped if needed).
    """
    print("  → Saving temporary image...")
    # save the image temporarily so moviepy can use it
    tmp_img = os.path.join(OUTPUT_DIR, "tmp_schedule_image.png")
    img.save(tmp_img)

    W, H = video_size
    img_w, img_h = img.size

    # Calculate scroll distance: start with image fully below screen, end with image fully above screen
    # Start position: y = H (image bottom edge at screen bottom, image not visible)
    # End position: y = -img_h (image top edge at screen top, image not visible)
    start_y = H
    end_y = -img_h
    total_scroll = start_y - end_y  # Total distance to travel
    
    # Calculate duration for one complete scroll
    # We want smooth scrolling that shows everything
    single_loop_duration = max(VIDEO_DURATION_SECONDS, total_scroll / 100.0)  # At least 100 pixels per second
    
    # Total duration for 3 loops
    total_duration = single_loop_duration * 3
    
    print(f"  → Video will be {total_duration:.1f} seconds ({single_loop_duration:.1f}s per loop, 3 loops)")
    print(f"  → Creating video clip...")

    def make_frame(t):
        # Calculate which loop we're in
        loop_progress = (t / single_loop_duration) % 1.0  # 0 to 1 for current loop
        
        # Calculate y position: interpolate from start_y to end_y
        y = start_y - (loop_progress * total_scroll)
        y = int(y)
        
        # Create a blank canvas
        canvas = Image.new("RGB", (W, H), (10, 10, 30))
        
        # Paste the schedule image at the calculated y position
        # Only paste if any part of the image is visible
        if y < H and y + img_h > 0:
            canvas.paste(img, (0, y))
        
        return np.array(canvas)

    video = VideoClip(make_frame, duration=total_duration)

    # set fps
    video = video.set_fps(fps).resize(width=W)

    # attach audio if available
    if audio_path and os.path.isfile(audio_path):
        print(f"  → Loading audio from {audio_path}...")
        try:
            audio = AudioFileClip(audio_path)
            # loop audio to match duration if shorter
            if audio.duration < total_duration:
                print(f"  → Looping audio ({audio.duration:.1f}s → {total_duration:.1f}s)...")
                # calculate how many loops needed
                loops = math.ceil(total_duration / audio.duration)
                # concatenate audio clips
                audio_clips = [audio] * loops
                from moviepy.audio.AudioClip import CompositeAudioClip
                looped_audio = CompositeAudioClip([ac.set_start(i * audio.duration) for i, ac in enumerate(audio_clips)])
                audio = looped_audio.subclip(0, total_duration)
            else:
                audio = audio.subclip(0, total_duration)
            video = video.set_audio(audio)
            print("  → Audio attached")
        except Exception as e:
            print("  → Warning: unable to load audio:", e)
            import traceback
            traceback.print_exc()
    else:
        print("  → No background audio (file not found)")

    print(f"  → Writing MP4 file (this may take 2-3 minutes)...")
    print(f"     Encoding {total_duration:.0f} seconds at {fps} fps = {int(total_duration * fps)} frames")
    # write the mp4
    video.write_videofile(out_path, codec="libx264", audio_codec="aac", threads=0, verbose=False, logger=None)
    print(f"  → Video saved: {out_path}")
    
    # cleanup tmp image
    try:
        os.remove(tmp_img)
    except:
        pass

def sleep_until_5_mins_before_hour():
    """Sleep until 5 minutes before the next hour"""
    now = datetime.now()
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    target_time = next_hour - timedelta(minutes=5)
    
    # If we're past :55, target next hour's :55
    if now >= target_time:
        target_time = target_time + timedelta(hours=1)
    
    seconds = (target_time - now).total_seconds()
    print(f"Sleeping {int(seconds)}s until {target_time.strftime('%Y-%m-%d %H:%M:%S')} (5 mins before hour)")
    time.sleep(seconds)

def sleep_until_top_of_hour():
    """Sleep until the top of the hour"""
    now = datetime.now()
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    seconds = (next_hour - now).total_seconds()
    print(f"Sleeping {int(seconds)}s until {next_hour.strftime('%Y-%m-%d %H:%M:%S')} (top of hour)")
    time.sleep(seconds)

def create_schedule_cycle():
    tz_london = pytz.timezone("Europe/London")
    
    while True:
        try:
            # Phase 1: Generate temp file at :55
            sleep_until_5_mins_before_hour()
            
            print("="*60)
            print("PHASE 1: GENERATING NEW VIDEO")
            print("="*60)
            print(f"Time: {datetime.now(tz=tz_london).isoformat()}")
            
            print("\n[1/5] Fetching schedule JSON...")
            j = fetch_schedule_json()
            print(f"      ✓ Received {len(j)} top-level keys")
            
            print("\n[2/5] Building event list...")
            events = build_event_list(j)
            print(f"      ✓ Found {len(events)} total events")
            
            print("\n[3/5] Filtering and sorting events...")
            grouped = filter_and_sort_events(events)
            total_kept = sum(len(evs) for evs in grouped.values())
            print(f"      ✓ Kept {total_kept} events in {len(grouped)} categories")
            
            print("\n[4/5] Building text layout...")
            lines = build_text_lines(grouped)
            if not lines:
                lines = ["No upcoming events"]
            print(f"      ✓ Generated {len(lines)} lines of text")
            
            print("\n[5/5] Rendering video...")
            # render a tall image
            img = render_text_image(lines, width=VIDEO_WIDTH)
            
            # make video to TEMP file
            audio_path = BACKGROUND_AUDIO if os.path.isfile(BACKGROUND_AUDIO) else None
            make_scrolling_video_from_image(img, TEMP_VIDEO_FILENAME, duration=VIDEO_DURATION_SECONDS, audio_path=audio_path)
            print(f"\n✓ Temp video ready: {os.path.abspath(TEMP_VIDEO_FILENAME)}")
            
            # Phase 2: Wait until top of hour, then swap files
            print("\n" + "="*60)
            print("PHASE 2: WAITING TO SWAP FILES")
            print("="*60)
            sleep_until_top_of_hour()
            
            print("\n" + "="*60)
            print("SWAPPING FILES")
            print("="*60)
            print(f"Time: {datetime.now(tz=tz_london).isoformat()}")
            
            # Delete old file if exists
            if os.path.isfile(VIDEO_FILENAME):
                print(f"  → Deleting old file: {VIDEO_FILENAME}")
                os.remove(VIDEO_FILENAME)
            
            # Rename temp to active
            print(f"  → Renaming {TEMP_VIDEO_FILENAME} → {VIDEO_FILENAME}")
            os.rename(TEMP_VIDEO_FILENAME, VIDEO_FILENAME)
            print(f"\n✓ Video updated successfully!")
            print(f"  Active file: {os.path.abspath(VIDEO_FILENAME)}")
            print("")
            
        except Exception as e:
            print("\n" + "="*60)
            print("ERROR OCCURRED")
            print("="*60)
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            print("\nWaiting for next cycle...")
            # If error, wait until next cycle
            sleep_until_5_mins_before_hour()


if __name__ == "__main__":
    # quick pre-checks
    try:
        subprocess.check_call(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        print("WARNING: ffmpeg not found on PATH. Not required for MP4 generation.")
        
    try:
        import moviepy  # noqa: F401
    except Exception:
        print("ERROR: moviepy or its dependencies missing. Install with:")
        print("    py -m pip install moviepy imageio-ffmpeg pillow")
        exit(1)

    print("Starting schedule->MP4 generator.")
    print("Output directory:", os.path.abspath(OUTPUT_DIR))
    print("Output file: schedule.mp4 (6 minutes - 3 loops of 2 minutes)")
    print("Cycle: Generates at :55, swaps at :00")
    print("")
    print("Press Ctrl+C to stop.")
    print("")
    
    # Check if we should do an initial generation now
    now = datetime.now()
    if not os.path.isfile(VIDEO_FILENAME):
        print("No existing schedule.mp4 found. Generating initial video...")
        try:
            from pytz import timezone
            tz_london = timezone("Europe/London")
            j = fetch_schedule_json()
            events = build_event_list(j)
            grouped = filter_and_sort_events(events)
            lines = build_text_lines(grouped)
            if not lines:
                lines = ["No upcoming events"]
            img = render_text_image(lines, width=VIDEO_WIDTH)
            audio_path = BACKGROUND_AUDIO if os.path.isfile(BACKGROUND_AUDIO) else None
            make_scrolling_video_from_image(img, VIDEO_FILENAME, duration=VIDEO_DURATION_SECONDS, audio_path=audio_path)
            print("Initial MP4 video created:", os.path.abspath(VIDEO_FILENAME))
            print("")
        except Exception as e:
            print("Error creating initial video:", e)
            import traceback
            traceback.print_exc()
            print("")
    
    create_schedule_cycle()