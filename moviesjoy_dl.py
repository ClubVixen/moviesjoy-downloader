#!/usr/bin/env python3
"""
moviesjoy_dl.py — core library + CLI for downloading movies from moviesjoy.mom
(VidUp server) without torrents.

Replays the site's player chain: ythd.org embed -> vid-src player -> encrypted
stream_urls (per-window ChaCha20 WASM) -> per-IP playlist token -> HLS segments.

Library use (from the GUI):
    import moviesjoy_dl as core
    prep  = core.prepare_movie(session, "https://moviesjoy.mom/moviesjoy/<slug>")
    result = core.download_movie(session, prep, quality="best", out="X.mp4",
                                 log=print, progress=lambda d,t,b: ..., cancel=evt)

CLI:
    python moviesjoy_dl.py "https://moviesjoy.mom/moviesjoy/zootopia-2-2"
    python moviesjoy_dl.py <url> --quality list     # show available qualities
    python moviesjoy_dl.py <url> --quality 720 --limit 10   # quick test

Requires: python 3.9+, `pip install requests wasmtime`, ffmpeg on PATH.
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, quote

import requests
from wasmtime import Engine, Instance, Module, Store

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def log_default(msg):
    print(msg, flush=True)


def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


class Canceled(Exception):
    pass


# ---------------------------------------------------------------- search

def search_movies(session, query, limit=12):
    """Search moviesjoy (server-rendered /filter page). Returns [{title, url}]."""
    r = session.get("https://moviesjoy.mom/filter?q=" + quote(query),
                    headers={"Referer": "https://moviesjoy.mom/"}, timeout=30)
    r.raise_for_status()
    items = re.findall(
        r'href="(https://moviesjoy\.mom/moviesjoy/[^"]+)"[^>]*>'
        r'((?:(?!/moviesjoy/)[\s\S]){0,900}?alt="([^"]*)")', r.text)
    seen, out = set(), []
    for url, _, title in items:
        if url in seen:
            continue
        seen.add(url)
        out.append({"title": title.strip(), "url": url})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- resolution

def resolve_target(session, target):
    """Accepts a moviesjoy page URL or a raw TMDB id. Returns (tmdb_id, page_url)."""
    if str(target).isdigit():
        return int(target), None
    page = session.get(target, headers={"Referer": "https://moviesjoy.mom/"}, timeout=30)
    page.raise_for_status()
    m = re.search(r"vidup\.php\?id=(\d+)", page.text)
    if not m:
        m = re.search(r"vid(?:-?src|up|link|nest)\.php\?id=(\d+)", page.text)
    if not m:
        raise RuntimeError("Could not find a server id on that page.")
    return int(m.group(1)), target


# ---------------------------------------------------------------- token chain

def get_vs_embed_url(session, tmdb_id):
    r = session.get(f"https://ythd.org/embed/movie/{tmdb_id}",
                    headers={"Referer": "https://vidup.to/"}, timeout=30)
    r.raise_for_status()
    m = re.search(r'data-api="([^"]+)"', r.text)
    if not m:
        raise RuntimeError("ythd embed page did not expose data-api.")
    api = m.group(1).replace("&amp;", "&")
    r2 = session.get(urljoin("https://ythd.org/", api),
                     headers={"Referer": f"https://ythd.org/embed/movie/{tmdb_id}"},
                     timeout=30)
    r2.raise_for_status()
    src = r2.json().get("src")
    if not src:
        raise RuntimeError("vs_src.php returned no src.")
    return src


def get_player_config(session, embed_url):
    emb = session.get(embed_url, headers={"Referer": "https://ythd.org/"}, timeout=30)
    emb.raise_for_status()
    m = re.search(r"window\.CFG = (\{.*?\});", emb.text, re.S)
    if not m:
        raise RuntimeError("Embed page: window.CFG not found.")
    cfg = json.loads(m.group(1))
    player_url = urljoin(embed_url, cfg["playerUrl"])
    pl = session.get(player_url, headers={"Referer": embed_url}, timeout=30)
    pl.raise_for_status()
    m = re.search(r"window\.CONFIG = (\{.*?\});", pl.text, re.S)
    if not m:
        raise RuntimeError("Player page: window.CONFIG not found.")
    return json.loads(m.group(1)), player_url, cfg


def decrypt_stream_urls(wasm_bytes, enc_b64):
    engine = Engine()
    store = Store(engine)
    instance = Instance(store, Module(engine, wasm_bytes), [])
    ex = instance.exports(store)
    alloc, decrypt, mem = ex["alloc"], ex["decrypt"], ex["memory"]
    enc = base64.b64decode(enc_b64)
    ptr = alloc(store, len(enc))
    mem.write(store, enc, ptr)
    out_len = decrypt(store, ptr, len(enc))
    raw = bytes(mem.read(store, ptr + 12, ptr + 12 + out_len)).decode("utf-8", "replace")
    return [u for u in raw.split("\n") if u.strip()]


def mint_token(session, pool_host, api_token, referer):
    r = session.get(f"https://{pool_host}/generate.php?api_token={quote(api_token)}",
                    headers={"Referer": referer}, timeout=30)
    tok = r.text.strip()
    if tok.startswith("{"):
        j = json.loads(tok)
        tok = j.get("token") or j.get("data") or j.get("string") or j.get("result") or ""
    if not tok:
        raise RuntimeError("generate.php returned no token.")
    return tok


def parse_variants(master_body):
    out, lines = [], master_body.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            url = next((l for l in lines[i + 1:] if l.strip() and not l.startswith("#")), "")
            bw = re.search(r"BANDWIDTH=(\d+)", line)
            res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            out.append({
                "bandwidth": int(bw.group(1)) if bw else 0,
                "width": int(res.group(1)) if res else 0,
                "height": int(res.group(2)) if res else 0,
                "url": url.strip(),
            })
    return out


def stamp(url, token):
    return url + ("&" if "?" in url else "?") + "token=" + quote(token)


# ---------------------------------------------------------------- high level

def prepare_movie(session, target, source=0, log=log_default):
    """Run the full token chain and enumerate qualities. Returns a prep dict."""
    tmdb_id, page_url = resolve_target(session, target)
    log(f"Resolved TMDB id {tmdb_id}")
    embed_url = get_vs_embed_url(session, tmdb_id)
    config, player_url, cfg = get_player_config(session, embed_url)
    meta_api = cfg.get("metaApi") or f"https://data.vidsrc.sh/api.php?type=movie&tmdb={tmdb_id}"
    meta = session.get(meta_api, headers={"Referer": player_url}, timeout=30).json()
    data = meta.get("data") or {}
    title = data.get("title") or f"movie-{tmdb_id}"
    pool_host = data.get("stream_urls_api_host")

    api_url = config["api"]
    if "api_token=" not in api_url:
        api_url += ("&" if "?" in api_url else "?") + "api_token=" + quote(config["apiToken"])
    if pool_host:
        api_url = urlparse(api_url)._replace(netloc=pool_host).geturl()

    env = session.get(api_url, headers={"Referer": player_url}, timeout=30).json()
    enc = env.get("data", {}).get("stream_urls")
    vs = env.get("vs") or {}
    if not isinstance(enc, str) or not vs:
        raise RuntimeError("Stream envelope did not contain encrypted stream_urls.")
    wasm = (requests.get(vs["wasm_url"], timeout=30).content if vs.get("wasm_url")
            else base64.b64decode(vs["wasm"]))
    playlists = decrypt_stream_urls(wasm, enc)
    if not playlists:
        raise RuntimeError("No playlists after decryption.")

    token = mint_token(session, pool_host, config["apiToken"], player_url)
    src_idx = min(source, len(playlists) - 1)
    master_body = session.get(stamp(playlists[src_idx], token),
                              headers={"Referer": player_url}, timeout=30).text
    variants = parse_variants(master_body)

    return {
        "tmdb_id": tmdb_id, "page_url": page_url, "title": title,
        "pool_host": pool_host, "config": config, "player_url": player_url,
        "playlists": playlists, "source": src_idx, "token": token,
        "variants": variants,
    }


def choose_variant(variants, quality):
    if not variants:
        raise RuntimeError("Master playlist had no variants.")
    if quality == "best":
        return max(variants, key=lambda v: v["bandwidth"])
    want = int(re.sub(r"\D", "", str(quality)) or 0)
    return min(variants, key=lambda v: abs(v["height"] - want))


def unique_path(path):
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(path):
        path = f"{base} ({n}){ext}"
        n += 1
    return path


def safe_name(title):
    return re.sub(r'[<>:"/\\|?*]+', " ", title).strip() or "movie"


def fetch_segment(session, url, referer, dest, token_holder, tries=4, cancel=None):
    for attempt in range(tries):
        if cancel is not None and cancel.is_set():
            raise Canceled()
        try:
            r = session.get(stamp(url, token_holder[0]), headers={"Referer": referer}, timeout=60)
            if r.status_code == 200 and r.content:
                with open(dest, "wb") as f:
                    f.write(r.content)
                return len(r.content)
            if r.status_code == 403:
                token_holder[0] = mint_token(session, token_holder[2], token_holder[1],
                                             token_holder[3])
            time.sleep(1.0 * (attempt + 1))
        except requests.RequestException:
            if attempt == tries - 1:
                raise
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"segment kept failing: {url}")


def download_movie(session, prep, quality="best", out=None, workers=8, limit=0,
                   keep=False, log=log_default, progress=None, cancel=None):
    """Download one prepared movie. Returns {'status': done|canceled|error, 'path'|'error'}."""
    token_box = [prep["token"], prep["config"]["apiToken"], prep["pool_host"],
                 prep["player_url"]]
    try:
        pick = choose_variant(prep["variants"], quality)
        log(f"Quality: {pick['width']}x{pick['height']} @ {pick['bandwidth'] // 1000} kbps")

        v_base = urljoin(prep["playlists"][prep["source"]], pick["url"])
        v_body = session.get(stamp(v_base, token_box[0]),
                             headers={"Referer": prep["player_url"]}, timeout=30).text
        segs = [urljoin(v_base, l.strip()) for l in v_body.splitlines()
                if l.strip() and not l.startswith("#")]
        if limit:
            segs = segs[:limit]
        log(f"{len(segs)} segments -> {workers} workers")

        out_path = out or os.path.join(os.path.expanduser("~"), "Videos",
                                       safe_name(prep["title"]) + ".mp4")
        if os.path.isdir(out_path) or out_path.lower().endswith((".mp4", ".mkv")) is False:
            os.makedirs(out_path, exist_ok=True)
            out_path = os.path.join(out_path, safe_name(prep["title"]) + ".mp4")
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        if os.path.exists(out_path):
            out_path = unique_path(out_path)

        tmp_dir = tempfile.mkdtemp(prefix="mjdl_")
        total_bytes, done, t0 = 0, 0, time.time()

        def report():
            if progress:
                progress(done, len(segs), total_bytes)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {}
            for i, seg in enumerate(segs):
                if cancel is not None and cancel.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    return {"status": "canceled"}
                dest = os.path.join(tmp_dir, f"seg_{i:05d}.ts")
                futs[pool.submit(fetch_segment, session, seg, prep["player_url"],
                                 dest, token_box, cancel=cancel)] = (i, dest)
            for fut in as_completed(futs):
                i, dest = futs[fut]
                try:
                    total_bytes += fut.result()
                except Canceled:
                    pool.shutdown(wait=False, cancel_futures=True)
                    return {"status": "canceled"}
                except RuntimeError:
                    total_bytes += fetch_segment(session, segs[i], prep["player_url"],
                                                 dest, token_box, cancel=cancel)
                done += 1
                if done % 20 == 0 or done == len(segs):
                    rate = done / max(time.time() - t0, 1)
                    eta = (len(segs) - done) / max(rate, 0.1)
                    log(f"  {done}/{len(segs)} | {total_bytes / 1e6:.0f} MB | eta {eta:.0f}s")
                    report()

        ts_path = os.path.join(tmp_dir, "all.ts")
        with open(ts_path, "wb") as o:
            for i in range(len(segs)):
                with open(os.path.join(tmp_dir, f"seg_{i:05d}.ts"), "rb") as f:
                    shutil.copyfileobj(f, o, 1 << 20)

        log("Muxing to MP4 ...")
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", ts_path,
                            "-c", "copy", "-movflags", "+faststart", out_path])
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed; raw .ts kept at {ts_path}")
        if not keep:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        log(f"DONE -> {out_path} ({os.path.getsize(out_path) / 1e6:.0f} MB)")
        return {"status": "done", "path": out_path}
    except Canceled:
        return {"status": "canceled"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="Download a movie from moviesjoy.mom (VidUp server).")
    ap.add_argument("target", help="moviesjoy movie URL or TMDB id")
    ap.add_argument("--quality", default="best",
                    help="best | list | <height> e.g. 1080 (default: best)")
    ap.add_argument("--source", type=int, default=0, help="playlist index when several exist")
    ap.add_argument("--out", default=None, help="output mp4 path or directory")
    ap.add_argument("--workers", type=int, default=8, help="parallel segment downloads")
    ap.add_argument("--limit", type=int, default=0, help="only download first N segments (test)")
    ap.add_argument("--keep", action="store_true", help="keep the concatenated .ts")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found on PATH.")
    session = make_session()
    prep = prepare_movie(session, args.target, source=args.source)
    print(f"Title: {prep['title']} | playlists: {len(prep['playlists'])}")
    if args.quality == "list":
        for i, v in enumerate(prep["variants"]):
            print(f"  [{i}] {v['width']}x{v['height']} @ {v['bandwidth'] // 1000} kbps")
        return
    res = download_movie(session, prep, quality=args.quality, out=args.out,
                         workers=args.workers, limit=args.limit, keep=args.keep)
    if res["status"] != "done":
        sys.exit(1 if res["status"] == "error" else 0)


if __name__ == "__main__":
    main()
