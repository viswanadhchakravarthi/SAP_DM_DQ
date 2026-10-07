# Offline reference data

Static files the agent reads at run time. Nothing here is fetched while the agent runs.

| File | What | Built by | In git |
|---|---|---|---|
| `geo_postal.db` | Country + postal code -> place, region (SQLite). Fills a blank City from a postal code when the client's own data has no answer (`explorer_agent/geo_reference.py`, `enrichment.py`). | `python -m explorer_agent.tools.build_geo_postal` | no (a few MB; rebuild it) |

Without `geo_postal.db` every lookup returns nothing and the agent behaves as before.

Rule-pack reference lists (ISO 3166-1 countries, ISO 4217 currencies, Incoterms 2020, postal and tax formats) live in
`explorer_agent/rule_packs/sap_master_data.yaml`, not here.

## Attribution

Postal code data: GeoNames (https://www.geonames.org), licensed under Creative Commons Attribution 4.0
(https://creativecommons.org/licenses/by/4.0/). Keep this notice with the data when you redistribute it.
