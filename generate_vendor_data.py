"""
generate_vendor_data.py
------------------------------------------------------------
Synthetic SAP Vendor Master Data generator.

Produces 4 relational tables that mirror the real SAP vendor
master structure:

    LFA1  - General vendor data       (key: LIFNR)
    LFB1  - Company code data         (key: LIFNR, BUKRS)
    LFM1  - Purchasing org data       (key: LIFNR, EKORG)
    LFBK  - Bank details              (key: LIFNR, BANKS, BANKL, BANKN)

Controlled, configurable data-quality (DQ) anomalies are injected on
top of a fully clean/relationally-consistent base dataset. Every
single injected anomaly is logged to Answer_Key.csv so the output
can be used as a labeled dataset for testing DQ/validation rule
engines, dashboards, or ML classifiers.

Output (6 CSVs) is written to /mnt/user-data/outputs/
------------------------------------------------------------
"""

import numpy as np
import pandas as pd
import random
import string
from datetime import datetime, timedelta

# =====================================================================
# 0. CONFIG - tune volumes and error rates here
# =====================================================================

CONFIG = {
    "seed": 42,
    "n_vendors": 1000,
    "output_dir": "/mnt/user-data/outputs",

    # Probability (0-1) that a GIVEN eligible record/field is hit by
    # each anomaly type. Applied independently per anomaly category.
    "error_rates": {
        "missing_mandatory":      0.03,   # blank/NaN mandatory fields
        "invalid_format":         0.03,   # malformed tax id / IBAN / postal code / phone
        "duplicate_records":      0.02,   # duplicate primary-key rows
        "orphan_fk":              0.02,   # child row pointing to a LIFNR that doesn't exist in LFA1
        "cross_table_violation":  0.02,   # business-rule breaks across tables
    },
}

rng = np.random.default_rng(CONFIG["seed"])
random.seed(CONFIG["seed"])

# =====================================================================
# 1. REFERENCE / MOCK DATA POOLS (multi-country)
# =====================================================================

COUNTRIES = {
    "US": {"currency": "USD", "tax_label": "EIN",   "iban": False, "regio": ["CA", "NY", "TX", "IL", "WA"],
           "cities": ["Chicago", "Dallas", "Seattle", "Atlanta", "Denver"], "phone_cc": "1"},
    "DE": {"currency": "EUR", "tax_label": "USt-IdNr", "iban": True, "iban_len": 22, "regio": ["BY", "NW", "BW", "HE"],
           "cities": ["Munich", "Cologne", "Stuttgart", "Frankfurt"], "phone_cc": "49"},
    "GB": {"currency": "GBP", "tax_label": "VAT",   "iban": True, "iban_len": 22, "regio": ["ENG", "SCT", "WLS"],
           "cities": ["London", "Manchester", "Leeds", "Bristol"], "phone_cc": "44"},
    "FR": {"currency": "EUR", "tax_label": "SIREN", "iban": True, "iban_len": 27, "regio": ["IDF", "PAC", "OCC"],
           "cities": ["Paris", "Lyon", "Marseille", "Toulouse"], "phone_cc": "33"},
    "IN": {"currency": "INR", "tax_label": "GSTIN", "iban": False, "regio": ["MH", "KA", "DL", "TN"],
           "cities": ["Mumbai", "Bengaluru", "Delhi", "Chennai"], "phone_cc": "91"},
    "JP": {"currency": "JPY", "tax_label": "CorpNo", "iban": False, "regio": ["13", "27", "14"],
           "cities": ["Tokyo", "Osaka", "Yokohama"], "phone_cc": "81"},
    "BR": {"currency": "BRL", "tax_label": "CNPJ",  "iban": False, "regio": ["SP", "RJ", "MG"],
           "cities": ["Sao Paulo", "Rio de Janeiro", "Belo Horizonte"], "phone_cc": "55"},
    "CN": {"currency": "CNY", "tax_label": "USCC",  "iban": False, "regio": ["31", "44", "11"],
           "cities": ["Shanghai", "Shenzhen", "Beijing"], "phone_cc": "86"},
}
COUNTRY_CODES = list(COUNTRIES.keys())

COMPANY_SUFFIX = {
    "US": ["Inc.", "LLC", "Corp."], "DE": ["GmbH", "AG"], "GB": ["Ltd.", "PLC"],
    "FR": ["SARL", "SA"], "IN": ["Pvt Ltd", "Ltd"], "JP": ["K.K.", "Co."],
    "BR": ["Ltda", "S.A."], "CN": ["Co. Ltd", "Group"],
}
COMPANY_STEM = ["Nova", "Atlas", "Summit", "Blue Ridge", "Cobalt", "Meridian", "Sterling",
                "Pioneer", "Harbor", "Granite", "Vertex", "Horizon", "Crescent", "Falcon",
                "Union", "Lumen", "Cedar", "Anchor", "Orion", "Fenwick", "Redwood", "Keystone"]
COMPANY_KIND = ["Trading", "Manufacturing", "Logistics", "Industries", "Supplies",
                "Components", "Services", "Engineering", "Materials", "Distribution"]

STREET_NAMES = ["Main St", "Industrial Ave", "Market Rd", "Church Ln", "Park Blvd",
                "Station St", "Mill Rd", "High St", "Commerce Way", "Oak Ave"]

BUKRS_POOL = ["1000", "2000", "3000", "4000", "5000"]           # company codes
EKORG_POOL = ["EK01", "EK02", "EK03", "EK04"]                    # purchasing orgs
ZTERM_POOL = ["NT30", "NT45", "NT60", "2/10 NT30", "ZB30", "0001"]
INCO1_POOL = ["EXW", "FOB", "CIF", "DAP", "DDP", "FCA"]
KTOKK_POOL = ["KRED", "LIEF", "0001"]                             # vendor account groups
ZWELS_POOL = ["C", "T", "U"]                                       # payment methods (check/transfer/other)
BVTYP_POOL = ["0001", "0002", ""]

BANK_NAME_POOL = ["First National Bank", "Global Trust Bank", "Continental Bank",
                   "Meridian Savings", "Union Commercial Bank", "Pacific Bank"]


def rand_str(n, chars=string.ascii_uppercase + string.digits):
    return "".join(random.choices(chars, k=n))


def rand_date(start_year=2005, end_year=2025):
    start = datetime(start_year, 1, 1)
    end = datetime(end_year, 12, 31)
    delta = end - start
    return (start + timedelta(days=random.randint(0, delta.days))).strftime("%Y-%m-%d")


def make_company_name():
    return f"{random.choice(COMPANY_STEM)} {random.choice(COMPANY_KIND)}"


def make_tax_id(country):
    """Produce a plausible tax id matching each country's typical length/pattern."""
    if country == "US":
        return f"{random.randint(10,99)}-{random.randint(1000000,9999999)}"
    if country == "DE":
        return "DE" + "".join(random.choices(string.digits, k=9))
    if country == "GB":
        return "GB" + "".join(random.choices(string.digits, k=9))
    if country == "FR":
        return "".join(random.choices(string.digits, k=9))
    if country == "IN":
        return f"{random.randint(10,35)}" + rand_str(10) + "1Z" + random.choice(string.ascii_uppercase)
    if country == "JP":
        return "".join(random.choices(string.digits, k=13))
    if country == "BR":
        return "".join(random.choices(string.digits, k=14))
    if country == "CN":
        return rand_str(18)
    return rand_str(10)


def make_iban(country):
    info = COUNTRIES[country]
    if not info["iban"]:
        return ""
    body_len = info["iban_len"] - 4
    return f"{country}{random.randint(10,99)}" + rand_str(body_len)


def make_bank_account_number(country):
    if COUNTRIES[country]["iban"]:
        return ""  # IBAN countries carry the account inside the IBAN for this dataset
    return "".join(random.choices(string.digits, k=random.choice([8, 10, 12])))


def make_postal_code(country):
    if country == "US":
        return f"{random.randint(10000,99999)}"
    if country in ("DE", "FR"):
        return f"{random.randint(10000,99999)}"
    if country == "GB":
        return f"{random.choice(string.ascii_uppercase)}{random.randint(1,9)} {random.randint(1,9)}{random.choice(string.ascii_uppercase)}{random.choice(string.ascii_uppercase)}"
    if country == "IN":
        return f"{random.randint(100000,999999)}"
    if country == "JP":
        return f"{random.randint(100,999)}-{random.randint(1000,9999)}"
    if country == "BR":
        return f"{random.randint(10000,99999)}-{random.randint(100,999)}"
    if country == "CN":
        return f"{random.randint(100000,999999)}"
    return str(random.randint(10000, 99999))


def make_phone(country):
    cc = COUNTRIES[country]["phone_cc"]
    return f"+{cc}-{random.randint(200,999)}-{random.randint(1000000,9999999)}"


# =====================================================================
# 2. ANSWER KEY LOGGER
# =====================================================================

answer_key_rows = []


def log_issue(table, lifnr, org_key, field, issue_type, description):
    answer_key_rows.append({
        "Table": table,
        "LIFNR": lifnr,
        "Org_Key": org_key,
        "Field": field,
        "Issue_Type": issue_type,
        "Issue_Description": description,
    })


# =====================================================================
# 3. BUILD CLEAN BASE DATA (fully relationally consistent)
# =====================================================================

def build_lfa1(n):
    rows = []
    for i in range(n):
        lifnr = f"{100000 + i:010d}"
        country = random.choice(COUNTRY_CODES)
        info = COUNTRIES[country]
        name1 = f"{make_company_name()} {random.choice(COMPANY_SUFFIX[country])}"
        rows.append({
            "LIFNR": lifnr,
            "NAME1": name1,
            "NAME2": "",
            "SORTL": name1[:10].upper().replace(" ", ""),
            "STRAS": f"{random.randint(1,9999)} {random.choice(STREET_NAMES)}",
            "ORT01": random.choice(info["cities"]),
            "PSTLZ": make_postal_code(country),
            "LAND1": country,
            "REGIO": random.choice(info["regio"]),
            "SPRAS": "EN",
            "STCD1": make_tax_id(country),
            "STCD2": "",
            "KTOKK": random.choice(KTOKK_POOL),
            "TELF1": make_phone(country),
            "ERDAT": rand_date(),
            "ERNAM": "DATALOAD",
            "SPERR": "",     # central posting block
            "LOEVM": "",     # central deletion flag
        })
    return pd.DataFrame(rows)


def build_lfb1(lfa1):
    rows = []
    for _, v in lfa1.iterrows():
        n_codes = random.choice([1, 1, 1, 2])  # most vendors: 1 company code, some: 2
        codes = random.sample(BUKRS_POOL, n_codes)
        for bukrs in codes:
            rows.append({
                "LIFNR": v["LIFNR"],
                "BUKRS": bukrs,
                "AKONT": "160000",
                "ZWELS": random.choice(ZWELS_POOL),
                "ZTERM": random.choice(ZTERM_POOL),
                "WAERS": COUNTRIES[v["LAND1"]]["currency"],
                "ZAHLS": "X" if v["SPERR"] == "X" else "",   # payment block mirrors central block by default
                "LOEVM": v["LOEVM"],
                "ERDAT": v["ERDAT"],
            })
    return pd.DataFrame(rows)


def build_lfm1(lfa1):
    rows = []
    for _, v in lfa1.iterrows():
        n_orgs = random.choice([1, 1, 2])
        orgs = random.sample(EKORG_POOL, n_orgs)
        for ekorg in orgs:
            rows.append({
                "LIFNR": v["LIFNR"],
                "EKORG": ekorg,
                "WAERS": COUNTRIES[v["LAND1"]]["currency"],
                "ZTERM": random.choice(ZTERM_POOL),
                "INCO1": random.choice(INCO1_POOL),
                "INCO2": "",
                "VERKF": rand_str(6, string.ascii_uppercase),
                "TELF1": make_phone(v["LAND1"]),
                "SPERM": v["SPERR"],
                "LOEVM": v["LOEVM"],
            })
    return pd.DataFrame(rows)


def build_lfbk(lfa1):
    rows = []
    for _, v in lfa1.iterrows():
        n_banks = random.choice([1, 1, 1, 2])
        for _ in range(n_banks):
            country = v["LAND1"]
            rows.append({
                "LIFNR": v["LIFNR"],
                "BANKS": country,
                "BANKL": rand_str(8, string.digits),
                "BANKN": make_bank_account_number(country),
                "IBAN": make_iban(country),
                "BKONT": random.choice(["00", "01"]),
                "BVTYP": random.choice(BVTYP_POOL),
                "KOINH": v["NAME1"],
                "BANKA": random.choice(BANK_NAME_POOL),
            })
    return pd.DataFrame(rows)


lfa1 = build_lfa1(CONFIG["n_vendors"])
lfb1 = build_lfb1(lfa1)
lfm1 = build_lfm1(lfa1)
lfbk = build_lfbk(lfa1)

# =====================================================================
# 4. MANDATORY FIELDS PER TABLE (used for missing-field injection & docs)
# =====================================================================

MANDATORY = {
    "LFA1": ["NAME1", "LAND1", "STCD1", "KTOKK", "PSTLZ"],
    "LFB1": ["AKONT", "ZTERM", "WAERS"],
    "LFM1": ["EKORG", "WAERS"],
    "LFBK": ["BANKL", "KOINH"],
}

ORG_KEY_COL = {"LFA1": None, "LFB1": "BUKRS", "LFM1": "EKORG", "LFBK": "BANKL"}


# =====================================================================
# 5. ANOMALY INJECTION FUNCTIONS
# =====================================================================

def inject_missing_mandatory(df, table):
    rate = CONFIG["error_rates"]["missing_mandatory"]
    n_hits = int(len(df) * rate)
    idxs = rng.choice(df.index, size=min(n_hits, len(df)), replace=False)
    for idx in idxs:
        field = random.choice(MANDATORY[table])
        df.at[idx, field] = ""
        org_key = df.at[idx, ORG_KEY_COL[table]] if ORG_KEY_COL[table] else ""
        log_issue(table, df.at[idx, "LIFNR"], org_key, field,
                   "Missing Mandatory Field",
                   f"{field} is mandatory for {table} but is blank.")
    return df


def inject_invalid_format(df, table, field, corrupt_fn, description):
    rate = CONFIG["error_rates"]["invalid_format"]
    eligible = df[df[field].astype(str).str.len() > 0].index
    n_hits = int(len(df) * rate)
    idxs = rng.choice(eligible, size=min(n_hits, len(eligible)), replace=False) if len(eligible) else []
    for idx in idxs:
        df.at[idx, field] = corrupt_fn(df.at[idx, field])
        org_key = df.at[idx, ORG_KEY_COL[table]] if ORG_KEY_COL[table] else ""
        log_issue(table, df.at[idx, "LIFNR"], org_key, field,
                   "Invalid Format", description)
    return df


def inject_duplicates(df, table):
    rate = CONFIG["error_rates"]["duplicate_records"]
    n_hits = max(1, int(len(df) * rate))
    idxs = rng.choice(df.index, size=min(n_hits, len(df)), replace=False)
    dup_rows = df.loc[idxs].copy()
    df = pd.concat([df, dup_rows], ignore_index=True)
    for _, row in dup_rows.iterrows():
        org_key = row[ORG_KEY_COL[table]] if ORG_KEY_COL[table] else ""
        key_desc = f"LIFNR={row['LIFNR']}" + (f", {ORG_KEY_COL[table]}={org_key}" if org_key != "" else "")
        log_issue(table, row["LIFNR"], org_key, "ALL",
                   "Duplicate Record",
                   f"Duplicate primary-key row in {table} ({key_desc}).")
    return df


def inject_orphan_fk(df, table, valid_lifnrs):
    rate = CONFIG["error_rates"]["orphan_fk"]
    n_hits = int(len(df) * rate)
    idxs = rng.choice(df.index, size=min(n_hits, len(df)), replace=False)
    for idx in idxs:
        fake_lifnr = f"{999000 + random.randint(0, 900):010d}"
        while fake_lifnr in valid_lifnrs:
            fake_lifnr = f"{999000 + random.randint(0, 900):010d}"
        org_key = df.at[idx, ORG_KEY_COL[table]] if ORG_KEY_COL[table] else ""
        old_lifnr = df.at[idx, "LIFNR"]
        df.at[idx, "LIFNR"] = fake_lifnr
        log_issue(table, fake_lifnr, org_key, "LIFNR",
                   "Orphan Foreign Key",
                   f"{table} row references LIFNR {fake_lifnr} (was {old_lifnr}) which does not exist in LFA1.")
    return df


def inject_cross_table_violations():
    rate = CONFIG["error_rates"]["cross_table_violation"]

    # Rule A: central deletion flag in LFA1 not propagated to LFB1
    n_hits = int(len(lfa1) * rate)
    idxs = rng.choice(lfa1.index, size=min(n_hits, len(lfa1)), replace=False)
    for idx in idxs:
        lfa1.at[idx, "LOEVM"] = "X"
        lifnr = lfa1.at[idx, "LIFNR"]
        child_rows = lfb1[lfb1["LIFNR"] == lifnr]
        if len(child_rows):
            crow = child_rows.iloc[0]
            lfb1.loc[crow.name, "LOEVM"] = ""  # deliberately NOT propagated
            log_issue("LFB1", lifnr, crow["BUKRS"], "LOEVM",
                       "Cross-Table Business Rule Violation",
                       "LFA1 marked for deletion (LOEVM=X) but LFB1 company code record is not flagged for deletion.")

    # Rule B: vendor blocked centrally (SPERR=X) but LFB1 payment block not set
    n_hits = int(len(lfa1) * rate)
    idxs = rng.choice(lfa1.index, size=min(n_hits, len(lfa1)), replace=False)
    for idx in idxs:
        lfa1.at[idx, "SPERR"] = "X"
        lifnr = lfa1.at[idx, "LIFNR"]
        child_rows = lfb1[lfb1["LIFNR"] == lifnr]
        if len(child_rows):
            crow = child_rows.iloc[0]
            lfb1.loc[crow.name, "ZAHLS"] = ""  # deliberately left un-blocked
            log_issue("LFB1", lifnr, crow["BUKRS"], "ZAHLS",
                       "Cross-Table Business Rule Violation",
                       "Vendor is centrally posting-blocked in LFA1 (SPERR=X) but LFB1 payment block (ZAHLS) is not set.")

    # Rule C: currency mismatch between LFB1 and LFM1 for the same vendor
    n_hits = int(len(lfa1) * rate)
    idxs = rng.choice(lfa1.index, size=min(n_hits, len(lfa1)), replace=False)
    for idx in idxs:
        lifnr = lfa1.at[idx, "LIFNR"]
        b_rows = lfb1[lfb1["LIFNR"] == lifnr]
        m_rows = lfm1[lfm1["LIFNR"] == lifnr]
        if len(b_rows) and len(m_rows):
            brow, mrow = b_rows.iloc[0], m_rows.iloc[0]
            wrong_currency = random.choice([c for c in set(v["currency"] for v in COUNTRIES.values())
                                             if c != mrow["WAERS"]])
            lfm1.loc[mrow.name, "WAERS"] = wrong_currency
            log_issue("LFM1", lifnr, mrow["EKORG"], "WAERS",
                       "Cross-Table Business Rule Violation",
                       f"LFM1 currency ({wrong_currency}) does not match LFB1 currency ({brow['WAERS']}) for the same vendor.")

    # Rule D: bank country (LFBK.BANKS) does not match vendor's LFA1 country
    n_hits = int(len(lfa1) * rate)
    idxs = rng.choice(lfa1.index, size=min(n_hits, len(lfa1)), replace=False)
    for idx in idxs:
        lifnr = lfa1.at[idx, "LIFNR"]
        vendor_country = lfa1.at[idx, "LAND1"]
        k_rows = lfbk[lfbk["LIFNR"] == lifnr]
        if len(k_rows):
            krow = k_rows.iloc[0]
            wrong_country = random.choice([c for c in COUNTRY_CODES if c != vendor_country])
            lfbk.loc[krow.name, "BANKS"] = wrong_country
            log_issue("LFBK", lifnr, krow["BANKL"], "BANKS",
                       "Cross-Table Business Rule Violation",
                       f"Bank country ({wrong_country}) does not match vendor country in LFA1 ({vendor_country}).")


# --- format corruption helper functions -------------------------------

def corrupt_iban(value):
    if value == "":
        return "INVALID"
    choice = random.choice(["truncate", "bad_chars", "no_prefix"])
    if choice == "truncate":
        return value[:6]
    if choice == "bad_chars":
        return value[:2] + "##" + value[4:]
    return value[2:]  # drop country prefix


def corrupt_tax_id(value):
    choice = random.choice(["truncate", "special_chars", "all_zero"])
    if choice == "truncate":
        return value[:3]
    if choice == "special_chars":
        return "!!" + value
    return "0" * max(len(value), 4)


def corrupt_postal_code(value):
    choice = random.choice(["letters_in_numeric", "too_short", "blank_spaces"])
    if choice == "letters_in_numeric":
        return "ABCDE"
    if choice == "too_short":
        return value[:2]
    return "   "


def corrupt_phone(value):
    choice = random.choice(["strip_plus", "letters", "too_short"])
    if choice == "strip_plus":
        return value.replace("+", "").replace("-", "")
    if choice == "letters":
        return "CALL-ME-MAYBE"
    return value[:4]


# =====================================================================
# 6. APPLY ANOMALIES (order matters: FK/dup integrity first, then formats)
# =====================================================================

valid_lifnrs = set(lfa1["LIFNR"])

lfa1 = inject_missing_mandatory(lfa1, "LFA1")
lfb1 = inject_missing_mandatory(lfb1, "LFB1")
lfm1 = inject_missing_mandatory(lfm1, "LFM1")
lfbk = inject_missing_mandatory(lfbk, "LFBK")

lfa1 = inject_invalid_format(lfa1, "LFA1", "STCD1", corrupt_tax_id,
                              "Tax ID does not match the expected country format.")
lfa1 = inject_invalid_format(lfa1, "LFA1", "PSTLZ", corrupt_postal_code,
                              "Postal code does not match the expected country format.")
lfa1 = inject_invalid_format(lfa1, "LFA1", "TELF1", corrupt_phone,
                              "Phone number format is invalid.")
lfbk = inject_invalid_format(lfbk, "LFBK", "IBAN", corrupt_iban,
                              "IBAN is malformed (wrong length, invalid characters, or missing country prefix).")

lfa1 = inject_duplicates(lfa1, "LFA1")
lfb1 = inject_duplicates(lfb1, "LFB1")
lfbk = inject_duplicates(lfbk, "LFBK")

lfb1 = inject_orphan_fk(lfb1, "LFB1", valid_lifnrs)
lfm1 = inject_orphan_fk(lfm1, "LFM1", valid_lifnrs)
lfbk = inject_orphan_fk(lfbk, "LFBK", valid_lifnrs)

inject_cross_table_violations()

# =====================================================================
# 7. DATA DICTIONARY
# =====================================================================

data_dictionary_rows = [
    # LFA1
    ("LFA1", "LIFNR", "Vendor Number (primary key)", "Yes", "0000100001"),
    ("LFA1", "NAME1", "Vendor name (line 1)", "Yes", "Atlas Industries Inc."),
    ("LFA1", "NAME2", "Vendor name (line 2, optional)", "No", ""),
    ("LFA1", "SORTL", "Sort field / search term", "No", "ATLASINDUS"),
    ("LFA1", "STRAS", "Street and house number", "No", "123 Main St"),
    ("LFA1", "ORT01", "City", "No", "Chicago"),
    ("LFA1", "PSTLZ", "Postal code", "Yes", "60601"),
    ("LFA1", "LAND1", "Country key (ISO2)", "Yes", "US"),
    ("LFA1", "REGIO", "Region / state code", "No", "IL"),
    ("LFA1", "SPRAS", "Language key", "No", "EN"),
    ("LFA1", "STCD1", "Tax number 1 (country-specific)", "Yes", "12-3456789"),
    ("LFA1", "STCD2", "Tax number 2", "No", ""),
    ("LFA1", "KTOKK", "Vendor account group", "Yes", "KRED"),
    ("LFA1", "TELF1", "Telephone number", "No", "+1-312-5551234"),
    ("LFA1", "ERDAT", "Record creation date", "No", "2019-05-14"),
    ("LFA1", "ERNAM", "Created by (user)", "No", "DATALOAD"),
    ("LFA1", "SPERR", "Central posting block (X = blocked)", "No", ""),
    ("LFA1", "LOEVM", "Central deletion flag (X = flagged)", "No", ""),
    # LFB1
    ("LFB1", "LIFNR", "Vendor Number (FK to LFA1)", "Yes", "0000100001"),
    ("LFB1", "BUKRS", "Company code (part of primary key)", "Yes", "1000"),
    ("LFB1", "AKONT", "Reconciliation account (GL)", "Yes", "160000"),
    ("LFB1", "ZWELS", "Payment methods", "No", "T"),
    ("LFB1", "ZTERM", "Payment terms key", "Yes", "NT30"),
    ("LFB1", "WAERS", "Currency key", "Yes", "USD"),
    ("LFB1", "ZAHLS", "Payment block for company code (X = blocked)", "No", ""),
    ("LFB1", "LOEVM", "Company-code-level deletion flag", "No", ""),
    ("LFB1", "ERDAT", "Record creation date", "No", "2019-05-14"),
    # LFM1
    ("LFM1", "LIFNR", "Vendor Number (FK to LFA1)", "Yes", "0000100001"),
    ("LFM1", "EKORG", "Purchasing organization (part of primary key)", "Yes", "EK01"),
    ("LFM1", "WAERS", "Purchasing currency", "Yes", "USD"),
    ("LFM1", "ZTERM", "Payment terms key (purchasing org level)", "No", "NT30"),
    ("LFM1", "INCO1", "Incoterms part 1", "No", "FOB"),
    ("LFM1", "INCO2", "Incoterms part 2 (location)", "No", ""),
    ("LFM1", "VERKF", "Vendor's sales contact / representative", "No", "JSMITHX"),
    ("LFM1", "TELF1", "Purchasing contact telephone", "No", "+1-312-5551234"),
    ("LFM1", "SPERM", "Purchasing block", "No", ""),
    ("LFM1", "LOEVM", "Purchasing-org-level deletion flag", "No", ""),
    # LFBK
    ("LFBK", "LIFNR", "Vendor Number (FK to LFA1)", "Yes", "0000100001"),
    ("LFBK", "BANKS", "Bank country key", "Yes", "US"),
    ("LFBK", "BANKL", "Bank key / routing number (part of primary key)", "Yes", "12345678"),
    ("LFBK", "BANKN", "Bank account number (non-IBAN countries)", "No", "1234567890"),
    ("LFBK", "IBAN", "IBAN (IBAN-using countries)", "No", "DE12345678901234567890"),
    ("LFBK", "BKONT", "Bank control key", "No", "00"),
    ("LFBK", "BVTYP", "Partner bank type", "No", "0001"),
    ("LFBK", "KOINH", "Account holder name", "Yes", "Atlas Industries Inc."),
    ("LFBK", "BANKA", "Bank name", "No", "First National Bank"),
]

data_dictionary = pd.DataFrame(
    data_dictionary_rows,
    columns=["Table", "Field", "Description", "Mandatory", "Example"],
)

# =====================================================================
# 8. FINALIZE & EXPORT
# =====================================================================

answer_key = pd.DataFrame(answer_key_rows,
                           columns=["Table", "LIFNR", "Org_Key", "Field", "Issue_Type", "Issue_Description"])

# Shuffle child tables so injected duplicate/orphan rows aren't all at the tail
lfb1 = lfb1.sample(frac=1, random_state=CONFIG["seed"]).reset_index(drop=True)
lfm1 = lfm1.sample(frac=1, random_state=CONFIG["seed"]).reset_index(drop=True)
lfbk = lfbk.sample(frac=1, random_state=CONFIG["seed"]).reset_index(drop=True)
lfa1 = lfa1.sample(frac=1, random_state=CONFIG["seed"]).reset_index(drop=True)

import os
os.makedirs(CONFIG["output_dir"], exist_ok=True)

lfa1.to_csv(f"{CONFIG['output_dir']}/LFA1.csv", index=False)
lfb1.to_csv(f"{CONFIG['output_dir']}/LFB1.csv", index=False)
lfm1.to_csv(f"{CONFIG['output_dir']}/LFM1.csv", index=False)
lfbk.to_csv(f"{CONFIG['output_dir']}/LFBK.csv", index=False)
data_dictionary.to_csv(f"{CONFIG['output_dir']}/Data_Dictionary.csv", index=False)
answer_key.to_csv(f"{CONFIG['output_dir']}/Answer_Key.csv", index=False)

print("Row counts:")
print(f"  LFA1  : {len(lfa1)}")
print(f"  LFB1  : {len(lfb1)}")
print(f"  LFM1  : {len(lfm1)}")
print(f"  LFBK  : {len(lfbk)}")
print(f"  Data_Dictionary : {len(data_dictionary)}")
print(f"  Answer_Key      : {len(answer_key)}")
print("\nAnswer_Key issue type breakdown:")
print(answer_key["Issue_Type"].value_counts())
print("\nAnswer_Key by table:")
print(answer_key["Table"].value_counts())
