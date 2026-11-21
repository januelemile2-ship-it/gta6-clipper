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

MAX_STREAMS = _i("MAX_STREAMS", 16)           # lives surveilles en meme temps
HQ_STREAMS = _i("HQ_STREAMS", 6)              # les N plus gros sont captes en 1080p, les autres en 720p
SWAP_RATIO = _f("SWAP_RATIO", 1.25)           # un live doit avoir 25 % de viewers en plus pour en remplacer un autre
ANALYZE_EVERY = _i("ANALYZE_EVERY", 2)        # analyse du son/image 1 segment sur N (economise le processeur)
VIP = {x.strip().lower() for x in os.environ.get("VIP_STREAMERS", "").split(",") if x.strip()}
IGNORE = {x.strip().lower() for x in os.environ.get("IGNORE_STREAMERS", "").split(",") if x.strip()}
Q_HQ = "1080p60,1080p,720p60,720p,480p,best"
Q_STD = "720p60,720p,480p,best"
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
WARMUP = _i("WARMUP", 300)                    # on ignore un stream pendant ses 5 premieres minutes
COOLDOWN = _i("COOLDOWN", 240)                # pause par streamer (s)
MAX_CLIPS_PER_RUN = _i("MAX_CLIPS_PER_RUN", 6)     # 6 x 4 tranches = ~24 clips par jour max
BURST = _i("BURST", 2)                        # clips autorises d'entree, le reste est reparti sur la tranche
MIN_SCORE = _f("MIN_SCORE", 2.5)              # score minimum (chat + son + image) pour clipper
REFRESH = _i("REFRESH", 120)                  # rafraichit la liste des lives
RUN_SECONDS = _i("RUN_SECONDS", 20700)        # 5 h 45 puis relais auto
DRAIN_SECONDS = _i("DRAIN_SECONDS", 480)      # temps max pour finir la file
ENABLE_SUBS = os.environ.get("ENABLE_SUBS", "1") == "1"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")

# ---- reglages du montage (tout est modifiable depuis le fichier clipper.yml) ----
LAYOUT = os.environ.get("LAYOUT", "fill")             # fill = plein ecran zoome | blur = image entiere + fond flou
AUTO_CROP = os.environ.get("AUTO_CROP", "1") == "1"   # cadrage automatique sur la zone d'action
CROP_MIN = _f("CROP_MIN", 0.30)                       # le cadrage reste entre ces deux positions
CROP_MAX = _f("CROP_MAX", 0.70)
ZOOM_END = _f("ZOOM_END", 0.07)                       # zoom lent sur toute la duree (7 %)
PUNCH = _f("PUNCH", 0.10)                             # effet "coup de zoom" des 0,45 premieres secondes
SHARPEN = _f("SHARPEN", 0.7)                          # nettete (0 = aucune)
SATURATION = _f("SATURATION", 1.12)                   # couleurs
CONTRAST = _f("CONTRAST", 1.05)
BRIGHTNESS = _f("BRIGHTNESS", 0.0)
VIGNETTE = os.environ.get("VIGNETTE", "1") == "1"     # leger assombrissement des bords
HOOK_SECONDS = _f("HOOK_SECONDS", 3.5)                # duree du titre d'accroche
HOOK_SIZE = _i("HOOK_SIZE", 72)
HOOK_Y = _i("HOOK_Y", 230)
CREDIT = os.environ.get("CREDIT", "1") == "1"         # mention "via twitch.tv/streamer"
SUB_WORDS = _i("SUB_WORDS", 3)                        # mots par ligne de sous-titre
SUB_SIZE = _i("SUB_SIZE", 66)
SUB_MARGIN = _i("SUB_MARGIN", 420)                    # hauteur des sous-titres depuis le bas
OUTRO_SECONDS = _f("OUTRO_SECONDS", 3.0)
CHANNEL_TAG = os.environ.get("CHANNEL_TAG", "")       # ex: @tachaine, affiche sur l'ecran de fin
CRF = _i("QUALITY_CRF", 19)                           # qualite video (plus bas = meilleur, 17-23)
PRESET = os.environ.get("X264_PRESET", "fast")
MAXRATE = os.environ.get("VIDEO_MAXRATE", "9M")
AUDIO_KBPS = _i("AUDIO_KBPS", 192)
LUFS = _f("LUFS", -14)                                # volume final (standard shorts/tiktok)
TRUE_PEAK = _f("TRUE_PEAK", -1.5)
COMP_RATIO = _f("COMP_RATIO", 3.0)                    # compression de la voix et du jeu
EQ_PRESENCE = _f("EQ_PRESENCE", 2.0)                  # dB de "presence" ajoutes vers 3,5 kHz
HIGHPASS = _i("HIGHPASS", 60)                         # coupe les graves parasites (Hz)
SFX = os.environ.get("SFX", "1") == "1"               # sound design (whoosh, impact, ding)
SFX_VOL = _f("SFX_VOL", 1.0)
MIN_MEAN_DB = _f("MIN_MEAN_DB", -42)                  # en dessous : clip quasi muet, ignore

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


YT_HINTS = {
    "uploadLimitExceeded": "limite d'envois atteinte pour ta chaine (verifie-la par telephone sur youtube.com/verify, ou attends 24 h)",
    "quotaExceeded": "quota quotidien de l'API atteint, ca repart demain",
    "dailyLimitExceeded": "quota quotidien de l'API atteint, ca repart demain",
    "invalid_grant": "cle d'acces expiree ou revoquee : il faut refaire le refresh token",
    "invalid_client": "ID client ou code secret incorrect dans les secrets GitHub",
    "youtubeSignupRequired": "aucune chaine YouTube liee a ce compte Google",
    "forbidden": "acces refuse par YouTube",
    "authError": "autorisation refusee, refaire le refresh token",
}


class YTError(Exception):
    def __init__(self, status, reason, msg=""):
        super().__init__(reason)
        self.status, self.reason, self.msg = status, reason, msg


def _yt_fail(resp):
    reason, msg = "inconnue", ""
    try:
        j = resp.json()
        e = j.get("error")
        if isinstance(e, dict):
            reason = ((e.get("errors") or [{}])[0].get("reason")) or e.get("status") or "inconnue"
            msg = e.get("message", "")
        elif e:
            reason, msg = str(e), j.get("error_description", "")
    except Exception:
        msg = resp.text[:120]
    raise YTError(resp.status_code, reason, msg)


def _yt_once(path, title, description):
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
    if t.status_code != 200:
        _yt_fail(t)
    access = t.json()["access_token"]
    meta = {
        "snippet": {
            "title": title[:95],
            "description": description[:4900],
            "tags": ["GTA6", "GTAVI", "Shorts", "Gaming"],
            "categoryId": "20",
        },
        "status": {"privacyStatus": YT_PRIVACY, "selfDeclaredMadeForKids": False},
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
    if init.status_code != 200:
        _yt_fail(init)
    with open(path, "rb") as f:
        up = requests.put(
            init.headers["Location"],
            data=f,
            headers={"Content-Length": str(size), "Content-Type": "video/mp4"},
            timeout=1800,
        )
    if up.status_code not in (200, 201):
        _yt_fail(up)
    return "https://youtube.com/shorts/" + up.json()["id"]


def yt_upload(path, title, description):
    """Depose le clip sur ta chaine ('private' tant que l'audit YouTube n'est pas passe).
    Renvoie (lien, None) ou (None, explication de l'echec)."""
    last = "erreur reseau"
    for attempt in range(3):
        try:
            return _yt_once(path, title, description), None
        except YTError as e:
            hint = YT_HINTS.get(e.reason, e.msg[:140])
            last = f"{e.reason} (HTTP {e.status})" + (f" - {hint}" if hint else "")
            log("youtube: erreur", e.status, e.reason, e.msg[:120])
            if e.status < 500:
                break                      # inutile de reessayer une erreur de configuration
        except Exception as e:
            last = f"probleme reseau ({type(e).__name__})"
            log("youtube: erreur", type(e).__name__)
        time.sleep(20)
    return None, last


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
    def __init__(self, login, hq=True):
        super().__init__(daemon=True)
        self.login = login
        self.hq = hq
        self.protect_until = 0.0
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
            ["nice", "-n", "10", "ffmpeg", "-hide_banner", "-nostats", "-threads", "1", "-i", path,
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
                for f in done[-6:]:
                    if f in self.analyzed:
                        continue
                    self.analyzed.add(f)
                    try:
                        idx = int(os.path.basename(f)[4:10])
                    except ValueError:
                        idx = 0
                    if idx % ANALYZE_EVERY:
                        continue
                    self.metrics.append(self.analyze_segment(f))
                if len(self.analyzed) > 200:
                    self.analyzed = set(done)
            except Exception as e:
                log("analyse:", self.login, type(e).__name__)
            time.sleep(1)

    def signals(self):
        """(saut de volume en dB, rapport d'activite visuelle, activite recente)"""
        m = list(self.metrics)
        if len(m) < 20:                      # ~80 s de reference minimum
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
                        Q_HQ if self.hq else Q_STD,
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
        shutil.rmtree(self.dir, ignore_errors=True)

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


def probe_duration(jobdir, name="raw.ts"):
    try:
        return float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", name],
            cwd=jobdir, capture_output=True, text=True, timeout=30,
        ).stdout.strip())
    except Exception:
        return float(CLIP_SECONDS)


def qc(jobdir):
    """Controle qualite : on ignore un clip quasi muet."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", "raw.ts", "-vn",
             "-af", "volumedetect", "-f", "null", "-"],
            cwd=jobdir, capture_output=True, text=True, timeout=120,
        )
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
        return (float(m.group(1)) if m else -91.0) >= MIN_MEAN_DB
    except Exception:
        return True


def auto_crop_x(jobdir):
    """Position horizontale du cadrage 9:16, calee sur la zone ou il y a le plus d'action."""
    if not AUTO_CROP:
        return CROP_X
    try:
        w, h = 96, 54
        fs = w * h
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", "raw.ts", "-an",
             "-vf", f"fps=4,scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
            cwd=jobdir, capture_output=True, timeout=180,
        )
        data = r.stdout
        n = len(data) // fs
        if n < 10:
            return CROP_X
        cols = [0.0] * w
        prev = data[0:fs]
        for i in range(1, n):
            cur = data[i * fs:(i + 1) * fs]
            for idx, (a, b) in enumerate(zip(cur, prev)):
                d = a - b if a > b else b - a
                if d > 12:
                    cols[idx % w] += d
            prev = cur
        total = sum(cols)
        if total <= 0:
            return CROP_X
        cx = sum(x * c for x, c in enumerate(cols)) / total / (w - 1)
        cx = 0.35 * 0.5 + 0.65 * cx           # on reste prudent : melange avec le centre
        return max(CROP_MIN, min(CROP_MAX, cx))
    except Exception as e:
        log("cadrage auto ignore:", type(e).__name__)
        return CROP_X


def ass_time(t):
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s_, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s_:02d}.{cs:02d}"


def make_subs(jobdir):
    """Sous-titres animes mot par mot (faster-whisper, gratuit, sur CPU)."""
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
            language="fr", vad_filter=True, beam_size=1, word_timestamps=True,
            condition_on_previous_text=False,
            initial_prompt="Streamers francais qui jouent a GTA 6, Vice City, Leonida, Lucia, Jason.",
        )
        words = []
        for sg in segs:
            for w in (sg.words or []):
                t = w.word.strip()
                if t:
                    words.append((w.start, w.end, t.upper()))
        if not words:
            return False
        lines, cur = [], []
        for w in words:
            if cur and (len(cur) >= SUB_WORDS or w[0] - cur[-1][1] > 0.8):
                lines.append(cur)
                cur = []
            cur.append(w)
        if cur:
            lines.append(cur)
        ev = []
        for ln in lines:
            txt = ""
            for i, (st, en, t) in enumerate(ln):
                nxt = ln[i + 1][0] if i + 1 < len(ln) else en
                txt += "{\\kf%d}%s " % (max(int(round((nxt - st) * 100)), 1), t)
            ev.append(
                f"Dialogue: 0,{ass_time(ln[0][0])},{ass_time(ln[-1][1] + 0.2)},Main,,0,0,0,,{txt.strip()}"
            )
        head = (
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
            "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
            "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
            "Alignment,MarginL,MarginR,MarginV,Encoding\n"
            f"Style: Main,DejaVu Sans,{SUB_SIZE},&H0000E6FF,&H00FFFFFF,&H00000000,&H64000000,"
            f"-1,0,0,0,100,100,0,0,1,6,2,2,70,70,{SUB_MARGIN},1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
        )
        with open(os.path.join(jobdir, "clip.ass"), "w", encoding="utf-8") as f:
            f.write(head + "\n".join(ev) + "\n")
        return True
    except Exception as e:
        log("sous-titres ignores:", type(e).__name__)
        return False


def render(jobdir, screen_title, with_subs, cx, login):
    with open(os.path.join(jobdir, "title.txt"), "w", encoding="utf-8") as f:
        f.write(textwrap.fill(screen_title.upper(), 20))
    with open(os.path.join(jobdir, "credit.txt"), "w", encoding="utf-8") as f:
        f.write(f"via twitch.tv/{login}")

    D = max(probe_duration(jobdir), 5.0)
    O = OUTRO_SECONDS
    H = HOOK_SECONDS

    grade = (
        f"eq=contrast={CONTRAST}:saturation={SATURATION}:brightness={BRIGHTNESS},"
        f"unsharp=5:5:{SHARPEN}:5:5:0.0"
        + (",vignette=angle=PI/5" if VIGNETTE else "")
    )

    if LAYOUT == "blur":
        base = (
            "[0:v]setpts=PTS-STARTPTS,split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=25:3,eq=brightness=-0.05[bgb];"
            "[fg]scale=1080:-2:flags=lanczos[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,fps=30,setsar=1,{grade}"
        )
    else:
        zoom = f"1+{ZOOM_END}*min(t/{D:.2f},1)+{PUNCH}*max(0,1-t/0.45)"
        base = (
            "[0:v]setpts=PTS-STARTPTS,"
            f"crop=w='min(iw,ih*9/16)':h=ih:x='(iw-ow)*{cx:.3f}':y=0,"
            "scale=1080:1920:flags=lanczos,"
            f"scale=w='trunc(1080*({zoom})/2)*2':h=-2:eval=frame:flags=bicubic,"
            f"crop=1080:1920,fps=30,setsar=1,{grade}"
        )

    hook = (
        f",drawtext=fontfile={FONT}:textfile=title.txt:expansion=none:fontsize={HOOK_SIZE}:"
        "fontcolor=yellow:borderw=6:bordercolor=black:line_spacing=14:"
        "box=1:boxcolor=black@0.45:boxborderw=24:x=(w-text_w)/2:"
        f"y='{HOOK_Y}+if(lt(t,0.3),-(0.3-t)*400,0)':"
        f"alpha='if(lt(t,0.3),t/0.3,if(gt(t,{H}-0.4),({H}-t)/0.4,1))':"
        f"enable='between(t,0,{H})'"
    )
    credit = ""
    if CREDIT:
        credit = (
            f",drawtext=fontfile={FONT}:textfile=credit.txt:expansion=none:fontsize=38:"
            "fontcolor=white@0.9:borderw=4:bordercolor=black:x=(w-text_w)/2:y=h-170:"
            "enable='between(t,0.5,6)'"
        )
    subs = ""
    if with_subs:
        subs = ",ass=clip.ass:fontsdir=/usr/share/fonts/truetype/dejavu"

    tag = ""
    if CHANNEL_TAG:
        with open(os.path.join(jobdir, "tag.txt"), "w", encoding="utf-8") as f:
            f.write(CHANNEL_TAG)
        tag = (
            f",drawtext=fontfile={FONT}:textfile=tag.txt:expansion=none:fontsize=60:"
            "fontcolor=white:x=(w-text_w)/2:y=1500"
        )

    v = (
        f"{base}{hook}{credit}{subs},format=yuv420p[v0a];"
        f"[v0a][2:v]overlay=x='-w+w*t/{D:.2f}':y=H-16:shortest=1[v0];"
        # ecran de fin : degrade anime + bouton qui pulse
        "[1:v]format=gbrp,geq=r='22+34*Y/H':g='18+22*Y/H':b='38+70*Y/H',format=yuv420p,"
        "drawbox=x=150:y=980:w=780:h=170:color=0xFF0000@0.95:t=fill,"
        f"drawtext=fontfile={FONT}:text=ABONNE-TOI:fontsize=86:fontcolor=white:"
        "x=(w-text_w)/2:y=1025+10*sin(2*PI*t*3),"
        f"drawtext=fontfile={FONT}:text=pour ne rater aucun des moments:fontsize=50:"
        "fontcolor=white:x=(w-text_w)/2:y=1260:alpha='0.7+0.3*sin(2*PI*t*2)',"
        f"drawtext=fontfile={FONT}:text=les plus fous du jeu vidéo:fontsize=50:"
        "fontcolor=yellow:x=(w-text_w)/2:y=1335:alpha='0.7+0.3*sin(2*PI*t*2)'"
        f"{tag},fade=t=in:st=0:d=0.4,fps=30,setsar=1,format=yuv420p[v1];"
    )

    sv = SFX_VOL if SFX else 0.0
    a = (
        "[0:a]asetpts=PTS-STARTPTS,"
        f"highpass=f={HIGHPASS},"
        f"acompressor=threshold=0.125:ratio={COMP_RATIO}:attack=5:release=80:makeup=2,"
        f"equalizer=f=3500:t=q:w=1:g={EQ_PRESENCE},"
        f"loudnorm=I={LUFS}:TP={TRUE_PEAK}:LRA=9,"
        "aresample=48000,aformat=channel_layouts=stereo[am];"
    )
    if SFX:
        a += (
            f"[3:a]highpass=f=700,lowpass=f=7000,afade=t=in:d=0.4,afade=t=out:st=0.4:d=0.4,"
            f"volume={0.35 * sv:.3f},aformat=channel_layouts=stereo[wh];"
            f"[4:a]afade=t=out:st=0.05:d=0.8,volume={0.9 * sv:.3f},aformat=channel_layouts=stereo[bo];"
            f"[5:a]lowpass=f=180,afade=t=out:st=0:d=0.9,volume={0.5 * sv:.3f},aformat=channel_layouts=stereo[bn];"
            "[am][wh][bo][bn]amix=inputs=4:duration=first:normalize=0,alimiter=limit=0.95[a0];"
            f"[6:a]afade=t=out:st=0.05:d=0.65,volume={0.25 * sv:.3f}[d1];"
            f"[7:a]afade=t=out:st=0.05:d=0.5,volume={0.12 * sv:.3f}[d2];"
            f"[d1][d2]amix=inputs=2:normalize=0,adelay=300|300,apad=whole_dur={O},"
            f"atrim=0:{O},aformat=channel_layouts=stereo[a1];"
        )
    else:
        a += (
            "[am]alimiter=limit=0.95[a0];"
            f"[6:a]volume=0,apad=whole_dur={O},atrim=0:{O},aformat=channel_layouts=stereo[a1];"
        )
    fg = v + a + "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", "raw.ts",
        "-f", "lavfi", "-t", f"{O}", "-i", "color=c=0x14141e:s=1080x1920:r=30",
        "-f", "lavfi", "-t", f"{D + 1:.2f}", "-i", "color=c=yellow:s=1080x14:r=30",
        "-f", "lavfi", "-t", "0.8", "-i", "anoisesrc=d=0.8:c=pink:a=0.5:r=48000",
        "-f", "lavfi", "-t", "0.9", "-i", "sine=f=52:d=0.9:r=48000",
        "-f", "lavfi", "-t", "0.9", "-i", "anoisesrc=d=0.9:c=brown:a=0.6:r=48000",
        "-f", "lavfi", "-t", "0.7", "-i", "sine=f=1318:d=0.7:r=48000",
        "-f", "lavfi", "-t", "0.7", "-i", "sine=f=1975:d=0.7:r=48000",
        "-filter_complex", fg,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
        "-maxrate", MAXRATE, "-bufsize", "18M", "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ar", "48000",
        "-movflags", "+faststart",
        "final.mp4",
    ]
    subprocess.run(cmd, cwd=jobdir, check=True, timeout=1500)
    return os.path.join(jobdir, "final.mp4")


def tg_version(jobdir, path):
    """Version allegee pour Telegram (limite 50 Mo) ; YouTube recoit la version haute qualite."""
    if os.path.getsize(path) <= 45 * 1024 * 1024:
        return path
    out = os.path.join(jobdir, "tg.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
         "-maxrate", "3M", "-bufsize", "6M",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out],
        check=True, timeout=600,
    )
    return out


def process(jobdir, login, display, reasons=()):
    if not qc(jobdir):
        log("clip ignore (son quasi absent):", login)
        return
    screen_title, caption_title = make_title(display, reasons)
    cx = auto_crop_x(jobdir)
    with_subs = ENABLE_SUBS and make_subs(jobdir)
    try:
        out = render(jobdir, screen_title, with_subs, cx, login)
    except subprocess.CalledProcessError:
        if not with_subs:
            raise
        log("montage: nouvel essai sans sous-titres")
        out = render(jobdir, screen_title, False, cx, login)
    caption = (
        f"{caption_title}\n\n"
        f"Clip du live de {display} (twitch.tv/{login}). "
        f"Abonne-toi pour ne rater aucun des moments les plus fous du jeu vidéo !\n\n{HASHTAGS}"
    )
    try:
        tg_path = tg_version(jobdir, out)
    except Exception:
        tg_path = out
    ok = tg_video(tg_path, caption)
    log("clip envoye" if ok else "clip NON envoye", login)
    if yt_enabled():
        link, err = yt_upload(out, f"{caption_title} #Shorts", caption)
        if link:
            log("youtube: depose", YT_PRIVACY)
            tg_text(f"▶️ YouTube ({YT_PRIVACY}) : {link}")
        else:
            tg_text(f"⚠️ Dépôt YouTube échoué : {err}")
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
        self.t0 = time.time()
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
        if now - self.first_seen.get(login, now) < WARMUP:
            return
        if now - self.last_clip.get(login, 0) < COOLDOWN:
            return
        budget = BURST + int((now - self.t0) / (RUN_SECONDS / max(MAX_CLIPS_PER_RUN - BURST, 1)))
        if self.clips >= min(MAX_CLIPS_PER_RUN, budget) or self.jobs.qsize() >= 25:
            return
        aj, vr, mot = rec.signals()
        chat_hit = recent >= MIN_MSGS and recent / WINDOW >= RATIO * max(base_rate, 0.3)
        audio_hit = aj is not None and aj >= AUDIO_DB
        visual_hit = vr is not None and vr >= VIS_RATIO and mot >= VIS_MIN
        score = chat_hit + audio_hit + visual_hit
        strong = chat_hit and recent >= 2.5 * MIN_MSGS
        ratio = (recent / WINDOW) / max(base_rate, 0.3)
        q = (min(ratio / RATIO, 3) if recent >= MIN_MSGS else 0.0)
        q += min(max(aj, 0) / AUDIO_DB, 3) if aj is not None else 0.0
        q += min(vr / VIS_RATIO, 3) if (vr is not None and mot >= VIS_MIN) else 0.0
        if (score < 2 and not strong) or q < MIN_SCORE:
            return
        reasons = [n for n, hit in (("chat", chat_hit), ("audio", audio_hit), ("visuel", visual_hit)) if hit]
        self.last_clip[login] = now
        rec.protect_until = now + POST_DELAY + 90
        log(
            f"PIC detecte sur {login} ({'+'.join(reasons) or 'fort'}): chat {recent}/{WINDOW}s "
            f"(normal {base_rate:.2f}/s), son {aj if aj is None else round(aj, 1)} dB, "
            f"image x{vr if vr is None else round(vr, 1)}, score {q:.1f}"
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
            parts.append(f"{login}{'*' if rec.hq else ''} cache={rec.count()}seg chat={n}/60s{sig}")
        try:
            load = f"charge={os.getloadavg()[0]:.1f}"
        except Exception:
            load = ""
        log("ETAT:", " | ".join(parts) if parts else "aucun stream",
            f"| {len(self.recorders)} lives, clips={self.clips} {load}")

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
    def prio(self, login, live):
        s = live.get(login)
        return (s["viewers"] if s else 0) + (10_000_000 if login in VIP else 0)

    def protected(self, login, now):
        """Un live qu'on vient de lancer ou dont un clip est en cours n'est pas remplacable."""
        r = self.recorders[login]
        return now < r.protect_until or now - self.first_seen.get(login, 0) < WARMUP + 60

    def start_rec(self, login, live, hq):
        r = Recorder(login, hq)
        r.start()
        self.recorders[login] = r
        self.first_seen[login] = time.time()
        with self.lock:
            self.stamps.pop(login, None)
        log(f"surveille: {login} ({live[login]['viewers']} viewers, {'1080p' if hq else '720p'})")

    def sync(self, streams):
        now = time.time()
        live = {s["login"]: s for s in streams if s["login"] not in IGNORE}
        for login, s in live.items():
            self.info[login] = (s["name"], s["viewers"])
        for login in list(self.recorders):
            if login not in live:
                self.recorders.pop(login).stop()
                log("fin de live:", login)
        ranked = sorted(live, key=lambda l: self.prio(l, live), reverse=True)
        wanted = ranked[:MAX_STREAMS]
        for login in wanted:                       # du plus gros au plus petit
            if login in self.recorders:
                continue
            if len(self.recorders) < MAX_STREAMS:
                hq = sum(1 for r in self.recorders.values() if r.hq) < HQ_STREAMS
                self.start_rec(login, live, hq)
                continue
            cands = [l for l in self.recorders if l not in wanted and not self.protected(l, now)]
            if not cands:
                continue
            weakest = min(cands, key=lambda l: self.prio(l, live))
            if self.prio(login, live) >= SWAP_RATIO * self.prio(weakest, live):
                hq = self.recorders[weakest].hq
                self.recorders.pop(weakest).stop()
                log(f"remplace {weakest} ({live[weakest]['viewers']} viewers) "
                    f"par {login} ({live[login]['viewers']} viewers)")
                self.start_rec(login, live, hq)
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
        self.t0 = start
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
