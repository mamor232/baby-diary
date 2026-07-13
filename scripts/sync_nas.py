"""
Synology NAS -> baby-diary site sync script.

Runs inside GitHub Actions (scheduled every 12h). Logs into DSM via the
Web API using a dedicated read-only account, lists files under the
"photo" shared folder, downloads anything not seen before (tracked in
manifest.json), processes it (EXIF date / pregnancy week, image resize,
video transcode), regenerates index.html, then lets the workflow commit
+ push the result.

Required environment variables (set as GitHub Secrets):
  SYNOLOGY_HOST  e.g. https://shimbbo.tw3.quickconnect.to  (no trailing slash)
  SYNOLOGY_USER  e.g. github-bot
  SYNOLOGY_PASS  the account's password
"""
import os
import json
import subprocess
from collections import defaultdict
from datetime import date, datetime

import requests
from PIL import Image
from PIL.ExifTags import TAGS

HOST = os.environ["SYNOLOGY_HOST"].rstrip("/")
USER = os.environ["SYNOLOGY_USER"]
PASS = os.environ["SYNOLOGY_PASS"]
SHARE_PATH = "/photo"
DUE = date(2027, 1, 5)
MANIFEST_PATH = "manifest.json"
TMP_DIR = "tmp_download"

SESSION = requests.Session()
SESSION.verify = True


def ga_week_day(d: date):
    ga_days = 280 - (DUE - d).days
    return divmod(ga_days, 7)


def login():
    r = SESSION.get(f"{HOST}/webapi/auth.cgi", params={
        "api": "SYNO.API.Auth", "version": 6, "method": "login",
        "account": USER, "passwd": PASS, "session": "FileStation", "format": "sid",
    }, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Synology login failed: {data}")
    return data["data"]["sid"]


def list_files(sid, folder):
    files = []
    r = SESSION.get(f"{HOST}/webapi/entry.cgi", params={
        "api": "SYNO.FileStation.List", "version": 2, "method": "list",
        "folder_path": folder, "additional": '["size","time"]', "_sid": sid,
    }, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Synology list failed for {folder}: {data}")
    for item in data["data"]["files"]:
        if item["isdir"]:
            files.extend(list_files(sid, item["path"]))
        else:
            files.append(item)
    return files


def download_file(sid, remote_path, local_path):
    r = SESSION.get(f"{HOST}/webapi/entry.cgi", params={
        "api": "SYNO.FileStation.Download", "version": 2, "method": "download",
        "path": remote_path, "mode": "download", "_sid": sid,
    }, stream=True, timeout=180)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)


def process_photo(name, local_path, manifest):
    img = Image.open(local_path)
    exif = img._getexif() or {}
    info = {TAGS.get(k, k): v for k, v in exif.items()}
    dt_str = info.get("DateTimeOriginal")
    dt = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S") if dt_str else datetime.now()
    w, d = ga_week_day(dt.date())

    web_name = name.rsplit(".", 1)[0] + "_web.jpg"
    out_img = img.convert("RGB")
    iw, ih = out_img.size
    if iw > 1400:
        ratio = 1400 / iw
        out_img = out_img.resize((1400, int(ih * ratio)), Image.LANCZOS)
    out_img.save(web_name, "JPEG", quality=80, optimize=True)

    manifest.append({
        "source_file": name, "file": web_name, "type": "photo",
        "datetime": dt.isoformat(), "date": dt.date().isoformat(),
        "week": w, "day": d,
    })


def process_video(name, local_path, manifest):
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format_tags=creation_time",
         "-of", "default=noprint_wrappers=1:nokey=1", local_path],
        capture_output=True, text=True,
    ).stdout.strip()
    if probe:
        try:
            dt = datetime.fromisoformat(probe.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            dt = datetime.now()
    else:
        dt = datetime.now()
    w, d = ga_week_day(dt.date())

    dur = float(subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", local_path],
        capture_output=True, text=True,
    ).stdout.strip())

    web_name = name.rsplit(".", 1)[0] + "_web.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-i", local_path, "-vf", "scale=854:-2",
        "-c:v", "libx264", "-crf", "28", "-preset", "medium",
        "-movflags", "+faststart", "-c:a", "aac", "-b:a", "96k", web_name,
    ], check=True)

    manifest.append({
        "source_file": name, "file": web_name, "type": "video",
        "date": dt.date().isoformat(), "week": w, "day": d,
        "duration_sec": dur,
    })


def build_site(manifest):
    groups = defaultdict(list)
    for m in manifest:
        groups[(m["week"], m["day"])].append(m)
    group_keys = sorted(groups.keys())

    pastels = ['#e6f7f2', '#ffe8e0', '#fff6df', '#e9e4ff', '#ffe0ef', '#e4f7e0']
    stickers = ['🌸', '⭐', '🎀', '💛', '🍼', '🧸', '☁️', '💫']
    rotations = [-2, 1.5, -1]

    def week_label(w, d):
        return f"임신 {w}주 {d}일"

    nav_html, sections_html = [], []
    photo_counter = 0
    for i, key in enumerate(group_keys):
        w, d = key
        entries = groups[key]
        entries_photo = [e for e in entries if e["type"] == "photo"]
        entries_video = [e for e in entries if e["type"] == "video"]
        date_str = entries[0]["date"]
        sec_id = f"w{w}-{d}"
        pastel = pastels[i % len(pastels)]
        nav_html.append(f'<a href="#{sec_id}">{w}주{d}일</a>')

        cards = []
        n_photos = len(entries_photo)
        for idx, e in enumerate(entries_photo):
            rot = rotations[photo_counter % len(rotations)]
            sticker = stickers[photo_counter % len(stickers)]
            photo_counter += 1
            badge = f'<span class="count-badge">{idx + 1}/{n_photos}</span>' if n_photos > 1 else ''
            cards.append(f'''
        <div class="card photo-card" style="--rot:{rot}deg;">
          <span class="sticker">{sticker}</span>
          {badge}
          <div class="photo-wrap">
            <img src="{e['file']}" alt="{date_str}" loading="lazy" onclick="openPhotoLightbox('{date_str}',{idx})">
          </div>
          <div class="polaroid-cap">
            <span class="cap-date">{date_str}</span>
            <span class="cap-label">초음파 사진</span>
          </div>
        </div>''')
        for e in entries_video:
            mins = int(e["duration_sec"] // 60)
            secs = int(e["duration_sec"] % 60)
            cards.append(f'''
        <div class="card wide video-card">
          <div class="video-frame">
            <video controls preload="metadata" playsinline>
              <source src="{e['file']}" type="video/mp4">
            </video>
          </div>
          <div class="video-meta">
            <span class="cap-date">{date_str}</span>
            <span class="cap-label">🎬 영상 · {mins}:{secs:02d}</span>
            <span class="comment-chip" onclick="openVideoLightbox('{e['file']}')">💬 댓글 남기기</span>
          </div>
        </div>''')

        sections_html.append(f'''
    <section class="week-block" id="{sec_id}">
      <div class="week-header">
        <div class="week-icon" style="background:{pastel}">🤰</div>
        <h2 class="round-font">{week_label(w, d)}</h2>
        <span class="date-tag">📅 {date_str}</span>
      </div>
      <div class="grid">
        {''.join(cards)}
      </div>
    </section>''')

    nav_joined = ''.join(nav_html)
    sections_joined = ''.join(sections_html)

    calendar_data = defaultdict(list)
    for m in manifest:
        calendar_data[m["date"]].append({"file": m["file"], "type": m["type"]})
    calendar_json = json.dumps(dict(calendar_data), ensure_ascii=False)

    html = f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>우리 아기 이야기 · 태담 다이어리</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Baloo+2:wght@500;600;700;800&family=Gaegu:wght@400;700&family=Pretendard:wght@400;500;600;700&display=swap');
  :root{{
    --bg:#fffdf6; --mint:#a9ddd0; --mint-soft:#e6f7f2;
    --coral:#ff9d85; --coral-soft:#ffe8e0; --yellow:#ffd873; --yellow-soft:#fff6df;
    --lavender:#c9bdf5; --lavender-soft:#f0ecff;
    --ink:#4a4038; --ink-soft:#a89b8f; --card:#ffffff; --line:#f0e8de;
  }}
  *{{box-sizing:border-box;}}
  html,body{{margin:0;padding:0;}}
  body{{
    font-family:'Pretendard','Apple SD Gothic Neo',sans-serif;color:var(--ink);-webkit-font-smoothing:antialiased;
    background-color:var(--bg);
    background-image:radial-gradient(circle at 18px 18px, rgba(169,221,208,.22) 2.5px, transparent 0);
    background-size:36px 36px;
  }}
  .round-font{{font-family:'Baloo 2','Pretendard',sans-serif;}}
  .hand-font{{font-family:'Gaegu','Pretendard',sans-serif;}}

  .hero{{padding:60px 24px 46px;text-align:center;background:linear-gradient(180deg, var(--mint-soft) 0%, var(--bg) 82%);border-radius:0 0 44px 44px;position:relative;overflow:hidden;}}
  .hero .deco{{position:absolute;font-size:26px;opacity:.6;animation:float 5s ease-in-out infinite;}}
  .hero .d1{{top:20px;left:8%;}} .hero .d2{{top:60px;right:10%;animation-delay:1.2s;}}
  .hero .d3{{bottom:10px;left:20%;animation-delay:2s;}} .hero .d4{{bottom:30px;right:22%;animation-delay:.6s;}}
  .hero .d5{{top:100px;left:44%;animation-delay:1.8s;font-size:20px;}} .hero .d6{{bottom:70px;right:6%;animation-delay:.9s;font-size:22px;}}
  @keyframes float{{0%,100%{{transform:translateY(0) rotate(0deg);}}50%{{transform:translateY(-10px) rotate(6deg);}}}}
  .hero-badge{{display:inline-block;background:var(--card);color:var(--coral);font-weight:700;font-size:12.5px;padding:6px 16px;border-radius:999px;box-shadow:0 4px 12px rgba(255,157,133,.25);margin-bottom:16px;letter-spacing:.2px;}}
  .hero h1{{font-family:'Baloo 2',sans-serif;font-size:clamp(26px,5vw,40px);font-weight:700;margin:0 0 10px;line-height:1.35;}}
  .hero h1 .hl{{color:var(--coral);}}
  .hero p{{color:var(--ink-soft);font-size:14.5px;max-width:420px;margin:0 auto;line-height:1.6;font-family:'Gaegu','Pretendard',sans-serif;}}
  .dday{{display:inline-block;margin-top:18px;background:var(--card);border:2px solid var(--coral-soft);padding:8px 22px;border-radius:999px;font-weight:700;color:var(--coral);font-size:14px;animation:pulseDday 2.6s ease-in-out infinite;}}
  @keyframes pulseDday{{0%{{box-shadow:0 0 0 0 rgba(255,157,133,.35);}}70%{{box-shadow:0 0 0 10px rgba(255,157,133,0);}}100%{{box-shadow:0 0 0 0 rgba(255,157,133,0);}}}}

  .week-nav{{display:flex;justify-content:center;gap:10px;flex-wrap:wrap;margin-top:26px;}}
  .week-nav a{{text-decoration:none;font-weight:600;font-size:13px;color:var(--ink);background:var(--card);border:2px solid var(--mint-soft);padding:7px 16px;border-radius:999px;transition:all .2s ease;}}
  .week-nav a:hover{{background:var(--mint);border-color:var(--mint);color:#fff;transform:translateY(-3px) rotate(-2deg);}}

  .note{{max-width:640px;margin:26px auto 0;padding:12px 18px;background:var(--yellow-soft);border-radius:14px;font-size:12px;color:#8a6a2a;text-align:center;}}

  /* calendar (now the main hero visual) */
  .calendar-wrap{{max-width:600px;margin:34px auto 0;}}
  .calendar-card{{background:var(--card);border-radius:30px;padding:26px 26px 30px;box-shadow:0 14px 34px rgba(74,64,56,.14);}}
  .calendar-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px;}}
  .calendar-title{{font-family:'Baloo 2',sans-serif;font-weight:700;font-size:22px;color:var(--ink);}}
  .cal-nav{{background:var(--mint-soft);border:none;width:38px;height:38px;border-radius:50%;font-size:19px;color:var(--ink);cursor:pointer;line-height:1;}}
  .cal-nav:hover{{background:var(--mint);color:#fff;}}
  .calendar-weekdays{{display:grid;grid-template-columns:repeat(7,1fr);text-align:center;font-size:12.5px;font-weight:600;color:var(--ink-soft);margin-bottom:10px;}}
  .calendar-grid{{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;}}
  .cal-cell{{aspect-ratio:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;border-radius:14px;position:relative;padding-top:6px;}}
  .cal-cell.empty{{visibility:hidden;}}
  .cal-num{{font-size:12.5px;color:var(--ink-soft);}}
  .cal-cell.today .cal-num{{color:var(--coral);font-weight:700;}}
  .cal-cell.today{{background:var(--mint-soft);}}
  .cal-cell.due{{background:var(--coral-soft);}}
  .cal-cell.has-photo{{cursor:pointer;}}
  .cal-thumb{{width:40px;height:40px;border-radius:50%;overflow:hidden;background:var(--mint-soft);display:flex;align-items:center;justify-content:center;font-size:18px;margin-top:4px;position:relative;box-shadow:0 3px 8px rgba(74,64,56,.18);transition:transform .15s ease;}}
  .cal-cell.has-photo:hover .cal-thumb{{transform:scale(1.12);}}
  .cal-thumb img{{width:100%;height:100%;object-fit:cover;}}
  .cal-badge{{position:absolute;bottom:-4px;right:-6px;background:var(--coral);color:#fff;font-size:9.5px;font-weight:700;border-radius:999px;padding:2px 5px;line-height:1.2;}}

  .day-panel{{position:fixed;inset:0;background:rgba(20,15,12,.7);display:none;align-items:center;justify-content:center;z-index:998;padding:24px;}}
  .day-panel.open{{display:flex;}}
  .day-panel-inner{{background:var(--card);border-radius:22px;padding:20px;max-width:420px;width:100%;max-height:80vh;overflow-y:auto;position:relative;}}
  .day-panel-close{{position:absolute;top:14px;right:16px;cursor:pointer;font-size:22px;color:var(--ink-soft);}}
  .day-panel-inner h3{{margin:0 0 14px;font-family:'Baloo 2',sans-serif;font-size:16px;}}
  .day-panel-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;}}
  .day-panel-item{{border-radius:12px;overflow:hidden;cursor:pointer;aspect-ratio:1;background:var(--mint-soft);display:flex;align-items:center;justify-content:center;font-size:12px;color:var(--ink);}}
  .day-panel-item img{{width:100%;height:100%;object-fit:cover;}}

  main{{max-width:900px;margin:0 auto;padding:48px 20px 90px;}}
  .week-block{{margin-bottom:56px;scroll-margin-top:30px;}}
  .week-header{{display:flex;align-items:center;gap:12px;margin-bottom:24px;padding-bottom:16px;border-bottom:2px solid var(--line);}}
  .week-icon{{width:46px;height:46px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0;border:3px solid #fff;box-shadow:0 3px 8px rgba(74,64,56,.14);}}
  .week-header h2{{font-size:19px;margin:0;}}
  .date-tag{{font-size:12px;font-weight:600;color:#fff;background:var(--coral);padding:5px 13px;border-radius:999px;margin-left:auto;}}

  .grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:20px 16px;}}
  @media (max-width:640px){{.grid{{grid-template-columns:repeat(2,1fr);}}}}

  /* photo polaroid cards */
  .photo-card{{position:relative;background:var(--card);border-radius:18px;padding:8px 8px 4px;box-shadow:0 6px 16px rgba(74,64,56,.12);transition:transform .3s ease, box-shadow .3s ease;transform:rotate(var(--rot,0deg));}}
  .photo-card:hover{{transform:translateY(-6px) scale(1.04) rotate(0deg);box-shadow:0 14px 26px rgba(74,64,56,.18);z-index:2;}}
  .photo-wrap{{border-radius:12px;overflow:hidden;background:#000;}}
  .photo-card img{{width:100%;height:190px;object-fit:cover;display:block;cursor:zoom-in;}}
  .polaroid-cap{{text-align:center;padding:9px 4px 12px;}}
  .cap-date{{display:block;font-family:'Gaegu','Pretendard',sans-serif;font-size:12px;color:var(--ink-soft);}}
  .cap-label{{display:block;font-family:'Gaegu','Pretendard',sans-serif;font-size:14.5px;font-weight:700;color:var(--ink);margin-top:2px;}}
  .sticker{{position:absolute;top:-12px;right:-8px;font-size:24px;filter:drop-shadow(0 2px 3px rgba(0,0,0,.18));transform:rotate(14deg);z-index:2;}}
  .count-badge{{position:absolute;top:8px;left:8px;background:rgba(20,15,12,.55);color:#fff;font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:999px;z-index:2;}}

  /* video cards */
  .video-card{{grid-column:span 3;background:linear-gradient(135deg,var(--mint-soft),var(--lavender-soft));padding:12px;border-radius:24px;box-shadow:0 6px 18px rgba(74,64,56,.10);}}
  @media (max-width:640px){{.video-card{{grid-column:span 2;}}}}
  .video-frame{{border-radius:16px;overflow:hidden;background:#000;}}
  .video-card video{{width:100%;display:block;max-height:480px;}}
  .video-meta{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 4px 2px;}}
  .video-meta .cap-date{{color:var(--ink-soft);}}
  .video-meta .cap-label{{margin-top:0;}}
  .comment-chip{{margin-left:auto;cursor:pointer;background:var(--coral);color:#fff;font-size:12.5px;font-weight:700;padding:7px 15px;border-radius:999px;box-shadow:0 4px 10px rgba(255,157,133,.38);transition:transform .15s ease;}}
  .comment-chip:hover{{transform:translateY(-2px) scale(1.05);}}

  .lightbox{{position:fixed;inset:0;background:rgba(20,15,12,.88);display:none;align-items:center;justify-content:center;z-index:999;padding:24px;}}
  .lightbox.open{{display:flex;}}
  .lightbox-close{{position:absolute;top:20px;right:24px;color:#fff;font-size:32px;font-weight:700;cursor:pointer;line-height:1;background:rgba(255,255,255,.15);width:44px;height:44px;border-radius:50%;display:flex;align-items:center;justify-content:center;z-index:3;}}
  .lightbox-close:hover{{background:rgba(255,255,255,.3);}}
  .lightbox-inner{{display:flex;flex-direction:column;max-width:520px;width:100%;max-height:92vh;}}
  .lightbox-media{{position:relative;}}
  .lightbox-inner img{{max-width:100%;max-height:58vh;border-radius:16px 16px 0 0;box-shadow:0 12px 40px rgba(0,0,0,.4);object-fit:contain;background:#000;display:block;width:100%;user-select:none;touch-action:pan-y;}}
  .lightbox-inner img[src=""]{{display:none;}}
  .lb-arrow{{position:absolute;top:50%;transform:translateY(-50%);background:rgba(20,15,12,.5);color:#fff;border:none;width:38px;height:38px;border-radius:50%;font-size:19px;cursor:pointer;display:none;align-items:center;justify-content:center;z-index:2;}}
  .lb-arrow:hover{{background:rgba(20,15,12,.75);}}
  .lb-prev{{left:10px;}}
  .lb-next{{right:10px;}}
  .lb-counter{{position:absolute;top:12px;left:50%;transform:translateX(-50%);background:rgba(20,15,12,.5);color:#fff;font-size:11.5px;font-weight:600;padding:4px 12px;border-radius:999px;display:none;z-index:2;}}
  .comment-box{{background:var(--card);border-radius:0 0 20px 20px;padding:14px 16px 16px;display:flex;flex-direction:column;gap:10px;max-height:34vh;}}
  .comment-box.no-image{{border-radius:20px;max-height:60vh;}}
  .comment-list{{overflow-y:auto;display:flex;flex-direction:column;gap:8px;flex:1;min-height:40px;}}
  .c-item{{max-width:88%;background:var(--mint-soft);border-radius:14px 14px 14px 4px;padding:8px 12px;font-size:13.5px;line-height:1.5;font-family:'Gaegu','Pretendard',sans-serif;}}
  .c-item:nth-child(even){{background:var(--coral-soft);align-self:flex-end;border-radius:14px 14px 4px 14px;}}
  .c-meta{{display:flex;justify-content:space-between;align-items:baseline;gap:8px;}}
  .c-name{{font-family:'Pretendard',sans-serif;font-weight:700;color:var(--coral);font-size:12px;}}
  .c-time{{font-size:10px;color:var(--ink-soft);font-family:'Pretendard',sans-serif;white-space:nowrap;}}
  .c-del{{font-size:10px;color:var(--coral);cursor:pointer;text-decoration:underline;margin-left:6px;font-family:'Pretendard',sans-serif;}}
  .c-text{{color:var(--ink);word-break:break-word;}}
  .c-empty, .c-loading{{color:var(--ink-soft);font-size:12.5px;text-align:center;padding:10px 0;font-family:'Gaegu','Pretendard',sans-serif;}}
  .comment-form{{display:flex;gap:6px;flex-wrap:wrap;}}
  .comment-form input{{border:2px solid var(--mint-soft);border-radius:999px;padding:7px 12px;font-size:13px;font-family:inherit;outline:none;}}
  .comment-form input:focus{{border-color:var(--lavender);}}
  .comment-form .c-input-name{{flex:0 0 76px;}}
  .comment-form .c-input-text{{flex:1;min-width:120px;}}
  .comment-form button{{background:var(--coral);color:#fff;border:none;border-radius:999px;padding:7px 16px;font-size:13px;font-weight:700;cursor:pointer;flex-shrink:0;}}
  .comment-form button:disabled{{opacity:.5;}}

  footer{{text-align:center;padding:34px 24px 54px;color:var(--ink-soft);font-size:12px;}}
  footer .divider{{font-size:16px;letter-spacing:8px;color:var(--mint);margin-bottom:12px;opacity:.9;}}
  footer strong{{color:var(--ink);}}
  .admin-link{{display:inline-block;margin-top:12px;font-size:11px;color:var(--ink-soft);cursor:pointer;text-decoration:underline;}}
</style>
</head>
<body>
<div class="hero">
  <span class="deco d1">⭐</span><span class="deco d2">🎈</span><span class="deco d3">🧸</span><span class="deco d4">☁️</span><span class="deco d5">🍼</span><span class="deco d6">🌸</span>
  <div class="hero-badge">🤰 Pregnancy Diary</div>
  <h1>우리 아기를 <span class="hl">기다리는</span> 시간,<br>하루하루 기록해요</h1>
  <p>초음파 사진과 영상을 촬영일 기준 임신 주차별로 자동 정리했어요.</p>
  <div class="dday" id="dday">💗 D-day 계산 중...</div>
  <div class="week-nav">
    {nav_joined}
  </div>

  <div class="calendar-wrap">
    <div class="calendar-card">
      <div class="calendar-header">
        <button class="cal-nav" onclick="calShift(-1)">‹</button>
        <div class="calendar-title" id="cal-title">-</div>
        <button class="cal-nav" onclick="calShift(1)">›</button>
      </div>
      <div class="calendar-weekdays"><span>일</span><span>월</span><span>화</span><span>수</span><span>목</span><span>금</span><span>토</span></div>
      <div class="calendar-grid" id="calendar-grid"></div>
    </div>
  </div>

  <div class="note">🔒 이 페이지는 공개 링크입니다 · 가족 누구나 링크로 볼 수 있고, 사진을 눌러 댓글을 남길 수 있어요</div>
</div>
<main>
  {sections_joined}
</main>
<footer>
  <div class="divider">🍼 ⋯ ⭐ ⋯ 🧸</div>
  Pregnancy diary · 출산예정일 <strong>2027-01-05</strong> · 나스에 새 사진이 추가되면 12시간마다 자동으로 갱신됩니다
  <div><span class="admin-link" id="admin-link">🔧 관리자 로그인</span></div>
</footer>

<div class="day-panel" id="day-panel" onclick="closeDayPanel(event)">
  <div class="day-panel-inner" onclick="event.stopPropagation()">
    <div class="day-panel-close" onclick="closeDayPanel(event)">&times;</div>
    <h3 id="day-panel-title"></h3>
    <div class="day-panel-grid" id="day-panel-grid"></div>
  </div>
</div>

<div class="lightbox" id="lightbox" onclick="closeLightbox(event)">
  <div class="lightbox-close" onclick="closeLightbox(event)">&times;</div>
  <div class="lightbox-inner" onclick="event.stopPropagation()">
    <div class="lightbox-media" id="lightbox-media">
      <span class="lb-counter" id="lb-counter"></span>
      <img id="lightbox-img" src="" alt="확대 이미지">
      <button class="lb-arrow lb-prev" id="lb-prev" onclick="lightboxPrev()">‹</button>
      <button class="lb-arrow lb-next" id="lb-next" onclick="lightboxNext()">›</button>
    </div>
    <div class="comment-box" id="comment-box">
      <div class="comment-list" id="comment-list"></div>
      <form class="comment-form" id="comment-form">
        <input type="text" class="c-input-name" id="c-input-name" placeholder="이름" maxlength="20" required>
        <input type="text" class="c-input-text" placeholder="댓글을 남겨보세요" maxlength="300" required>
        <button type="submit">💌 등록</button>
      </form>
    </div>
  </div>
</div>
<script type="module">
  import {{ initializeApp }} from "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js";
  import {{ getFirestore, collection, addDoc, deleteDoc, doc, query, orderBy, onSnapshot, serverTimestamp }}
    from "https://www.gstatic.com/firebasejs/10.12.2/firebase-firestore.js";
  import {{ getAuth, signInWithEmailAndPassword, onAuthStateChanged, signOut }}
    from "https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js";

  const firebaseConfig = {{
    apiKey: "AIzaSyDjVPXOIBIQFQvFBN7tSjw1hB6I4b-T7Uk",
    authDomain: "baby-a52d3.firebaseapp.com",
    projectId: "baby-a52d3",
    storageBucket: "baby-a52d3.firebasestorage.app",
    messagingSenderId: "958352459153",
    appId: "1:958352459153:web:c725156fc33e1de1578630"
  }};
  const app = initializeApp(firebaseConfig);
  const db = getFirestore(app);
  const auth = getAuth(app);
  let currentUser = null;

  const DUE_DATE = "2027-01-05";
  const due = new Date("2027-01-05T00:00:00");
  const now = new Date();
  const diff = Math.ceil((due - now) / (1000*60*60*24));
  document.getElementById('dday').textContent = diff > 0 ? `💗 D-${{diff}}` : (diff === 0 ? '🎉 D-Day' : `💗 출산 후 ${{-diff}}일`);

  const savedName = localStorage.getItem('diary-commenter-name');
  if(savedName) document.getElementById('c-input-name').value = savedName;

  /* ---------- Calendar data ---------- */
  const CALENDAR_DATA = {calendar_json};

  function photosForDate(dateStr){{
    return (CALENDAR_DATA[dateStr] || []).filter(it => it.type === 'photo');
  }}

  /* ---------- Lightbox (photo gallery + video) ---------- */
  let unsub = null;
  let currentGallery = [];
  let currentIndex = 0;
  let currentMode = 'photo'; // 'photo' | 'video'

  function updateArrows(){{
    const show = currentMode === 'photo' && currentGallery.length > 1;
    document.getElementById('lb-prev').style.display = show ? 'flex' : 'none';
    document.getElementById('lb-next').style.display = show ? 'flex' : 'none';
    const counter = document.getElementById('lb-counter');
    if(show){{
      counter.style.display = 'block';
      counter.textContent = `${{currentIndex + 1}} / ${{currentGallery.length}}`;
    }} else {{
      counter.style.display = 'none';
    }}
  }}

  function showCurrentPhoto(){{
    const item = currentGallery[currentIndex];
    if(!item) return;
    document.getElementById('lightbox-img').src = item.file;
    document.getElementById('comment-box').classList.remove('no-image');
    updateArrows();
    loadComments(item.file);
  }}

  window.openPhotoLightbox = function(dateStr, idx){{
    currentMode = 'photo';
    currentGallery = photosForDate(dateStr);
    currentIndex = Math.max(0, Math.min(idx, currentGallery.length - 1));
    document.getElementById('lightbox').classList.add('open');
    showCurrentPhoto();
  }};

  window.openVideoLightbox = function(file){{
    currentMode = 'video';
    currentGallery = [];
    document.getElementById('lightbox-img').src = '';
    document.getElementById('comment-box').classList.add('no-image');
    updateArrows();
    document.getElementById('lightbox').classList.add('open');
    loadComments(file);
  }};

  window.lightboxPrev = function(){{
    if(currentMode !== 'photo' || currentGallery.length < 2) return;
    currentIndex = (currentIndex - 1 + currentGallery.length) % currentGallery.length;
    showCurrentPhoto();
  }};

  window.lightboxNext = function(){{
    if(currentMode !== 'photo' || currentGallery.length < 2) return;
    currentIndex = (currentIndex + 1) % currentGallery.length;
    showCurrentPhoto();
  }};

  window.closeLightbox = function(e){{
    if(e.target.id === 'lightbox' || e.target.classList.contains('lightbox-close')){{
      document.getElementById('lightbox').classList.remove('open');
      if(unsub){{ unsub(); unsub = null; }}
    }}
  }};

  document.addEventListener('keydown', (e) => {{
    const lbOpen = document.getElementById('lightbox').classList.contains('open');
    if(e.key === 'Escape'){{
      document.getElementById('lightbox').classList.remove('open');
      document.getElementById('day-panel').classList.remove('open');
      if(unsub){{ unsub(); unsub = null; }}
    }} else if(lbOpen && e.key === 'ArrowLeft'){{
      lightboxPrev();
    }} else if(lbOpen && e.key === 'ArrowRight'){{
      lightboxNext();
    }}
  }});

  /* swipe support */
  let touchStartX = null;
  const mediaEl = document.getElementById('lightbox-media');
  mediaEl.addEventListener('touchstart', (e) => {{
    touchStartX = e.changedTouches[0].clientX;
  }}, {{passive: true}});
  mediaEl.addEventListener('touchend', (e) => {{
    if(touchStartX === null) return;
    const dx = e.changedTouches[0].clientX - touchStartX;
    if(Math.abs(dx) > 45){{
      if(dx > 0) lightboxPrev(); else lightboxNext();
    }}
    touchStartX = null;
  }}, {{passive: true}});

  function fmtTime(ts){{
    if(!ts || !ts.toDate) return '';
    const d = ts.toDate();
    const mm = String(d.getMonth()+1).padStart(2,'0');
    const dd = String(d.getDate()).padStart(2,'0');
    const hh = String(d.getHours()).padStart(2,'0');
    const mi = String(d.getMinutes()).padStart(2,'0');
    return `${{mm}}.${{dd}} ${{hh}}:${{mi}}`;
  }}

  function loadComments(photoId){{
    const list = document.getElementById('comment-list');
    list.innerHTML = '<div class="c-loading">불러오는 중...</div>';
    document.getElementById('comment-form').dataset.photoId = photoId;
    if(unsub) unsub();
    const q = query(collection(db, 'comments', photoId, 'items'), orderBy('createdAt', 'asc'));
    unsub = onSnapshot(q, (snap) => {{
      if(snap.empty){{
        list.innerHTML = '<div class="c-empty">아직 댓글이 없어요. 첫 댓글을 남겨보세요 💌</div>';
        return;
      }}
      list.innerHTML = '';
      snap.forEach(docSnap => {{
        const d = docSnap.data();
        const item = document.createElement('div');
        item.className = 'c-item';

        const meta = document.createElement('div');
        meta.className = 'c-meta';
        const nameEl = document.createElement('span');
        nameEl.className = 'c-name';
        nameEl.textContent = d.name;
        meta.appendChild(nameEl);

        const rightWrap = document.createElement('span');
        const timeEl = document.createElement('span');
        timeEl.className = 'c-time';
        timeEl.textContent = fmtTime(d.createdAt);
        rightWrap.appendChild(timeEl);
        if(currentUser){{
          const delEl = document.createElement('span');
          delEl.className = 'c-del';
          delEl.textContent = '삭제';
          delEl.addEventListener('click', async () => {{
            if(confirm('이 댓글을 삭제할까요?')){{
              try {{
                await deleteDoc(doc(db, 'comments', photoId, 'items', docSnap.id));
              }} catch(err) {{
                alert('삭제 실패: ' + err.message);
              }}
            }}
          }});
          rightWrap.appendChild(delEl);
        }}
        meta.appendChild(rightWrap);

        const textEl = document.createElement('div');
        textEl.className = 'c-text';
        textEl.textContent = d.text;

        item.appendChild(meta);
        item.appendChild(textEl);
        list.appendChild(item);
      }});
      list.scrollTop = list.scrollHeight;
    }}, (err) => {{
      list.innerHTML = '<div class="c-empty">댓글을 불러오지 못했어요.</div>';
      console.error(err);
    }});
  }}

  document.getElementById('comment-form').addEventListener('submit', async (e) => {{
    e.preventDefault();
    const form = e.target;
    const photoId = form.dataset.photoId;
    const nameInput = form.querySelector('.c-input-name');
    const textInput = form.querySelector('.c-input-text');
    const name = nameInput.value.trim();
    const text = textInput.value.trim();
    if(!name || !text || !photoId) return;
    const btn = form.querySelector('button');
    btn.disabled = true;
    try {{
      await addDoc(collection(db, 'comments', photoId, 'items'), {{
        name: name.slice(0, 20), text: text.slice(0, 300), createdAt: serverTimestamp()
      }});
      localStorage.setItem('diary-commenter-name', name);
      textInput.value = '';
    }} catch(err) {{
      console.error(err);
      alert('댓글 등록에 실패했어요. 잠시 후 다시 시도해주세요.');
    }} finally {{
      btn.disabled = false;
    }}
  }});

  /* ---------- Admin login ---------- */
  document.getElementById('admin-link').addEventListener('click', async () => {{
    if(currentUser){{
      await signOut(auth);
      return;
    }}
    const email = prompt('관리자 이메일');
    if(!email) return;
    const pw = prompt('비밀번호');
    if(!pw) return;
    try {{
      await signInWithEmailAndPassword(auth, email, pw);
    }} catch(err) {{
      alert('로그인 실패: ' + err.message);
    }}
  }});

  onAuthStateChanged(auth, (user) => {{
    currentUser = user;
    document.getElementById('admin-link').textContent = user ? '🔓 관리자 로그아웃' : '🔧 관리자 로그인';
    const openPhotoId = document.getElementById('comment-form').dataset.photoId;
    if(openPhotoId && document.getElementById('lightbox').classList.contains('open')){{
      loadComments(openPhotoId);
    }}
  }});

  /* ---------- Calendar render ---------- */
  function pad(n){{ return String(n).padStart(2,'0'); }}

  const dateKeys = Object.keys(CALENDAR_DATA).sort();
  const initDate = dateKeys.length ? new Date(dateKeys[dateKeys.length-1] + 'T00:00:00') : new Date();
  let calYear = initDate.getFullYear();
  let calMonth = initDate.getMonth();

  function renderCalendar(){{
    document.getElementById('cal-title').textContent = `${{calYear}}.${{pad(calMonth+1)}}`;
    const grid = document.getElementById('calendar-grid');
    grid.innerHTML = '';
    const firstDay = new Date(calYear, calMonth, 1).getDay();
    const daysInMonth = new Date(calYear, calMonth+1, 0).getDate();
    const todayStr = new Date().toISOString().slice(0,10);

    for(let i=0;i<firstDay;i++){{
      const empty = document.createElement('div');
      empty.className = 'cal-cell empty';
      grid.appendChild(empty);
    }}

    for(let d=1; d<=daysInMonth; d++){{
      const dateStr = `${{calYear}}-${{pad(calMonth+1)}}-${{pad(d)}}`;
      const cell = document.createElement('div');
      cell.className = 'cal-cell';
      if(dateStr === DUE_DATE) cell.classList.add('due');
      if(dateStr === todayStr) cell.classList.add('today');

      const num = document.createElement('div');
      num.className = 'cal-num';
      num.textContent = d;
      cell.appendChild(num);

      const items = CALENDAR_DATA[dateStr];
      if(items && items.length){{
        cell.classList.add('has-photo');
        const thumb = document.createElement('div');
        thumb.className = 'cal-thumb';
        const first = items[0];
        if(first.type === 'photo'){{
          const img = document.createElement('img');
          img.src = first.file;
          img.loading = 'lazy';
          thumb.appendChild(img);
        }} else {{
          thumb.textContent = '🎬';
        }}
        if(items.length > 1){{
          const badge = document.createElement('span');
          badge.className = 'cal-badge';
          badge.textContent = '+' + (items.length - 1);
          thumb.appendChild(badge);
        }}
        cell.appendChild(thumb);
        cell.addEventListener('click', () => openDayPanel(dateStr));
      }}
      grid.appendChild(cell);
    }}
  }}

  window.calShift = function(delta){{
    calMonth += delta;
    if(calMonth < 0){{ calMonth = 11; calYear -= 1; }}
    if(calMonth > 11){{ calMonth = 0; calYear += 1; }}
    renderCalendar();
  }};

  function openDayPanel(dateStr){{
    const items = CALENDAR_DATA[dateStr] || [];
    document.getElementById('day-panel-title').textContent = dateStr;
    const grid = document.getElementById('day-panel-grid');
    grid.innerHTML = '';
    let photoIdx = 0;
    items.forEach(it => {{
      const cell = document.createElement('div');
      cell.className = 'day-panel-item';
      if(it.type === 'photo'){{
        const thisIdx = photoIdx;
        photoIdx += 1;
        const img = document.createElement('img');
        img.src = it.file;
        img.loading = 'lazy';
        cell.appendChild(img);
        cell.addEventListener('click', () => {{ closeDayPanel(); openPhotoLightbox(dateStr, thisIdx); }});
      }} else {{
        cell.classList.add('is-video');
        cell.textContent = '🎬 영상';
        cell.addEventListener('click', () => {{ closeDayPanel(); openVideoLightbox(it.file); }});
      }}
      grid.appendChild(cell);
    }});
    document.getElementById('day-panel').classList.add('open');
  }}

  window.closeDayPanel = function(e){{
    if(!e || e.target.id === 'day-panel' || e.target.classList.contains('day-panel-close')){{
      document.getElementById('day-panel').classList.remove('open');
    }}
  }};

  renderCalendar();
</script>
</body>
</html>
'''
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)


def main():
    manifest = json.load(open(MANIFEST_PATH)) if os.path.exists(MANIFEST_PATH) else []
    known = {m["source_file"] for m in manifest}

    sid = login()
    remote_files = list_files(sid, SHARE_PATH)

    os.makedirs(TMP_DIR, exist_ok=True)
    new_count = 0

    for rf in remote_files:
        name = rf["name"]
        if name in known:
            continue
        ext = name.lower().rsplit(".", 1)[-1]
        if ext not in ("jpg", "jpeg", "mp4", "mov"):
            continue

        local_tmp = os.path.join(TMP_DIR, name)
        download_file(sid, rf["path"], local_tmp)

        if ext in ("jpg", "jpeg"):
            process_photo(name, local_tmp, manifest)
        else:
            process_video(name, local_tmp, manifest)
        new_count += 1

    if new_count > 0:
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    # Always rebuild the site from the current manifest, even if no new
    # files were found on the NAS. This keeps index.html in sync whenever
    # the site template itself changes (e.g. new features), not just when
    # photos are added.
    build_site(manifest)

    if new_count == 0:
        print("No new files found on NAS. Site regenerated from existing manifest.")
    else:
        print(f"Processed {new_count} new file(s).")


if __name__ == "__main__":
    main()
