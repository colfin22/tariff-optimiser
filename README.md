# Irish Tariff Optimiser

Self-hosted app that replays your actual ESB Networks half-hourly smart-meter data against a
library of Irish electricity tariff plans and ranks them by true annual cost — including solar
export credit, standing charges and time-of-use bands (day/night/peak/EV/free windows).

![Plan ranking dashboard](docs/screenshot.png)

## How it works
- A nightly job copies the HDF interval export (`esbn_hdf_latest.csv`, produced by the
  esbn-to-mqtt Home Assistant add-on) into `data/` and POSTs `/api/ingest`.
- The costing engine assigns each half-hour to a plan's rate band by local clock time and
  weekday (interval START time), sums import costs, subtracts export credit, adds the standing
  charge and annualises.
- Plans and their rate bands are managed in the UI (`/plans`); seed data for the six main
  suppliers is in `app/seed.py` with an as-of date — rates are entered including VAT.
- Contract-expiry alerts fire at 30 and 7 days via a Home Assistant notify service (`/settings`).

## Run
    docker compose up -d --build
    docker compose exec tariff python -m app.seed   # first run only
    # put an HDF file at data/esbn_hdf_latest.csv, then:
    curl -X POST localhost:8000/api/ingest

## Tests
    python -m pytest tests/
