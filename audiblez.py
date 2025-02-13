#!/usr/bin/env python3
# audiblez - A program to convert e-books into audiobooks using
# Kokoro-82M model for high-quality text-to-speech synthesis.
# by Claudio Santini 2025 - https://claudio.uk

import argparse
import os
import sys
import time
import shutil
import subprocess
import soundfile as sf
import ebooklib
import warnings
import re
from pathlib import Path
from string import Formatter
from bs4 import BeautifulSoup
from kokoro_onnx import config
from kokoro_onnx import Kokoro
from ebooklib import epub
from pydub import AudioSegment
from pick import pick
import onnxruntime as ort
from tempfile import NamedTemporaryFile

import torch

from src.torch_inference.models import build_model as kt_build_model
from src.torch_inference.kokoro import generate as kt_generate
from src.torch_inference.kokoro import generate_full as kt_generate_full

MODEL_FILE = 'kokoro-v0_19.onnx'
VOICES_FILE = 'voices.json'
config.MAX_PHONEME_LENGTH = 128


def main(kokoro, file_path, lang, voice, pick_manually, speed, providers):
    

    
    # Set ONNX providers if specified

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    MODEL = kt_build_model('kokoro-v0_19-half.pth', device)
    VOICE_NAME = 'af_sky'
    # [
    # 'af', # Default voice is a 50-50 mix of Bella & Sarah
    # 'af_bella', 'af_sarah', 'am_adam', 'am_michael',
    # 'bf_emma', 'bf_isabella', 'bm_george', 'bm_lewis',
    # 'af_nicole', 'af_sky',
    # ][0]
    VOICEPACK = torch.load(f'voices/{VOICE_NAME}.pt', weights_only=True).to(device)
    print(f'Loaded voice: {VOICE_NAME}')
    if providers:
        available_providers = ort.get_available_providers()
        invalid_providers = [p for p in providers if p not in available_providers]
        if invalid_providers:
            print(f"Invalid ONNX providers: {', '.join(invalid_providers)}")
            print(f"Available providers: {', '.join(available_providers)}")
            sys.exit(1)
        # kokoro.sess.set_providers(providers)
        print(f"Using ONNX providers: {', '.join(providers)}")
    filename = Path(file_path).name
    warnings.simplefilter("ignore")
    book = epub.read_epub(file_path)
    title = book.get_metadata('DC', 'title')[0][0]
    creator = book.get_metadata('DC', 'creator')[0][0]

    cover_maybe = [c for c in book.get_items() if c.get_type() == ebooklib.ITEM_COVER]
    cover_image = cover_maybe[0].get_content() if cover_maybe else b""
    if cover_maybe:
        print(f'Found cover image {cover_maybe[0].file_name} in {cover_maybe[0].media_type} format')

    intro = f'{title} by {creator}'
    print(intro)
    print('Found Chapters:', [c.get_name() for c in book.get_items() if c.get_type() == ebooklib.ITEM_DOCUMENT])
    if pick_manually:
        chapters = pick_chapters(book)
    else:
        chapters = find_chapters(book)
    print('Automatically selected chapters:', [c.get_name() for c in chapters])
    texts = extract_texts(chapters)

    has_ffmpeg = shutil.which('ffmpeg') is not None
    if not has_ffmpeg:
        print('\033[91m' + 'ffmpeg not found. Please install ffmpeg to create mp3 and m4b audiobook files.' + '\033[0m')

    total_chars, processed_chars = sum(map(len, texts)), 0
    print('Started at:', time.strftime('%H:%M:%S'))
    print(f'Total characters: {total_chars:,}')
    print('Total words:', len(' '.join(texts).split()))
    chars_per_sec = 50  # assume 50 chars per second at the beginning
    print(f'Estimated time remaining (assuming 50 chars/sec): {strfdelta((total_chars - processed_chars) / chars_per_sec)}')

    chapter_mp3_files = []
    durations = {}

    for i, text in enumerate(texts, start=1):
        chapter_filename = filename.replace('.epub', f'_chapter_{i}.wav')
        chapter_mp3_files.append(chapter_filename)
        if Path(chapter_filename).exists():
            print(f'File for chapter {i} already exists. Skipping')
            continue
        if len(text.strip()) < 10:
            print(f'Skipping empty chapter {i}')
            chapter_mp3_files.remove(chapter_filename)
            continue
        print(f'Reading chapter {i} ({len(text):,} characters)...')
        if i == 1:
            text = intro + '.\n\n' + text
        start_time = time.time()
        
        # This is the code to replace
        #samples, sample_rate = kokoro.create(text, voice=voice, speed=speed, lang=lang)
        sample_rate = 24000
        #def generate(model, text, voicepack, lang='a', speed=1, ps=None):
        samples, ps = kt_generate_full(
            model=MODEL, text=text, voicepack=VOICEPACK
        )
        
        sf.write(f'{chapter_filename}', samples, sample_rate)
        durations[chapter_filename] = len(samples) / sample_rate
        end_time = time.time()
        delta_seconds = end_time - start_time
        chars_per_sec = len(text) / delta_seconds
        processed_chars += len(text)
        print(f'Estimated time remaining: {strfdelta((total_chars - processed_chars) / chars_per_sec)}')
        print('Chapter written to', chapter_filename)
        print(f'Chapter {i} read in {delta_seconds:.2f} seconds ({chars_per_sec:.0f} characters per second)')
        progress = processed_chars * 100 // total_chars
        print('Progress:', f'{progress}%\n')

    if has_ffmpeg:
        create_index_file(title, creator, chapter_mp3_files, durations)
        create_m4b(chapter_mp3_files, filename, title, creator, cover_image)


def extract_texts(chapters):
    texts = []
    for chapter in chapters:
        xml = chapter.get_body_content()
        soup = BeautifulSoup(xml, features='lxml')
        chapter_text = ''
        html_content_tags = ['title', 'p', 'h1', 'h2', 'h3', 'h4']
        for child in soup.find_all(html_content_tags):
            inner_text = child.text.strip() if child.text else ""
            if inner_text:
                chapter_text += inner_text + '\n'
        texts.append(chapter_text)
    return texts


def is_chapter(c):
    name = c.get_name().lower()
    return bool(
        'chapter' in name.lower()
        or re.search(r'part\d{1,3}', name)
        or re.search(r'ch\d{1,3}', name)
        or re.search(r'chap\d{1,3}', name)
    )


def find_chapters(book, verbose=False):
    chapters = [c for c in book.get_items() if c.get_type() == ebooklib.ITEM_DOCUMENT and is_chapter(c)]
    if verbose:
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                print(f"'{item.get_name()}'" + ', #' + str(len(item.get_body_content())))
                # print(f'{item.get_name()}'.ljust(60), str(len(item.get_body_content())).ljust(15), 'X' if item in chapters else '-')
    if len(chapters) == 0:
        print('Not easy to find the chapters, defaulting to all available documents.')
        chapters = [c for c in book.get_items() if c.get_type() == ebooklib.ITEM_DOCUMENT]
    return chapters


def pick_chapters(book):
    all_chapters_names = [c.get_name() for c in book.get_items() if c.get_type() == ebooklib.ITEM_DOCUMENT]
    title = 'Select which chapters to read in the audiobook'
    selected_chapters_names = pick(all_chapters_names, title, multiselect=True, min_selection_count=1)
    selected_chapters_names = [c[0] for c in selected_chapters_names]
    selected_chapters = [c for c in book.get_items() if c.get_name() in selected_chapters_names]
    return selected_chapters


def strfdelta(tdelta, fmt='{D:02}d {H:02}h {M:02}m {S:02}s'):
    remainder = int(tdelta)
    f = Formatter()
    desired_fields = [field_tuple[1] for field_tuple in f.parse(fmt)]
    possible_fields = ('W', 'D', 'H', 'M', 'S')
    constants = {'W': 604800, 'D': 86400, 'H': 3600, 'M': 60, 'S': 1}
    values = {}
    for field in possible_fields:
        if field in desired_fields and field in constants:
            values[field], remainder = divmod(remainder, constants[field])
    return f.format(fmt, **values)


def create_m4b(chapter_files, filename, title, author, cover_image):
    tmp_filename = filename.replace('.epub', '.tmp.mp4')
    if not Path(tmp_filename).exists():
        combined_audio = AudioSegment.empty()
        for wav_file in chapter_files:
            audio = AudioSegment.from_wav(wav_file)
            combined_audio += audio
        print('Converting to Mp4...')
        combined_audio.export(tmp_filename, format="mp4", codec="aac", bitrate="64k")
    final_filename = filename.replace('.epub', '.m4b')
    print('Creating M4B file...')

    if cover_image:
        # Create the temporary file but don't lock it
        cover_image_file = NamedTemporaryFile("wb", delete=False)
        try:
            cover_image_file.write(cover_image)
            cover_image_file.close()  # Explicitly close to release the file lock on Windows
            cover_image_args = ["-i", cover_image_file.name, "-map", "0:a", "-map", "2:v"]
        finally:
            # Ensure the file is deleted after FFmpeg finishes
            cover_image_path = cover_image_file.name
    else:
        cover_image_args = []
        cover_image_path = None

    proc = subprocess.run([
        'ffmpeg',
        '-i', f'{tmp_filename}',
        '-i', 'chapters.txt',
        *cover_image_args,
        '-map', '0',
        '-map_metadata', '1',
        '-c:a', 'copy',
        '-c:v', 'copy',
        '-disposition:v', 'attached_pic',
        '-c', 'copy',
        '-f', 'mp4',
        f'{final_filename}'
    ])

    # Clean up temporary files
    Path(tmp_filename).unlink()
    if cover_image_path:
        Path(cover_image_path).unlink()

    if proc.returncode == 0:
        print(f'{final_filename} created. Enjoy your audiobook.')
        print('Feel free to delete the intermediary .wav chapter files, the .m4b is all you need.')


def probe_duration(file_name):
    args = ['ffprobe', '-i', file_name, '-show_entries', 'format=duration', '-v', 'quiet', '-of', 'default=noprint_wrappers=1:nokey=1']
    proc = subprocess.run(args, capture_output=True, text=True, check=True)
    return float(proc.stdout.strip())


def create_index_file(title, creator, chapter_mp3_files, durations):
    with open("chapters.txt", "w") as f:
        f.write(f";FFMETADATA1\ntitle={title}\nartist={creator}\n\n")
        start = 0
        i = 0
        for c in chapter_mp3_files:
            if c not in durations:
                durations[c] = probe_duration(c)
            end = start + (int)(durations[c] * 1000)
            f.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={start}\nEND={end}\ntitle=Chapter {i}\n\n")
            i += 1
            start = end


def cli_main():
    # if not Path(MODEL_FILE).exists() or not Path(VOICES_FILE).exists():
    #     print('Error: kokoro-v0_19.onnx and voices.json must be in the current directory. Please download them with:')
    #     print('wget https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx')
    #     print('wget https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.json')
    #     sys.exit(1)
    #kokoro = Kokoro(MODEL_FILE, VOICES_FILE)
    kokoro = None
    #voices = list(kokoro.get_voices())
    #voices_str = ', '.join(voices)
    epilog = 'example:\n' + \
             '  audiblez book.epub -l en-us -v af_sky'
    default_voice = 'af_sky' #if 'af_sky' in voices else voices[0]

    # Get available ONNX providers
    available_providers = ort.get_available_providers()
    providers_help = f"Available ONNX providers: {', '.join(available_providers)}"

    parser = argparse.ArgumentParser(epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('epub_file_path', help='Path to the epub file')
    parser.add_argument('-l', '--lang', default='en-gb', help='Language code: en-gb, en-us, fr-fr, ja, ko, cmn')
    parser.add_argument('-v', '--voice', default=default_voice, help=f'Choose narrating voice: voices_str')
    parser.add_argument('-p', '--pick', default=False, help=f'Interactively select which chapters to read in the audiobook',
                        action='store_true')
    parser.add_argument('-s', '--speed', default=1.0, help=f'Set speed from 0.5 to 2.0', type=float)
    parser.add_argument('--providers', nargs='+', metavar='PROVIDER', help=f"Specify ONNX providers. {providers_help}")

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)
    args = parser.parse_args()
    main(kokoro, args.epub_file_path, args.lang, args.voice, args.pick, args.speed, args.providers)


if __name__ == '__main__':
    os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = r"C:\Program Files\eSpeak NG\libespeak-ng.dll"
    os.environ["PHONEMIZER_ESPEAK_PATH"] = r"C:\Program Files\eSpeak NG\espeak-ng.exe"
    cli_main()
