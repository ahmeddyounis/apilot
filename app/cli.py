"""Admin CLI: `uv run python -m app.cli keys create --name demo --scopes ingest,read`."""

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import typer
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import dispose_engine, get_sessionmaker
from app.core.security import generate_api_key, parse_scopes

if TYPE_CHECKING:
    from app.models import ApiKey

cli = typer.Typer(help="APilot admin CLI", no_args_is_help=True)
keys_cli = typer.Typer(help="Manage API keys", no_args_is_help=True)
cli.add_typer(keys_cli, name="keys")


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    async def wrapper() -> T:
        try:
            return await coro
        finally:
            await dispose_engine()

    return asyncio.run(wrapper())


@keys_cli.command("create")
def keys_create(
    name: str = typer.Option(..., help="Human-readable key name"),
    # Least privilege by default: a key that can submit invoices and read them
    # back. Approving payments and pulling the accounting export are separate
    # grants you have to ask for.
    scopes: str = typer.Option("ingest,read", help="Comma-separated scopes"),
    expires_days: int | None = typer.Option(
        None, help="Days until the key stops working (default: never)"
    ),
) -> None:
    """Mint a new API key. The secret is printed once and never stored."""
    from app.models import ApiKey

    try:
        parsed_scopes = parse_scopes(scopes)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    generated = generate_api_key(get_settings().env)

    async def create() -> UUID:
        async with get_sessionmaker()() as session:
            api_key = ApiKey(
                name=name,
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                scopes=parsed_scopes,
                expires_at=(
                    datetime.now(UTC) + timedelta(days=expires_days)
                    if expires_days is not None
                    else None
                ),
            )
            session.add(api_key)
            await session.commit()
            return api_key.id

    key_id = _run(create())
    typer.echo(f"API key created: id={key_id} name={name} scopes={','.join(parsed_scopes)}")
    typer.echo(generated.full_key)
    typer.echo("Store this key now — it will not be shown again.")


def _key_status(row: "ApiKey") -> str:
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
        return "expired"
    if row.expires_at is not None:
        return f"expires {row.expires_at:%Y-%m-%d}"
    return "active"


@keys_cli.command("list")
def keys_list() -> None:
    from app.models import ApiKey

    async def fetch() -> list[tuple[str, str, str, str, str]]:
        async with get_sessionmaker()() as session:
            rows = (await session.scalars(select(ApiKey).order_by(ApiKey.created_at))).all()
            return [
                (
                    str(row.id),
                    row.name,
                    row.prefix + "…",
                    ",".join(row.scopes),
                    _key_status(row),
                )
                for row in rows
            ]

    rows = _run(fetch())
    if not rows:
        typer.echo("No API keys.")
        return
    for row in rows:
        typer.echo("  ".join(row))


@keys_cli.command("revoke")
def keys_revoke(key_id: str = typer.Argument(..., help="API key id (UUID)")) -> None:
    from app.models import ApiKey

    async def revoke() -> bool:
        async with get_sessionmaker()() as session:
            api_key = await session.get(ApiKey, UUID(key_id))
            if api_key is None:
                return False
            api_key.revoked_at = datetime.now(UTC)
            await session.commit()
            return True

    if _run(revoke()):
        typer.echo(f"Revoked {key_id}")
    else:
        typer.echo(f"error: no API key with id {key_id}", err=True)
        raise typer.Exit(code=1)


@keys_cli.command("rotate")
def keys_rotate(
    key_id: str = typer.Argument(..., help="API key id (UUID) to supersede"),
    grace_hours: int = typer.Option(
        24, help="How long the old key keeps working while callers cut over"
    ),
) -> None:
    """Mint a replacement with the same name and scopes, and expire the old one.

    Revoking outright would break every caller at the instant of rotation, so the
    old key expires after a grace window instead. The link between them is recorded
    on the old row, so the audit trail follows one credential across the rotation
    rather than showing two unrelated keys.
    """
    from app.models import ApiKey

    async def rotate() -> tuple[str, str] | None:
        async with get_sessionmaker()() as session:
            old = await session.get(ApiKey, UUID(key_id))
            if old is None:
                return None
            generated = generate_api_key(get_settings().env)
            new = ApiKey(
                name=old.name,
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                scopes=list(old.scopes),
            )
            session.add(new)
            await session.flush()
            old.expires_at = datetime.now(UTC) + timedelta(hours=grace_hours)
            old.replaced_by_id = new.id
            await session.commit()
            return str(new.id), generated.full_key

    result = _run(rotate())
    if result is None:
        typer.echo(f"error: no API key with id {key_id}", err=True)
        raise typer.Exit(code=1)
    new_id, full_key = result
    typer.echo(f"Rotated {key_id} -> {new_id}; the old key stops working in {grace_hours}h")
    typer.echo(full_key)
    typer.echo("Store this key now — it will not be shown again.")


if __name__ == "__main__":
    cli()
