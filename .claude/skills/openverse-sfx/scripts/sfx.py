#!/usr/bin/env python3
"""openverse-sfx: add license-tracked sound effects from Openverse to a video.

Stdlib only; needs ffmpeg/ffprobe on PATH.

Pipeline (each step reads/writes JSON in a work directory, so a human or
Claude can inspect and edit between steps):

  analyze VIDEO          -> analysis.json, sheet.jpg (contact sheet of shots)
  plan    VIDEO          -> cues.json  (heuristic cue list; edit freely)
  fetch   VIDEO          -> sfx/*.mp3, manifest.json (search + download)
  mix     VIDEO          -> <stem>_sfx.<ext>, credits.txt
  run     VIDEO          -> all of the above, unattended
  check                  -> dependency + API check
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.openverse.org/v1/audio/"
# Wikimedia rejects generic User-Agents; set OPENVERSE_SFX_UA to include your contact.
UA = os.environ.get("OPENVERSE_SFX_UA",
                    "openverse-sfx/1.0 (Claude Code skill; https://github.com/tagjgjggggggpg/Tu)")
CACHE = os.path.expanduser(os.environ.get("OPENVERSE_SFX_CACHE", "~/.cache/openverse-sfx"))
CACHE_TTL = 7 * 24 * 3600

# Licenses usable in a commercial video. BY-SA is excluded on purpose: syncing
# audio to moving images makes the video an "adaptation" under CC 4.0, which
# would force the whole video under BY-SA. NC / ND / sampling+ are excluded too.
LICENSE_TIERS = [["cc0", "pdm"], ["by"]]
NEEDS_ATTRIBUTION = {"by"}

# Default trim length (s) for a cue when the cue does not set max_dur.
DEFAULT_MAX_DUR = 1.2
BAD_WORDS = {"loop", "music", "song", "pack", "compilation", "collection", "melody",
             "beat", "voice", "speech", "talking", "ambience", "ambient", "drone"}
SKIP_CATEGORIES = {"music", "pronunciation", "podcast", "audiobook", "news"}
# Freesound is the main SFX source; Wikimedia is a fallback (sparser SFX).
DEFAULT_SOURCES = ("freesound", "wikimedia_audio")


# ---------------------------------------------------------------- utilities

def log(msg):
    print(msg, file=sys.stderr)


def run(cmd, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{p.stderr[-2000:]}")
    return p


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", path]).stdout
    return json.loads(out)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def workdir_for(video, work=None):
    if work:
        d = work
    else:
        stem = os.path.splitext(os.path.basename(video))[0]
        d = os.path.join(os.path.dirname(os.path.abspath(video)), f"{stem}_sfx_work")
    os.makedirs(d, exist_ok=True)
    return d


def volume_stats(path, extra_filter=None):
    """mean/max volume in dBFS via volumedetect."""
    af = "volumedetect" if not extra_filter else f"{extra_filter},volumedetect"
    err = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn", "-af", af,
               "-f", "null", "-"], check=False).stderr
    mean = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)", err)
    peak = re.search(r"max_volume:\s*(-?[\d.]+|-inf)", err)
    to_f = lambda m: None if not m or m.group(1) == "-inf" else float(m.group(1))
    return to_f(mean), to_f(peak)


# ---------------------------------------------------------------- analyze

def analyze(video, work, threshold=0.3, max_frames=16):
    info = probe(video)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not v:
        raise SystemExit("no video stream")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    duration = float(info["format"].get("duration") or v.get("duration") or 0)

    # Per-frame scene-change score on a tiny copy: cheap motion/cut signal.
    err = run(["ffmpeg", "-hide_banner", "-nostats", "-i", video, "-an",
               "-vf", "scale=160:-2,select='gte(scene,0)',metadata=print:file=-",
               "-f", "null", "-"], check=False)
    scores, t = [], None
    for line in err.stdout.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1))
        m = re.search(r"lavfi\.scene_score=([\d.]+)", line)
        if m and t is not None:
            scores.append((t, float(m.group(1))))

    cuts = []
    for t, s in scores:
        if s >= threshold and t > 0.2 and (not cuts or t - cuts[-1]["t"] > 0.5):
            cuts.append({"t": round(t, 3), "score": round(s, 3)})

    # Motion peaks: strong local maxima that are not cuts, >= 1 s apart.
    cut_times = [c["t"] for c in cuts]
    cand = sorted((x for x in scores if 0.08 <= x[1] < threshold), key=lambda x: -x[1])
    peaks = []
    for t, s in cand:
        if all(abs(t - p["t"]) > 1.0 for p in peaks) and all(abs(t - c) > 0.7 for c in cut_times):
            peaks.append({"t": round(t, 3), "score": round(s, 3)})
        if len(peaks) >= 12:
            break
    peaks.sort(key=lambda p: p["t"])

    # Audio: overall level + where sound is active (narration/BGM) vs. silent.
    audio = None
    if has_audio:
        mean, peak = volume_stats(video)
        err = run(["ffmpeg", "-hide_banner", "-nostats", "-i", video, "-vn",
                   "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-"],
                  check=False).stderr
        silences, start = [], None
        for line in err.splitlines():
            m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
            if m:
                start = max(0.0, float(m.group(1)))
            m = re.search(r"silence_end:\s*([\d.]+)", line)
            if m and start is not None:
                silences.append([round(start, 3), round(float(m.group(1)), 3)])
                start = None
        if start is not None:
            silences.append([round(start, 3), round(duration, 3)])
        active, cur = [], 0.0
        for s, e in silences:
            if s - cur > 0.05:
                active.append([round(cur, 3), round(s, 3)])
            cur = e
        if duration - cur > 0.05:
            active.append([round(cur, 3), round(duration, 3)])
        audio = {"mean_db": mean, "peak_db": peak, "active": active}

    # Contact sheet: one frame per shot (start + just after each cut).
    shots = [0.0] + cut_times
    if len(shots) > max_frames:
        step = len(shots) / max_frames
        shots = [shots[int(i * step)] for i in range(max_frames)]
    frame_times = [min(round(s + 0.15, 3), max(duration - 0.05, 0)) for s in shots]
    sheet = os.path.join(work, "sheet.jpg")
    make_sheet(video, frame_times, sheet)

    data = {
        "video": os.path.abspath(video),
        "duration": round(duration, 3),
        "width": v.get("width"), "height": v.get("height"),
        "fps": v.get("avg_frame_rate"),
        "has_audio": has_audio,
        "audio": audio,
        "cuts": cuts,
        "motion_peaks": peaks,
        "sheet": sheet,
        "sheet_frames": frame_times,
    }
    save_json(os.path.join(work, "analysis.json"), data)
    return data


def make_sheet(video, times, out):
    tmp = os.path.join(os.path.dirname(out), "frames")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for i, t in enumerate(times):
        label = f"#{i} {t:.2f}s"
        base = ["ffmpeg", "-hide_banner", "-y", "-ss", str(t), "-i", video, "-frames:v", "1"]
        dst = os.path.join(tmp, f"{i:03d}.jpg")
        draw = (f",drawtext=text='{label}':x=6:y=6:fontsize=20:fontcolor=white"
                ":box=1:boxcolor=black@0.6")
        if run(base + ["-vf", "scale=320:-2" + draw, dst], check=False).returncode:
            run(base + ["-vf", "scale=320:-2", dst])  # no freetype: unlabeled
    cols = min(4, len(times))
    rows = math.ceil(len(times) / cols)
    run(["ffmpeg", "-hide_banner", "-y", "-i", os.path.join(tmp, "%03d.jpg"),
         "-vf", f"pad=ceil(iw/2)*2:ceil(ih/2)*2,tile={cols}x{rows}:padding=4",
         "-frames:v", "1", "-q:v", "4", out])
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- plan

def plan(work, min_gap=1.2, per_seconds=2.5, motion=False):
    a = load_json(os.path.join(work, "analysis.json"))
    max_cues = max(1, int(a["duration"] / per_seconds))
    cand = [{"t": max(0.0, round(c["t"] - 0.25, 3)), "query": "whoosh",
             "max_dur": 1.0, "reason": f"cut at {c['t']}s", "weight": c["score"] + 1}
            for c in a["cuts"]]
    if motion:
        cand += [{"t": p["t"], "query": "swish", "max_dur": 0.6,
                  "reason": f"motion peak at {p['t']}s", "weight": p["score"]}
                 for p in a["motion_peaks"]]
    # Keep the strongest cues, respecting min_gap and overall density.
    chosen = []
    for c in sorted(cand, key=lambda c: -c["weight"]):
        if len(chosen) >= max_cues:
            break
        if all(abs(c["t"] - x["t"]) >= min_gap for x in chosen):
            chosen.append(c)
    chosen.sort(key=lambda c: c["t"])
    for i, c in enumerate(chosen):
        c.pop("weight")
        c["id"] = i
    cues = {"cues": chosen,
            "notes": "Edit freely: t (s, when the sound starts), query (English "
                     "SFX keywords), max_dur (s), gain_db (relative). Delete cues "
                     "that do not help."}
    save_json(os.path.join(work, "cues.json"), cues)
    return cues


# ---------------------------------------------------------------- openverse

_last_call = [0.0]


def api_search(params):
    key = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()
    path = os.path.join(CACHE, "search", key + ".json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL:
        return load_json(path)
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        wait = 3.1 - (time.time() - _last_call[0])  # stay under 20 req/min anon
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        token = os.environ.get("OPENVERSE_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
                left = r.headers.get("x-ratelimit-available-anon_sustained")
                if left is not None and int(left) < 20:
                    log(f"warning: only {left} anonymous Openverse calls left today")
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                delay = int(e.headers.get("Retry-After") or 20)
                log(f"rate limited, sleeping {delay}s")
                time.sleep(min(delay, 120))
                continue
            raise SystemExit(f"Openverse API error {e.code}: {e.read()[:300]!r}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    save_json(path, data)
    return data


def score_result(r, query, target):
    words = set(re.findall(r"[a-z]+", query.lower()))
    title = r.get("title") or ""
    text = set(re.findall(r"[a-z]+", title.lower()))
    tags = {t["name"].lower() for t in r.get("tags") or []}
    hit = len(words & text) * 2 + len(words & tags)
    dur = (r.get("duration") or 0) / 1000
    if dur <= 0.05:
        return None
    fit = abs(math.log(max(dur, 0.1) / max(target, 0.3)))
    bad = len((BAD_WORDS - words) & (text | tags))
    return hit * 1.5 - fit - bad * 2


def find_sound(query, target, source):
    """Ranked candidates from one source; CC0/PDM first, CC BY only if needed."""
    ranked = []
    for rank, tier in enumerate(LICENSE_TIERS):
        params = {"q": query, "license": ",".join(tier), "source": source,
                  "length": "shortest,short", "page_size": 20}
        for r in api_search(params).get("results", []):
            if (r.get("license") not in tier or not r.get("url")
                    or r.get("category") in SKIP_CATEGORIES):
                continue
            s = score_result(r, query, target)
            if s is not None:
                ranked.append((s - rank * 1.5, r))
        if sum(1 for s, _ in ranked if s > 0) >= 3:
            break
    ranked.sort(key=lambda x: -x[0])
    return [r for _, r in ranked]


def api_detail(sound_id):
    path = os.path.join(CACHE, "detail", sound_id + ".json")
    if os.path.exists(path):
        return load_json(path)
    req = urllib.request.Request(f"{API}{sound_id}/", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    save_json(path, data)
    return data


_blocked_hosts = set()


def download(r, dest_dir):
    host = urllib.parse.urlparse(r["url"]).netloc
    ext = (r.get("filetype") or "mp3").lower()
    cache = os.path.join(CACHE, "audio", f"{r['id']}.{ext}")
    if not os.path.exists(cache):
        if host in _blocked_hosts:
            raise OSError(f"{host} refused earlier downloads")
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        req = urllib.request.Request(r["url"], headers={"User-Agent": UA})
        for attempt in range(3):
            time.sleep(1.0)  # be polite to file hosts
            try:
                with urllib.request.urlopen(req, timeout=60) as resp, \
                        open(cache + ".part", "wb") as f:
                    shutil.copyfileobj(resp, f)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(min(int(e.headers.get("Retry-After") or 10), 30))
                    continue
                if e.code in (401, 403, 429):
                    _blocked_hosts.add(host)
                    log(f"warning: {host} returned {e.code}; skipping its files "
                        "(some hosts block cloud/datacenter IPs)")
                raise
        os.replace(cache + ".part", cache)
    os.makedirs(dest_dir, exist_ok=True)
    dst = os.path.join(dest_dir, os.path.basename(cache))
    shutil.copyfile(cache, dst)
    return dst


def try_download(cands, dest_dir):
    """First candidate that downloads and decodes, or None."""
    for r in cands[:6]:
        try:
            path = download(r, dest_dir)
            if probe(path)["streams"]:
                return r, path
        except (OSError, urllib.error.URLError, SystemExit) as e:
            log(f"skip {r.get('id')}: {e}")
    return None


def sound_entry(r, path, alternatives):
    _, peak = volume_stats(path)
    return {
        "file": path, "peak_db": peak,
        "title": r.get("title"), "creator": r.get("creator"),
        "creator_url": r.get("creator_url"),
        "license": r.get("license"), "license_version": r.get("license_version"),
        "license_url": r.get("license_url"),
        "landing_url": r.get("foreign_landing_url"),
        "source": r.get("source"), "openverse_id": r["id"],
        "attribution": r.get("attribution"),
        "needs_attribution": r.get("license") in NEEDS_ATTRIBUTION,
        "duration": (r.get("duration") or 0) / 1000,
        "alternatives": [{"sound_id": x["id"], "title": x.get("title"),
                          "license": x.get("license"),
                          "duration": (x.get("duration") or 0) / 1000}
                         for x in alternatives[:3]],
    }


def fetch(work, sources=DEFAULT_SOURCES):
    cues = load_json(os.path.join(work, "cues.json"))["cues"]
    dest = os.path.join(work, "sfx")
    by_key, manifest = {}, {"sounds": {}, "cues": []}
    allowed = {l for tier in LICENSE_TIERS for l in tier}
    for c in cues:
        q = c.get("query", "").strip().lower()
        key = c.get("sound_id") or q
        if key not in by_key:
            chosen, cands = None, []
            if c.get("sound_id"):  # pinned sound (e.g. picked from alternatives)
                r = api_detail(c["sound_id"])
                if r.get("license") in allowed:
                    chosen = try_download([r], dest)
                else:
                    log(f"[{key}] license {r.get('license')} not allowed; skipped")
            else:
                for source in sources:  # later sources only if earlier ones fail
                    cands = find_sound(q, c.get("max_dur", DEFAULT_MAX_DUR), source)
                    chosen = try_download(cands, dest)
                    if chosen:
                        cands = [x for x in cands if x is not chosen[0]]
                        break
            by_key[key] = chosen
            if chosen:
                r, path = chosen
                manifest["sounds"][r["id"]] = sound_entry(r, path, cands)
                log(f"[{key}] -> {r.get('title')!r} ({r.get('license')}, {r.get('source')})"
                    f" by {r.get('creator')}")
            else:
                log(f"[{key}] no usable result; cue(s) will be skipped")
        chosen = by_key[key]
        manifest["cues"].append({**c, "sound": chosen[0]["id"] if chosen else None})
    save_json(os.path.join(work, "manifest.json"), manifest)
    return manifest


# ---------------------------------------------------------------- mix

def in_ranges(t, ranges):
    return any(s - 0.1 <= t <= e + 0.1 for s, e in ranges)


def default_output(video):
    stem, ext = os.path.splitext(os.path.abspath(video))
    return f"{stem}_sfx{ext or '.mp4'}"


def mix(video, work, out=None, credits=None, force=False):
    a = load_json(os.path.join(work, "analysis.json"))
    m = load_json(os.path.join(work, "manifest.json"))
    out = os.path.abspath(out or default_output(video))
    if os.path.realpath(out) == os.path.realpath(video):
        raise SystemExit("refusing to overwrite the source video")
    if os.path.exists(out) and not force:
        raise SystemExit(f"{out} exists (use --force)")
    credits = credits or os.path.join(os.path.dirname(out), "credits.txt")

    dur = a["duration"]
    audio = a.get("audio") or {}
    active = audio.get("active") or []
    # SFX peak target: a few dB above the programme's average level, so they
    # read clearly without fighting narration/BGM. Silent video: fixed level.
    base = -9.0 if audio.get("mean_db") is None else min(-6.0, max(-24.0, audio["mean_db"] + 8))

    cues = [c for c in m["cues"] if c.get("sound") and 0 <= c["t"] < dur]
    inputs, chains, labels, used = ["-i", video], [], [], {}
    for i, c in enumerate(cues, start=1):
        snd = m["sounds"][c["sound"]]
        inputs += ["-i", snd["file"]]
        length = min(float(c.get("max_dur", DEFAULT_MAX_DUR)), dur - c["t"])
        target = base + float(c.get("gain_db", 0)) - (4 if in_ranges(c["t"], active) else 0)
        gain = target - (snd["peak_db"] if snd["peak_db"] is not None else -3.0)
        fade = min(0.15, length / 3)
        ms = int(round(c["t"] * 1000))
        chains.append(
            f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"silenceremove=start_periods=1:start_threshold=-50dB,"
            f"atrim=0:{length:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=out:st={max(0, length - fade):.3f}:d={fade:.3f},"
            f"volume={gain:.2f}dB,adelay={ms}|{ms}[s{i}]")
        labels.append(f"[s{i}]")
        used.setdefault(c["sound"], []).append(c["t"])
    if not cues:
        raise SystemExit("no usable cues; nothing to mix")

    if a["has_audio"]:
        chains.append("[0:a]aformat=sample_rates=48000:channel_layouts=stereo[base]")
    else:
        chains.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.3f}[base]")
    chains.append(f"[base]{''.join(labels)}amix=inputs={len(labels) + 1}:duration=first:"
                  f"normalize=0,alimiter=limit=0.89[aout]")

    cmd = ["ffmpeg", "-hide_banner", "-y", *inputs, "-filter_complex", ";".join(chains),
           "-map", "0:v:0", "-map", "[aout]", "-map_metadata", "0", "-map_chapters", "0",
           "-c:v", "copy",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    run(cmd)
    write_credits(credits, m, used, out)
    log(f"wrote {out}\nwrote {credits}")
    return out, credits


def write_credits(path, m, used, out):
    lines = [f"Sound effect credits for {os.path.basename(out)}", "",
             "Sounds found via Openverse (https://openverse.org). This video/tool is not",
             "endorsed or certified by Openverse. Openverse does not verify licenses;",
             "check each source page before publishing.", ""]
    for sid, times in used.items():
        s = m["sounds"][sid]
        lic = f"{s['license'].upper()} {s.get('license_version') or ''}".strip()
        lines += [f"- \"{s['title']}\" by {s['creator']}",
                  f"  License: {lic} ({s['license_url']})",
                  f"  Source: {s['landing_url']}  [{s['source']}, openverse id {sid}]",
                  f"  Used at: {', '.join(f'{t:.2f}s' for t in sorted(times))}",
                  f"  Attribution: {s['attribution']}",
                  f"  Attribution required: {'YES' if s['needs_attribution'] else 'no (public domain)'}",
                  ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------- check

def check():
    ok = True
    for tool in ("ffmpeg", "ffprobe"):
        p = shutil.which(tool)
        print(f"{tool}: {p or 'MISSING'}")
        ok &= bool(p)
    if not ok:
        print("Install: apt-get install -y ffmpeg | brew install ffmpeg | winget install ffmpeg")
        return 1
    try:
        req = urllib.request.Request(API + "?q=click&page_size=1", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            print("openverse: OK, anonymous calls left today:",
                  r.headers.get("x-ratelimit-available-anon_sustained", "?"),
                  "| burst:", r.headers.get("x-ratelimit-limit-anon_burst", "?"))
    except Exception as e:  # noqa: BLE001
        print(f"openverse: UNREACHABLE ({e})")
        return 1
    print(f"cache: {CACHE}")
    return 0


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    for name in ("analyze", "plan", "fetch", "mix", "run"):
        p = sub.add_parser(name)
        p.add_argument("video")
        p.add_argument("--work", help="work dir (default: <stem>_sfx_work next to video)")
        if name in ("analyze", "run"):
            p.add_argument("--threshold", type=float, default=0.3, help="scene-cut threshold")
        if name in ("plan", "run"):
            p.add_argument("--min-gap", type=float, default=1.2)
            p.add_argument("--per-seconds", type=float, default=2.5,
                           help="at most one cue per this many seconds")
            p.add_argument("--motion", action="store_true", help="also cue motion peaks")
        if name in ("fetch", "run"):
            p.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                           help="Openverse sources to try, in order")
        if name in ("mix", "run"):
            p.add_argument("-o", "--out")
            p.add_argument("--credits")
            p.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.cmd == "check":
        sys.exit(check())

    if not os.path.isfile(args.video):
        raise SystemExit(f"not found: {args.video}")
    work = workdir_for(args.video, args.work)
    if args.cmd in ("analyze", "run"):
        a = analyze(args.video, work, args.threshold)
        log(f"analysis: {len(a['cuts'])} cuts, {len(a['motion_peaks'])} motion peaks, "
            f"sheet {a['sheet']}")
    if args.cmd in ("plan", "run"):
        c = plan(work, args.min_gap, args.per_seconds, args.motion)
        log(f"plan: {len(c['cues'])} cues -> {os.path.join(work, 'cues.json')}")
    if args.cmd in ("fetch", "run"):
        fetch(work, args.sources.split(","))
    if args.cmd in ("mix", "run"):
        mix(args.video, work, args.out, args.credits, args.force)
    if args.cmd == "analyze":
        print(json.dumps(load_json(os.path.join(work, "analysis.json")), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
