# MTG Search

A local web app for browsing and searching Magic: The Gathering cards, with a
functional-card similarity engine.

## Features

- **Card browser** — browse by set, view card details (image + Oracle text)
- **Full-text search** — search cards by name, type, or Oracle text
- **Similarity engine** — find cards that play similarly to a given card
  - IDF-weighted Oracle text overlap (rare terms matter more)
  - Optional factor toggles: Types, Keywords, Subtypes, Supertypes, Mana Value, Color Identity
  - MTG-domain stop-word filter to strip ubiquitous terms
  - Score normalized to 0–100% regardless of active factors

## Setup

Requires Python 3.9+.

```bash
git clone <this-repo>
cd mtg-search
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Database

This app reads from an MTGJSON **AllPrintings.sqlite** file.

1. Download from [mtgjson.com](https://mtgjson.com/downloads/all-files/)
2. Place the file in the project root, or create a symlink:
   ```bash
   ln -s /path/to/AllPrintings.sqlite AllPrintings.sqlite
   ```

## Usage

```bash
# Start
./run.sh start

# Open http://127.0.0.1:5000

# Stop
./run.sh stop
```

## Project structure

```
app.py              # Flask application (single file)
templates/          # Jinja2 templates
  base.html         #   layout, nav, card-detail panel
  index.html        #   home page
  search.html       #   text search
  sets.html         #   set list
  set.html          #   cards within a set
  card.html         #   card detail
  card_panel.html   #   card detail (side-panel fragment)
  similar.html      #   similarity results + tuning panel
static/
  style.css         # stylesheet
run.sh              # start/stop/restart/status script
requirements.txt    # Python dependencies
```

## Data sources

- Card data: [MTGJSON](https://mtgjson.com)
- Card images: [Scryfall](https://scryfall.com)

## License

MIT
