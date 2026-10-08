"""Hybrid skill reuse: exact table + column first, otherwise a skill matched by MEANING.

A promoted skill is a human-approved check. ``cache_runner.run_cached_skills`` runs it on the exact table
and column it was learned on. That is closed to names: a skill learned on LFBK.IBAN never helps a table
that calls the same thing ``Iban_no``, nor any non-SAP structure. This module is the fallback for a column
with no exact skill:

1. Describe the column the way the mapping already understands it - its name, its concept (POSTAL_CODE,
   EMAIL, AMOUNT, ...) from ``column_mapping`` and its dictionary text - and describe each promoted skill by
   what it checks (its hypothesis). Compare them with the local embedding model (free, offline).
2. Accept only a clear winner: cosine >= ``cache.similarity_min_score`` and a lead over the next DIFFERENT
   skill of ``similarity_min_margin`` (skills that are near-copies of each other do not count as rivals).
3. Adapt the skill's code to the column: its own column name is renamed to this column's, and the result
   must pass ``preflight`` against the real tables - so a skill that reads other columns or tables that do
   not exist here is dropped, never guessed.
4. Run it like any cached skill (sandbox, reflector, human review), and drop the finding if it flags more
   than ``similarity_max_flag_ratio`` of the rows: a skill applied to the wrong kind of column flags nearly
   everything.

Nothing here trusts the match: it only decides which skills are worth trying. The check is then run on
the real data, judged, and reviewed by a person, and its code is stored on the finding.
"""

import ast
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd

from src.agents.config import Config
from src.agents.logging_config import get_logger
from src.agents.memory import skill_registry as registry
from src.agents.metrics import metrics
from orchestrator.preflight import preflight

logger = get_logger("skill_reuse")

# What a mapped concept means in words: the embedding model understands "postal code", not "POSTAL_CODE".
CONCEPT_WORDS = {
    "KEY": "identifier", "ORG_UNIT": "organisational unit", "DELETION_FLAG": "deletion flag",
    "BLOCK_FLAG": "block flag", "CREATED_DATE": "creation date", "DATE": "date", "COUNTRY": "country",
    "POSTAL_CODE": "postal code", "CITY": "city", "STREET": "street address", "TAX_ID": "tax number",
    "LEGAL_NAME": "name", "SEARCH_TERM": "search term", "EMAIL": "e-mail address", "PHONE": "phone number",
    "AMOUNT": "amount of money", "QUANTITY": "quantity", "CURRENCY": "currency", "CODE": "code",
    "TEXT": "free text", "OTHER": "",
    "BANK_KEY": "bank key", "BANK_ACCOUNT": "bank account number", "IBAN": "IBAN",
}
_NEAR_COPY = 0.90   # two skills this alike are the same kind of check, not rivals


def column_words(name: str) -> str:
    """'Iban_no' / 'ibanNo' / 'ACCOUNT-HOLDER' -> 'iban no' / 'iban no' / 'account holder'."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return re.sub(r"[_\W]+", " ", spaced).strip().lower()


def candidate_text(column: str, concept: Optional[str], meaning: Optional[str]) -> str:
    """The column as the embedding model sees it: its name in words, its concept in words, its dictionary text."""
    meaning = "" if not meaning or meaning.lower().startswith("no description") else meaning
    return ", ".join(x for x in (column_words(column), CONCEPT_WORDS.get(str(concept or "OTHER").upper(), ""), meaning) if x)


def skill_text(skill: Dict[str, Any]) -> str:
    """A skill as the embedding model sees it: what it checks."""
    return (skill.get("hypothesis") or skill.get("description") or column_words(skill.get("column", ""))).strip()


# ---------------------------------------------------------------------------
# Adapting a skill's code to another column
# ---------------------------------------------------------------------------

def adapt_code(code: str, old_column: str, new_column: str, df: pd.DataFrame,
               all_tables: Dict[str, pd.DataFrame]) -> Tuple[Optional[str], List[str]]:
    """The skill's code with its own column renamed, or (None, why) when it cannot run here.

    Every string constant equal to the old column name becomes the new one (`df['IBAN']`, `.set_index('IBAN')`,
    `key_field: 'IBAN'`). Whatever else the code reads - another column, another table - must exist in this
    upload under the same name: `preflight` checks that against the real tables, which is what makes the
    adaptation safe rather than a guess."""
    if not code or not code.strip():
        return None, ["no code"]
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return None, [f"does not parse ({exc.msg})"]

    replaced = [0]

    class Rename(ast.NodeTransformer):
        def visit_Constant(self, node: ast.Constant):
            if isinstance(node.value, str) and node.value == old_column:
                replaced[0] += 1
                return ast.copy_location(ast.Constant(new_column), node)
            return node

    adapted = ast.unparse(ast.fix_missing_locations(Rename().visit(tree)))
    if old_column != new_column and replaced[0] == 0:
        return None, [f"never mentions its own column '{old_column}', so there is nothing to rename"]
    problems = preflight(adapted, df, all_tables)
    return (None, problems) if problems else (adapted, [])


def adapted_hypothesis(skill: Dict[str, Any], new_column: str) -> str:
    """The skill's hypothesis, speaking about this column, and saying where it came from."""
    hyp = skill.get("hypothesis") or skill.get("description") or ""
    old = skill.get("column") or ""
    if old and old != new_column:
        hyp = re.sub(rf"\b{re.escape(old)}\b", new_column, hyp, flags=re.IGNORECASE)
    return f"{hyp} [adapted from the skill on {skill.get('table')}.{skill.get('column')}]"


# ---------------------------------------------------------------------------
# Matching by meaning
# ---------------------------------------------------------------------------

class SkillMatcher:
    """Embeds the promoted skills once and ranks them against candidate columns."""

    def __init__(self, memory_store: Any):
        self.store = memory_store
        self._vectors: Dict[Tuple[str, str], np.ndarray] = {}   # (skill_id, text) -> vector

    def _skill_vectors(self, skills: Sequence[Dict[str, Any]]) -> np.ndarray:
        missing = [(s["skill_id"], skill_text(s)) for s in skills if (s["skill_id"], skill_text(s)) not in self._vectors]
        if missing:
            for key, vec in zip(missing, self.store.embed([t for _, t in missing])):
                self._vectors[key] = np.asarray(vec, dtype=float)
        return np.array([self._vectors[(s["skill_id"], skill_text(s))] for s in skills])

    def best_skills(self, candidates: Dict[str, str], skills: Sequence[Dict[str, Any]],
                    covered: Optional[Set[Tuple[str, str]]] = None) -> Dict[str, Tuple[Dict[str, Any], float, float]]:
        """{column: (skill, score, margin)} for every candidate that has a clear winning skill.

        `candidates` maps column -> its text (see candidate_text). `covered` holds {(COLUMN, pillar)} the
        client already has a finding for: a winner that would only repeat one of those is dropped."""
        if not candidates or not skills:
            return {}
        columns = list(candidates)
        cand = np.array([np.asarray(v, dtype=float) for v in self.store.embed([candidates[c] for c in columns])])
        skill_vecs = self._skill_vectors(skills)
        scores = cand @ skill_vecs.T                      # vectors are L2-normalised: dot product = cosine
        skill_sim = skill_vecs @ skill_vecs.T
        out: Dict[str, Tuple[Dict[str, Any], float, float]] = {}
        for ci, column in enumerate(columns):
            order = np.argsort(-scores[ci])
            best = int(order[0])
            best_score = float(scores[ci][best])
            rivals = [float(scores[ci][int(j)]) for j in order[1:] if skill_sim[best][int(j)] < _NEAR_COPY]
            margin = best_score - (rivals[0] if rivals else 0.0)
            if best_score < Config.SIMILARITY_MIN_SCORE or margin < Config.SIMILARITY_MIN_MARGIN:
                continue
            skill = skills[best]
            pillar = registry.skill_classification(skill)["category"]
            if covered and (column.upper(), pillar) in covered:
                continue
            out[column] = (skill, best_score, margin)
        return out


def reuse_by_similarity(table_name: str, df: pd.DataFrame, all_tables: Dict[str, pd.DataFrame],
                        columns: Sequence[str], concepts: Dict[str, str], meanings: Dict[str, str],
                        memory_store: Any, reflector_llm: Optional[Any] = None,
                        covered: Optional[Set[Tuple[str, str]]] = None,
                        rule_coverage: Optional[Any] = None
                        ) -> Tuple[List[Dict[str, Any]], Set[Tuple[str, str]]]:
    """Findings from promoted skills matched by meaning to `columns` (columns that have no exact skill),
    and {(COLUMN, pillar)} they cover so the planner does not propose the same check again.

    A match is skipped when the built-in rule engine already checks that column and pillar
    (`rule_coverage`, the same filter the planner's checks go through): it would only repeat a finding.

    Never raises: any failure (no embedding model, a broken registry) only means no reuse this run."""
    if not Config.SIMILARITY_REUSE or memory_store is None or not columns:
        return [], set()
    try:
        skills = [s for s in registry.get_all_skills() if s.get("check_code")]
        if not skills:
            return [], set()
        matcher = SkillMatcher(memory_store)
        candidates = {c: candidate_text(c, concepts.get(c), meanings.get(c)) for c in columns}
        # A skill learned on this very column is the exact path's job, not a similar one.
        skills = [s for s in skills if not (str(s["table"]).upper() == table_name.upper() and s["column"] in candidates)]
        matches = matcher.best_skills(candidates, skills, covered)
    except NotImplementedError:
        logger.info("The vector backend does not expose embeddings - skill reuse by similarity is off")
        return [], set()
    except Exception as exc:
        logger.warning("[%s] skill matching by similarity failed (%s) - continuing without it", table_name, exc)
        return [], set()

    from orchestrator.cache_runner import run_adapted_skill     # local import: cache_runner is the executor, this is the matcher

    findings: List[Dict[str, Any]] = []
    cover: Set[Tuple[str, str]] = set()
    ranked = sorted(matches.items(), key=lambda kv: -kv[1][1])[:max(Config.SIMILARITY_MAX_PER_TABLE, 0)]
    for column, (skill, score, margin) in ranked:
        cls = registry.skill_classification(skill)
        if rule_coverage is not None and rule_coverage.covers(column.upper(), cls["category"], cls["sub_type"],
                                                              include_soft=False):
            metrics.skills_similarity_rejected += 1
            logger.info("[%s.%s] similar skill on %s.%s skipped: the built-in rules already validate this column (%s)",
                        table_name, column, skill["table"], skill["column"], cls["category"])
            continue
        code, why = adapt_code(skill["check_code"], skill["column"], column, df, all_tables)
        if code is None:
            metrics.skills_similarity_rejected += 1
            logger.info("[%s.%s] similar skill on %s.%s (cosine %.2f) not usable here: %s", table_name, column,
                        skill["table"], skill["column"], score, "; ".join(why)[:200])
            continue
        detail = None
        if skill.get("detail_code"):
            detail, _ = adapt_code(skill["detail_code"], skill["column"], column, df, all_tables)   # None = no row detail
        logger.info("[%s.%s] matched by meaning to the skill on %s.%s (cosine %.2f, lead %.2f)", table_name, column,
                    skill["table"], skill["column"], score, margin)
        got = run_adapted_skill(skill, code, detail, adapted_hypothesis(skill, column), score, table_name, column,
                                df, all_tables, reflector_llm)
        findings.extend(got)
        cover.add((column.upper(), registry.skill_classification(skill)["category"]))
    return findings, cover
