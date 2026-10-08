"""Proposing a value for a blank City / Postal Code from the client's own data.

A blank mandatory field used to be reported as a manual defect even when the same
file already knows the answer: every verified record with country IN and postal
code 400001 says Mumbai. This module builds the lookup (country, postal code) ->
city from the records that are complete and plausible, and proposes it for a blank
city when the evidence is strong:

* the record has a valid ISO country and a postal code that fits that country's format,
* at least ``enrichment.min_support`` verified records share that (country, postal code), and
* at least ``enrichment.min_cooccurrence`` (default 90%) of them agree on one city.

The same works the other way (country + city -> postal code) when a city has one
postal code, and for a blank REGION (SAP REGIO: state / province) from country +
postal code. Nothing leaves the process and no LLM is involved; the proposal is a
pre-selection for the reviewer, never applied silently. A reference directory
(``geo_reference``, offline GeoNames extract) is consulted only when the client's
own data has no answer.

A region code from GeoNames is NOT automatically an SAP region key: SAP keys German
states 01-16 (09 = Bayern) where GeoNames says BY. So a GeoNames region is proposed
only for ``enrichment.region_countries`` (checked to agree with T005S), or when the
client's own records already use that exact code for that country - and, when the
Metadata Repository delivered the column's domain (T005S), only a value inside it.
"""

import re
from typing import Any, Dict, List, Optional

import pandas as pd

from src.agents.engines import geo_reference
from src.agents.config import Config
from src.agents.engines.rule_context import Ctx, blank, text

# SAP REGIO is CHAR 3, upper case.
_REGION_CODE_RE = re.compile(r"^[A-Z0-9]{1,3}$")
_LABELS = {"CITY": "city", "POSTAL_CODE": "postal code", "REGION": "region"}


def _norm_place(series: pd.Series) -> pd.Series:
    return text(series).str.upper().str.replace(r"\s+", " ", regex=True)


def infer_missing(ctx: Ctx, target: str, hits: pd.Index) -> Dict[int, Dict[str, Any]]:
    """{row index: {value, source, support, share, evidence}} for blank ``target`` cells that can be filled.

    ``target`` is a CITY, POSTAL_CODE or REGION column of ``ctx.table``; rows without a usable proposal
    are simply absent from the result.
    """
    if not Config.ENRICHMENT_ENABLED or not len(hits):
        return {}
    concept = ctx.b(target)["concept"]
    if concept not in ("CITY", "POSTAL_CODE", "REGION"):
        return {}
    if concept == "POSTAL_CODE" and not Config.ENRICHMENT_POSTAL_FROM_CITY:
        return {}   # a city usually has many postal codes: weak evidence, opt-in only
    country_col = ctx.country_for(target)
    partner_concept = "CITY" if concept == "POSTAL_CODE" else "POSTAL_CODE"
    partners = [c for c in ctx.cols(partner_concept) if ctx.country_for(c) == country_col] \
        if partner_concept == "POSTAL_CODE" else ctx.cols(partner_concept)
    if not country_col or len(partners) != 1:
        return {}
    partner = partners[0]

    df = ctx.df
    countries = text(df[country_col]).str.upper()
    valid_country = countries.isin(ctx.pack["_iso"])
    partner_raw, target_raw = text(df[partner]), text(df[target])
    partner_key = _norm_place(df[partner])
    target_key = _norm_place(df[target])

    # "Verified" = valid country, both fields filled, postal code (whichever side it is) fits its country.
    verified = valid_country & (partner_key != "") & (target_key != "")
    if concept == "REGION":
        verified &= target_key.str.match(_REGION_CODE_RE.pattern)
    postal_col = target if concept == "POSTAL_CODE" else partner
    postal_key = target_key if concept == "POSTAL_CODE" else partner_key
    formats = ctx.pack["_postal"]
    for country in set(countries[verified]) & set(formats):
        in_country = verified & (countries == country)
        verified &= ~(in_country & ~postal_key.str.match(formats[country][0]))
    proven = pd.DataFrame({"country": countries[verified], "partner": partner_key[verified],
                           "value": target_key[verified], "shown": target_raw[verified]})
    stats: Dict[tuple, Dict[str, Any]] = {}
    if len(proven):
        for (country, partner_value), group in proven.groupby(["country", "partner"], sort=False):
            counts = group["value"].value_counts()
            share = counts.iloc[0] / len(group)
            if len(group) >= Config.ENRICHMENT_MIN_SUPPORT and share >= Config.ENRICHMENT_MIN_COOCCURRENCE:
                # A region is a key: propose it upper case, as SAP stores it. A city keeps the spelling
                # the records use most.
                shown = (counts.index[0] if concept == "REGION" else
                         group.loc[group["value"] == counts.index[0], "shown"].value_counts().index[0])
                stats[(country, partner_value)] = {"value": shown, "support": len(group), "share": float(share)}

    # Region codes the client's verified records already use, per country (the guard for GeoNames codes).
    used_regions: Dict[str, set] = {}
    if concept == "REGION" and len(proven):
        used_regions = {c: set(g) for c, g in proven.groupby("country")["value"]}
    allowed = set(ctx.b(target).get("allowed_values") or [])

    out: Dict[int, Dict[str, Any]] = {}
    for i in hits:
        country, partner_value = countries[i], partner_key[i]
        if not valid_country[i] or not partner_value:
            continue
        if partner == postal_col and country in formats and not pd.Series([partner_value]).str.match(
                formats[country][0])[0]:
            continue   # the record's own postal code is not plausible: it cannot vouch for a city / region
        hit = stats.get((country, partner_value))
        source = "dataset"
        if hit is None and concept == "CITY":
            ref = geo_reference.city_for(country, partner_raw[i])
            if ref:
                hit, source = {"value": ref, "support": 0, "share": 1.0}, "reference"
        if hit is None and concept == "REGION":
            ref = _reference_region(country, partner_raw[i], used_regions.get(country, set()))
            if ref:
                hit, source = {"value": ref["code"], "support": 0, "share": 1.0, "name": ref["name"]}, "reference"
        if hit is None or (allowed and hit["value"] not in allowed):
            continue   # outside the Metadata Repository's domain (e.g. T005S): never propose it
        if source == "dataset":
            evidence = (f"{hit['support']} verified record(s) with country {country} and {_LABELS[partner_concept]} "
                        f"'{partner_raw[i]}' have {_LABELS[concept]} '{hit['value']}' ({hit['share']:.0%})")
        else:
            named = f" ({hit['name']})" if hit.get("name") else ""
            evidence = (f"the offline postal directory lists {_LABELS[concept]} '{hit['value']}'{named} for country "
                        f"{country} and postal code '{partner_raw[i]}'")
        out[int(i)] = {**hit, "source": source, "evidence": evidence}
    return out


def _reference_region(country: str, postal: str, used: set) -> Optional[Dict[str, str]]:
    """GeoNames region for a postal code, only when it can be trusted to be the SAP region key:
    a code of at most 3 characters, and either the country is in ``enrichment.region_countries`` or
    the client's own verified records already use this code for the country."""
    ref = geo_reference.region_for(country, postal)
    if not ref or not ref.get("code"):
        return None
    code = str(ref["code"]).strip().upper()
    if not _REGION_CODE_RE.match(code):
        return None
    if country not in Config.ENRICHMENT_REGION_COUNTRIES and code not in used:
        return None
    return {"code": code, "name": ref.get("name") or ""}


def hypothesis(concept: str, from_reference: bool = False) -> str:
    """The finding's hypothesis text: says which evidence the proposal rests on."""
    if concept == "CITY":
        text_ = "City inferred from matching Postal Code and Country across verified records in dataset"
    elif concept == "REGION":
        text_ = "Region inferred from matching Postal Code and Country across verified records in dataset"
    else:
        text_ = "Postal Code inferred from matching City and Country across verified records in dataset"
    return text_ + (" (and, where the dataset had no answer, the offline postal directory)" if from_reference else "")
