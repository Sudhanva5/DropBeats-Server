"""One-shot export of Supabase licences to JSON, for loading into Railway.

Kept in the repo rather than run ad hoc so the migration stays auditable after
the fact. Reads credentials from the environment; nothing is hardcoded.

PostgREST rather than pg_dump because there is no Postgres client configured
against the Supabase project and no Supabase CLI installed on the machine that
ran this.

Usage:
    set -a; . ../../Supabase/.env; set +a
    python3 export_supabase.py > licences.json
"""

import json
import os
import sys
import urllib.request

# device_id and last_country are deliberately omitted: the Railway schema drops
# both. device_id was never written and last_country was never read.
COLUMNS = (
    "email,full_name,phone_number,country,license_key,sale_id,"
    "is_active,is_beta,has_completed_onboarding,last_login,created_at"
)


def main() -> int:
    try:
        base = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    except KeyError as missing:
        print(f"{missing} is not set; source Supabase/.env first", file=sys.stderr)
        return 1

    request = urllib.request.Request(
        f"{base}/rest/v1/licenses?select={COLUMNS}&order=created_at.asc",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        rows = json.load(response)

    json.dump(rows, sys.stdout, indent=2)
    print(f"\nexported {len(rows)} licences", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
