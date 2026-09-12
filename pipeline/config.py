"""Runtime configuration. Reads .env (if present) then environment variables."""
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE_DIR = pathlib.Path(os.environ.get("STATE_DIR", ROOT / "state"))


def _load_dotenv() -> None:
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

SITE_BASE = os.environ.get("SITE_BASE", "https://analyst-assessment-production.up.railway.app").rstrip("/")
API_BASE = os.environ.get("CRM_API_BASE", SITE_BASE + "/api/v1").rstrip("/")
TOKEN = os.environ.get("CRM_TOKEN", "")

# The operator we are reconciling. The parent account is discovered by name at
# runtime so the pipeline works against any sandbox copy; the env var is an override.
OPERATOR_NAME = os.environ.get("OPERATOR_NAME", "Bellhaven Senior Living")
PARENT_ID_OVERRIDE = os.environ.get("BELLHAVEN_PARENT_ID", "")

# Matching thresholds (see match.py for how the score is built)
CONFIDENT = 0.80   # >= : treat as the same facility
POSSIBLE = 0.55    # >= : surface to reviewer as a low-confidence match
# < POSSIBLE      : not the same facility

# The number on a community's own page is its public number, so a differing CRM
# phone is proposed as a fix (on the surviving account only). Set to 0 to use phone
# purely as a matching signal.
PROPOSE_PHONE_UPDATES = os.environ.get("PROPOSE_PHONE_UPDATES", "1") == "1"

PORT = int(os.environ.get("PORT", "5055"))

# Files
SITE_SNAPSHOT = STATE_DIR / "site_locations.json"
CRM_SNAPSHOT = STATE_DIR / "crm_accounts.json"
PROPOSALS = STATE_DIR / "proposals.json"
DECISIONS = STATE_DIR / "decisions.json"
RUN_LOG = STATE_DIR / "runs.jsonl"
