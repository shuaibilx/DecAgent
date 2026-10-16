"""Explicit local plugin administration and authenticated business task commands."""

import json
from pathlib import Path
from uuid import UUID

from decagent.business.codec import strict_json
from decagent.business.employee_accounts import seed_employee_accounts
from decagent.business.registry import PluginRegistry
from decagent.storage.database import Database


def add_commands(commands):
    plugins = commands.add_parser("plugins", help="Manage trusted installed business plugins")
    plugin_actions = plugins.add_subparsers(dest="plugin_action", required=True)
    plugin_actions.add_parser("list")
    inspect = plugin_actions.add_parser("inspect", help="Inspect the complete registered contract")
    inspect.add_argument("scope_id")
    register = plugin_actions.add_parser("register")
    register.add_argument("manifest", help="Local trusted plugin manifest JSON")
    plugin_actions.add_parser("install-tickets", help="Register the packaged service-ticket plugin")
    disable = plugin_actions.add_parser("disable")
    disable.add_argument("scope_id")
    commands.add_parser("seed-tickets", help="Explicitly initialize the synthetic people directory")
    business = commands.add_parser("business", help="Manage local employee accounts")
    actions = business.add_subparsers(dest="business_action", required=True)
    accounts = actions.add_parser("accounts", help="Manage synthetic employee accounts locally")
    account_actions = accounts.add_subparsers(dest="account_action", required=True)
    account_actions.add_parser(
        "seed", help="Create initial employee accounts and private passwords"
    )


def run_command(settings, args):
    accounts_seed = args.command == "business" and args.business_action == "accounts"
    if args.command in {"plugins", "seed-tickets"} or accounts_seed:
        database = Database(settings.database_url)
        database.check_version("business-2", task_kind="business")
        if accounts_seed:
            result = seed_employee_accounts(database, settings.data_dir / "demo-auth")
        elif args.command == "seed-tickets":
            from decagent.business.tickets.store import TicketStore

            result = TicketStore(database.url).seed_directory()
            if not result["ok"]:
                raise ValueError("Synthetic directory initialization failed")
        elif args.plugin_action == "list":
            registry = PluginRegistry(database)
            result = [
                {
                    key: row[key]
                    for key in ("id", "plugin_id", "plugin_version", "manifest_digest", "enabled")
                }
                for row in registry.list()
            ]
        elif args.plugin_action == "inspect":
            registry = PluginRegistry(database)
            result = registry.inspect(UUID(args.scope_id))
        else:
            registry = PluginRegistry(database)
            if args.plugin_action == "disable":
                row = registry.disable(UUID(args.scope_id))
            else:
                if args.plugin_action == "install-tickets":
                    from decagent.business.tickets import builtin_manifest

                    manifest = builtin_manifest()
                else:
                    manifest = strict_json(Path(args.manifest).read_text(encoding="utf-8"))
                row = registry.register(manifest)
            result = {
                key: row[key]
                for key in ("id", "plugin_id", "plugin_version", "manifest_digest", "enabled")
            }
    else:
        raise ValueError("Unsupported local business administration command")
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
