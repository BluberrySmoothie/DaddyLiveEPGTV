"""
whatson.py

Generates a scrolling schedule video from the DaddyLiveHD HTML schedule.
Refreshes every hour on the hour.

Usage:
    py whatson.py

Dependencies (install with):
    py -m pip install requests python-dateutil pytz moviepy imageio-ffmpeg pillow beautifulsoup4

Important:
- Place an optional background audio file named "background.mp3" next to this script if you want music.
- Output is written to ./output/schedule.mp4 (6 minutes total - 3 loops of 2 minutes each)

Behavior summary:
- Fetches HTML from https://dlhd.dad/
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
from bs4 import BeautifulSoup  # <-- ADDED
from dateutil import parser as dparser
from dateutil import tz
import pytz
from moviepy.editor import (
    ImageClip, AudioFileClip, VideoClip, CompositeVideoClip, concatenate_videoclips
)
from PIL import Image, ImageDraw, ImageFont
import numpy as np

# Config
SCHEDULE_URL = "https://dlhd.dad/"  # <-- UPDATED
REFERER = "https://dlhd.dad/"      # <-- UPDATED
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

# --- OLD FUNCTIONS REMOVED ---
# def fetch_schedule_json(url=SCHEDULE_URL):
# def choose_date_from_top_key(json_obj):

# --- NEW FUNCTIONS ADDED/MODIFIED ---

def get_schedule_date(soup):
    """
    Parses the date from the <div class="schedule__dayTitle"> element.
    e.g., "Saturday 25th Oct 2025 - Schedule Time UK GMT"
    """
    try:
        title_element = soup.find('div', class_='schedule__dayTitle')
        if not title_element:
            raise ValueError("Could not find 'schedule__dayTitle' element")
        
        date_part = title_element.get_text(strip=True).split(" - ")[0].strip()
        parsed_date = dparser.parse(date_part, fuzzy=True).date()
        return parsed_date
    except Exception as e:
        print(f"Warning: couldn't parse date from HTML title. Falling back to today's UK date. Error: {e}")
        return datetime.now(tz=tz.gettz("Europe/London")).date()

def fetch_and_build_event_list():
    """
    Fetches the HTML from SCHEDULE_URL, parses it with BeautifulSoup,
    and builds the event list in the format expected by the rest of the script.
    """
    headers = {"Referer": REFERER, "User-Agent": "schedule-generator/1.0"}
    try:
        print("Fetching HTML schedule from:", SCHEDULE_URL)
        r = requests.get(SCHEDULE_URL, headers=headers, timeout=20)
        r.raise_for_status()
        print("Successfully fetched HTML.")
    except requests.RequestException as e:
        print(f"Error fetching HTML: {e}")
        return []

    soup = BeautifulSoup(r.text, 'html.parser')
    
    # Get the base date for the schedule
    schedule_date = get_schedule_date(soup)
    tz_london = pytz.timezone("Europe/London")
    events = []  # dicts with keys: category, dt (aware), event, channels (list)

    # Find all category blocks
    category_blocks = soup.find_all('div', class_='schedule__category')
    
    for category_block in category_blocks:
        # Get category name
        category_header = category_block.find('div', class_='schedule__catHeader')
        if not category_header:
            continue
            
        category_meta = category_header.find('div', class_='card__meta')
        if not category_meta:
            continue
            
        category_name = category_meta.get_text(strip=True)

        # skip TV Show categories (case-insensitive substring)
        if "tv show" in category_name.lower() or "tv shows" in category_name.lower():
            continue

        # Find all events in this category
        event_blocks = category_block.find_all('div', class_='schedule__event')
        
        for event_block in event_blocks:
            time_str_elem = event_block.find('span', class_='schedule__time')
            event_title_elem = event_block.find('span', class_='schedule__eventTitle')
            
            if not time_str_elem or not event_title_elem:
                continue

            time_str = time_str_elem.get_text(strip=True)
            event_title = event_title_elem.get_text(strip=True)
            
            # Get channels
            channels = []
            channels_container = event_block.find('div', class_='schedule__channels')
            if channels_container:
                channel_links = channels_container.find_all('a')
                for link in channel_links:
                    channels.append(link.get_text(strip=True))
            
            if not time_str:
                continue

            try:
                # parse time like '15:57' and combine with schedule_date
                t = dparser.parse(time_str).time()
                naive_dt = datetime.combine(schedule_date, t)
                aware_dt = tz_london.localize(naive_dt)
            except Exception as e:
                print(f"Warning: Could not parse time '{time_str}' for event '{event_title}'. Skipping. Error: {e}")
                continue

            events.append({
                "category": category_name,
                "dt": aware_dt,
                "event": event_title,
                "channels": channels
            })

    print(f"Parsed {len(events)} events from HTML.")
    return events


# --- SCRIPT REMAINS UNCHANGED FROM THIS POINT DOWN ---
# (Except for the main block at the very end)

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
    Returns a list of text lines (strings) that will be
    drawn onto the image, based on the sorted/grouped events.
    """
    lines = []
    for category, events in grouped.items():
        lines.append(f"TITLE:{category}")
        for e in events:
            time_str = e["dt"].strftime("%H:%M")
            lines.append(f"LINE:{time_str} {e['event']}")
            if e["channels"]:
                # add channel line, indented
                lines.append(f"CHANNEL:    ({', '.join(e['channels'])})")
            lines.append(f"SPACER:{EVENT_SPACING}") # small gap
        lines.append(f"SPACER:{CATEGORY_SPACING}") # big gap
    return lines

def find_font():
    """Find a usable font from the preferred list."""
    global FONT_PATH
    if FONT_PATH and os.path.isfile(FONT_PATH):
        return FONT_PATH

    for font_path in FONT_PATHS_TO_TRY:
        if os.path.isfile(font_path):
            FONT_PATH = font_path
            print(f"Using font: {FONT_PATH}")
            return FONT_PATH
    
    print("Warning: No preferred fonts found. Relying on PIL default font.")
    FONT_PATH = "default"
    return None

def load_font(size):
    """Loads a font at a specific size, falling back to default if needed."""
    font_path = find_font()
    if font_path == "default":
        try:
            # Try to get a default font, size might not be respected
            return ImageFont.load_default()
        except IOError:
            print("Error: Cannot load default PIL font.")
            return None
    try:
        return ImageFont.truetype(font_path, size)
    except IOError:
        print(f"Error: Could not load font {font_path}. Trying default.")
        try:
            return ImageFont.load_default()
        except IOError:
            print("Error: Cannot load default PIL font.")
            return None

def render_text_image(lines, width):
    """
    Renders the schedule (list of lines) onto a tall PIL Image.
    Returns the PIL Image object.
    """
    # Load fonts
    font_title = load_font(FONT_SIZE_TITLE)
    font_line = load_font(FONT_SIZE_LINE)
    font_channel = load_font(FONT_SIZE_CHANNEL)
    if not font_title or not font_line:
        print("Error: Critical fonts failed to load. Exiting.")
        sys.exit(1)

    # First, measure the total height needed
    total_height = 0
    padding_top = VIDEO_HEIGHT // 2  # Start with half a screen of blank space
    padding_bottom = VIDEO_HEIGHT # End with a full screen of blank space
    total_height += padding_top
    
    for line in lines:
        if line.startswith("TITLE:"):
            total_height += FONT_SIZE_TITLE + LINE_SPACING
        elif line.startswith("LINE:"):
            total_height += FONT_SIZE_LINE + LINE_SPACING
        elif line.startswith("CHANNEL:"):
            total_height += FONT_SIZE_CHANNEL + LINE_SPACING
        elif line.startswith("SPACER:"):
            total_height += int(line.split(":")[1])
    
    total_height += padding_bottom
    
    # Create the image
    img = Image.new('RGB', (width, total_height), color=(0, 0, 0)) # Black background
    d = ImageDraw.Draw(img)
    
    # Start drawing
    y = padding_top
    text_x = 40 # Left padding
    
    for line in lines:
        if line.startswith("TITLE:"):
            text = line.replace("TITLE:", "")
            d.text((text_x, y), text, font=font_title, fill=(255, 255, 0)) # Yellow
            y += FONT_SIZE_TITLE + LINE_SPACING
        elif line.startswith("LINE:"):
            text = line.replace("LINE:", "")
            d.text((text_x, y), text, font=font_line, fill=(255, 255, 255)) # White
            y += FONT_SIZE_LINE + LINE_SPACING
        elif line.startswith("CHANNEL:"):
            text = line.replace("CHANNEL:", "")
            d.text((text_x, y), text, font=font_channel, fill=(160, 160, 160)) # Gray
            y += FONT_SIZE_CHANNEL + LINE_SPACING
        elif line.startswith("SPACER:"):
            y += int(line.split(":")[1])
            
    print(f"Rendered image: {width}x{total_height} pixels")
    return img

def make_scrolling_video_from_image(img, output_path, duration, audio_path=None):
    """
    Converts the tall PIL image into a scrolling video.
    """
    img_width, img_height = img.size
    
    # Convert PIL Image to numpy array for MoviePy
    img_np = np.array(img)
    
    # Create the ImageClip
    img_clip = ImageClip(img_np)
    
    # Total distance to scroll is the image height minus one screen height
    scroll_height = img_height - VIDEO_HEIGHT
    
    if scroll_height <= 0:
        print("Image is not tall enough to scroll. Creating static video.")
        # Just show the top of the image, static
        scrolled_clip = img_clip.set_position((0, 0)).set_duration(duration)
    else:
        # Define the scrolling animation
        # (t) goes from 0 to `duration`
        def scroll(t):
            # Calculate y position based on time
            # Linear scroll: y = - (t / duration) * scroll_height
            # We add a 1-second pause at the start
            pause_at_start = 1.0
            scroll_duration = duration - pause_at_start
            
            if t < pause_at_start:
                y = 0
            else:
                # Ease-in-out effect for scrolling
                # x is progress from 0 to 1
                x = (t - pause_at_start) / scroll_duration
                # Use a cosine ease-in-out curve
                ease_x = 0.5 * (1 - math.cos(x * math.pi))
                y = -ease_x * scroll_height

            return (0, int(y)) # (x, y) position

        scrolled_clip = VideoClip(make_frame=lambda t: img_clip.get_frame(t), duration=duration)
        scrolled_clip = scrolled_clip.set_position(scroll)

    # Create a final composite on a black background
    final_clip = CompositeVideoClip(
        [scrolled_clip],
        size=(VIDEO_WIDTH, VIDEO_HEIGHT),
        bg_color=(0, 0, 0)
    ).set_duration(duration)
    
    # Add audio
    if audio_path and os.path.isfile(audio_path):
        print(f"Adding audio from {audio_path}")
        try:
            audio = AudioFileClip(audio_path).subclip(0, duration)
            # If audio is shorter than video, loop it
            if audio.duration < duration:
                audio = audio.fx(vfx.loop, duration=duration)
            final_clip = final_clip.set_audio(audio)
        except Exception as e:
            print(f"Warning: Could not process audio file {audio_path}. Video will be silent. Error: {e}")
            final_clip = final_clip.set_audio(None)
    else:
        print("No background.mp3 found. Video will be silent.")
        final_clip = final_clip.set_audio(None)

    # Write the file
    print(f"Writing video to {output_path}...")
    final_clip.write_videofile(
        output_path,
        fps=FPS,
        codec='libx264',
        audio_codec='aac',
        temp_audiofile='temp-audio.m4a',
        remove_temp=True,
        preset='medium',
        threads=4
    )
    print("Video write complete.")


def generate_full_video_cycle():
    """
    Fetches, builds, and renders the video, creating 3 loops.
    Writes to a temp file first, then swaps.
    """
    print(f"--- Starting new cycle at {datetime.now()} ---")
    
    # 1. Fetch and parse data
    try:
        events = fetch_and_build_event_list() # <-- MODIFIED
        grouped = filter_and_sort_events(events)
        lines = build_text_lines(grouped)
        if not lines:
            lines = ["TITLE:No upcoming events"]
    except Exception as e:
        print(f"CRITICAL: Failed to fetch or parse schedule: {e}")
        # Don't overwrite existing video if fetch fails
        return

    # 2. Render image
    try:
        img = render_text_image(lines, width=VIDEO_WIDTH)
    except Exception as e:
        print(f"CRITICAL: Failed to render text image: {e}")
        return

    # 3. Make video (single loop)
    try:
        audio_path = BACKGROUND_AUDIO if os.path.isfile(BACKGROUND_AUDIO) else None
        # Write to a temporary file first
        make_scrolling_video_from_image(img, TEMP_VIDEO_FILENAME, duration=VIDEO_DURATION_SECONDS, audio_path=audio_path)
    except Exception as e:
        print(f"CRITICAL: Failed to generate video: {e}")
        return

    # 4. Concatenate loops and move to final file
    try:
        print("Looping video 3 times...")
        with VideoFileClip(TEMP_VIDEO_FILENAME) as clip:
            final_loop = concatenate_videoclips([clip, clip, clip])
            # Use 'ffmpeg' for the final write, it's often faster for simple concats
            final_loop.write_videofile(
                VIDEO_FILENAME,
                codec='libx264',
                audio_codec='aac',
                temp_audiofile='temp-audio.m4a',
                remove_temp=True,
                preset='fast', # faster preset for loop concat
                threads=4,
                logger='bar' # show progress
            )
        print("Looping complete. Final file updated:", VIDEO_FILENAME)
        if os.path.isfile(TEMP_VIDEO_FILENAME):
            os.remove(TEMP_VIDEO_FILENAME)
    except Exception as e:
        print(f"CRITICAL: Failed to loop video and swap file: {e}")
        

def wait_for_next_run():
    """
    Waits until 55 minutes past the hour.
    """
    while True:
        now = datetime.now()
        # We want to generate at :55 so it's ready to swap at :00
        if now.minute == 55:
            print(f"Time is {now.strftime('%H:%M:%S')}. Starting generation.")
            return
        
        # Calculate time until next :55
        if now.minute > 55:
            # Need to wait for next hour's 55
            next_run = (now + timedelta(hours=1)).replace(minute=55, second=0, microsecond=0)
        else:
            # Wait for this hour's 55
            next_run = now.replace(minute=55, second=0, microsecond=0)
            
        wait_seconds = (next_run - now).total_seconds()
        
        if wait_seconds > 60:
            print(f"Sleeping until {next_run.strftime('%H:%M:%S')}...")
            time.sleep(wait_seconds)
        else:
            # If we are very close, just sleep 1 sec and check again
            time.sleep(1)


def main():
    # --- Check for dependencies ---
    try:
        # Check for ffmpeg binary
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: ffmpeg executable not found.")
        print("Please install ffmpeg and ensure it is in your system's PATH.")
        print("You can often install it via: py -m pip install imageio-ffmpeg")
        exit(1)
        
    if 'moviepy' not in sys.modules or 'PIL' not in sys.modules:
        print("Error: Missing required Python libraries.")
        print("Please install all dependencies with:")
        print("py -m pip install requests python-dateutil pytz moviepy imageio-ffmpeg pillow")
        exit(1)

    print("Starting schedule->MP4 generator.")
    print("Output directory:", os.path.abspath(OUTPUT_DIR))
    print("Output file: schedule.mp4 (6 minutes - 3 loops of 2 minutes)\n")
    print("Press Ctrl+C to stop.\n")
    
    # --- Initial generation on first run ---
    if not os.path.isfile(VIDEO_FILENAME):
        print("No existing schedule.mp4 found. Generating initial video...\n")
        try:
            events = fetch_and_build_event_list() # <-- MODIFIED
            grouped = filter_and_sort_events(events)
            lines = build_text_lines(grouped)
            if not lines:
                lines = ["TITLE:No upcoming events"]
            img = render_text_image(lines, width=VIDEO_WIDTH)
            
            # --- Make single loop first for temp file ---
            audio_path = BACKGROUND_AUDIO if os.path.isfile(BACKGROUND_AUDIO) else None
            
            # -----------------------------------------------------------------
            # !!! THIS IS THE CORRECTED LINE !!!
            # I was passing TEMP_VIDEO_FILENAME as the 'img' argument by mistake.
            make_scrolling_video_from_image(img, TEMP_VIDEO_FILENAME, duration=VIDEO_DURATION_SECONDS, audio_path=audio_path)
            # -----------------------------------------------------------------
            
            # --- Loop it for the final file ---
            with VideoFileClip(TEMP_VIDEO_FILENAME) as clip:
                final_loop = concatenate_videoclips([clip, clip, clip])
                final_loop.write_videofile(
                    VIDEO_FILENAME,
                    codec='libx264',
                    audio_codec='aac',
                    temp_audiofile='temp-audio.m4a',
                    remove_temp=True,
                    preset='fast',
                    threads=4,
                    logger='bar'
                )
            
            print("\nInitial MP4 video created:", os.path.abspath(VIDEO_FILENAME))
            if os.path.isfile(TEMP_VIDEO_FILENAME):
                os.remove(TEMP_VIDEO_FILENAME)
            
        except Exception as e:
            print(f"CRITICAL: Failed to create initial video: {e}")
            # We can still continue and try the main loop
    
    # --- Main hourly loop ---
    print("\n--- Entering main loop ---")
    try:
        while True:
            wait_for_next_run()
            generate_full_video_cycle()
            print(f"\n--- Cycle complete. Next check at {datetime.now() + timedelta(seconds=60)} ---")
            time.sleep(60) # Sleep 60s to ensure we don't run twice in the same minute
    except KeyboardInterrupt:
        print("\nStopping script.")
        sys.exit(0)

if __name__ == "__main__":
    main()