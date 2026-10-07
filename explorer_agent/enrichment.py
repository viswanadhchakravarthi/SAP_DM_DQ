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
postal code. Nothing leaves the process and no LLM is involved; the proposal is a
pre-selection for the reviewer, never applied silently. A reference directory
(``geo_reference``, offline GeoNames extract) is consulted only when the client's
own data has no answer.
"""

from typing import Any, Dict, List, Optional

import pandas as pd

from . import geo_reference
from .config import Config
from .rule_context import Ctx, blank, text


def _norm_place(series: pd.Series) -> pd.Series:
    return text(series).str.upper().str.replace(r"\s+", " ", regex=True)


def infer_missing(ctx: Ctx, target: str, hits: pd.Index) -> Dict[int, Dict[str, Any]]:
    """{row index: {value, source, support, share, evidence}} for blank ``target`` cells that can be filled.

    ``target`` is a CITY or POSTAL_CODE column of ``ctx.table``; rows without a usable proposal are
    simply absent from the result.
    """
    if not Config.ENRICHMENT_ENABLED or not len(hits):
        return {}
    concept = ctx.b(target)["concept"]
    if concept not in ("CITY", "POSTAL_CODE"):
        return {}
    if concept == "POSTAL_CODE" and not Config.ENRICHMENT_POSTAL_FROM_CITY:
        return {}   # a city usually has many postal codes: weak evidence, opt-in only
    country_col = ctx.country_for(target)
    partner_concept = "POSTAL_CODE" if concept == "CITY" else "CITY"
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
    postal_col = partner if concept == "CITY" else target
    postal_key = partner_key if concept == "CITY" else target_key
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
                shown = group.loc[group["value"] == counts.index[0], "shown"].value_counts().index[0]
                stats[(country, partner_value)] = {"value": shown, "support": len(group), "share": float(share)}

    labels = {"CITY": "city", "POSTAL_CODE": "postal code"}
    out: Dict[int, Dict[str, Any]] = {}
    for i in hits:
        country, partner_value = countries[i], partner_key[i]
        if not valid_country[i] or not partner_value:
            continue
        if partner == postal_col and country in formats and not pd.Series([partner_value]).str.match(
                formats[country][0])[0]:
            continue   # the record's own postal code is not plausible: it cannot vouch for a city
        hit = stats.get((country, partner_value))
        source = "dataset"
        if hit is None and concept == "CITY":
            ref = geo_reference.city_for(country, partner_raw[i])
            if ref:
                hit, source = {"value": ref, "support": 0, "share": 1.0}, "reference"
        if hit is None:
            continue
        if source == "dataset":
            evidence = (f"{hit['support']} verified record(s) with country {country} and {labels[partner_concept]} "
                        f"'{partner_raw[i]}' have {labels[concept]} '{hit['value']}' ({hit['share']:.0%})")
        else:
            evidence = (f"the offline postal directory lists '{hit['value']}' for country {country} and "
                        f"postal code '{partner_raw[i]}'")
        out[int(i)] = {**hit, "source": source, "evidence": evidence}
    return out


def hypothesis(concept: str, from_reference: bool = False) -> str:
    """The finding's hypothesis text: says which evidence the proposal rests on."""
    if concept == "CITY":
        text_ = "City inferred from matching Postal Code and Country across verified records in dataset"
    else:
        text_ = "Postal Code inferred from matching City and Country across verified records in dataset"
    return text_ + (" (and, where the dataset had no answer, the offline postal directory)" if from_reference else "")
