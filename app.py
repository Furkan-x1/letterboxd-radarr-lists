import io
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
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, url_for

DB_PATH = os.getenv("DB_PATH", "/data/app.db")
PORT = int(os.getenv("PORT", "5000"))
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL_SECONDS", "43200"))
REQUEST_DELAY = float(os.getenv("LETTERBOXD_REQUEST_DELAY_SECONDS", "3"))
REQUEST_TIMEOUT = int(os.getenv("LETTERBOXD_REQUEST_TIMEOUT_SECONDS", "20"))
MAX_PAGES = int(os.getenv("MAX_PAGES_PER_LIST", "100"))
MAX_MOVIES = int(os.getenv("MAX_MOVIES_PER_LIST", "5000"))
UPDATER_POLL_SECONDS = int(os.getenv("UPDATER_POLL_SECONDS", "60"))
RETRY_ATTEMPTS = int(os.getenv("LETTERBOXD_RETRY_ATTEMPTS", "3"))
POST_RATE_LIMIT_WINDOW = int(os.getenv("POST_RATE_LIMIT_WINDOW_SECONDS", "60"))
POST_RATE_LIMIT_MAX = int(os.getenv("POST_RATE_LIMIT_MAX", "20"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "LetterboxdRadarrLists/0.1",
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
log = logging.getLogger("letterboxd-radarr-lists")
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

robots = robotparser.RobotFileParser()
robots_loaded = False
robots_lock = threading.Lock()
scrape_lock = threading.Lock()
scrape_job_lock = threading.Lock()
refresh_lock = threading.Lock()
active_refreshes = {}
refresh_cancel_requested = False
last_request_at = 0.0
post_rate_limit = {}
post_rate_lock = threading.Lock()


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

        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(movies)")
        }
        if "letterboxd_path" not in columns:
            connection.execute(
                "ALTER TABLE movies ADD COLUMN letterboxd_path TEXT NOT NULL DEFAULT ''"
            )

        list_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(lists)")
        }
        if "enabled" not in list_columns:
            connection.execute(
                "ALTER TABLE lists ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        if "update_interval_seconds" not in list_columns:
            connection.execute(
                f"ALTER TABLE lists ADD COLUMN update_interval_seconds INTEGER NOT NULL DEFAULT {UPDATE_INTERVAL}"
            )

        connection.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS update_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                state TEXT NOT NULL,
                movie_count INTEGER NOT NULL DEFAULT 0,
                added_count INTEGER NOT NULL DEFAULT 0,
                removed_count INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                FOREIGN KEY (list_id) REFERENCES lists(id) ON DELETE CASCADE
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS update_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                history_id INTEGER NOT NULL,
                change_type TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                year INTEGER,
                letterboxd_path TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (history_id) REFERENCES update_history(id) ON DELETE CASCADE
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_update_changes_history_id ON update_changes(history_id)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS unmatched_movies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                history_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                letterboxd_path TEXT NOT NULL,
                reason TEXT NOT NULL,
                FOREIGN KEY (history_id) REFERENCES update_history(id) ON DELETE CASCADE
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_unmatched_movies_history_id ON unmatched_movies(history_id)"
        )


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def next_update_timestamp(updated_at, interval_seconds):
    parsed = parse_iso(updated_at)
    if not parsed:
        return time.time()
    return parsed.timestamp() + interval_seconds


def get_setting(key, default=None):
    with db() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db() as connection:
        connection.execute(
            """INSERT INTO settings(key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, str(value)),
        )


def get_global_pause_until():
    try:
        value = float(get_setting("global_pause_until", "0"))
    except (TypeError, ValueError):
        value = 0.0
    if value <= time.time():
        if value:
            set_setting("global_pause_until", "0")
        return 0.0
    return value


def create_history(list_id):
    with db() as connection:
        cursor = connection.execute(
            "INSERT INTO update_history(list_id, started_at, state) VALUES (?, ?, 'updating')",
            (list_id, now()),
        )
        return cursor.lastrowid


def finish_history(history_id, state, movie_count=0, added_count=0, removed_count=0, message=""):
    with db() as connection:
        connection.execute(
            """UPDATE update_history
               SET completed_at = ?, state = ?, movie_count = ?, added_count = ?,
                   removed_count = ?, message = ?
               WHERE id = ?""",
            (now(), state, movie_count, added_count, removed_count, message, history_id),
        )


def record_changes(history_id, added_movies, removed_movies):
    changes = [
        (
            history_id,
            "added",
            movie["id"],
            movie["title"],
            movie["release_year"],
            movie["letterboxd_path"],
        )
        for movie in added_movies
    ]
    changes.extend(
        (
            history_id,
            "removed",
            movie["id"],
            movie["title"],
            movie["release_year"],
            movie["letterboxd_path"],
        )
        for movie in removed_movies
    )

    if not changes:
        return

    with db() as connection:
        connection.executemany(
            """INSERT INTO update_changes(
                history_id, change_type, tmdb_id, title, year, letterboxd_path
            )
            VALUES (?, ?, ?, ?, ?, ?)""",
            changes,
        )


def record_unmatched(history_id, unmatched_movies):
    if not unmatched_movies:
        return

    with db() as connection:
        connection.executemany(
            """INSERT INTO unmatched_movies(
                history_id, title, letterboxd_path, reason
            )
            VALUES (?, ?, ?, ?)""",
            [
                (
                    history_id,
                    movie["title"],
                    movie["letterboxd_path"],
                    movie["reason"],
                )
                for movie in unmatched_movies
            ],
        )


def normalize_letterboxd_url(value):
    value = value.strip()

    if not value:
        raise ValueError("Enter a Letterboxd list URL or path.")

    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "https://letterboxd.com/" + value.lstrip("/")

    parsed = urlparse(value)

    if parsed.scheme != "https" or parsed.netloc.lower() not in {
        "letterboxd.com",
        "www.letterboxd.com",
    }:
        raise ValueError("Only Letterboxd URLs are supported.")

    path = parsed.path.rstrip("/") + "/"

    if not re.match(r"^/[^/]+/(watchlist|films)/$", path) and not re.match(
        r"^/[^/]+/list/[^/]+/$", path
    ):
        raise ValueError(
            "Enter a public Letterboxd path such as username/watchlist or username/list/example."
        )

    return urljoin("https://letterboxd.com", path)


def is_watchlist_url(value):
    path = urlparse(value).path.rstrip("/")
    return bool(re.match(r"^/[^/]+/watchlist$", path))


def refresh_robots():
    global robots, robots_loaded

    log.info("Fetching Letterboxd robots.txt.")
    response = session.get(
        "https://letterboxd.com/robots.txt",
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    parser = robotparser.RobotFileParser()
    parser.parse(response.text.splitlines())

    with robots_lock:
        robots = parser
        robots_loaded = True

    log.info("Letterboxd robots.txt loaded.")


def allowed(url):
    with robots_lock:
        if not robots_loaded:
            return False
        return robots.can_fetch(USER_AGENT, url)


def throttle():
    global last_request_at

    with scrape_lock:
        wait = REQUEST_DELAY - (time.monotonic() - last_request_at)
        if wait > 0:
            time.sleep(wait)
        last_request_at = time.monotonic()


class RefreshCancelled(Exception):
    pass


def check_refresh_cancelled():
    if refresh_cancel_requested:
        raise RefreshCancelled("Refresh cancelled by user.")


def get(url):
    check_refresh_cancelled()
    if not allowed(url):
        raise RuntimeError("Letterboxd robots.txt does not allow this request.")

    last_error = None
    for attempt in range(RETRY_ATTEMPTS):
        check_refresh_cancelled()
        throttle()
        response = None
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            check_refresh_cancelled()
            if response.status_code == 429:
                last_error = RuntimeError(
                    "Letterboxd rate-limited the request (429)."
                )
            elif response.status_code == 403:
                last_error = RuntimeError(
                    "Letterboxd denied the request (403)."
                )
            elif response.status_code == 404:
                last_error = RuntimeError(
                    "Letterboxd page was not found (404). The list may be private or deleted."
                )
            elif response.status_code >= 500:
                last_error = RuntimeError(
                    f"Letterboxd server error ({response.status_code})."
                )
            else:
                response.raise_for_status()
                return response.text
        except requests.Timeout:
            last_error = RuntimeError(
                f"Letterboxd request timed out after {REQUEST_TIMEOUT}s."
            )
        except requests.ConnectionError as exc:
            last_error = RuntimeError(
                f"Could not connect to Letterboxd: {exc}"
            )
        except requests.RequestException as exc:
            last_error = RuntimeError(
                f"Letterboxd request failed: {exc}"
            )

        if attempt + 1 < RETRY_ATTEMPTS:
            retry_after = response.headers.get("Retry-After") if response is not None else None
            try:
                delay = min(max(1, int(retry_after)), 300)
            except (TypeError, ValueError):
                delay = min(60, 2 ** attempt)

            log.warning(
                "Request failed for %s; retrying in %ss (%s/%s).",
                url,
                delay,
                attempt + 1,
                RETRY_ATTEMPTS - 1,
            )

            end_time = time.monotonic() + delay
            while time.monotonic() < end_time:
                check_refresh_cancelled()
                time.sleep(min(0.5, end_time - time.monotonic()))

    raise last_error or RuntimeError(f"Request failed for {url}")


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


def extract_list_name(html, fallback):
    soup = BeautifulSoup(html, "html.parser")

    heading = soup.select_one("h1")
    if heading:
        value = heading.get_text(" ", strip=True)
        if value:
            return value

    title = soup.select_one("title")
    if title:
        value = re.sub(
            r"\s*[•|]\s*Letterboxd.*$",
            "",
            title.get_text(" ", strip=True),
        )
        if value:
            return value

    return fallback


def extract_tmdb_id(html):
    soup = BeautifulSoup(html, "html.parser")

    patterns = (
        r'tmdb[_-]?id["\']?\s*[:=]\s*["\']?(\d+)',
        r'themoviedb\.org/movie/(\d+)',
        r'tmdb\.org/movie/(\d+)',
    )

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1)

    for anchor in soup.select(
        'a[href*="themoviedb.org/movie/"], a[href*="tmdb.org/movie/"]'
    ):
        match = re.search(
            r"(?:themoviedb|tmdb)\.org/movie/(\d+)",
            anchor.get("href", ""),
        )
        if match:
            return match.group(1)

    return None


def parse_film(path):
    html = get(urljoin("https://letterboxd.com", path))
    soup = BeautifulSoup(html, "html.parser")

    tmdb_id = extract_tmdb_id(html)

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

    if year is None:
        for script in soup.select('script[type="application/ld+json"]'):
            match = re.search(
                r'"dateCreated"\s*:\s*"((?:19|20)\d{2})',
                script.get_text(),
            )
            if match:
                year = int(match.group(1))
                break

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
        "letterboxd_path": path,
    }


def set_refresh_status(list_id, **values):
    with refresh_lock:
        status = active_refreshes.get(list_id)
        if status is not None:
            status.update(values)


def load_cached_movies(list_id):
    with db() as connection:
        rows = connection.execute(
            """SELECT tmdb_id, imdb_id, title, year, letterboxd_path
               FROM movies
               WHERE list_id = ? AND letterboxd_path != ''""",
            (list_id,),
        ).fetchall()

    return {
        row["letterboxd_path"]: {
            "id": row["tmdb_id"],
            "imdb_id": row["imdb_id"] or "",
            "title": row["title"],
            "release_year": row["year"],
            "letterboxd_path": row["letterboxd_path"],
        }
        for row in rows
    }


def scrape_list(list_id, list_url):
    with scrape_job_lock:
        set_refresh_status(
            list_id,
            state="updating",
            message="Fetching Letterboxd list...",
            current=0,
            total=0,
        )

        refresh_robots()
        movies = {}
        cached_movies = load_cached_movies(list_id)
        base = list_url.rstrip("/")
        list_name = None
        saw_film_paths = False
        unmatched_movies = []

        for page in range(1, MAX_PAGES + 1):
            page_url = f"{base}/" if page == 1 else f"{base}/page/{page}/"
            set_refresh_status(
                list_id,
                message=f"Fetching list page {page}...",
            )

            html = get(page_url)

            if page == 1:
                list_name = extract_list_name(
                    html,
                    base.rsplit("/", 1)[-1],
                )

            film_paths = extract_film_paths(html)
            log.info("Found %s film paths on %s.", len(film_paths), page_url)

            if film_paths:
                saw_film_paths = True
            else:
                break

            set_refresh_status(
                list_id,
                message=f"Found {len(film_paths)} films on page {page}.",
                total=len(film_paths),
                current=0,
            )

            before = len(movies)

            for index, path in enumerate(film_paths, start=1):
                check_refresh_cancelled()

                if len(movies) >= MAX_MOVIES or path in movies:
                    continue

                cached = cached_movies.get(path)
                if cached:
                    movies[path] = cached
                    set_refresh_status(
                        list_id,
                        message=f"Page {page} · cached film {index}/{len(film_paths)}",
                        current=index,
                        total=len(film_paths),
                        page=page,
                    )
                    continue

                set_refresh_status(
                    list_id,
                    message=f"Page {page} · fetching film {index}/{len(film_paths)}",
                    current=index,
                    total=len(film_paths),
                    page=page,
                )

                try:
                    movies[path] = parse_film(path)
                except Exception as exc:
                    unmatched_movies.append({
                        "title": path,
                        "letterboxd_path": path,
                        "reason": str(exc),
                    })
                    log.warning("Could not match %s: %s", path, exc)

            if len(movies) == before or len(film_paths) < 20:
                break

        log.info(
            "Scraped %s: %s movies found, %s film paths seen.",
            list_url,
            len(movies),
            sum(1 for _ in movies),
        )
        return list(movies.values()), list_name, saw_film_paths, unmatched_movies


def store_movies(list_id, movies, watchlist):
    with db() as connection:
        if watchlist:
            connection.execute(
                "DELETE FROM movies WHERE list_id = ?",
                (list_id,),
            )

        connection.executemany(
            """INSERT INTO movies(
                list_id, tmdb_id, imdb_id, title, year, letterboxd_path
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(list_id, tmdb_id) DO UPDATE SET
                imdb_id = excluded.imdb_id,
                title = excluded.title,
                year = excluded.year,
                letterboxd_path = excluded.letterboxd_path""",
            [
                (
                    list_id,
                    movie["id"],
                    movie["imdb_id"],
                    movie["title"],
                    movie["release_year"],
                    movie["letterboxd_path"],
                )
                for movie in movies
            ],
        )


def refresh_list(list_id, force=False):
    pause_until = get_global_pause_until()

    with refresh_lock:
        if not force and pause_until > time.time():
            log.info("Global refresh pause is active; skipping list %s.", list_id)
            return
        if list_id in active_refreshes:
            log.info("Refresh already running for list %s; skipping.", list_id)
            return

        active_refreshes[list_id] = {
            "state": "starting",
            "message": "Starting update...",
            "current": 0,
            "total": 0,
            "started_at": now(),
        }

    history_id = create_history(list_id)

    try:
        with db() as connection:
            row = connection.execute(
                "SELECT * FROM lists WHERE id = ?",
                (list_id,),
            ).fetchone()

        if not row:
            return

        try:
            set_refresh_status(
                list_id,
                state="updating",
                message="Starting Letterboxd update...",
            )

            old_movies = load_cached_movies(list_id)
            old_paths = set(old_movies)
            movies, scraped_name, saw_film_paths, unmatched_movies = scrape_list(
                list_id,
                row["letterboxd_url"],
            )

            watchlist = is_watchlist_url(row["letterboxd_url"])
            new_by_path = {
                movie["letterboxd_path"]: movie
                for movie in movies
            }
            new_paths = set(new_by_path)
            added_movies = [
                movie for path, movie in new_by_path.items()
                if path not in old_paths
            ]
            removed_movies = [
                old_movies[path]
                for path in old_paths - new_paths
                if watchlist and path in old_movies
            ]
            added_count = len(added_movies)
            removed_count = len(removed_movies)

            if watchlist and saw_film_paths and not movies:
                raise RuntimeError(
                    "Letterboxd films were found, but none could be processed."
                )

            store_movies(list_id, movies, watchlist)

            with db() as connection:
                connection.execute(
                    """UPDATE lists
                       SET updated_at = ?, last_error = NULL
                       WHERE id = ?""",
                    (now(), list_id),
                )

                if not row["updated_at"] and scraped_name:
                    connection.execute(
                        "UPDATE lists SET name = ? WHERE id = ?",
                        (scraped_name, list_id),
                    )

            message = f"Updated successfully: {len(movies)} films."
            record_changes(history_id, added_movies, removed_movies)
            record_unmatched(history_id, unmatched_movies)
            finish_history(
                history_id,
                "completed",
                len(movies),
                added_count,
                removed_count,
                message,
            )
            set_refresh_status(
                list_id,
                state="completed",
                message=message,
                current=len(movies),
                total=len(movies),
                added=added_count,
                removed=removed_count,
            )

            log.info(
                "Updated %s: %s movies (%s mode).",
                row["letterboxd_url"],
                len(movies),
                "watchlist" if watchlist else "persistent list",
            )

        except RefreshCancelled:
            message = "Update stopped by user."
            finish_history(history_id, "paused", message=message)
            set_refresh_status(
                list_id,
                state="paused",
                message=message,
            )
            log.info("Update stopped for %s.", row["letterboxd_url"])
        except Exception as exc:
            with db() as connection:
                connection.execute(
                    "UPDATE lists SET last_error = ? WHERE id = ?",
                    (str(exc), list_id),
                )

            finish_history(history_id, "error", message=str(exc))
            set_refresh_status(
                list_id,
                state="error",
                message=str(exc),
            )
            log.exception("Failed to update %s", row["letterboxd_url"])

        time.sleep(2)

    finally:
        with refresh_lock:
            active_refreshes.pop(list_id, None)


def start_refresh(list_id, force=False):
    thread = threading.Thread(
        target=refresh_list,
        args=(list_id, force),
        daemon=True,
        name=f"refresh-{list_id}",
    )
    thread.start()


def updater_loop():
    while True:
        try:
            if get_global_pause_until() <= time.time():
                with db() as connection:
                    rows = connection.execute(
                        "SELECT id, updated_at, update_interval_seconds FROM lists WHERE enabled = 1"
                    ).fetchall()

                for row in rows:
                    if next_update_timestamp(
                        row["updated_at"],
                        row["update_interval_seconds"],
                    ) <= time.time():
                        refresh_list(row["id"])

        except Exception:
            log.exception("Background update failed")

        time.sleep(UPDATER_POLL_SECONDS)




def post_allowed():
    ip = request.remote_addr or "unknown"
    cutoff = time.monotonic() - POST_RATE_LIMIT_WINDOW
    with post_rate_lock:
        timestamps = [
            value for value in post_rate_limit.get(ip, [])
            if value > cutoff
        ]
        if len(timestamps) >= POST_RATE_LIMIT_MAX:
            post_rate_limit[ip] = timestamps
            return False
        timestamps.append(time.monotonic())
        post_rate_limit[ip] = timestamps
    return True


@app.before_request
def protect_posts():
    if request.method == "POST" and not post_allowed():
        return jsonify({"error": "Too many requests. Try again later."}), 429


@app.post("/pause-refresh")
def pause_refresh():
    global refresh_cancel_requested
    set_setting("global_pause_until", time.time() + 12 * 60 * 60)
    refresh_cancel_requested = True
    return redirect(url_for("index"))


@app.post("/resume-refresh")
def resume_refresh():
    global refresh_cancel_requested
    set_setting("global_pause_until", 0)
    refresh_cancel_requested = False
    return redirect(url_for("index"))


@app.get("/refresh-status")
def refresh_status():
    with refresh_lock:
        active_count = len(active_refreshes)
    pause_until = get_global_pause_until()

    return jsonify({
        "paused": pause_until > time.time(),
        "pause_until": datetime.fromtimestamp(
            pause_until, timezone.utc
        ).isoformat() if pause_until > time.time() else None,
        "active_updates": active_count,
    })


@app.get("/status")
def status_page():
    with db() as connection:
        list_count = connection.execute("SELECT COUNT(*) FROM lists").fetchone()[0]
        movie_count = connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
        history_count = connection.execute("SELECT COUNT(*) FROM update_history").fetchone()[0]

    with robots_lock:
        robots_ok = robots_loaded

    with refresh_lock:
        active_count = len(active_refreshes)

    return render_template(
        "status.html",
        list_count=list_count,
        movie_count=movie_count,
        history_count=history_count,
        robots_loaded=robots_ok,
        pause_until=get_global_pause_until(),
        active_updates=active_count,
        update_interval=UPDATE_INTERVAL,
        updater_poll=UPDATER_POLL_SECONDS,
        request_delay=REQUEST_DELAY,
        retry_attempts=RETRY_ATTEMPTS,
    )


@app.get("/health")
def health():
    with db() as connection:
        list_count = connection.execute(
            "SELECT COUNT(*) FROM lists"
        ).fetchone()[0]
        movie_count = connection.execute(
            "SELECT COUNT(*) FROM movies"
        ).fetchone()[0]

    with refresh_lock:
        active_count = len(active_refreshes)

    return {
        "status": "ok",
        "lists": list_count,
        "movies": movie_count,
        "active_updates": active_count,
    }


@app.get("/")
def index():
    with db() as connection:
        lists = connection.execute(
            """SELECT l.*, COUNT(m.tmdb_id) AS movie_count,
               (SELECT added_count FROM update_history h
                WHERE h.list_id = l.id ORDER BY h.id DESC LIMIT 1) AS last_added,
               (SELECT removed_count FROM update_history h
                WHERE h.list_id = l.id ORDER BY h.id DESC LIMIT 1) AS last_removed
               FROM lists l
               LEFT JOIN movies m ON m.list_id = l.id
               GROUP BY l.id
               ORDER BY l.created_at DESC"""
        ).fetchall()

    pause_until = get_global_pause_until()

    with refresh_lock:
        active_count = len(active_refreshes)

    due_count = sum(
        1
        for item in lists
        if item["enabled"]
        and next_update_timestamp(
            item["updated_at"],
            item["update_interval_seconds"],
        ) <= time.time()
    )

    return render_template(
        "index.html",
        lists=lists,
        error=request.args.get("error"),
        duplicate_id=request.args.get("duplicate_id"),
        update_interval_ms=UPDATE_INTERVAL * 1000,
        global_pause_until=pause_until,
        active_count=active_count,
        due_count=due_count,
        time_zone=os.getenv("TZ", "UTC"),
    )


@app.get("/lists/<list_id>")
def list_detail(list_id):
    with db() as connection:
        item = connection.execute(
            "SELECT * FROM lists WHERE id = ?",
            (list_id,),
        ).fetchone()

        if not item:
            return redirect(url_for("index"))

        query = request.args.get("q", "").strip()
        selected_year = request.args.get("year", "").strip()

        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1

        try:
            per_page = min(100, max(10, int(request.args.get("per_page", "50"))))
        except ValueError:
            per_page = 50

        where = ["list_id = ?"]
        params = [list_id]

        if query:
            where.append("(title LIKE ? OR imdb_id LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])

        if selected_year.isdigit():
            where.append("year = ?")
            params.append(int(selected_year))

        clause = " AND ".join(where)

        total = connection.execute(
            f"SELECT COUNT(*) FROM movies WHERE {clause}",
            params,
        ).fetchone()[0]

        stored_count = connection.execute(
            "SELECT COUNT(*) FROM movies WHERE list_id = ?",
            (list_id,),
        ).fetchone()[0]

        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)

        movies = connection.execute(
            f"""SELECT tmdb_id, imdb_id, title, year, letterboxd_path
                FROM movies
                WHERE {clause}
                ORDER BY rowid
                LIMIT ? OFFSET ?""",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()

        years = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT year FROM movies WHERE list_id = ? AND year IS NOT NULL ORDER BY year DESC",
                (list_id,),
            )
        ]

        history = connection.execute(
            """SELECT id, started_at, completed_at, state, movie_count,
                      added_count, removed_count, message
               FROM update_history
               WHERE list_id = ?
               ORDER BY id DESC
               LIMIT 20""",
            (list_id,),
        ).fetchall()

        history_ids = [entry["id"] for entry in history]
        unmatched_by_history = {history_id: [] for history_id in history_ids}
        if history_ids:
            placeholders = ",".join("?" for _ in history_ids)
            unmatched = connection.execute(
                f"""SELECT history_id, title, letterboxd_path, reason
                    FROM unmatched_movies
                    WHERE history_id IN ({placeholders})
                    ORDER BY id DESC
                    LIMIT 2000""",
                history_ids,
            ).fetchall()

            for movie in unmatched:
                unmatched_by_history[movie["history_id"]].append(movie)

        changes_by_history = {history_id: [] for history_id in history_ids}
        if history_ids:
            placeholders = ",".join("?" for _ in history_ids)
            changes = connection.execute(
                f"""SELECT history_id, change_type, tmdb_id, title, year, letterboxd_path
                    FROM update_changes
                    WHERE history_id IN ({placeholders})
                    ORDER BY id DESC
                    LIMIT 2000""",
                history_ids,
            ).fetchall()

            for change in changes:
                changes_by_history[change["history_id"]].append(change)

    return render_template(
        "list.html",
        item=item,
        movies=movies,
        history=history,
        changes_by_history=changes_by_history,
        unmatched_by_history=unmatched_by_history,
        query=query,
        selected_year=selected_year,
        years=years,
        page=page,
        pages=pages,
        total=total,
        stored_count=stored_count,
        per_page=per_page,
    )


@app.get("/lists/<list_id>/status")
def list_status(list_id):
    with db() as connection:
        row = connection.execute(
            """SELECT updated_at, last_error, enabled, update_interval_seconds
               FROM lists WHERE id = ?""",
            (list_id,),
        ).fetchone()

        latest = connection.execute(
            """SELECT added_count, removed_count
               FROM update_history
               WHERE list_id = ?
               ORDER BY id DESC LIMIT 1""",
            (list_id,),
        ).fetchone()

    if not row:
        return jsonify({"error": "List not found"}), 404

    with refresh_lock:
        active = active_refreshes.get(list_id)

    if active:
        return jsonify(active)

    if not row["enabled"]:
        return jsonify({
            "state": "paused",
            "message": "Automatic updates paused",
            "updated_at": row["updated_at"],
            "next_update_at": None,
            "current": 0,
            "total": 0,
        })

    next_at = next_update_timestamp(
        row["updated_at"],
        row["update_interval_seconds"],
    )

    if row["last_error"]:
        return jsonify({
            "state": "error",
            "message": row["last_error"],
            "updated_at": row["updated_at"],
            "next_update_at": datetime.fromtimestamp(
                next_at,
                timezone.utc,
            ).isoformat(),
            "current": 0,
            "total": 0,
        })

    return jsonify({
        "state": "idle",
        "message": "Up to date" if row["updated_at"] else "Not updated yet",
        "updated_at": row["updated_at"],
        "next_update_at": datetime.fromtimestamp(
            next_at,
            timezone.utc,
        ).isoformat(),
        "current": 0,
        "total": 0,
        "added": latest["added_count"] if latest else 0,
        "removed": latest["removed_count"] if latest else 0,
    })


@app.post("/lists")
def create_list():
    try:
        letterboxd_url = normalize_letterboxd_url(
            request.form["letterboxd_url"]
        )
    except (KeyError, ValueError) as exc:
        return render_template(
            "index.html",
            error=str(exc),
            lists=[],
            update_interval_ms=UPDATE_INTERVAL * 1000,
            global_pause_until=get_global_pause_until(),
            active_count=0,
            due_count=0,
            time_zone=os.getenv("TZ", "UTC"),
            duplicate_id=None,
        ), 400

    with db() as connection:
        existing = connection.execute(
            "SELECT id FROM lists WHERE letterboxd_url = ?",
            (letterboxd_url,),
        ).fetchone()

    if existing:
        return redirect(
            url_for(
                "index",
                error="This Letterboxd list is already added.",
                duplicate_id=existing["id"],
            )
        )

    list_id = uuid.uuid4().hex[:12]
    name = letterboxd_url.rstrip("/").split("/")[-1]

    with db() as connection:
        connection.execute(
            """INSERT INTO lists(
                id, letterboxd_url, name, created_at, update_interval_seconds
            )
            VALUES (?, ?, ?, ?, ?)""",
            (list_id, letterboxd_url, name, now(), UPDATE_INTERVAL),
        )

    start_refresh(list_id, force=True)
    return redirect(url_for("index"))


@app.post("/lists/<list_id>/refresh")
def manual_refresh(list_id):
    with db() as connection:
        exists = connection.execute(
            "SELECT 1 FROM lists WHERE id = ?",
            (list_id,),
        ).fetchone()

    if exists:
        start_refresh(list_id, force=True)

    return redirect(url_for("index"))


@app.post("/lists/<list_id>/interval")
def set_interval(list_id):
    try:
        value = int(request.form.get("update_interval_seconds", ""))
    except ValueError:
        return redirect(url_for("index"))

    if value not in {21600, 43200, 86400}:
        return redirect(url_for("index"))

    with db() as connection:
        connection.execute(
            "UPDATE lists SET update_interval_seconds = ? WHERE id = ?",
            (value, list_id),
        )

    return redirect(url_for("index"))


@app.post("/lists/<list_id>/toggle")
def toggle_list(list_id):
    with db() as connection:
        row = connection.execute(
            "SELECT enabled FROM lists WHERE id = ?",
            (list_id,),
        ).fetchone()

        if row:
            connection.execute(
                "UPDATE lists SET enabled = ? WHERE id = ?",
                (0 if row["enabled"] else 1, list_id),
            )

    return redirect(url_for("index"))


@app.post("/lists/<list_id>/rename")
def rename_list(list_id):
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("list_detail", list_id=list_id))

    with db() as connection:
        connection.execute(
            "UPDATE lists SET name = ? WHERE id = ?",
            (name[:120], list_id),
        )

    return redirect(url_for("list_detail", list_id=list_id))


@app.post("/lists/<list_id>/delete")
def delete_list(list_id):
    with db() as connection:
        connection.execute(
            """DELETE FROM update_changes
               WHERE history_id IN (
                   SELECT id FROM update_history WHERE list_id = ?
               )""",
            (list_id,),
        )
        connection.execute(
            """DELETE FROM unmatched_movies
               WHERE history_id IN (
                   SELECT id FROM update_history WHERE list_id = ?
               )""",
            (list_id,),
        )
        connection.execute("DELETE FROM movies WHERE list_id = ?", (list_id,))
        connection.execute("DELETE FROM update_history WHERE list_id = ?", (list_id,))
        connection.execute("DELETE FROM lists WHERE id = ?", (list_id,))

    return redirect(url_for("index"))


@app.get("/backup")
def backup_database():
    source = sqlite3.connect(DB_PATH)
    target = sqlite3.connect(":memory:")

    try:
        source.backup(target)
        data = target.serialize()
    finally:
        target.close()
        source.close()

    return send_file(
        io.BytesIO(data),
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name="letterboxd-radarr-lists.db",
    )


@app.get("/export")
def export_data():
    with db() as connection:
        lists = connection.execute(
            """SELECT id, letterboxd_url, name, created_at, updated_at,
                      last_error, enabled, update_interval_seconds
               FROM lists
               ORDER BY created_at"""
        ).fetchall()

        export_lists = []
        for item in lists:
            movies = connection.execute(
                """SELECT tmdb_id, imdb_id, title, year, letterboxd_path
                   FROM movies
                   WHERE list_id = ?
                   ORDER BY rowid""",
                (item["id"],),
            ).fetchall()

            export_lists.append({
                "id": item["id"],
                "letterboxd_url": item["letterboxd_url"],
                "name": item["name"],
                "created_at": item["created_at"],
                "updated_at": item["updated_at"],
                "last_error": item["last_error"],
                "enabled": bool(item["enabled"]),
                "update_interval_seconds": item["update_interval_seconds"],
                "movies": [dict(movie) for movie in movies],
            })

    payload = {
        "format": 1,
        "exported_at": now(),
        "lists": export_lists,
    }

    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={
            "Content-Disposition": "attachment; filename=letterboxd-radarr-lists.json"
        },
    )


@app.post("/refresh-all")
def refresh_all():
    with db() as connection:
        rows = connection.execute("SELECT id FROM lists ORDER BY created_at").fetchall()

    for row in rows:
        start_refresh(row["id"], force=True)

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
