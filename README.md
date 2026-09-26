# Letterboxd Radarr Lists

A self-hosted service that turns public Letterboxd lists into stable JSON URLs that Radarr can use as import lists.

## How it works

1. Open the web interface.
2. Paste a public Letterboxd list or watchlist URL.
3. The service scrapes the list and stores its movie data locally.
4. A stable Radarr URL is generated for that list.
5. Radarr reads the generated URL instead of scraping Letterboxd directly.
6. The service periodically refreshes the stored list.

The service uses Letterboxd TMDB IDs as the primary movie identifier. It does not need a Radarr API key and does not add movies to Radarr.

## Anti-abuse measures

The service is intentionally conservative when accessing Letterboxd:

- respects Letterboxd robots.txt;
- uses a descriptive User-Agent;
- limits request frequency;
- waits after HTTP 429 responses;
- stores scraped results locally;
- refreshes lists periodically instead of on every Radarr request;
- limits pages and movies per list.

The default refresh interval is 6 hours and the default delay between Letterboxd requests is 2 seconds.

## Local deployment

```bash
docker compose up -d --build
```

Open `http://localhost:5000/`.

The database is stored in `./data/app.db`.

## Radarr

After adding a list, the web interface displays a stable URL such as:

```
http://your-server:5000/radarr/abc123def456
```

Use that URL in Radarr's HTTP/Custom List source.

## Scope

The first version intentionally focuses on maintaining stable Radarr-compatible endpoints from public Letterboxd lists. More advanced features can be added later.
