"""
Rotate the shared secrets in this deployment's environment files.

Run when a secret has been exposed -- a populated .env in a build context, a
key pasted somewhere, someone leaving -- and after any incident where you
cannot prove it was not. It rewrites the values in place across every file that
carries them, keeping the three copies of each shared secret in step, and it
never prints one.

    python scripts/rotate_secrets.py [--only NAME,NAME] [--dry-run]

Deliberately NOT rotated by default:

  WHATSAPP_TOKEN_ENCRYPTION_KEY
      The Fernet key that stored WhatsApp provider tokens and tenants' own AI
      API keys are encrypted *with*. Replacing it does not re-encrypt anything;
      it makes every stored credential permanently unreadable. Rotating it is a
      migration (decrypt with the old key, re-encrypt with the new, in one
      pass), not an edit to a file, and belongs behind a management command
      that does exactly that.

  Third-party API keys (OPENAI_API_KEY, EMAIL_HOST_PASSWORD, Twilio, ...)
      Nothing here can issue those. They are rotated in the provider's own
      console and pasted back; this script only ever handles secrets this
      deployment mints for itself.

After running: DB_PASSWORD must also be changed on the database role itself
(the value in POSTGRES_PASSWORD only applies when the data directory is first
created), and every service has to be restarted so it reads the new values.
The script prints those steps when it changes something that needs them.
"""

import argparse
import re
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def django_secret_key() -> str:
    # Django's own get_random_secret_key alphabet, without importing Django.
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)"
    return "".join(secrets.choice(alphabet) for _ in range(50))


def url_safe(length: int = 32) -> str:
    # No "@", ":" or "/", so a value is safe to appear in a URL, a connection
    # string or a shell word without anybody having to remember to quote it.
    return secrets.token_urlsafe(length)


#: name -> (generator, files it appears in)
#:
#: Every file listed for a name gets the *same* new value, because these are
#: shared secrets: the backend and the qr-bridge authenticate to each other
#: with QR_PROVIDER_API_KEY, and a rotation that updated one side would take
#: QR connections down.
ROTATABLE = {
    "SECRET_KEY": (django_secret_key, [".env", "backend/.env"]),
    "JWT_SECRET": (url_safe, [".env", "backend/.env"]),
    "DB_PASSWORD": (url_safe, [".env", "backend/.env"]),
    "REDIS_PASSWORD": (url_safe, [".env", "backend/.env"]),
    "QR_PROVIDER_API_KEY": (url_safe, [".env", "backend/.env", "qr-bridge/.env"]),
    "QR_PROVIDER_WEBHOOK_SECRET": (url_safe, [".env", "backend/.env", "qr-bridge/.env"]),
}

#: Rotating these needs more than a new value, so they are never done silently.
REFUSED = {
    "WHATSAPP_TOKEN_ENCRYPTION_KEY":
        "stored WhatsApp and AI credentials are encrypted with it; rotating without "
        "re-encrypting them makes every one of them unreadable",
    "OPENAI_API_KEY": "rotate this in the OpenAI console, then paste the new key in",
    "EMAIL_HOST_PASSWORD": "rotate this with your mail provider, then paste it in",
}


def set_value(path: Path, name: str, value: str) -> bool:
    """Replace ``NAME=...`` in ``path``. Returns whether the file had the key."""
    if not path.exists():
        return False
    original = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(name)}=.*$", re.MULTILINE)
    if not pattern.search(original):
        return False
    updated = pattern.sub(f"{name}={value}", original)
    if updated != original:
        path.write_text(updated, encoding="utf-8")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="comma-separated subset of names to rotate")
    parser.add_argument("--dry-run", action="store_true", help="say what would change, change nothing")
    args = parser.parse_args(argv)

    names = list(ROTATABLE)
    if args.only:
        names = [name.strip().upper() for name in args.only.split(",") if name.strip()]
        for name in names:
            if name in REFUSED:
                print(f"Refusing to rotate {name}: {REFUSED[name]}.", file=sys.stderr)
                return 2
            if name not in ROTATABLE:
                print(f"Unknown secret: {name}. Known: {', '.join(ROTATABLE)}", file=sys.stderr)
                return 2

    rotated, missing = [], []
    for name in names:
        generate, files = ROTATABLE[name]
        value = generate()
        touched = []
        for relative in files:
            path = REPO_ROOT / relative
            if args.dry_run:
                exists = path.exists() and re.search(
                    rf"^{re.escape(name)}=", path.read_text(encoding="utf-8"), re.MULTILINE
                )
                (touched if exists else missing).append(relative)
                continue
            (touched if set_value(path, name, value) else missing).append(relative)
        if touched:
            # The name and where it went. Never the value -- this output ends
            # up in a terminal buffer, a CI log, or a screenshot.
            rotated.append(f"  {name}: {', '.join(touched)}")

    verb = "would rotate" if args.dry_run else "rotated"
    print(f"{verb.capitalize()} {len(rotated)} secret(s):")
    print("\n".join(rotated) or "  (none)")

    if any(name == "DB_PASSWORD" for name in names):
        print(
            "\nDB_PASSWORD changed. POSTGRES_PASSWORD only takes effect when the data\n"
            "directory is first created, so the role itself must be changed too:\n"
            "  docker compose exec -T postgres psql -U <user> -d <db> \\\n"
            "    -c \"ALTER ROLE <user> WITH PASSWORD '<the new value from .env>';\"\n"
            "Do that BEFORE restarting the application, or it will fail to connect."
        )
    if not args.dry_run:
        print("\nRestart every service so the new values are read:\n  docker compose up -d --force-recreate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
