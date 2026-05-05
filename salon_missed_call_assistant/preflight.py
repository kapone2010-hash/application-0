from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app


def env_status(name: str) -> str:
    return "SET" if os.getenv(name) else "MISSING"


def main() -> int:
    app.init_db()
    print("Salon missed-call assistant preflight")
    print(f"database={app.DB_PATH}")
    print(f"salons={len(app.salons_df(active_only=True))}")
    for name in (
        "SALON_STAFF_PASSCODE",
        "SALON_WEBHOOK_SECRET",
        "SALON_REQUIRE_WEBHOOK_SECRET",
        "SALON_CONSENT_POLICY_APPROVED",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "SALON_DATABASE_URL",
    ):
        print(f"{name}={env_status(name)}")
    for row in app.salons_df(active_only=True).to_dict("records"):
        sid = int(row["id"])
        report = app.salon_setup_report(sid)
        ready = sum(1 for item in report if item["ready"])
        print(f"\n{row['name']} [{sid}] readiness: {ready}/{len(report)}")
        for item in report:
            if not item["ready"]:
                print(f"- {item['area']}: {item['next_step']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
