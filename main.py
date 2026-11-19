#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GTA 6 FR Clipper (prototype)

1. Surveille les lives Twitch francais de la categorie GTA VI
2. Garde ~90 s de cache par stream (streamlink + ffmpeg, en copie, tres leger)
3. Detecte les pics d'activite du chat -> coupe le moment
4. Monte en vertical 9:16 : titre, sous-titres (optionnel), outro "Abonne-toi"
5. File d'attente -> envoi du clip fini sur ton Telegram (a publier en 2 appuis)
"""
import glob
import math
import os
import queue
import random
import re
import shutil
import socket
import statistics
import subprocess
import sys
import textwrap
import threading
import time
from collections import deque

import requests


# ----------------------------------------------------------------- reglages
def _i(name, default):
    return int(os.environ.get(name, default))


def _f(name, default):
    return float(os.environ.get(name, default))


CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "")
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "")
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "")
YT_PRIVACY = os.environ.get("YT_PRIVACY", "private") or "private"
TT_CLIENT_KEY = os.environ.get("TT_CLIENT_KEY", "")
TT_CLIENT_SECRET = os.environ.get("TT_CLIENT_SECRET", "")
TT_REFRESH_TOKEN = os.environ.get("TT_REFRESH_TOKEN", "")

GAME_NAME = os.environ.get("GAME_NAME", "Grand Theft Auto VI")
LANGUAGE = os.environ.get("STREAM_LANGUAGE", "fr")

MAX_STREAMS = _i("MAX_STREAMS", 8)            # streams surveilles en meme temps
SEG_LEN = 4                                   # secondes par segment de cache
KEEP_SEGMENTS = 24                            # 24 x 4 s = 96 s de cache
CLIP_SECONDS = _i("CLIP_SECONDS", 76)         # duree du clip (> 1 minute)
POST_DELAY = _i("POST_DELAY", 7)              # on attend un peu apres le pic
WINDOW = 8                                    # fenetre "recente" du chat (s)
BASE_WINDOW = 180                             # fenetre de reference (s)
MIN_MSGS = _i("MIN_MSGS", 15)                 # messages min dans la fenetre
RATIO = _f("RATIO", 3.0)                      # pic = X fois le niveau normal
AUDIO_DB = _f("AUDIO_DB", 6.0)                # saut de volume (dB) = moment fort
VIS_RATIO = _f("VIS_RATIO", 2.0)              # activite a l'ecran x fois la normale
VIS_MIN = _f("VIS_MIN", 0.05)                 # activite visuelle minimale
CROP_X = _f("CROP_X", 0.5)                    # cadrage vertical : 0 gauche, 0.5 centre, 1 droite
COOLDOWN = _i("COOLDOWN", 240)                # pause par streamer (s)
MAX_CLIPS_PER_RUN = _i("MAX_CLIPS_PER_RUN", 12)
REFRESH = _i("REFRESH", 120)                  # rafraichit la liste des lives
RUN_SECONDS = _i("RUN_SECONDS", 20700)        # 5 h 45 puis relais auto
DRAIN_SECONDS = _i("DRAIN_SECONDS", 480)      # temps max pour finir la file
ENABLE_SUBS = os.environ.get("ENABLE_SUBS", "1") == "1"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
ROOT = os.path.abspath("work")
BUF_DIR = os.path.join(ROOT, "buffers")
JOB_DIR = os.path.join(ROOT, "jobs")

TITLES = {
    "audio": [
        "{n} hurle sur GTA 6, ca part en vrille",
        "Le cri de {n} sur GTA 6 est legendaire",
        "{n} perd completement le controle sur GTA 6",
    ],
    "chat": [
        "Le chat de {n} explose sur GTA 6",
        "Tout le chat de {n} devient fou sur GTA 6",
        "{n} met le feu au chat sur GTA 6",
    ],
    "action": [
        "Cette scene sur GTA 6 est completement folle",
        "Personne n'etait pret pour ca sur GTA 6",
        "Mais qu'est-ce qui vient de se passer sur GTA 6 ?!",
    ],
    "generic": [
        "{n} vit un moment INCROYABLE sur GTA 6",
        "GTA 6 : {n} vient de tout casser",
        "Attends la fin, c'est dingue sur GTA 6",
    ],
}
EMOJIS = ["🔥", "😱", "🤯", "😂", "💀"]
HASHTAGS = "#GTA6 #GTAVI #Shorts #Gaming #JeuxVideo #Twitch #GTA #Clips"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ----------------------------------------------------------------- telegram
_chat_id = TG_CHAT


def tg_chat_id():
    global _chat_id
    if _chat_id:
        return _chat_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", timeout=20
        ).json()
        for u in reversed(r.get("result", [])):
            m = u.get("message") or u.get("channel_post")
            if m:
                _chat_id = str(m["chat"]["id"])
                return _chat_id
    except Exception as e:
        log("telegram: getUpdates en erreur", type(e).__name__)
    log("telegram: aucun chat trouve. Envoie /start a ton bot.")
    return ""


def tg_text(text):
    cid = tg_chat_id()
    if not cid:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": cid, "text": text},
            timeout=30,
        )
    except Exception as e:
        log("telegram: message en erreur", type(e).__name__)


def tg_video(path, caption):
    cid = tg_chat_id()
    if not cid:
        return False
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendVideo",
                data={
                    "chat_id": cid,
                    "caption": caption[:1000],
                    "supports_streaming": "true",
                    "width": 1080,
                    "height": 1920,
                },
                files={"video": f},
                timeout=600,
            )
        ok = r.status_code == 200
        if not ok:
            log("telegram: envoi refuse", r.status_code)
        return ok
    except Exception as e:
        log("telegram: video en erreur", type(e).__name__)
        return False


# ------------------------------------------------------------------ youtube
def yt_enabled():
    return bool(YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN)


def yt_upload(path, title, description):
    """Depose le clip sur ta chaine. 'private' tant que l'audit YouTube n'est pas passe."""
    try:
        t = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": YT_CLIENT_ID,
                "client_secret": YT_CLIENT_SECRET,
                "refresh_token": YT_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        t.raise_for_status()
        access = t.json()["access_token"]
        meta = {
            "snippet": {
                "title": title[:95],
                "description": description[:4900],
                "tags": ["GTA6", "GTAVI", "Shorts", "Gaming"],
                "categoryId": "20",
            },
            "status": {
                "privacyStatus": YT_PRIVACY,
                "selfDeclaredMadeForKids": False,
            },
        }
        size = os.path.getsize(path)
        init = requests.post(
            "https://www.googleapis.com/upload/youtube/v3/videos"
            "?uploadType=resumable&part=snippet,status",
            headers={
                "Authorization": "Bearer " + access,
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4",
            },
            json=meta,
            timeout=60,
        )
        init.raise_for_status()
        with open(path, "rb") as f:
            up = requests.put(
                init.headers["Location"],
                data=f,
                headers={"Content-Length": str(size), "Content-Type": "video/mp4"},
                timeout=900,
            )
        up.raise_for_status()
        return "https://youtube.com/shorts/" + up.json()["id"]
    except requests.HTTPError as e:
        log("youtube: erreur", e.response.status_code, e.response.text[:200])
    except Exception as e:
        log("youtube: erreur", type(e).__name__)
    return None


# ------------------------------------------------------------------- tiktok
_tt = {"tok": "", "exp": 0}


def tt_enabled():
    return bool(TT_CLIENT_KEY and TT_CLIENT_SECRET and TT_REFRESH_TOKEN)


def tt_token():
    if _tt["tok"] and time.time() < _tt["exp"]:
        return _tt["tok"]
    r = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": TT_CLIENT_KEY,
            "client_secret": TT_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": TT_REFRESH_TOKEN,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    j = r.json()
    if "access_token" not in j:
        raise RuntimeError("token tiktok refuse: " + str(j.get("error", "?")))
    _tt["tok"] = j["access_token"]
    _tt["exp"] = time.time() + int(j.get("expires_in", 86400)) - 600
    if j.get("refresh_token") and j["refresh_token"] != TT_REFRESH_TOKEN:
        log("tiktok: le refresh token a change, a renouveler dans les secrets")
    return _tt["tok"]


def tt_upload(path):
    """Envoie le clip dans ta boite de reception TikTok (brouillon a finir dans l'appli)."""
    try:
        tok = tt_token()
        size = os.path.getsize(path)
        h = {"Authorization": "Bearer " + tok,
             "Content-Type": "application/json; charset=UTF-8"}
        init = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
            headers=h,
            json={"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                  "chunk_size": size, "total_chunk_count": 1}},
            timeout=60,
        ).json()
        data = init.get("data") or {}
        if not data.get("upload_url"):
            log("tiktok: init refuse", (init.get("error") or {}).get("code"))
            return False
        with open(path, "rb") as f:
            up = requests.put(
                data["upload_url"],
                data=f,
                headers={"Content-Range": f"bytes 0-{size - 1}/{size}",
                         "Content-Type": "video/mp4",
                         "Content-Length": str(size)},
                timeout=600,
            )
        if up.status_code not in (200, 201):
            log("tiktok: upload refuse", up.status_code)
            return False
        return True
    except Exception as e:
        log("tiktok: erreur", type(e).__name__, str(e)[:120] if isinstance(e, RuntimeError) else "")
        return False


# ------------------------------------------------------------------- twitch
class Twitch:
    def __init__(self):
        self.token = None
        self.exp = 0

    def _auth(self):
        if self.token and time.time() < self.exp:
            return
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=20,
        )
        r.raise_for_status()
        j = r.json()
        self.token = j["access_token"]
        self.exp = time.time() + j.get("expires_in", 3600) - 300

    def get(self, path, params):
        self._auth()
        r = requests.get(
            "https://api.twitch.tv/helix/" + path,
            params=params,
            headers={
                "Client-Id": CLIENT_ID,
                "Authorization": "Bearer " + self.token,
            },
            timeout=20,
        )
        r.raise_for_status()
        return r.json().get("data", [])

    def find_game_id(self):
        data = self.get("games", {"name": GAME_NAME})
        if data:
            return data[0]["id"]
        for c in self.get("search/categories", {"query": GAME_NAME, "first": 10}):
            n = c["name"].lower()
            if "grand theft auto" in n and ("vi" in n.split() or "6" in n):
                return c["id"]
        return None

    def streams(self, game_id):
        data = self.get(
            "streams",
            {"game_id": game_id, "language": LANGUAGE, "first": 100},
        )
        return [
            {
                "login": s["user_login"].lower(),
                "name": s["user_name"],
                "viewers": s["viewer_count"],
            }
            for s in data
            if s.get("type") == "live"
        ]


# --------------------------------------------------------------- chat (IRC)
class Chat(threading.Thread):
    """Lecture anonyme du chat Twitch (aucun compte necessaire)."""

    def __init__(self, on_msg):
        super().__init__(daemon=True)
        self.on_msg = on_msg
        self.want = set()
        self.joined = set()
        self.lock = threading.Lock()

    def set_channels(self, chans):
        with self.lock:
            self.want = set(chans)

    def _sync(self, s):
        with self.lock:
            want = set(self.want)
        for c in want - self.joined:
            s.sendall(f"JOIN #{c}\r\n".encode())
            self.joined.add(c)
        for c in self.joined - want:
            s.sendall(f"PART #{c}\r\n".encode())
            self.joined.discard(c)

    def _line(self, s, line):
        if line.startswith("PING"):
            s.sendall(b"PONG :tmi.twitch.tv\r\n")
        elif " PRIVMSG #" in line:
            chan = line.split(" PRIVMSG #", 1)[1].split(" ", 1)[0].lower()
            self.on_msg(chan)

    def run(self):
        while True:
            try:
                s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=10)
                s.settimeout(1.0)
                log("chat: connecte")
                s.sendall(
                    b"PASS SCHMOOPIIE\r\nNICK justinfan%d\r\n"
                    % random.randint(10000, 99999)
                )
                self.joined = set()
                buf = ""
                while True:
                    self._sync(s)
                    try:
                        data = s.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        raise ConnectionError("ferme")
                    buf += data.decode("utf-8", "ignore")
                    while "\r\n" in buf:
                        line, buf = buf.split("\r\n", 1)
                        self._line(s, line)
            except Exception as e:
                log("chat: reconnexion", type(e).__name__)
                time.sleep(5)


# ----------------------------------------------------- cache video (rolling)
class Recorder(threading.Thread):
    def __init__(self, login):
        super().__init__(daemon=True)
        self.login = login
        self.dir = os.path.join(BUF_DIR, login)
        self.stop_evt = threading.Event()
        self.procs = []
        self.metrics = deque(maxlen=60)   # (volume moyen dB, activite visuelle) par segment
        self.analyzed = set()

    def stop(self):
        self.stop_evt.set()
        for p in self.procs:
            try:
                p.terminate()
            except Exception:
                pass

    def _segments(self):
        return sorted(glob.glob(os.path.join(self.dir, "seg_*.ts")))

    def _trim(self):
        for f in self._segments()[:-KEEP_SEGMENTS]:
            try:
                os.remove(f)
            except OSError:
                pass

    def count(self):
        return len(self._segments())

    def ready(self):
        return self.count() >= 16   # ~ 64 s de cache minimum

    # -- analyse du son et de l'image
    @staticmethod
    def analyze_segment(path):
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
             "-vf", "fps=4,scale=160:90,select='gte(scene,0)',metadata=print:file=-",
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
        vol = float(m.group(1)) if m else -91.0
        sc = [float(x) for x in re.findall(r"scene_score=([\d.]+)", r.stdout)]
        motion = sum(sc) / len(sc) if sc else 0.0
        return vol, motion

    def _analyze_loop(self):
        while not self.stop_evt.is_set():
            try:
                done = self._segments()[:-1]      # le dernier est en cours d'ecriture
                for f in done[-4:]:
                    if f not in self.analyzed:
                        self.analyzed.add(f)
                        self.metrics.append(self.analyze_segment(f))
                if len(self.analyzed) > 200:
                    self.analyzed = set(done)
            except Exception as e:
                log("analyse:", self.login, type(e).__name__)
            time.sleep(1)

    def signals(self):
        """(saut de volume en dB, rapport d'activite visuelle, activite recente)"""
        m = list(self.metrics)
        if len(m) < 8:
            return None, None, 0.0
        recent, base = m[-2:], m[-32:-2]
        vol_r = sum(x[0] for x in recent) / len(recent)
        vol_b = statistics.median(x[0] for x in base)
        mot_r = sum(x[1] for x in recent) / len(recent)
        mot_b = statistics.median(x[1] for x in base)
        return vol_r - vol_b, mot_r / max(mot_b, 0.02), mot_r

    def run(self):
        threading.Thread(target=self._analyze_loop, daemon=True).start()
        while not self.stop_evt.is_set():
            shutil.rmtree(self.dir, ignore_errors=True)
            os.makedirs(self.dir, exist_ok=True)
            self.metrics.clear()
            self.analyzed = set()
            try:
                sl = subprocess.Popen(
                    [
                        "streamlink", "--loglevel", "none", "--stdout",
                        f"https://twitch.tv/{self.login}",
                        "720p60,720p,480p,best",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                ff = subprocess.Popen(
                    [
                        "ffmpeg", "-loglevel", "error", "-i", "pipe:0",
                        "-c", "copy", "-f", "segment",
                        "-segment_time", str(SEG_LEN),
                        "-reset_timestamps", "1",
                        "-segment_format", "mpegts",
                        os.path.join(self.dir, "seg_%06d.ts"),
                    ],
                    stdin=sl.stdout,
                    stderr=subprocess.DEVNULL,
                )
                sl.stdout.close()
                self.procs = [sl, ff]
                while ff.poll() is None and not self.stop_evt.is_set():
                    time.sleep(1)
                    self._trim()
                for p in (sl, ff):
                    try:
                        p.terminate()
                    except Exception:
                        pass
            except Exception as e:
                log("cache: erreur", self.login, type(e).__name__)
            if not self.stop_evt.is_set():
                time.sleep(10)

    def snapshot(self, seconds):
        """Copie immediatement les derniers `seconds` secondes du cache."""
        files = self._segments()[:-1]            # le dernier est en cours d'ecriture
        files = files[-math.ceil(seconds / SEG_LEN):]
        if len(files) < 15:                      # au moins ~60 s
            return None
        jobdir = os.path.join(JOB_DIR, f"{self.login}_{int(time.time())}")
        os.makedirs(jobdir, exist_ok=True)
        lst = os.path.join(jobdir, "list.txt")
        with open(lst, "w") as f:
            for p in files:
                f.write(f"file '{p}'\n")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                 "-i", lst, "-c", "copy", os.path.join(jobdir, "raw.ts")],
                check=True, timeout=120,
            )
        except Exception as e:
            log("snapshot: erreur", type(e).__name__)
            shutil.rmtree(jobdir, ignore_errors=True)
            return None
        return jobdir


# ------------------------------------------------------------------ montage
_wm = None


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def make_subs(jobdir):
    """Sous-titres automatiques (faster-whisper, gratuit, sur CPU)."""
    global _wm
    try:
        from faster_whisper import WhisperModel

        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", "raw.ts", "-vn",
             "-ac", "1", "-ar", "16000", "audio.wav"],
            cwd=jobdir, check=True, timeout=120,
        )
        if _wm is None:
            _wm = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segs, _ = _wm.transcribe(
            os.path.join(jobdir, "audio.wav"),
            language="fr", vad_filter=True, beam_size=1,
        )
        blocks, i = [], 1
        for s in segs:
            t = s.text.strip()
            if t:
                blocks.append(f"{i}\n{srt_time(s.start)} --> {srt_time(s.end)}\n{t}\n")
                i += 1
        if not blocks:
            return False
        with open(os.path.join(jobdir, "clip.srt"), "w", encoding="utf-8") as f:
            f.write("\n".join(blocks))
        return True
    except Exception as e:
        log("sous-titres ignores:", type(e).__name__)
        return False


def clean_name(n):
    return re.sub(r"[^\w\- ]", "", n)[:20].strip() or "Ce streamer"


def make_title(display, reasons=()):
    if "audio" in reasons:
        kind = "audio"
    elif "chat" in reasons:
        kind = "chat"
    elif "visuel" in reasons:
        kind = "action"
    else:
        kind = "generic"
    base = random.choice(TITLES[kind]).format(n=clean_name(display))
    return base, f"{random.choice(EMOJIS)} {base}"


def render(jobdir, screen_title, with_subs):
    with open(os.path.join(jobdir, "title.txt"), "w", encoding="utf-8") as f:
        f.write(textwrap.fill(screen_title.upper(), 20))

    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", "raw.ts"],
            cwd=jobdir, capture_output=True, text=True, timeout=30,
        ).stdout.strip())
    except Exception:
        dur = float(CLIP_SECONDS)
    dur = max(dur, 5.0)

    subs = ""
    if with_subs:
        subs = (
            ",subtitles=clip.srt:original_size=1080x1920:"
            "fontsdir=/usr/share/fonts/truetype/dejavu:force_style="
            "'FontName=DejaVu Sans,FontSize=9,Bold=1,Outline=1.2,Alignment=2,"
            "MarginV=60,PrimaryColour=&H0000FFFF'"
        )

    # plein cadre : on decoupe la partie centrale de l'image en 9:16 et on zoome
    fg = (
        "[0:v]setpts=PTS-STARTPTS,"
        f"crop=w='min(iw,ih*9/16)':h=ih:x='(iw-ow)*{CROP_X}':y=0,"
        "scale=1080:1920:flags=lanczos,fps=30,setsar=1,"
        f"drawtext=fontfile={FONT}:textfile=title.txt:expansion=none:fontsize=70:"
        "fontcolor=yellow:borderw=5:bordercolor=black:line_spacing=12:"
        "box=1:boxcolor=black@0.45:boxborderw=22:"
        "x=(w-text_w)/2:y=230:enable='between(t,0,4)'"
        f"{subs},format=yuv420p[v0a];"
        # barre de progression jaune en bas (retient l'attention)
        "[v0a][3:v]overlay=x='-w+w*t/" + f"{dur:.2f}" + "':y=H-16:shortest=1[v0];"
        "[1:v]drawbox=x=150:y=980:w=780:h=170:color=0xFF0000@0.95:t=fill,"
        f"drawtext=fontfile={FONT}:text=ABONNE-TOI:fontsize=86:fontcolor=white:"
        "x=(w-text_w)/2:y=1025+10*sin(2*PI*t*3),"
        f"drawtext=fontfile={FONT}:text=pour ne rater aucun des moments:fontsize=50:"
        "fontcolor=white:x=(w-text_w)/2:y=1260:alpha='0.7+0.3*sin(2*PI*t*2)',"
        f"drawtext=fontfile={FONT}:text=les plus fous du jeu vidéo:fontsize=50:"
        "fontcolor=yellow:x=(w-text_w)/2:y=1335:alpha='0.7+0.3*sin(2*PI*t*2)',"
        "fade=t=in:st=0:d=0.4,fps=30,setsar=1,format=yuv420p[v1];"
        "[0:a]asetpts=PTS-STARTPTS,loudnorm=I=-14:TP=-1.5:LRA=11,"
        "aresample=44100,aformat=channel_layouts=stereo[a0];"
        "[2:a]aformat=channel_layouts=stereo[a1];"
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", "raw.ts",
        "-f", "lavfi", "-t", "3", "-i", "color=c=0x14141e:s=1080x1920:r=30",
        "-f", "lavfi", "-t", "3", "-i", "anullsrc=r=44100:cl=stereo",
        "-f", "lavfi", "-t", f"{dur + 1:.2f}", "-i", "color=c=yellow:s=1080x14:r=30",
        "-filter_complex", fg,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-maxrate", "4M", "-bufsize", "8M",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "final.mp4",
    ]
    subprocess.run(cmd, cwd=jobdir, check=True, timeout=900)
    return os.path.join(jobdir, "final.mp4")


def process(jobdir, login, display, reasons=()):
    screen_title, caption_title = make_title(display, reasons)
    with_subs = ENABLE_SUBS and make_subs(jobdir)
    try:
        out = render(jobdir, screen_title, with_subs)
    except subprocess.CalledProcessError:
        if not with_subs:
            raise
        log("montage: nouvel essai sans sous-titres")
        out = render(jobdir, screen_title, False)
    caption = (
        f"{caption_title}\n\n"
        f"Clip du live de {display} (twitch.tv/{login}). "
        f"Abonne-toi pour ne rater aucun des moments les plus fous du jeu vidéo !\n\n{HASHTAGS}"
    )
    ok = tg_video(out, caption)
    log("clip envoye" if ok else "clip NON envoye", login)
    if yt_enabled():
        link = yt_upload(out, f"{caption_title} #Shorts", caption)
        if link:
            log("youtube: depose", YT_PRIVACY)
            tg_text(f"▶️ YouTube ({YT_PRIVACY}) : {link}")
        else:
            tg_text("⚠️ Dépôt YouTube échoué, voir les logs GitHub.")
    if tt_enabled():
        if tt_upload(out):
            log("tiktok: brouillon envoye")
            tg_text("🎵 TikTok : brouillon dans ta boîte de réception TikTok (légende dans le message ci-dessus).")
        else:
            tg_text("⚠️ Envoi TikTok échoué, voir les logs GitHub.")


# -------------------------------------------------------------- orchestrateur
class App:
    def __init__(self):
        self.tw = Twitch()
        self.recorders = {}
        self.info = {}
        self.stamps = {}
        self.first_seen = {}
        self.last_clip = {}
        self.clips = 0
        self.jobs = queue.Queue(maxsize=30)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.chat = Chat(self.on_msg)

    # -- chat
    def on_msg(self, login):
        with self.lock:
            self.stamps.setdefault(login, deque()).append(time.time())

    # -- detection
    def check(self, login):
        now = time.time()
        with self.lock:
            ts = self.stamps.get(login)
            if ts is None:
                return
            while ts and ts[0] < now - (BASE_WINDOW + WINDOW):
                ts.popleft()
            recent = sum(1 for t in ts if t >= now - WINDOW)
            base_rate = (len(ts) - recent) / BASE_WINDOW
        rec = self.recorders.get(login)
        if not rec or not rec.ready():
            return
        if now - self.first_seen.get(login, now) < 90:
            return
        if now - self.last_clip.get(login, 0) < COOLDOWN:
            return
        if self.clips >= MAX_CLIPS_PER_RUN or self.jobs.qsize() >= 25:
            return
        aj, vr, mot = rec.signals()
        chat_hit = recent >= MIN_MSGS and recent / WINDOW >= RATIO * max(base_rate, 0.3)
        audio_hit = aj is not None and aj >= AUDIO_DB
        visual_hit = vr is not None and vr >= VIS_RATIO and mot >= VIS_MIN
        score = chat_hit + audio_hit + visual_hit
        strong = (chat_hit and recent >= 2.5 * MIN_MSGS) or (aj is not None and aj >= AUDIO_DB + 6)
        if score < 2 and not strong:
            return
        reasons = [n for n, hit in (("chat", chat_hit), ("audio", audio_hit), ("visuel", visual_hit)) if hit]
        self.last_clip[login] = now
        log(
            f"PIC detecte sur {login} ({'+'.join(reasons) or 'fort'}): chat {recent}/{WINDOW}s "
            f"(normal {base_rate:.2f}/s), son {aj if aj is None else round(aj, 1)} dB, "
            f"image x{vr if vr is None else round(vr, 1)}"
        )
        threading.Timer(POST_DELAY, self.grab, args=(login, reasons)).start()

    def detector(self):
        while not self.stop.is_set():
            for login in list(self.recorders):
                try:
                    self.check(login)
                except Exception as e:
                    log("detecteur:", type(e).__name__)
            time.sleep(1)

    def grab(self, login, reasons=()):
        rec = self.recorders.get(login)
        if not rec:
            return
        jobdir = rec.snapshot(CLIP_SECONDS)
        if not jobdir:
            log("cache trop court pour", login)
            return
        display = self.info.get(login, (login, 0))[0]
        try:
            self.jobs.put_nowait((jobdir, login, display, list(reasons)))
            self.clips += 1
            log(f"clip #{self.clips} en file d'attente ({self.jobs.qsize()} en attente)")
        except queue.Full:
            shutil.rmtree(jobdir, ignore_errors=True)

    def status(self):
        now = time.time()
        parts = []
        for login, rec in list(self.recorders.items()):
            with self.lock:
                n = sum(1 for t in self.stamps.get(login, ()) if t >= now - 60)
            aj, vr, _ = rec.signals()
            sig = "" if aj is None else f" son{aj:+.1f}dB image x{vr:.1f}"
            parts.append(f"{login} cache={rec.count()}seg chat={n}/60s{sig}")
        log("ETAT:", " | ".join(parts) if parts else "aucun stream", f"| clips={self.clips}")

    # -- file d'attente
    def worker(self):
        while not (self.stop.is_set() and self.jobs.empty()):
            try:
                jobdir, login, display, reasons = self.jobs.get(timeout=2)
            except queue.Empty:
                continue
            try:
                process(jobdir, login, display, reasons)
            except Exception as e:
                log("montage en erreur:", type(e).__name__)
            finally:
                shutil.rmtree(jobdir, ignore_errors=True)

    # -- liste des lives
    def sync(self, streams):
        live = {s["login"]: s for s in streams}
        for login, s in live.items():
            self.info[login] = (s["name"], s["viewers"])
        for login in list(self.recorders):
            if login not in live:
                self.recorders.pop(login).stop()
                log("fin de live:", login)
        for login in live:        # deja tries par nombre de viewers
            if len(self.recorders) >= MAX_STREAMS:
                break
            if login not in self.recorders:
                r = Recorder(login)
                r.start()
                self.recorders[login] = r
                self.first_seen[login] = time.time()
                log(f"surveille: {login} ({live[login]['viewers']} viewers)")
        self.chat.set_channels(list(self.recorders))

    def run(self):
        shutil.rmtree(ROOT, ignore_errors=True)
        os.makedirs(BUF_DIR, exist_ok=True)
        os.makedirs(JOB_DIR, exist_ok=True)
        self.chat.start()
        worker = threading.Thread(target=self.worker, daemon=True)
        worker.start()
        threading.Thread(target=self.detector, daemon=True).start()
        tg_text("🚀 Clipper GTA 6 démarré. Les clips arriveront ici.")

        start = time.time()
        gid = None
        while time.time() - start < RUN_SECONDS:
            try:
                if gid is None:
                    gid = self.tw.find_game_id()
                    if gid is None:
                        log(f"categorie '{GAME_NAME}' introuvable pour l'instant")
                        time.sleep(60)
                        continue
                    log("categorie trouvee, id", gid)
                streams = self.tw.streams(gid)
                if not streams:
                    log("aucun live FR sur la categorie pour l'instant")
                self.sync(streams)
                time.sleep(20)
                self.status()
            except Exception as e:
                log("twitch: erreur", type(e).__name__)
            time.sleep(REFRESH if self.recorders else 60)

        log("fin de tranche: arret des cache, fin de la file d'attente")
        self.stop.set()
        for r in list(self.recorders.values()):
            r.stop()
        worker.join(timeout=DRAIN_SECONDS)
        tg_text("🔁 Fin de tranche de 6 h, le relais automatique prend la suite.")


def main():
    missing = [
        k for k, v in {
            "TWITCH_CLIENT_ID": CLIENT_ID,
            "TWITCH_CLIENT_SECRET": CLIENT_SECRET,
            "TELEGRAM_BOT_TOKEN": TG_TOKEN,
        }.items() if not v
    ]
    if missing:
        sys.exit("Secrets manquants: " + ", ".join(missing))
    App().run()
    os._exit(0)


if __name__ == "__main__":
    main()
