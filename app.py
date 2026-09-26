import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, redirect, render_template, request, url_for

DB_PATH = os.getenv("DB_PATH", "/data/app.db")
PORT = int(os.getenv("PORT", "5000"))
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL_SECONDS", "21600"))
REQUEST_DELAY = float(os.getenv("LETTERBOXD_REQUEST_DELAY_SECONDS", "2"))
REQUEST_TIMEOUT = int(os.getenv("LETTERBOXD_REQUEST_TIMEOUT_SECONDS", "20"))
MAX_PAGES = int(os.getenv("MAX_PAGES_PER_LIST", "100"))
MAX_MOVIES = int(os.getenv("MAX_MOVIES_PER_LIST", "5000"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "LetterboxdRadarrLists/0.1 (+https://github.com/Furkan-x1/letterboxd-radarr-lists)",
)

app = Flask(__name__)
log = logging.getLogger("letterboxd-radarr-lists")
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})
robots = robotparser.RobotFileParser()
robots_lock = threading.Lock()
scrape_lock = threading.Lock()
refresh_lock = threading.Lock()
active_refreshes = set()
last_request_at = 0.0


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS lists (
                id TEXT PRIMARY KEY,
                letterboxd_url TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                last_error TEXT
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS movies (
                list_id TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL,
                imdb_id TEXT,
                title TEXT NOT NULL,
                year INTEGER,
                PRIMARY KEY (list_id, tmdb_id),
                FOREIGN KEY (list_id) REFERENCES lists(id) ON DELETE CASCADE
            )"""
        )


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize_letterboxd_url(value):
    value = value.strip()
    parsed = urlparse(value)

    if parsed.scheme != "https" or parsed.netloc.lower() != "letterboxd.com":
        raise ValueError("Only https://letterboxd.com/... URLs are supported.")

    path = parsed.path.rstrip("/") + "/"

    if not re.match(r"^/[^/]+/(watchlist|films)/$", path) and "/list/" not in path:
        raise ValueError("Enter a public Letterboxd list, watchlist, or films URL.")

    return urljoin("https://letterboxd.com", path)


def refresh_robots():
    global robots
    response = session.get(
        "https://letterboxd.com/robots.txt",
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    parser = robotparser.RobotFileParser()
    parser.parse(response.text.splitlines())

    with robots_lock:
        robots = parser


def allowed(url):
    with robots_lock:
        return robots.can_fetch(USER_AGENT, url)


def throttle():
    global last_request_at

    with scrape_lock:
        wait = REQUEST_DELAY - (time.monotonic() - last_request_at)
        if wait > 0:
            time.sleep(wait)
        last_request_at = time.monotonic()


def get(url):
    if not allowed(url):
        raise RuntimeError(f"robots.txt disallows {url}")

    throttle()
    response = session.get(url, timeout=REQUEST_TIMEOUT)

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "60")
        try:
            delay = min(int(retry_after), 300)
        except ValueError:
            delay = 60

        log.warning("Letterboxd returned 429; waiting %s seconds.", delay)
        time.sleep(delay)
        throttle()
        response = session.get(url, timeout=REQUEST_TIMEOUT)

    response.raise_for_status()
    return response.text


def extract_film_paths(html):
    soup = BeautifulSoup(html, "html.parser")
    result = []
    seen = set()

    for element in soup.select("[data-item-link], [data-target-link], a[href]"):
        for attribute in ("data-item-link", "data-target-link", "href"):
            value = element.get(attribute, "")
            match = re.match(r"^/film/([^/?#]+)/?$", value)

            if not match:
                continue

            path = f"/film/{match.group(1)}/"

            if path not in seen:
                seen.add(path)
                result.append(path)

            break

    return result


def parse_film(path):
    html = get(urljoin("https://letterboxd.com", path))
    soup = BeautifulSoup(html, "html.parser")

    body = soup.find("body")
    tmdb_id = body.get("data-tmdb-id") if body else None

    if not tmdb_id:
        match = re.search(r'data-tmdb-id=["\'](\d+)["\']', html)
        tmdb_id = match.group(1) if match else None

    if not tmdb_id or not tmdb_id.isdigit():
        raise ValueError(f"No TMDB ID found for {path}")

    title_node = soup.select_one("h1.headline-1, .headline-1")
    title = title_node.get_text(" ", strip=True) if title_node else path

    year = None
    year_node = soup.select_one('a[href*="/films/year/"]')

    if year_node:
        match = re.search(r"\b(19|20)\d{2}\b", year_node.get_text(" ", strip=True))
        if match:
            year = int(match.group(0))

    imdb_id = ""

    for anchor in soup.select('a[href*="imdb.com/title/tt"]'):
        match = re.search(r"imdb\.com/title/(tt\d+)", anchor.get("href", ""))

        if match:
            imdb_id = match.group(1)
            break

    return {
        "id": int(tmdb_id),
        "title": title,
        "release_year": year,
        "imdb_id": imdb_id,
    }


def scrape_list(list_url):
    refresh_robots()
    movies = {}
    base = list_url.rstrip("/")

    for page in range(1, MAX_PAGES + 1):
        page_url = f"{base}/" if page == 1 else f"{base}/page/{page}/"
        html = get(page_url)
        film_paths = extract_film_paths(html)

        if not film_paths:
            break

        before = len(movies)

        for path in film_paths:
            if len(movies) >= MAX_MOVIES or path in movies:
                continue

            try:
                movies[path] = parse_film(path)
            except Exception as exc:
                log.warning("Skipping %s: %s", path, exc)

        if len(movies) == before or len(film_paths) < 20:
            break

    return list(movies.values())


def store_movies(list_id, movies):
    with db() as connection:
        connection.execute("DELETE FROM movies WHERE list_id = ?", (list_id,))
        connection.executemany(
            """INSERT INTO movies(list_id, tmdb_id, imdb_id, title, year)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    list_id,
                    movie["id"],
                    movie["imdb_id"],
                    movie["title"],
                    movie["release_year"],
                )
                for movie in movies
            ],
        )


def refresh_list(list_id):
    with refresh_lock:
        if list_id in active_refreshes:
            log.info("Refresh already running for list %s; skipping.", list_id)
            return

        active_refreshes.add(list_id)

    try:
        with db() as connection:
            row = connection.execute(
                "SELECT * FROM lists WHERE id = ?",
                (list_id,),
            ).fetchone()

        if not row:
            return

        try:
            movies = scrape_list(row["letterboxd_url"])

            if not movies:
                raise RuntimeError("No movies were found in the Letterboxd list.")

            store_movies(list_id, movies)

            with db() as connection:
                connection.execute(
                    "UPDATE lists SET updated_at = ?, last_error = NULL WHERE id = ?",
                    (now(), list_id),
                )

            log.info("Updated %s: %s movies.", row["letterboxd_url"], len(movies))

        except Exception as exc:
            with db() as connection:
                connection.execute(
                    "UPDATE lists SET last_error = ? WHERE id = ?",
                    (str(exc), list_id),
                )
            log.exception("Failed to update %s", row["letterboxd_url"])
    finally:
        with refresh_lock:
            active_refreshes.discard(list_id)


def start_refresh(list_id):
    thread = threading.Thread(
        target=refresh_list,
        args=(list_id,),
        daemon=True,
        name=f"refresh-{list_id}",
    )
    thread.start()


def updater_loop():
    while True:
        try:
            with db() as connection:
                ids = [
                    row["id"]
                    for row in connection.execute("SELECT id FROM lists")
                ]

            for list_id in ids:
                refresh_list(list_id)

        except Exception:
            log.exception("Background update failed")

        time.sleep(UPDATE_INTERVAL)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    with db() as connection:
        lists = connection.execute(
            """SELECT l.*, COUNT(m.tmdb_id) AS movie_count
               FROM lists l
               LEFT JOIN movies m ON m.list_id = l.id
               GROUP BY l.id
               ORDER BY l.created_at DESC"""
        ).fetchall()

    return render_template("index.html", lists=lists)


@app.post("/lists")
def create_list():
    try:
        letterboxd_url = normalize_letterboxd_url(
            request.form["letterboxd_url"]
        )
    except (KeyError, ValueError) as exc:
        return render_template("index.html", error=str(exc)), 400

    with db() as connection:
        existing = connection.execute(
            "SELECT id FROM lists WHERE letterboxd_url = ?",
            (letterboxd_url,),
        ).fetchone()

    if existing:
        return redirect(url_for("index"))

    list_id = uuid.uuid4().hex[:12]
    name = letterboxd_url.rstrip("/").split("/")[-1]

    with db() as connection:
        connection.execute(
            """INSERT INTO lists(id, letterboxd_url, name, created_at)
               VALUES (?, ?, ?, ?)""",
            (list_id, letterboxd_url, name, now()),
        )

    start_refresh(list_id)
    return redirect(url_for("index"))


@app.post("/lists/<list_id>/refresh")
def manual_refresh(list_id):
    with db() as connection:
        exists = connection.execute(
            "SELECT 1 FROM lists WHERE id = ?",
            (list_id,),
        ).fetchone()

    if exists:
        start_refresh(list_id)

    return redirect(url_for("index"))


@app.post("/lists/<list_id>/delete")
def delete_list(list_id):
    with db() as connection:
        connection.execute("DELETE FROM movies WHERE list_id = ?", (list_id,))
        connection.execute("DELETE FROM lists WHERE id = ?", (list_id,))

    return redirect(url_for("index"))


@app.get("/radarr/<list_id>")
def radarr_list(list_id):
    with db() as connection:
        rows = connection.execute(
            """SELECT tmdb_id AS id, imdb_id, title, year AS release_year
               FROM movies
               WHERE list_id = ?
               ORDER BY rowid""",
            (list_id,),
        ).fetchall()

    return Response(
        json.dumps([dict(row) for row in rows], ensure_ascii=False),
        content_type="application/json; charset=utf-8",
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    init_db()

    try:
        refresh_robots()
    except Exception:
        log.exception("Could not load Letterboxd robots.txt during startup.")

    threading.Thread(target=updater_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
