import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

AWS_ACCESS_KEY_ID = os.environ["AWS_ACCESS_KEY_ID"]
AWS_SECRET_ACCESS_KEY = os.environ["AWS_SECRET_ACCESS_KEY"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

SES_SENDER_EMAIL = os.environ["SES_SENDER_EMAIL"]
TEAM_EMAILS = [e.strip() for e in os.environ["TEAM_EMAILS"].split(",")]

# Funder directory filter criteria
# Primary: exact sector match (as labelled in the Segal directory)
# "Quality Education" is Ubongo's focus area (maps to UN SDG 4).
# The actual count of matching funders will be determined on first run.
EDUCATION_SECTORS = ["quality education"]

# Secondary: geography — keep funders who work in Africa
# These are checked case-insensitively against the geography column.
AFRICA_KEYWORDS = [
    "africa",
    "sub-saharan",
    "sub saharan",
    "east africa",
    "west africa",
    "central africa",
    "southern africa",
    "north africa",
    # common individual country names that may appear as geography tags
    "kenya", "tanzania", "uganda", "ethiopia", "rwanda", "nigeria",
    "ghana", "senegal", "mozambique", "zambia", "zimbabwe", "malawi",
    "somalia", "south africa", "cameroon", "côte d'ivoire", "ivory coast",
]

# Crawler settings
CRAWLER_BATCH_SIZE = 10          # concurrent browser tabs per batch
CRAWLER_PAGE_TIMEOUT_MS = 30000  # 30 seconds per page load
CONTENT_MAX_CHARS = 24000        # ~6000 tokens; truncate before sending to Claude

# Claude model selection
CLAUDE_FAST_MODEL = "claude-haiku-4-5-20251001"   # used for all opportunity detection
CLAUDE_CAREFUL_MODEL = "claude-sonnet-4-6"          # escalation for low-confidence cases
