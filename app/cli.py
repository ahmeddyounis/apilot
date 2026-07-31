"""Admin CLI: `uv run python -m app.cli keys create --name demo --scopes ingest,read`."""

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import typer
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import dispose_engine, get_sessionmaker
from app.core.security import generate_api_key, parse_scopes

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
    scopes: str = typer.Option("ingest,read,review,export", help="Comma-separated scopes"),
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
            )
            session.add(api_key)
            await session.commit()
            return api_key.id

    key_id = _run(create())
    typer.echo(f"API key created: id={key_id} name={name} scopes={','.join(parsed_scopes)}")
    typer.echo(generated.full_key)
    typer.echo("Store this key now — it will not be shown again.")


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
                    "revoked" if row.revoked_at else "active",
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


if __name__ == "__main__":
    cli()
