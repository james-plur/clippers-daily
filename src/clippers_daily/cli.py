from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from .app import run_daily
from .collectors import collect_source
from .config import Settings
from .storage import Store
from .maintenance import maintain


def doctor(settings: Settings) -> int:
    email = settings.runtime.get("email", {})
    sender = next((item for item in email.get("senders", []) if item.get("id") == email.get("active_sender", "default")), {})
    checks = {
        "config": all((settings.config_dir / name).exists() for name in ("runtime.yaml", "coporations.yaml", "media.yaml", "papers.yaml")),
        "llm_key": any(os.getenv(item.get("api_key_env", "")) for item in settings.runtime.get("llm", {}).get("providers", [])),
        "smtp_sender": bool(os.getenv(sender.get("username_env", "CLIPPERS_SMTP_SENDER"))),
        "smtp_password_file": Path(sender.get("password_file", "/nonexistent")).is_file(),
        "paperlab_database": Path(settings.runtime.get("paperlab", {}).get("database", "/nonexistent")).is_file(),
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="clippers")
    sub = parser.add_subparsers(dest="command", required=True)
    daily = sub.add_parser("daily")
    daily.add_argument("--date", default=date.today().isoformat())
    daily.add_argument("--no-send", action="store_true")
    daily.add_argument("--force-send", action="store_true")
    source = sub.add_parser("source-test")
    source.add_argument("source_id")
    source.add_argument("--limit", type=int, default=20)
    sub.add_parser("doctor")
    sub.add_parser("migrate")
    sub.add_parser("maintain")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "doctor":
        raise SystemExit(doctor(settings))
    if args.command == "migrate":
        store = Store(settings.database)
        history = store.backfill_digest_editions(settings.output_dir)
        store.db.close()
        print(json.dumps({"ok": True, "database": str(settings.database), "history": history}, ensure_ascii=False))
        return
    if args.command == "maintain":
        print(json.dumps(maintain(settings), ensure_ascii=False, indent=2, default=str))
        return
    if args.command == "source-test":
        try:
            records, reports = collect_source(settings, args.source_id)
        except ValueError as exc:
            parser.error(str(exc))
        selected = [record.model_dump(mode="json") for record in records if record.source_id == args.source_id][:args.limit]
        report = [item.model_dump(mode="json") for item in reports if item.source_id == args.source_id]
        print(json.dumps({"records": selected, "reports": report}, ensure_ascii=False, indent=2, default=str))
        return
    markdown, html, reports = run_daily(settings, date.fromisoformat(args.date), not args.no_send, args.force_send)
    failed = [r.model_dump(mode="json") for r in reports if r.status not in {"success", "not_modified", "available", "delegated_to_papers"}]
    print(json.dumps({"markdown": str(markdown), "html": str(html), "degraded_sources": failed}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
