# Letterboxd Radarr Lists

A self-hosted service that turns public Letterboxd lists and watchlists into stable URLs that Radarr can use as custom lists.

## How it works

1. Add a public Letterboxd list or watchlist through the web interface.
2. The service retrieves and stores the movie data locally.
3. A stable URL is generated for Radarr.
4. The list is automatically updated periodically.

The service does not require a Radarr API key and does not add movies directly to Radarr.

## Installation

### Requirements

- Docker
- Docker Compose

Clone the repository:

```bash
git clone https://github.com/Furkan-x1/letterboxd-radarr-lists.git
cd letterboxd-radarr-lists
```

Start the service:

```bash
docker compose up -d --build
```

The web interface is available at:

```text
http://localhost:5000
```

For another machine, use the Docker host's IP address:

```text
http://192.168.1.100:5000
```

## Usage

Open the web interface and enter a public Letterboxd list.

Both full URLs and shorthand paths are supported:

```text
https://letterboxd.com/username/watchlist/
username/watchlist

https://letterboxd.com/username/list/example/
username/list/example
```

After adding a list, the service generates a Radarr URL such as:

```text
http://192.168.1.100:5000/radarr/abc123def456
```

## Radarr

In Radarr, add the generated URL as an **HTTP / Custom List** source.

The service returns movie information as JSON. Radarr reads this endpoint when updating the list.

No Radarr API key is required.

> The default Docker setup uses HTTP on port `5000`. Use `http://`, not `https://`, unless you have configured a reverse proxy with HTTPS.

## Updates

Lists are automatically updated every **12 hours** by default.

You can also:

- manually refresh a list;
- pause automatic updates for a list;
- pause all automatic updates for 12 hours.

The service caches previously retrieved movie data and uses a delay between requests to avoid unnecessary requests to Letterboxd.

## Configuration

The main settings are configured in `compose.yml`:

```yaml
environment:
  - TZ=Europe/Istanbul
  - UPDATE_INTERVAL_SECONDS=43200
  - LETTERBOXD_REQUEST_DELAY_SECONDS=3
  - LETTERBOXD_REQUEST_TIMEOUT_SECONDS=20
  - MAX_PAGES_PER_LIST=100
  - MAX_MOVIES_PER_LIST=5000
```

The database is stored in:

```text
./data/app.db
```

This directory should be preserved when updating or recreating the container.

## Updating

From the project directory:

```bash
git pull
docker compose up -d --build
```

To view logs:

```bash
docker logs -f letterboxd-radarr-lists
```
