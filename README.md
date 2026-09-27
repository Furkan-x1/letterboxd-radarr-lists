# Letterboxd Radarr Lists

A self-hosted service that turns public Letterboxd lists and watchlists into stable URLs that Radarr can use as custom lists.

## How it works

1. Add a public Letterboxd list or watchlist through the web interface.
2. The service retrieves and stores the movie data locally.
3. A stable URL is generated for Radarr.
4. The list is updated automatically on its configured schedule.

The service does not require a Radarr API key and does not add movies directly to Radarr.

## Installation

### Requirements

- Docker
- Docker Compose

Clone the repository:

\`\`\`bash
git clone https://github.com/Furkan-x1/letterboxd-radarr-lists.git
cd letterboxd-radarr-lists
\`\`\`

Start the service:

\`\`\`bash
docker compose up -d --build
\`\`\`

The web interface is available at:

\`\`\`text
http://localhost:5000
\`\`\`

## Usage

Enter a public Letterboxd list or watchlist. Full URLs and shorthand paths are supported:

\`\`\`text
https://letterboxd.com/username/watchlist/
username/watchlist

https://letterboxd.com/username/list/example/
username/list/example
\`\`\`

After adding a list, the service generates a Radarr URL such as:

\`\`\`text
http://192.168.1.100:5000/radarr/abc123def456
\`\`\`

Add this URL to Radarr as an **HTTP / Custom List** source.

Use \`http://\`, not \`https://\`, with the default Docker setup.

## Updates

Each list can be configured to update every **6, 12, or 24 hours**.

You can also:

- refresh a list manually;
- pause automatic updates for an individual list;
- pause all automatic updates for 12 hours;
- view update history and added/removed movie counts;
- search and filter stored movies.

Previously retrieved film data is cached to avoid unnecessary Letterboxd requests.

## Configuration

The main settings are configured in \`compose.yml\`:

\`\`\`yaml
environment:
  - TZ=Europe/Istanbul
  - UPDATE_INTERVAL_SECONDS=43200
  - LETTERBOXD_REQUEST_DELAY_SECONDS=3
  - LETTERBOXD_REQUEST_TIMEOUT_SECONDS=20
  - MAX_PAGES_PER_LIST=100
  - MAX_MOVIES_PER_LIST=5000
  - UPDATER_POLL_SECONDS=60
  - LETTERBOXD_RETRY_ATTEMPTS=3
  - POST_RATE_LIMIT_WINDOW_SECONDS=60
  - POST_RATE_LIMIT_MAX=20
\`\`\`

\`UPDATE_INTERVAL_SECONDS\` is the default interval for newly added lists. Individual lists can be changed from the web interface.

The database is stored in:

\`\`\`text
./data/app.db
\`\`\`

Keep the \`data\` directory when updating or recreating the container.

## Updating

From the project directory:

\`\`\`bash
git pull
docker compose up -d --build
\`\`\`

View logs:

\`\`\`bash
docker logs -f letterboxd-radarr-lists
\`\`\`

System information is available at:

\`\`\`text
http://localhost:5000/status
\`\`\`
