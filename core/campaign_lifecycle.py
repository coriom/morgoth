"""One SQL definition of campaign eligibility, evaluated by the database clock."""

# Table names are fixed identifiers, never user input. All callers use the
# Project-selected search_path; there is no fallback to public.
CAMPAIGN_LIVE_SQL = (
    "campaigns.status = 'active' AND campaigns.ended_at IS NULL "
    "AND campaigns.ends_at > CURRENT_TIMESTAMP"
)
OBJECTIVE_RESEARCH_SQL = (
    "(objectives.campaign_id IS NULL OR EXISTS ("
    "SELECT 1 FROM campaigns WHERE campaigns.campaign_id = objectives.campaign_id "
    f"AND ({CAMPAIGN_LIVE_SQL})))"
)
