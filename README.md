# Irish Tariff Optimiser

Self-hosted app that replays your actual ESB Networks half-hourly smart-meter data against a
library of Irish electricity tariff plans and ranks them by true annual cost — including solar
export credit, standing charges and time-of-use bands (day/night/peak/EV/free windows).

![Plan ranking dashboard](docs/screenshot.png)

The **Usage** tab shows where your kWh actually land across the day (the case for a night
tariff at a glance) and monthly import/export; the **Plans** tab manages suppliers, rate bands
and time windows:

![Usage profile](docs/usage.png)

![Plan editor](docs/plans.png)

## How it works
- A nightly job copies the HDF interval export (`esbn_hdf_latest.csv`) into `data/` and POSTs
  `/api/ingest`. The file comes from [my fork of the esbn-to-mqtt Home Assistant add-on](https://github.com/colfin22/esbn-to-mqtt),
  which saves a local copy of each HDF download (to `/share/esbn/`) alongside publishing the
  MQTT sensors — the stock add-on doesn't keep the file. Any other source of an ESB Networks
  HDF export works too (e.g. the manual download from your ESB online account).
- The costing engine assigns each half-hour to a plan's rate band by local clock time and
  weekday (interval START time), sums import costs, subtracts export credit, adds the standing
  charge and annualises.
- Plans and their rate bands are managed in the UI (`/plans`); seed data for the six main
  suppliers is in `app/seed.py` with an as-of date — rates are entered including VAT.
- Contract-expiry alerts fire at 30 and 7 days via a Home Assistant notify service (`/settings`).
- A monthly **scrape-and-suggest** job (`POST /api/scrape`) checks the selectra.ie rate cards and
  queues any rate differences as suggestions on the Plans page — nothing is applied without
  review, parse failures alert loudly, and band time-windows still need a human eye.

## Run
    docker compose up -d --build
    docker compose exec tariff python -m app.seed   # first run only
    # put an HDF file at data/esbn_hdf_latest.csv, then:
    curl -X POST localhost:8000/api/ingest

## Tests
    python -m pytest tests/
