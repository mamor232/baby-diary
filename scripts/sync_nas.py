"""
Synology NAS -> baby-diary site sync script.

Runs inside GitHub Actions (scheduled every 12h). Logs into DSM via the
Web API using a dedicated read-only account, lists files under the
"photo" shared folder, downloads anything not seen before (tracked in
manifest.json), processes it (EXIF date / pregnancy week, image resize,
video transcode), regenerates the timeline GIF and index.html, then lets
the workflow commit + push the result.

Required environment variables (set as GitHub Secrets):
  SYNOLOGY_HOST  e.g. https://shimbbo.tw3.quickconnect.to  (no trailing slash)
  SYNOLOGY_USER  e.g. github-bot
  SYNOLOGY_PASS  the account's password
"""
import os
import json
import glob
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


def build_gif(manifest):
    photos = [m for m in manifest if m["type"] == "photo"]
    photos.sort(key=lambda x: x["datetime"])
    if not photos:
        return None
    frames = []
    for p in photos:
        img = Image.open(p["file"]).convert("RGB")
        w, h = img.size
        target_w = 480
        ratio = target_w / w
        img = img.resize((target_w, int(h * ratio)), Image.LANCZOS)
        frames.append(img)
    durations = [2200] * len(frames)
    durations[-1] = 3200
    first = photos[0]["week"], photos[0]["day"]
    last = photos[-1]["week"], photos[-1]["day"]
    gif_name = f"timeline_{first[0]}w_{last[0]}w.gif"
    for old in glob.glob("timeline_*w_*w.gif"):
        if old != gif_name:
            os.remove(old)
    frames[0].save(gif_name, save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True)
    return gif_name, f"{first[0]}주 {first[1]}일 ~ {last[0]}주 {last[1]}일, 지금까지의 여정"


def build_site(manifest, gif_info):
    groups = defaultdict(list)
    for m in manifest:
        groups[(m["week"], m["day"])].append(m)
    group_keys = sorted(groups.keys())

    pastels = ['#e3f5f0', '#ffe4dc', '#fff3d6', '#e6ecff', '#ffe0ef', '#e4f7e0']

    def week_label(w, d):
        return f"임신 {w}주 {d}일"

    nav_html, sections_html = [], []
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
        for e in entries_photo:
            cards.append(f'''
        <div class="card">
          <img src="{e['file']}" alt="{date_str}" loading="lazy" onclick="openLightbox('{e['file']}')">
          <div class="card-overlay"><div class="date">{date_str}</div><div class="label">초음파 사진</div></div>
        </div>''')
        for e in entries_video:
            mins = int(e["duration_sec"] // 60)
            secs = int(e["duration_sec"] % 60)
            cards.append(f'''
        <div class="card wide">
          <video controls preload="metadata" playsinline>
            <source src="{e['file']}" type="video/mp4">
          </video>
          <div class="card-overlay"><div class="date">{date_str}</div><div class="label">영상 · {mins}:{secs:02d}</div></div>
        </div>''')

        sections_html.append(f'''
    <section class="week-block" id="{sec_id}">
      <div class="week-header">
        <div class="week-icon" style="background:{pastel}">🤰</div>
        <h2 class="round-font">{week_label(w, d)}</h2>
        <span class="date-tag">{date_str}</span>
      </div>
      <div class="grid">
        {''.join(cards)}
      </div>
    </section>''')

    nav_joined = ''.join(nav_html)
    sections_joined = ''.join(sections_html)
    gif_name, gif_caption = gif_info if gif_info else ("", "")

    html = f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>우리 아기 이야기 · 태담 다이어리</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Baloo+2:wght@500;600;700;800&family=Pretendard:wght@400;500;600;700&display=swap');
  :root{{
    --bg:#fffdf9; --mint:#a9ddd0; --mint-soft:#e3f5f0;
    --coral:#ff9d85; --coral-soft:#ffe4dc; --yellow:#ffd873; --yellow-soft:#fff3d6;
    --ink:#4a4038; --ink-soft:#a39a91; --card:#ffffff; --line:#f2ebe3;
  }}
  *{{box-sizing:border-box;}}
  html,body{{margin:0;padding:0;}}
  body{{font-family:'Pretendard','Apple SD Gothic Neo',sans-serif;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased;}}
  .round-font{{font-family:'Baloo 2','Pretendard',sans-serif;}}
  .hero{{padding:56px 24px 40px;text-align:center;background:linear-gradient(180deg, var(--mint-soft) 0%, var(--bg) 78%);border-radius:0 0 40px 40px;position:relative;overflow:hidden;}}
  .hero .deco{{position:absolute;font-size:28px;opacity:.55;animation:float 5s ease-in-out infinite;}}
  .hero .d1{{top:20px;left:8%;}} .hero .d2{{top:60px;right:10%;animation-delay:1.2s;}}
  .hero .d3{{bottom:10px;left:20%;animation-delay:2s;}} .hero .d4{{bottom:30px;right:22%;animation-delay:.6s;}}
  @keyframes float{{0%,100%{{transform:translateY(0) rotate(0deg);}}50%{{transform:translateY(-10px) rotate(6deg);}}}}
  .hero-badge{{display:inline-block;background:var(--card);color:var(--coral);font-weight:700;font-size:12.5px;padding:6px 16px;border-radius:999px;box-shadow:0 4px 12px rgba(255,157,133,.25);margin-bottom:16px;}}
  .hero h1{{font-family:'Baloo 2',sans-serif;font-size:clamp(26px,5vw,40px);font-weight:700;margin:0 0 10px;line-height:1.3;}}
  .hero h1 .hl{{color:var(--coral);}}
  .hero p{{color:var(--ink-soft);font-size:14.5px;max-width:420px;margin:0 auto;line-height:1.6;}}
  .dday{{display:inline-block;margin-top:18px;background:var(--card);border:2px solid var(--coral-soft);padding:8px 20px;border-radius:999px;font-weight:700;color:var(--coral);font-size:14px;}}
  .week-nav{{display:flex;justify-content:center;gap:10px;flex-wrap:wrap;margin-top:26px;}}
  .week-nav a{{text-decoration:none;font-weight:600;font-size:13px;color:var(--ink);background:var(--card);border:2px solid var(--mint-soft);padding:7px 16px;border-radius:999px;transition:all .2s ease;}}
  .week-nav a:hover{{background:var(--mint);border-color:var(--mint);color:#fff;transform:translateY(-2px);}}
  .highlight{{max-width:640px;margin:30px auto 0;text-align:center;}}
  .highlight img{{width:100%;border-radius:24px;box-shadow:0 8px 24px rgba(74,64,56,.12);}}
  .highlight .cap{{margin-top:10px;font-size:12.5px;color:var(--ink-soft);}}
  .note{{max-width:640px;margin:26px auto 0;padding:12px 18px;background:var(--yellow-soft);border-radius:14px;font-size:12px;color:#8a6a2a;text-align:center;}}
  main{{max-width:900px;margin:0 auto;padding:44px 20px 90px;}}
  .week-block{{margin-bottom:56px;scroll-margin-top:30px;}}
  .week-header{{display:flex;align-items:center;gap:12px;margin-bottom:18px;}}
  .week-icon{{width:44px;height:44px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0;}}
  .week-header h2{{font-size:20px;margin:0;}}
  .date-tag{{font-size:12px;font-weight:600;color:#fff;background:var(--coral);padding:4px 12px;border-radius:999px;margin-left:auto;}}
  .grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;}}
  @media (max-width:640px){{.grid{{grid-template-columns:repeat(2,1fr);}}}}
  .card{{position:relative;border-radius:20px;overflow:hidden;box-shadow:0 4px 14px rgba(74,64,56,.08);transition:transform .3s ease;background:#000;}}
  .card:hover{{transform:translateY(-4px) scale(1.02);}}
  .card.wide{{grid-column:span 3;}}
  @media (max-width:640px){{.card.wide{{grid-column:span 2;}}}}
  .card img{{width:100%;height:220px;object-fit:cover;display:block;cursor:zoom-in;}}
  .card.wide video{{width:100%;display:block;max-height:480px;}}
  .card-overlay{{position:absolute;left:0;right:0;bottom:0;padding:10px 12px;background:linear-gradient(to top, rgba(20,15,12,.7), rgba(20,15,12,0));color:#fff;pointer-events:none;}}
  .card.wide .card-overlay{{position:static;background:none;color:var(--ink-soft);padding:8px 2px 0;}}
  .card-overlay .date{{font-size:11px;opacity:.9;}}
  .card-overlay .label{{font-size:13px;font-weight:600;}}
  .lightbox{{position:fixed;inset:0;background:rgba(20,15,12,.88);display:none;align-items:center;justify-content:center;z-index:999;padding:24px;}}
  .lightbox.open{{display:flex;}}
  .lightbox img{{max-width:100%;max-height:88vh;border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.4);}}
  .lightbox-close{{position:absolute;top:20px;right:24px;color:#fff;font-size:32px;font-weight:700;cursor:pointer;line-height:1;background:rgba(255,255,255,.15);width:44px;height:44px;border-radius:50%;display:flex;align-items:center;justify-content:center;}}
  .lightbox-close:hover{{background:rgba(255,255,255,.3);}}
  footer{{text-align:center;padding:30px 24px 50px;color:var(--ink-soft);font-size:12px;}}
  footer strong{{color:var(--ink);}}
</style>
</head>
<body>
<div class="hero">
  <span class="deco d1">⭐</span><span class="deco d2">🎈</span><span class="deco d3">🧸</span><span class="deco d4">☁️</span>
  <div class="hero-badge">🤰 Pregnancy Diary</div>
  <h1>우리 아기를 <span class="hl">기다리는</span> 시간,<br>하루하루 기록해요</h1>
  <p>초음파 사진과 영상을 촬영일 기준 임신 주차별로 자동 정리했어요.</p>
  <div class="dday" id="dday">D-day 계산 중...</div>
  <div class="week-nav">
    {nav_joined}
  </div>
  <div class="highlight">
    <img src="{gif_name}" alt="지금까지의 여정">
    <div class="cap">{gif_caption}</div>
  </div>
  <div class="note">🔒 이 페이지는 공개 링크입니다 · 가족 누구나 링크로 볼 수 있어요</div>
</div>
<main>
  {sections_joined}
</main>
<footer>Pregnancy diary · 출산예정일 <strong>2027-01-05</strong> · 나스에 새 사진이 추가되면 12시간마다 자동으로 갱신됩니다</footer>
<div class="lightbox" id="lightbox" onclick="closeLightbox(event)">
  <div class="lightbox-close" onclick="closeLightbox(event)">&times;</div>
  <img id="lightbox-img" src="" alt="확대 이미지">
</div>
<script>
  const due = new Date("2027-01-05T00:00:00");
  const now = new Date();
  const diff = Math.ceil((due - now) / (1000*60*60*24));
  document.getElementById('dday').textContent = diff > 0 ? `D-${{diff}}` : (diff === 0 ? 'D-Day 🎉' : `출산 후 ${{-diff}}일`);
  function openLightbox(src){{
    document.getElementById('lightbox-img').src = src;
    document.getElementById('lightbox').classList.add('open');
  }}
  function closeLightbox(e){{
    if(e.target.id === 'lightbox' || e.target.classList.contains('lightbox-close')){{
      document.getElementById('lightbox').classList.remove('open');
    }}
  }}
  document.addEventListener('keydown', (e) => {{
    if(e.key === 'Escape') document.getElementById('lightbox').classList.remove('open');
  }});
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

    if new_count == 0:
        print("No new files found on NAS.")
        return

    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    gif_info = build_gif(manifest)
    build_site(manifest, gif_info)
    print(f"Processed {new_count} new file(s).")


if __name__ == "__main__":
    main()
